"""F-09 模型管理接口（23-F-09 V1.1 §3；契约源 5-API §八）。

权限：全部 `/llm/*` 仅 admin（复用 `require_admin`）；唯一例外
`GET /llm/providers` engineer 亦可读（响应本就只含 masked，两种角色
返回完全相同结构）。安全铁律：任何响应不含 `api_key_enc` / 明文 Key。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, status
from tortoise.transactions import in_transaction

from app.auth.dependencies import get_current_user, require_admin
from app.integrations.ssrf_guard import SsrfBlocked, validate_public_url
from app.llm import gateway, registry, vault
from app.llm.gateway import LlmCallError
from app.config import model_now
from app.models import IncidentStep, LlmProvider, User
from app.observability.logging import log_event
from app.schemas import LlmProviderIn, LlmProviderPatch, LlmTestIn

router = APIRouter(prefix="/llm", tags=["llm"])

VALID_ROLES = {"chat", "reasoning"}


def _checked_base_url(raw: str, provider_type: str) -> str:
    """P0-3：保存前对 base_url 做 SSRF 校验。

    默认"公网黑名单"；`ollama` / `custom`（自托管）允许私网，否则本地
    Ollama 与内网 vLLM 永远无法保存 —— 预设给了 localhost 却过不了校验。
    云元数据地址对两类都拒绝。
    """
    try:
        return validate_public_url(
            raw, allow_private=registry.allows_private_endpoint(provider_type)
        )
    except SsrfBlocked as exc:
        raise HTTPException(422, detail={
            "code": "INVALID_BASE_URL",
            "message": str(exc),
        })


def _masked(provider: LlmProvider) -> dict:
    """脱敏序列化（§5.5 永不回显：无 api_key_enc 出口）。"""
    return {
        "id": provider.id, "name": provider.name, "provider_type": provider.provider_type,
        "base_url": provider.base_url, "model_name": provider.model_name,
        "api_key_masked": provider.api_key_masked, "role": provider.role,
        "is_default": provider.is_default, "status": provider.status,
        "temperature": provider.temperature, "max_tokens": provider.max_tokens,
        "last_test_at": provider.last_test_at.isoformat() if provider.last_test_at else None,
        "last_test_latency_ms": provider.last_test_latency_ms,
        "last_test_status": provider.last_test_status,
    }


def _ping_error_detail(exc: LlmCallError) -> dict:
    """400 body：机器原因码（前端按 reason 映射中文提示）。"""
    hint = {
        "key_invalid": "API Key 无效",
        "endpoint_unreachable": "端点不可达",
        "model_not_found": "模型名不存在",
        "timeout": "连通测试超时",
        "rate_limited": "触发厂商限流",
        "provider_error": "厂商返回异常",
    }.get(exc.reason, "连通测试失败")
    return {"code": "LLM_TEST_FAILED", "reason": exc.reason, "message": hint}


@router.get("/providers")
async def list_providers(user: User = Depends(get_current_user)):
    rows = await LlmProvider.all().order_by("role", "-is_default", "id")
    return {"providers": [_masked(row) for row in rows]}


@router.get("/presets", dependencies=[Depends(require_admin)])
async def list_presets():
    return {"presets": registry.PRESETS}


@router.post("/providers", status_code=201, dependencies=[Depends(require_admin)])
async def create_provider(body: LlmProviderIn):
    # FR-9：embedding 维度硬拦截（Milvus 固定 1024 维，MVP 不开放自接）
    if body.role == "embedding":
        raise HTTPException(400, detail={"code": "EMBEDDING_FORBIDDEN",
                                         "message": "Embedding 沿用系统默认（1024 维约束）"})
    if body.provider_type not in registry.PROVIDER_TYPES:
        raise HTTPException(422, detail={"code": "INVALID_PROVIDER_TYPE",
                                         "message": f"provider_type 必须为 {'|'.join(sorted(registry.PROVIDER_TYPES))}"})
    # P0-3：由"只看前缀"升级为完整 SSRF 校验
    body.base_url = _checked_base_url(body.base_url, body.provider_type)
    if body.role not in VALID_ROLES and body.role != "embedding":
        raise HTTPException(422, detail={"code": "INVALID_ROLE", "message": "role 必须为 chat|reasoning|embedding"})
    key_required = body.provider_type not in registry.KEY_OPTIONAL_TYPES
    if key_required and not body.api_key:
        raise HTTPException(422, detail={"code": "API_KEY_REQUIRED", "message": "该厂商必须提供 API Key"})

    # FR-2 保存即测试：失败拒绝落库，明文 Key 随请求结束丢弃
    try:
        test = await gateway.test_connectivity(
            body.base_url, body.api_key, body.model_name,
            timeout=gateway.ping_timeout_for(body.role),
            allow_private=registry.allows_private_endpoint(body.provider_type),
        )
    except LlmCallError as exc:
        raise HTTPException(400, detail=_ping_error_detail(exc))
    try:
        enc, masked = vault.encrypt(body.api_key), vault.mask(body.api_key)
    except RuntimeError:
        raise HTTPException(500, detail={"code": "VAULT_LOCKED",
                                         "message": "MINDOPS_LLM_VAULT_KEY 未配置，无法加密保存"})

    async with in_transaction() as conn:
        # FR-6 同 role 默认互斥（事务内置 False）
        if body.is_default:
            await LlmProvider.filter(role=body.role).using_db(conn).update(is_default=False)
        provider = await LlmProvider.create(
            using_db=conn, name=body.name, provider_type=body.provider_type,
            base_url=body.base_url, model_name=body.model_name,
            api_key_enc=enc, api_key_masked=masked, role=body.role,
            is_default=body.is_default, temperature=body.temperature,
            max_tokens=body.max_tokens,
            last_test_at=model_now(), last_test_latency_ms=test["latency_ms"],
            last_test_status="success",
        )
    log_event(20, "llm.provider_created", provider_id=provider.id, role=body.role,
              provider_type=body.provider_type, latency_ms=test["latency_ms"])
    return {"id": provider.id, "api_key_masked": masked, "test": test}


@router.post("/test", dependencies=[Depends(require_admin)])
async def test_before_save(body: LlmTestIn):
    """保存前测试：前端"测试连通性"按钮的数据源（不落库、不回显 Key）。"""
    if body.provider_type not in registry.PROVIDER_TYPES:
        raise HTTPException(422, detail={"code": "INVALID_PROVIDER_TYPE",
                                         "message": f"provider_type 必须为 {'|'.join(sorted(registry.PROVIDER_TYPES))}"})
    if body.provider_type not in registry.KEY_OPTIONAL_TYPES and not body.api_key:
        raise HTTPException(422, detail={"code": "API_KEY_REQUIRED", "message": "该厂商必须提供 API Key"})
    base_url = _checked_base_url(body.base_url, body.provider_type)
    try:
        return await gateway.test_connectivity(
            base_url, body.api_key, body.model_name,
            timeout=gateway.ping_timeout_for(body.role),
            allow_private=registry.allows_private_endpoint(body.provider_type),
        )
    except LlmCallError as exc:
        raise HTTPException(400, detail=_ping_error_detail(exc))


@router.post("/providers/{pid}/test", dependencies=[Depends(require_admin)])
async def test_provider(pid: int):
    provider = await LlmProvider.get_or_none(id=pid)
    if provider is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "provider not found"})
    if provider.api_key_enc is None:
        # 系统种子无真实端点：测试语义 = 桩可用
        test = {"status": "success", "latency_ms": 0, "actual_model": provider.model_name or "system-stub"}
    else:
        try:
            api_key = vault.decrypt(provider.api_key_enc)
            test = await gateway.test_connectivity(
                provider.base_url, api_key, provider.model_name,
                timeout=gateway.ping_timeout_for(provider.role),
                allow_private=registry.allows_private_endpoint(provider.provider_type),
            )
        except (LlmCallError, RuntimeError) as exc:
            reason = exc.reason if isinstance(exc, LlmCallError) else "vault_locked"
            await LlmProvider.filter(id=pid).update(
                last_test_at=model_now(),
                last_test_status=f"failed:{reason}"[:32],  # §3.4：只标记，不改 status
            )
            raise HTTPException(400, detail={"code": "LLM_TEST_FAILED", "reason": reason,
                                             "message": "连通测试失败（已记录到 last_test_status）"})
    await LlmProvider.filter(id=pid).update(
        last_test_at=model_now(), last_test_latency_ms=test["latency_ms"],
        last_test_status="success",
    )
    return test


@router.put("/providers/{pid}", dependencies=[Depends(require_admin)])
async def update_provider(pid: int, body: LlmProviderPatch):
    provider = await LlmProvider.get_or_none(id=pid)
    if provider is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "provider not found"})
    values: dict = {}
    if body.name is not None:
        values["name"] = body.name
    if body.model_name is not None:
        values["model_name"] = body.model_name
    if body.base_url is not None:
        values["base_url"] = _checked_base_url(body.base_url, provider.provider_type)
    if body.role is not None:
        values["role"] = body.role
    if body.temperature is not None:
        values["temperature"] = body.temperature
    if body.max_tokens is not None:
        values["max_tokens"] = body.max_tokens
    if body.status is not None:
        values["status"] = body.status
    if body.api_key:  # 留空=不更换（FR-4）
        try:
            values["api_key_enc"] = vault.encrypt(body.api_key)
            values["api_key_masked"] = vault.mask(body.api_key)
        except RuntimeError:
            raise HTTPException(500, detail={"code": "VAULT_LOCKED",
                                             "message": "MINDOPS_LLM_VAULT_KEY 未配置，无法加密保存"})
    target_role = values.get("role", provider.role)
    async with in_transaction() as conn:
        if values:
            await LlmProvider.filter(id=pid).using_db(conn).update(**values)
        # §3.5：设默认即 PUT body is_default=true（无专用 /default 端点）
        if body.is_default is not None:
            if body.is_default:
                await LlmProvider.filter(role=target_role).exclude(id=pid).using_db(conn).update(is_default=False)
            await LlmProvider.filter(id=pid).using_db(conn).update(is_default=body.is_default)
        elif body.role is not None and provider.is_default:
            # 换角色后旧默认标记跟随迁移，避免新角色出现两个默认
            await LlmProvider.filter(role=target_role).exclude(id=pid).using_db(conn).update(is_default=False)
    refreshed = await LlmProvider.get(id=pid)
    log_event(20, "llm.provider_updated", provider_id=pid, fields=list(values.keys()),
              is_default=body.is_default)
    return _masked(refreshed)


@router.delete("/providers/{pid}", dependencies=[Depends(require_admin)])
async def delete_provider(pid: int):
    provider = await LlmProvider.get_or_none(id=pid)
    if provider is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "provider not found"})
    # FR-10 软删除保护：历史轨迹引用过的模型只停用
    if await IncidentStep.filter(model_id=pid).exists():
        await LlmProvider.filter(id=pid).update(status="disabled")
        log_event(20, "llm.provider_soft_deleted", provider_id=pid)
        return {"deleted": False, "soft": True, "reason": "历史轨迹引用保护"}
    await provider.delete()
    log_event(20, "llm.provider_deleted", provider_id=pid)
    return {"deleted": True, "soft": False}
