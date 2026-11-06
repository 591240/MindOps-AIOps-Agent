"""F-09 用户自接大模型回归测试（文档 V1.1 §9.1 TC-1~TC-16）。

全文件 async + pytest_asyncio fixture（对齐 tests/test_f06.py）。
"""

import os
import re
import uuid

os.environ.setdefault("MINDOPS_MYSQL_DB", "mindops_f09_test")

import pytest
import pytest_asyncio

from app.db import close_db, init_db
from app.models import LlmProvider

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def initialized_database():
    await init_db()
    yield
    await close_db()


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _clean_providers():
    await LlmProvider.filter(name__startswith="test-").delete()
    yield
    await LlmProvider.filter(name__startswith="test-").delete()


# ---- TC-1 vault ----


async def test_vault_encrypt_decrypt(monkeypatch):
    from app.llm import vault
    monkeypatch.setattr("app.llm.vault.settings", type("S", (), {"llm_vault_key": __import__("cryptography.fernet", fromlist=["Fernet"]).Fernet.generate_key().decode()})())
    enc = vault.encrypt("sk-abc1234567890xyz")
    assert vault.decrypt(enc) == "sk-abc1234567890xyz"
    assert vault.mask("sk-abc1234567890xyz") == "sk-****0xyz"
    assert vault.mask("short") == "****"


async def test_vault_locked(monkeypatch):
    from app.llm import vault
    monkeypatch.setattr("app.llm.vault.settings", type("S", (), {"llm_vault_key": ""})())
    with pytest.raises(RuntimeError, match="vault_locked"):
        vault.encrypt("sk-xxx")
    with pytest.raises(RuntimeError, match="vault_locked"):
        vault.decrypt("gAAAAA...")


# ---- TC-2 / TC-3 gateway.test_connectivity（mock） ----


async def test_ping_success(monkeypatch):
    from app.llm import gateway
    async def fake_post(*_, **__):
        return {"model": "deepseek-chat", "choices": [{"message": {"content": "pong"}}], "usage": {}}
    monkeypatch.setattr(gateway, "_post_chat", fake_post)
    result = await gateway.test_connectivity("https://api.deepseek.com/v1", "sk-xxx", "deepseek-chat")
    assert result["status"] == "success" and result["actual_model"] == "deepseek-chat"


async def test_ping_fail(monkeypatch):
    from app.llm import gateway
    async def fake_post(*_, **__):
        raise gateway.LlmCallError("key_invalid", "HTTP 401")
    monkeypatch.setattr(gateway, "_post_chat", fake_post)
    with pytest.raises(gateway.LlmCallError) as exc_info:
        await gateway.test_connectivity("https://api.deepseek.com/v1", "sk-bad", "deepseek-chat")
    assert exc_info.value.reason == "key_invalid"


# ---- TC-13 gateway.complete 兜底桩路径 ----


async def test_complete_stub_seed():
    from app.llm import gateway
    provider = await LlmProvider.create(name="test-stub", role="chat", provider_type="system", api_key_enc=None)
    result = await gateway.complete(provider.id, [{"role": "user", "content": "hello"}])
    assert result["degraded"] == "stub"
    assert "[stub]" in result["content"]


# ---- TC-8 降级链 ----


async def test_degradation_chain(monkeypatch):
    """降级链：首个候选失败 → 自动换下一个 → degraded=True。

    原实现用 `if model_id == 1` 判断"第一个候选"，隐含依赖
    「种子 general-chat 的自增 id 恰好是 1 且排在最前」——一旦库里有历史
    数据或种子被改过默认位，这个假设就破，用例会假红。
    这里改为按**调用次序**判断：第一次调用必失败，第二次必成功，
    与 provider id、排序、库内残留状态全部解耦。
    """
    from app.llm import gateway
    from app.agents import model_router

    attempts: list[int] = []

    async def fake_complete(model_id, *_, **__):
        attempts.append(model_id)
        if len(attempts) == 1:
            raise gateway.LlmCallError("timeout", "timeout 60s")
        return {"content": "ok", "prompt_tokens": 1, "completion_tokens": 1, "latency_ms": 10, "actual_model": "backup"}

    monkeypatch.setattr(gateway, "complete", fake_complete)
    for i in range(2):
        await LlmProvider.create(name=f"test-deg-{i}", role="chat", provider_type="system", is_default=i == 0)
    result = await model_router.complete_with_degradation("chat", [{"role": "user", "content": "x"}])
    assert result["degraded"] is True
    assert len(attempts) >= 2, "降级链没有尝试备用模型"


async def test_degradation_all_fail_raises(monkeypatch):
    from app.llm import gateway
    from app.agents import model_router

    async def fake_complete(*_, **__):
        raise gateway.LlmCallError("provider_error", "HTTP 500")
    monkeypatch.setattr(gateway, "complete", fake_complete)
    for i in range(3):
        await LlmProvider.create(name=f"test-degfail-{i}", role="chat", provider_type="system")
    with pytest.raises(model_router.ModelUnavailableError):
        await model_router.complete_with_degradation("chat", [{"role": "user", "content": "x"}])


# ---- TC-7 默认优先排序 + TC-15 status 过滤 ----


async def test_resolve_default_priority_and_status_filter():
    from app.agents import model_router

    # 隔离：清除种子数据（general-chat 等 id=1~3 也是 is_default=True）
    await LlmProvider.filter(status="active").update(is_default=False)
    await LlmProvider.create(name="test-old", role="chat", provider_type="system", status="active", is_default=False)
    await LlmProvider.create(name="test-disabled", role="chat", provider_type="system", status="disabled", is_default=True)
    await LlmProvider.create(name="test-active-default", role="chat", provider_type="system", status="active", is_default=True)
    resolved = await model_router.resolve_model_id("chat")
    provider = await LlmProvider.get(id=resolved)
    assert provider.name == "test-active-default"
    assert provider.status == "active"
    assert provider.is_default is True


# ---- TC-14 日志无密钥 ----


async def test_logs_never_contain_plaintext_key():
    from app.agents.diagnose import run
    state = {"title": "Redis 内存 OOM", "component": "redis", "brief": "告警", "model_id": None,
             "alerts": [{"title": "OOM", "severity": "P1"}], "kb_chunks": []}
    result = await run(state)
    assert result is not None
    # 审查：原断言恒真。真正扫描 diagnose 产物与 tool_calls，确保无明文密钥形态
    serialized = str(result)
    assert "sk-" not in serialized
    assert "api_key" not in serialized.lower().replace("api_key_enc", "")


# ---- 兜底启发式 ----


async def test_heuristic_oom():
    from app.agents.diagnose import _heuristic
    result = _heuristic({"title": "Redis 内存 OOM", "component": "redis", "brief": "", "alerts": [], "kb_chunks": [], "similar_cases": []})
    assert "内存" in result["root_cause"] and result["root_cause_confidence"] >= 0.65


async def test_heuristic_missing_info():
    from app.agents.diagnose import _heuristic
    result = _heuristic({"title": "Unknown XYZ", "component": "unknown", "brief": "", "alerts": [], "kb_chunks": [], "similar_cases": []})
    assert result["root_cause_confidence"] < 0.6
    assert "missing_info" in result
