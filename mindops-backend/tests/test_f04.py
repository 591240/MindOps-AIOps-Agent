"""F-04 置信度回环测试（文档 §9.1 单元测试矩阵）。

覆盖：阈值触发 / 回环上限 / 阈值通过 / 模型升级定点 / 升级熔断跳过 /
递进策略次序 / 查询改写 / 扩大召回 / 跨组件兜底 / 审计落库+回填 /
单次检索 / 阈值调参 / 调参隔离 / 关闭回环 / 校准采集 / 幂等递增。
（审计接口 `GET /incidents/{id}/loop-audits` 见 test_f04_api.py）
"""

import asyncio
import os
import uuid

os.environ.setdefault("MINDOPS_MYSQL_DB", "mindops_f04_test")

import pytest
import pytest_asyncio

from app.agents import (
    calibration,
    confidence_loop,
    loop_config,
    loop_strategies,
    model_router,
)
from app.db import close_db, init_db
from app.models import Incident, LlmProvider, LoopAudit, RuntimeConfig

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def initialized_database():
    await init_db()
    yield
    await close_db()


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _clean_overrides():
    """每个用例前清空 DB 覆盖与熔断，避免相互污染。"""
    await RuntimeConfig.all().delete()
    model_router.reset_breakers()
    yield
    model_router.reset_breakers()


def _make_state(**overrides) -> dict:
    base = {
        "run_id": f"run_t{uuid.uuid4().hex[:12]}",
        "incident_id": 900001,
        "title": "支付服务异常",
        "component": "pay-svc",
        "brief": "Redis 内存高",
        "kb_chunks": [],
        "missing_info": [],
        "similar_cases": [],
        "alerts": [],
        "root_cause_confidence": 0.5,
        "retry_count": 0,
        "conf_threshold": 0.6,
        "max_retry": 2,
        "reasoning_escalation": True,
        "cross_component_fallback": True,
        "top_k_base": 5,
        "top_k_loop": 10,
        "sim_floor_base": 0.7,
        "sim_floor_loop": 0.5,
    }
    base.update(overrides)
    return base


# ---- 1. 阈值触发 ----
async def test_low_confidence_triggers_loop():
    state = _make_state(root_cause_confidence=0.5, retry_count=0)
    decision = await confidence_loop.maybe_loop(state)
    assert decision == "context"
    assert state["retry_count"] == 1


# ---- 2. 回环上限（不死循环）----
async def test_loop_exhausted_goes_to_human():
    state = _make_state(root_cause_confidence=0.5, retry_count=0)
    # 第 1 次回环
    assert await confidence_loop.maybe_loop(state) == "context"
    assert state["retry_count"] == 1
    # 第 2 次回环（retry_count = 2，已用尽）
    assert await confidence_loop.maybe_loop(state) == "context"
    assert state["retry_count"] == 2
    # 仍低置信 → 转人工
    state["root_cause_confidence"] = 0.4
    assert await confidence_loop.maybe_loop(state) == "human_needed"
    assert state["human_reason"] == "low_confidence"
    assert state["loop_summary"]["retry_count"] == 2


# ---- 3. 阈值通过 ----
async def test_confidence_above_threshold_goes_to_remediate():
    state = _make_state(root_cause_confidence=0.62)
    assert await confidence_loop.maybe_loop(state) == "remediate"
    assert state["retry_count"] == 0


# ---- 4. 模型升级定点（retry_count == 1）----
async def test_reasoning_escalation_at_retry_one():
    chat = await LlmProvider.filter(role="chat", status="active").first()
    reasoning = await LlmProvider.filter(role="reasoning", status="active").first()
    assert chat and reasoning and chat.id != reasoning.id

    state = _make_state(root_cause_confidence=0.5, retry_count=0)
    await confidence_loop.maybe_loop(state)
    assert state["model_role"] == "reasoning"
    assert state["model_id"] == reasoning.id != chat.id

    audit = await LoopAudit.filter(run_id=state["run_id"], retry_index=1).first()
    assert audit is not None
    assert audit.role == "reasoning"
    assert audit.escalated is True


# ---- 5. 升级被熔断跳过 ----
async def test_escalation_skipped_when_breaker_open():
    for _ in range(model_router.BREAKER_FAILURE_THRESHOLD):
        model_router.record_failure("reasoning")
    assert model_router.is_breaker_open("reasoning") is True

    model_id, role, escalated, skip_reason = await model_router.pick_model("reasoning")
    assert role == "chat"
    assert escalated is False
    assert skip_reason is not None

    state = _make_state(root_cause_confidence=0.5, retry_count=0)
    await confidence_loop.maybe_loop(state)
    assert state["model_role"] == "chat"
    audit = await LoopAudit.filter(run_id=state["run_id"], retry_index=1).first()
    assert audit.escalated is False


# ---- 6. 递进策略次序（V1.1 修订）----
async def test_progressive_strategy_order():
    assert loop_strategies.build_strategy(1) == ["rewrite", "expand_recall"]
    assert loop_strategies.build_strategy(2) == ["expand_recall", "cross_component"]
    # 关闭跨组件时第 2 次退化为仅扩大召回
    assert loop_strategies.build_strategy(2, False) == ["expand_recall"]


# ---- 7. 查询改写 ----
async def test_query_rewrite_appends_synonyms():
    rewritten = await loop_strategies.rewrite_query("Redis 内存高", [])
    assert "OOM" in rewritten and "排查" in rewritten

    state = _make_state(brief="Redis 内存高", root_cause_confidence=0.5)
    await confidence_loop.maybe_loop(state)
    audit = await LoopAudit.filter(run_id=state["run_id"], retry_index=1).first()
    assert audit.query_before == "Redis 内存高"
    assert audit.query_after != audit.query_before
    assert "OOM" in audit.query_after


# ---- 8. 扩大召回参数 ----
async def test_expand_recall_widens_top_k_and_sim_floor():
    state = _make_state(root_cause_confidence=0.5, retry_count=0)
    audit = await confidence_loop.run_retrieval(state, role="chat", strategy=["expand_recall"])
    assert audit["recall_top_k"] == 10       # 基线 5
    assert audit["sim_floor"] == 0.5         # 基线 0.7


# ---- 9. 跨组件兜底 ----
async def test_cross_component_fallback_hits_global_knowledge():
    await Incident.create(
        title="Redis 内存高",
        component="other-comp",
        severity="P1",
        status="resolved",
        root_cause="Redis 内存高 OOM 排查",
        actual_cause="Redis 内存高 OOM 排查",
        root_cause_confidence=0.9,
    )
    state = _make_state(brief="Redis 内存高", component="pay-svc", root_cause_confidence=0.5)
    audit = await confidence_loop.run_retrieval(
        state, role="chat", strategy=["expand_recall", "cross_component"]
    )
    assert audit["component_scope"] == "__global__"
    assert audit["kb_hits_after"] > 0


# ---- 10. 审计落库 + conf_after 回填（§7.3 两阶段）----
async def test_audit_persisted_then_conf_after_backfilled():
    state = _make_state(root_cause_confidence=0.5, retry_count=0)
    await confidence_loop.maybe_loop(state)

    audit = await LoopAudit.filter(run_id=state["run_id"], retry_index=1).first()
    assert audit.conf_after is None   # 检索阶段落 NULL
    assert audit.conf_before == 0.5

    await confidence_loop.backfill_conf_after(state["run_id"], 1, 0.71)
    audit = await LoopAudit.filter(run_id=state["run_id"], retry_index=1).first()
    assert audit.conf_after == 0.71


# ---- 11. 单次检索（V1.1：删除 V1.0 双检索浪费）----
async def test_retrieval_called_once(monkeypatch):
    calls = []
    original = loop_strategies.search_knowledge

    async def counting(query, top_k, sim_floor, scope):
        calls.append((query, top_k, sim_floor, scope))
        return await original(query, top_k, sim_floor, scope)

    monkeypatch.setattr(loop_strategies, "search_knowledge", counting)

    state = _make_state(root_cause_confidence=0.5, retry_count=0, kb_chunks=[{"x": 1}, {"x": 2}])
    audit = await confidence_loop.run_retrieval(state, role="chat", strategy=["rewrite", "expand_recall"])
    assert len(calls) == 1
    assert audit["kb_hits_before"] == 2  # 取 len(state.kb_chunks)，不再二次检索


# ---- 12. 阈值调参写入 runtime_configs ----
async def test_put_config_persists_override():
    values = await loop_config.apply_patch({"conf_threshold": 0.55, "max_retry": 3}, "admin001")
    assert values["conf_threshold"] == 0.55
    assert values["max_retry"] == 3
    row = await RuntimeConfig.get(key="conf_threshold")
    assert row.value == "0.55"
    assert row.updated_by == "admin001"


# ---- 13. 调参隔离：per-run 快照不受后续调参影响 ----
async def test_snapshot_isolates_running_runs():
    first = await loop_config.snapshot()
    assert first["conf_threshold"] == 0.6

    await loop_config.apply_patch({"conf_threshold": 0.55}, "admin001")

    # 进行中 run 持有的仍是旧快照
    assert first["conf_threshold"] == 0.6
    # 新 run 才读到新值
    second = await loop_config.snapshot()
    assert second["conf_threshold"] == 0.55


# ---- 14. 关闭回环（max_retry = 0）----
async def test_max_retry_zero_disables_loop():
    state = _make_state(root_cause_confidence=0.5, retry_count=0, max_retry=0)
    assert await confidence_loop.maybe_loop(state) == "human_needed"
    assert state["retry_count"] == 0  # 0 次补查
    assert await LoopAudit.filter(run_id=state["run_id"]).count() == 0


# ---- 15. 校准采集（§5.5）----
async def test_calibration_collects_sample():
    result = await calibration.collect_sample(
        incident_id=1,
        reported_conf=0.9,
        root_cause="Redis 内存达到上限导致 OOM",
        actual_cause="Redis 内存达到上限导致 OOM",
    )
    assert result["collected"] is True
    assert result["actually_correct"] == 1
    assert result["method"] == "heuristic"  # 未注入 LLM-as-judge 时的降级标注

    summary = await calibration.daily_calibration_job()
    assert summary["samples"] >= 1
    assert 0.0 <= summary["calibration_error"] <= 1.0


# ---- 16. 幂等递增：+1 只发生在回环边 ----
async def test_retry_increment_only_on_loop_edge():
    from app.agents import context, diagnose

    state = _make_state(root_cause_confidence=0.5, retry_count=0)
    await confidence_loop.maybe_loop(state)
    assert state["retry_count"] == 1

    # Context / Diagnose 内部不得修改 retry_count（§5.6 铁律）
    state.update(await context.run(state))
    assert state["retry_count"] == 1
    state.update(await diagnose.run(state))
    assert state["retry_count"] == 1

    # 再次经过回环边才 +1
    state["root_cause_confidence"] = 0.5
    await confidence_loop.maybe_loop(state)
    assert state["retry_count"] == 2


# ---- 17. 审计查询：按 retry_index 升序、事件无回环返回空 ----
async def test_loop_audits_ordered_and_empty_when_no_loop():
    state = _make_state(root_cause_confidence=0.5, retry_count=0)
    await confidence_loop.maybe_loop(state)
    state["root_cause_confidence"] = 0.5
    await confidence_loop.maybe_loop(state)

    rows = await LoopAudit.filter(run_id=state["run_id"]).order_by("retry_index")
    assert [row.retry_index for row in rows] == [1, 2]

    empty = await LoopAudit.filter(incident_id=-999).order_by("retry_index")
    assert list(empty) == []
