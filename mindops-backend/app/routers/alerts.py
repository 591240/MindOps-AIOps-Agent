"""F-01 告警接入口（文档 §7.1）。

路由结构（§7.1）：
  POST /alerts/webhook   无 JWT + IP 白名单 + 限流(先于解析)
  POST /alerts            JWT (admin/engineer)
  POST /alerts/manual     JWT + admin
  GET  /alerts/recent     JWT (任意)
"""

from __future__ import annotations

import json
from collections import defaultdict
from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from tortoise.expressions import Q
from tortoise.functions import Count

from app.alerts import context
from app.alerts.normalizer import (
    normalize_app,
    normalize_manual,
    normalize_prometheus,
)
from app.alerts.service import ingest_batch, ingest_one
from app.alerts.webhook import check_ip_whitelist, check_webhook_secret
from app.auth.dependencies import get_current_user, require_admin
from app.config import model_now, settings
from app.models import Alert, Incident, User
from app.observability import metrics
from app.observability.logging import log_event
from app.schemas import (
    AlertIngestOut,
    AlertItem,
    AlertReportIn,
    ManualAlertIn,
    ManualAlertOut,
    PrometheusWebhook,
    RecentOut,
)
from app.services import task_service

router = APIRouter(prefix="/alerts", tags=["alerts"])


def _err(code: str, http: int, message: str) -> HTTPException:
    return HTTPException(status_code=http, detail={"code": code, "message": message})


def _reject(reason: str) -> None:
    metrics.alerts_rejected.labels(reason=reason).inc()


# ---------------------------------------------------------------- §3.1 webhook
@router.post("/webhook", response_model=AlertIngestOut)
async def webhook(
    request: Request,
    _secret: None = Depends(check_webhook_secret),
    client_ip: str = Depends(check_ip_whitelist),
):
    # 1) 限流先于解析（§6.2 V1.1）
    if not await context.limiter.check_global():
        _reject("RATE_LIMIT")
        log_event(20, "alert.rate_limited", scope="global", client_ip=client_ip)
        raise _err("RATE_LIMIT", status.HTTP_429_TOO_MANY_REQUESTS, "global rate limit")
    if not await context.limiter.check_ip(client_ip):
        _reject("RATE_LIMIT")
        log_event(20, "alert.rate_limited", scope="ip", client_ip=client_ip)
        raise _err("RATE_LIMIT", status.HTTP_429_TOO_MANY_REQUESTS, "ip rate limit")

    # 2) 读 body：webhook 独立上限 1MB（§2.3）。审查 #9：分块请求没有
    #   Content-Length，全局中间件拦不住 —— 这里流式读取并截断，超限即拒。
    chunks: list[bytes] = []
    received = 0
    async for chunk in request.stream():
        received += len(chunk)
        if received > settings.webhook_max_body_bytes:
            _reject("INVALID_PAYLOAD")
            raise _err("INVALID_PAYLOAD", 400, "body exceeds 1MB")
        chunks.append(chunk)
    body = b"".join(chunks)

    # 3) JSON 解析 + 批次 ≤ 200（§2.3 / §7.2）
    try:
        payload = json.loads(body)
    except Exception:
        _reject("INVALID_PAYLOAD")
        raise _err("INVALID_PAYLOAD", 400, "invalid json")

    try:
        hook = PrometheusWebhook(**payload)
    except Exception:
        _reject("INVALID_PAYLOAD")
        raise _err("INVALID_PAYLOAD", 400, "invalid payload schema")

    if len(hook.alerts) > settings.max_batch_size:
        _reject("INVALID_PAYLOAD")
        raise _err("INVALID_PAYLOAD", 400, "batch exceeds 200")
    if not hook.alerts:
        raise _err("INVALID_PAYLOAD", 400, "empty alerts array")

    # 4) 归一化（title 空 → 400）
    try:
        normalized = [normalize_prometheus(a.model_dump()) for a in hook.alerts]
    except ValueError as e:
        _reject("INVALID_PAYLOAD")
        raise _err("INVALID_PAYLOAD", 400, str(e))

    # 5) 入库
    try:
        return await ingest_batch(normalized)
    except Exception as e:
        log_event(40, "alert.ingest_failed", error=str(e))
        raise _err("INTERNAL", 500, "internal error")


# ------------------------------------------------------------ §3.2 主动上报
@router.post("", response_model=AlertIngestOut)
async def report(body: AlertReportIn, user: User = Depends(get_current_user)):
    if not await context.limiter.check_global():
        _reject("RATE_LIMIT")
        raise _err("RATE_LIMIT", status.HTTP_429_TOO_MANY_REQUESTS, "global rate limit")

    raw = body.model_dump()
    try:
        normalized = normalize_app(raw)
    except ValueError as e:
        _reject("INVALID_PAYLOAD")
        raise _err("INVALID_PAYLOAD", 400, str(e))

    try:
        result = await ingest_one(normalized)
    except Exception as e:
        log_event(40, "alert.ingest_failed", error=str(e))
        raise _err("INTERNAL", 500, "internal error")

    return {
        "accepted": 1 if result["is_new"] else 0,
        "incident_id": result["incident_id"],
        "deduped": result["deduped"],
        "alert_ids": [result["alert_id"]],
    }


# ------------------------------------------------------------ §3.3 手动注入
@router.post("/manual", response_model=ManualAlertOut)
async def manual(body: ManualAlertIn, admin: User = Depends(require_admin)):
    raw = body.model_dump()
    try:
        normalized = normalize_manual(raw)
    except ValueError as e:
        _reject("INVALID_PAYLOAD")
        raise _err("INVALID_PAYLOAD", 400, str(e))

    try:
        result = await ingest_one(
            normalized, trigger_pipeline=body.trigger_pipeline
        )
    except Exception as e:
        log_event(40, "alert.ingest_failed", error=str(e))
        raise _err("INTERNAL", 500, "internal error")

    return {
        "alert_id": result["alert_id"],
        "incident_id": result["incident_id"],
        "pipeline_triggered": body.trigger_pipeline and result["is_new"],
    }


# ------------------------------------------------------------ §3.4 最近告警
@router.get("/recent", response_model=RecentOut)
async def recent(
    minutes: int = Query(60, ge=1, le=1440),
    component: str | None = Query(None),
    severity: str | None = Query(None),
    task_id: str | None = Query(
        None, description="F-12 任务筛选：任务 ID；'none' = 仅未归属告警；缺省 = 全部"
    ),
    status: str | None = Query(
        None,
        description=(
            "按所属事件状态筛选：new/diagnosing/human_needed/resolved/closed；"
            "'unclustered' = 仅尚未聚类成事件的告警；缺省 = 全部"
        ),
    ),
    q: str | None = Query(None, description="标题关键字（大小写不敏感子串匹配）"),
    page: int = Query(1, ge=1),
    size: int = Query(50, ge=1, le=200),
    user: User = Depends(get_current_user),
):
    since = model_now() - timedelta(minutes=minutes)
    qs = Alert.filter(created_at__gte=since)          # 走 idx_created（§3.4 约束）
    if component:
        qs = qs.filter(component=component)
    if severity:
        qs = qs.filter(severity=severity)
    # 关键字搜索必须服务端做：分页之后前端只能搜到当前页，会给出错误结论
    if q:
        qs = qs.filter(title__icontains=q.strip())

    # ---- F-12 任务筛选（30-F-12 §3.7 / §5.3）----
    # 缺省行为与 F-12 之前完全一致（全部告警），保证向后兼容。
    if task_id == "none":
        # 未归属 = 没带 integration_id，或所带目标不被任何 active 任务绑定。
        # 这类告警**必须**可见，否则等于静默丢告警。
        active_ids = await task_service.active_integration_ids()
        cond = Q(integration_id__isnull=True)
        if active_ids:
            cond |= ~Q(integration_id__in=list(active_ids))
        qs = qs.filter(cond)
    elif task_id:
        bound_ids = await task_service.integration_ids_for_task(task_id)
        if not bound_ids:
            return {"items": [], "total": 0}
        qs = qs.filter(integration_id__in=bound_ids)

    # ---- 状态筛选（第二轮修复）----
    # 告警本身没有状态，状态属于它被聚类成的**事件**，所以这里必须转成
    # "先查出该状态的事件 id，再按 incident_id 反查告警"，而不是假装 alerts 有 status 列。
    if status == "unclustered":
        qs = qs.filter(incident_id__isnull=True)
    elif status:
        status_incident_ids = await Incident.filter(status=status).values_list("id", flat=True)
        if not status_incident_ids:
            return {"items": [], "total": 0}
        qs = qs.filter(incident_id__in=list(status_incident_ids))

    total = await qs.count()
    # 真实分页：offset 由 page 推导，不再只靠 limit（否则「下一页」是假的）
    rows = await qs.order_by("-created_at").offset((page - 1) * size).limit(size)

    name_map = await task_service.active_task_names_by_integration()

    # 告警看板「状态」列：告警本身没有状态，状态属于它被聚类成的**事件**。
    # 一次批量查回，避免逐行 N+1；未聚类（incident_id 为空）保持 None，前端显示「待聚类」。
    incident_ids = {r.incident_id for r in rows if r.incident_id}
    status_map: dict[int, str] = {}
    if incident_ids:
        status_map = {
            row["id"]: row["status"]
            for row in await Incident.filter(id__in=list(incident_ids)).values("id", "status")
        }

    return {
        "items": [
            AlertItem(
                id=r.id,
                source=r.source,
                component=r.component,
                severity=r.severity,
                title=r.title,
                fingerprint=r.fingerprint,
                incident_id=r.incident_id,
                integration_id=r.integration_id,
                task_names=name_map.get(r.integration_id, []) if r.integration_id else [],
                incident_status=status_map.get(r.incident_id) if r.incident_id else None,
                dedup_count=r.dedup_count or 1,
                last_seen_at=r.last_seen_at.isoformat() if r.last_seen_at else None,
                created_at=r.created_at.isoformat(),
            )
            for r in rows
        ],
        "total": total,
    }


# ------------------------------------------------------ §3.5 看板分面统计
# 严重度 / 事件状态的展示顺序：固定顺序比"按数量倒序"更稳，
# 否则数据一变，侧栏的条目就会上下跳，用户点第二次会点错。
SEVERITY_ORDER = ("P0", "P1", "P2", "P3")
STATUS_ORDER = ("unclustered", "new", "diagnosing", "human_needed", "resolved", "closed")


@router.get("/summary")
async def summary(
    minutes: int = Query(1440, ge=1, le=1440),
    task_id: str | None = Query(
        None, description="F-12 任务作用域：任务 ID / 'none'（仅未归属）/ 缺省（全部）"
    ),
    user: User = Depends(get_current_user),
):
    """看板分面计数（KPI 卡 + 左侧筛选栏的数据源）。

    设计立场：分面计数**只受时间窗口与任务作用域影响**，不受已选的严重度/状态/组件筛选影响。
    这是 Keep 式分面栏的通用语义——否则选中 P0 之后，其余严重度的计数会全部变成 0，
    用户无法判断"换个条件还剩多少"，也就失去了筛选项作为"态势概览"的价值。
    """
    since = model_now() - timedelta(minutes=minutes)
    base = Alert.filter(created_at__gte=since)          # 走 idx_created

    if task_id == "none":
        active_ids = await task_service.active_integration_ids()
        cond = Q(integration_id__isnull=True)
        if active_ids:
            cond |= ~Q(integration_id__in=list(active_ids))
        base = base.filter(cond)
    elif task_id:
        bound_ids = await task_service.integration_ids_for_task(task_id)
        if not bound_ids:
            return {
                "window_minutes": minutes,
                "total": 0,
                "by_severity": [{"key": s, "count": 0} for s in SEVERITY_ORDER],
                "by_status": [{"key": s, "count": 0} for s in STATUS_ORDER],
                "by_component": [],
            }
        base = base.filter(integration_id__in=bound_ids)

    total = await base.count()

    sev_rows = (
        await base.group_by("severity").annotate(cnt=Count("id")).values("severity", "cnt")
    )
    sev_map = {row["severity"]: row["cnt"] for row in sev_rows}

    comp_rows = (
        await base.group_by("component").annotate(cnt=Count("id")).values("component", "cnt")
    )
    by_component = sorted(
        (
            {"key": row["component"] or "unknown", "count": row["cnt"]}
            for row in comp_rows
        ),
        key=lambda item: item["count"],
        reverse=True,
    )[:8]

    # 状态属于事件，不属于告警：先按 incident_id 分组，再翻译成事件状态
    unclustered = await base.filter(incident_id__isnull=True).count()
    inc_rows = (
        await base.filter(incident_id__not_isnull=True)
        .group_by("incident_id")
        .annotate(cnt=Count("id"))
        .values("incident_id", "cnt")
    )
    status_map: dict[int, str] = {}
    if inc_rows:
        status_map = {
            row["id"]: row["status"]
            for row in await Incident.filter(
                id__in=[row["incident_id"] for row in inc_rows]
            ).values("id", "status")
        }
    status_count: dict[str, int] = defaultdict(int)
    status_count["unclustered"] = unclustered
    for row in inc_rows:
        st = status_map.get(row["incident_id"])
        if st:
            status_count[st] += row["cnt"]

    return {
        "window_minutes": minutes,
        "total": total,
        "by_severity": [{"key": s, "count": sev_map.get(s, 0)} for s in SEVERITY_ORDER],
        "by_status": [{"key": s, "count": status_count.get(s, 0)} for s in STATUS_ORDER],
        "by_component": by_component,
    }


# ---------------------------------------------------------------- §8 运维清理
@router.delete("/{alert_id}")
async def delete_alert(alert_id: int, admin: User = Depends(require_admin)) -> dict:
    """物理删除单条告警（admin-only）。

    定位是**运维清理**，不是常规业务操作：告警只是事件链路的入口记录，
    删除不影响已存在的事件（incident_id 为普通字段，无外键级联）。
    与 F-12 任务「软删除=归档」不同，这里就是硬删——测试/垃圾数据直接清掉。
    """
    alert = await Alert.get_or_none(id=alert_id)
    if alert is None:
        raise _err("ALERT_NOT_FOUND", status.HTTP_404_NOT_FOUND, f"alert {alert_id} not found")
    await alert.delete()
    log_event(20, "alert.deleted", alert_id=alert_id, title=alert.title, by=admin.username)
    return {"deleted": alert_id, "title": alert.title}
