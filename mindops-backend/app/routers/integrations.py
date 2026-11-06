"""F-11 集成管理接口（27-F-11 §3 接口契约）。

五个端点：
  GET  /integrations              — 列表（admin + engineer 可读）
  POST /integrations              — 新增（admin-only）
  POST /integrations/{iid}/test   — 连通性探测（admin-only）
  PUT  /integrations/{iid}        — 编辑/启停（admin-only）
  DELETE /integrations/{iid}      — 软删除（admin-only）

权限：写接口 Depends(require_admin)；GET 工程师可读。
限流：POST 按 user 10/60s，test 按 target 5/60s（FR-11）。
"""

from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query

from app.alerts import context
from app.auth.dependencies import get_current_user, require_admin
from app.config import model_now
from app.integrations.probes import probe_prometheus
from app.integrations.service import apply_probe_result, validate_status_transition
from app.integrations.ssrf_guard import SsrfBlocked, validate_base_url
from app.models import Integration, User
from app.observability.logging import log_event
from app.schemas import IntegrationIn, IntegrationPatch, IntegrationTestIn

router = APIRouter(prefix="/integrations", tags=["integrations"])

VALID_TYPES = {"prometheus"}
VALID_PROBE_STATUSES = {"active", "disabled"}


def _serialize(row: Integration) -> dict:
    """序列化为 API 响应格式（§3.1）。"""
    return {
        "id": row.id,
        "name": row.name,
        "type": row.type,
        "base_url": row.base_url,
        "scrape_config": row.scrape_config,
        "status": row.status,
        "last_heartbeat": row.last_heartbeat.isoformat() if row.last_heartbeat else None,
        "last_error": row.last_error,
        "last_latency_ms": row.last_latency_ms,
        "targets_total": row.targets_total,
        "targets_up": row.targets_up,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "disabled_at": row.disabled_at.isoformat() if row.disabled_at else None,
    }


# ---- §3.1 GET /integrations（admin + engineer 均可读）----
@router.get("")
async def list_integrations(
    include_disabled: bool = False,
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=200),
    user: User = Depends(get_current_user),
):
    """集成列表（P1-3：新增分页）。

    响应从裸数组升级为 `{items, total, page, size}`，与 `/incidents` 保持一致；
    `items` 字段结构与原 `integrations` 完全相同。
    """
    qs = Integration.all().order_by("-created_at")
    if not include_disabled:
        qs = qs.exclude(status="disabled")
    total = await qs.count()
    rows = await qs.offset((page - 1) * size).limit(size)
    items = [_serialize(r) for r in rows]
    return {
        "items": items,
        # 兼容旧消费者：同时保留 integrations 键（同一份数据，不额外查库）
        "integrations": items,
        "total": total,
        "page": page,
        "size": size,
    }


# ---- §3.2 POST /integrations（admin-only）----
@router.post("", status_code=201)
async def create_integration(body: IntegrationIn, user: User = Depends(require_admin)):
    # FR-11：按用户限流 10 次/60s（rate 单位为 token/秒，故 10/60）
    if not await context.limiter.acquire(
        f"integration:user:{user.id}", capacity=10, rate=10 / 60.0
    ):
        raise HTTPException(
            429, detail={"code": "RATE_LIMITED", "message": "操作过于频繁，请稍后再试"}
        )

    # 类型白名单（§1.4 本版仅 prometheus）
    if body.type not in VALID_TYPES:
        raise HTTPException(
            422,
            detail={
                "code": "INVALID_TYPE",
                "message": f"本版仅支持: {', '.join(sorted(VALID_TYPES))}",
            },
        )

    # SSRF 五道闸校验（§5.1，创建即校验，不依赖探测兜底）
    try:
        normalized_url = validate_base_url(body.base_url)
    except SsrfBlocked as exc:
        raise HTTPException(
            422,
            detail={
                "code": "INVALID_BASE_URL",
                "message": "仅允许内网或受信地址段",
                "reason": str(exc),
            },
        )

    # FR-10 重复登记拦截（应用层友好提示，DB 唯一索引为最后防线）
    exists = await Integration.filter(
        base_url=normalized_url, scrape_config=body.scrape_config
    ).exists()
    if exists:
        raise HTTPException(
            409,
            detail={
                "code": "DUPLICATE_TARGET",
                "message": "该地址与服务选择器已接入",
            },
        )

    # 落库 status='pending'
    integration = await Integration.create(
        name=body.name,
        type=body.type,
        base_url=normalized_url,
        scrape_config=body.scrape_config,
        status="pending",
        created_by=user.username,
    )

    log_event(
        20,
        "integration.created",
        integration_id=integration.id,
        operator=user.username,
        name=body.name,
        base_url=normalized_url,
    )

    return {
        "id": integration.id,
        "status": "pending",
        "msg": "已创建，等待连通性探测",
    }


# ---- §3.3a POST /integrations/test（新增前探测，不落库；admin-only）----
@router.post("/test")
async def test_draft_integration(body: IntegrationTestIn, user: User = Depends(require_admin)):
    """前端"测试连通性"按钮的数据源：校验 + 真实探测，不写库。

    此前前端只在本地校验 URL 格式却提示"探测成功"，用户无法在保存前
    确认目标是否真的可达。
    """
    if body.type not in VALID_TYPES:
        raise HTTPException(422, detail={"code": "INVALID_TYPE",
                                         "message": f"本版仅支持: {', '.join(sorted(VALID_TYPES))}"})
    if not await context.limiter.acquire(
        f"integration:probe-draft:{user.id}", capacity=5, rate=5 / 60.0
    ):
        raise HTTPException(429, detail={"code": "RATE_LIMITED", "message": "操作过于频繁，请稍后再试"})
    try:
        normalized_url = validate_base_url(body.base_url)
    except SsrfBlocked as exc:
        raise HTTPException(422, detail={
            "code": "INVALID_BASE_URL",
            "message": "仅允许内网或受信地址段",
            "reason": str(exc),
        })

    result = await probe_prometheus(normalized_url)
    log_event(20, "integration.draft_probed", operator=user.username,
              ok=result.ok, reason=result.reason, latency_ms=result.latency_ms)
    return {
        "ok": result.ok,
        "reason": result.reason,
        "latency_ms": result.latency_ms,
        "targets_total": result.targets_total,
        "targets_up": result.targets_up,
        "last_error": result.error or None,
    }


# ---- §3.3 POST /integrations/{iid}/test（admin-only）----
@router.post("/{iid}/test")
async def test_integration(iid: int, user: User = Depends(require_admin)):
    integration = await Integration.get_or_none(id=iid)
    if integration is None:
        raise HTTPException(
            404, detail={"code": "NOT_FOUND", "message": "目标不存在"}
        )

    # disabled 目标不可探测（§5.3）
    if integration.status == "disabled":
        raise HTTPException(
            409,
            detail={
                "code": "TARGET_DISABLED",
                "message": "该目标已停用，请先启用",
            },
        )

    # FR-11：按目标限流 5 次/60s（rate 单位为 token/秒，故 5/60）
    if not await context.limiter.acquire(
        f"integration:probe:{iid}", capacity=5, rate=5 / 60.0
    ):
        raise HTTPException(
            429, detail={"code": "RATE_LIMITED", "message": "操作过于频繁，请稍后再试"}
        )

    # 执行探测（§5.2）
    result = await probe_prometheus(integration.base_url)

    # 更新库（§5.2 写库规则）
    fields = apply_probe_result(integration, result)
    await Integration.filter(id=iid).update(**fields)

    log_event(
        20,
        "integration.probed",
        integration_id=iid,
        operator=user.username,
        ok=result.ok,
        reason=result.reason,
        latency_ms=result.latency_ms,
    )

    # 统一回读：成功/失败都返回库中落定的值。
    # 不可返回 datetime.now()——MySQL DATETIME 为秒精度，内存里的微秒值会与库中不一致，
    # 前端心跳随后刷新时会出现“同一时刻两个值”的跳变。
    refreshed = await Integration.get(id=iid)
    heartbeat = (
        refreshed.last_heartbeat.isoformat() if refreshed.last_heartbeat else None
    )

    if result.ok:
        return {
            "ok": True,
            "status": refreshed.status,
            "targets_total": refreshed.targets_total,
            "targets_up": refreshed.targets_up,
            "latency_ms": refreshed.last_latency_ms,
            "last_heartbeat": heartbeat,
        }
    else:
        # §3.3 失败时 last_heartbeat 返回上一次成功的时间（FR-6）
        return {
            "ok": False,
            "status": refreshed.status,
            "last_error": refreshed.last_error,
            "reason": result.reason,
            "last_heartbeat": heartbeat,
        }


# ---- §3.4 PUT /integrations/{iid}（admin-only）----
@router.put("/{iid}")
async def update_integration(
    iid: int, body: IntegrationPatch, user: User = Depends(require_admin)
):
    integration = await Integration.get_or_none(id=iid)
    if integration is None:
        raise HTTPException(
            404, detail={"code": "NOT_FOUND", "message": "目标不存在"}
        )

    values: dict = {}

    # 名称
    if body.name is not None:
        values["name"] = body.name

    # 地址变更 → 重新 SSRF 校验 + 回 pending（§3.4）
    url_changed = False
    if body.base_url is not None:
        try:
            normalized_url = validate_base_url(body.base_url)
        except SsrfBlocked as exc:
            raise HTTPException(
                422,
                detail={
                    "code": "INVALID_BASE_URL",
                    "message": "仅允许内网或受信地址段",
                    "reason": str(exc),
                },
            )
        if normalized_url != integration.base_url:
            values["base_url"] = normalized_url
            url_changed = True

    # 服务选择器（审查 #9：与 base_url 同属唯一键、同样改变探测语义，
    # 变更后同样回 pending + 清空探测数据）
    scrape_changed = body.scrape_config is not None and body.scrape_config != integration.scrape_config
    if body.scrape_config is not None:
        values["scrape_config"] = body.scrape_config

    if url_changed or scrape_changed:
        values["status"] = "pending"
        values["last_heartbeat"] = None
        values["last_error"] = None
        values["last_latency_ms"] = None
        values["targets_total"] = None
        values["targets_up"] = None
        values["disabled_at"] = None

    # 审查 #9：与 create 同口径的重复预检——(base_url, scrape_config) 有唯一键，
    # update 撞键时 Tortoise 会抛未捕获的 IntegrityError 变 500。
    new_base = values.get("base_url", integration.base_url)
    new_scrape = values.get("scrape_config", integration.scrape_config)
    if ("base_url" in values or "scrape_config" in values) and await Integration.filter(
        base_url=new_base, scrape_config=new_scrape
    ).exclude(id=iid).exists():
        raise HTTPException(
            409,
            detail={"code": "DUPLICATE_TARGET", "message": "相同地址与服务选择器的集成已存在"},
        )

    # 状态变更（§5.3 状态机守卫）
    if body.status is not None:
        err = validate_status_transition(integration.status, body.status)
        if err:
            raise HTTPException(422, detail={"code": "INVALID_STATUS", "message": err})
        values["status"] = body.status
        # 暂停即软删除（记审计时间）；恢复启用时清空
        values["disabled_at"] = model_now() if body.status == "disabled" else None

    if not values:
        return _serialize(integration)

    await Integration.filter(id=iid).update(**values)
    refreshed = await Integration.get(id=iid)

    log_event(
        20,
        "integration.updated",
        integration_id=iid,
        operator=user.username,
        fields=list(values.keys()),
    )

    return _serialize(refreshed)


# ---- §3.5 DELETE /integrations/{iid}（软删除；admin-only）----
@router.delete("/{iid}")
async def disable_integration(iid: int, user: User = Depends(require_admin)):
    integration = await Integration.get_or_none(id=iid)
    if integration is None:
        raise HTTPException(
            404, detail={"code": "NOT_FOUND", "message": "目标不存在"}
        )

    # 幂等：已停用再 DELETE 返回同一响应（§3.5）
    if integration.status != "disabled":
        await Integration.filter(id=iid).update(
            status="disabled",
            disabled_at=model_now(),
        )

        log_event(
            20,
            "integration.disabled",
            integration_id=iid,
            operator=user.username,
        )

    return {
        "deleted": False,
        "soft": True,
        "reason": "保留历史接入记录用于审计",
    }
