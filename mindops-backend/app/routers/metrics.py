"""统计大盘 JSON 接口（前端 MetricsView 的真实数据源）。

与 `GET /metrics` 的分工：
  - `GET /metrics`          —— Prometheus 文本指标（F-01 §7.4，给抓取器用）
  - `GET /metrics/dashboard` —— **结构化 JSON**（给前端「统计大盘」页用）

端点：
  GET /metrics/dashboard?range=today|7d|30d   —— **admin-only**（对齐 docs/22 §2.1 权限矩阵）

设计立场：
- 全部数值来自真实库表（alerts / incidents / incident_steps / evidence），
  区间内没有样本就返回 0 与空数组，绝不用演示值填充；
- `has_data` 显式告知前端该区间是否存在真实样本，
  避免把「0 条告警」误读成「系统异常」；
- 分桶粒度随 range 变化：today → 按小时，7d / 30d → 按天，
  由 `bucket` 字段回传，前端据此渲染副标题而不是硬编码「按天」。
"""

from __future__ import annotations

from collections import defaultdict
from datetime import datetime, timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from tortoise.functions import Count, Sum

from app.config import model_now

from app.auth.dependencies import require_admin
from app.models import Alert, Evidence, Incident, IncidentStep, User

router = APIRouter(prefix="/metrics", tags=["metrics"])

# 区间白名单：值 = (回看天数, 分桶粒度)
RANGE_SPEC: dict[str, tuple[int, str]] = {
    "today": (1, "hour"),
    "7d": (7, "day"),
    "30d": (30, "day"),
}

# 自动闭环：诊断闭环完成的终态
CLOSED_STATUSES = ("resolved", "closed")


def _err(code: str, http: int, message: str) -> HTTPException:
    """与 F-01 / F-11 / F-12 同款错误体：{"detail": {"code", "message"}}。"""
    return HTTPException(status_code=http, detail={"code": code, "message": message})


def _bucket_key(moment: datetime, bucket: str) -> str:
    """把时间点归到分桶标签：hour → 'HH:00'，day → 'MM-DD'。"""
    if bucket == "hour":
        return f"{moment.hour:02d}:00"
    return moment.strftime("%m-%d")


def _bucket_sequence(since: datetime, now: datetime, bucket: str) -> list[str]:
    """生成完整（含空桶）的分桶标签序列，保证图表 X 轴连续、不跳点。"""
    labels: list[str] = []
    if bucket == "hour":
        cursor = since.replace(minute=0, second=0, microsecond=0)
        step = timedelta(hours=1)
    else:
        cursor = since.replace(hour=0, minute=0, second=0, microsecond=0)
        step = timedelta(days=1)
    while cursor <= now:
        labels.append(_bucket_key(cursor, bucket))
        cursor += step
    return labels or [_bucket_key(now, bucket)]


def _pct(numerator: int, denominator: int) -> float:
    """百分比，保留 1 位小数；分母为 0 时返回 0.0（不抛错、不返回 NaN）。"""
    if denominator <= 0:
        return 0.0
    return round(numerator * 100.0 / denominator, 1)


@router.get("/dashboard")
async def metrics_dashboard(
    range: str = Query("today", description="today | 7d | 30d"),
    user: User = Depends(require_admin),
):
    """按时间范围返回统计大盘所需的全部结构化指标。"""
    spec = RANGE_SPEC.get(range)
    if spec is None:
        raise _err(
            "INVALID_RANGE",
            422,
            f"range 仅支持 {'/'.join(RANGE_SPEC)}，收到 {range!r}",
        )
    days, bucket = spec
    now = model_now()
    # today 取当日 00:00 起，其余按「近 N 天」滚动窗口
    if range == "today":
        since = now.replace(hour=0, minute=0, second=0, microsecond=0)
    else:
        since = now - timedelta(days=days)

    # ---- KPI：告警量 ----
    today_alerts = await Alert.filter(created_at__gte=since).count()

    # ---- 区间内事件（一次取回，后续多项指标复用，避免 N 次扫描）----
    incidents = await Incident.filter(created_at__gte=since).values(
        "id", "component", "status", "created_at", "resolved_at"
    )
    total_incidents = len(incidents)

    # ---- KPI：自动闭环率 ----
    closed = [row for row in incidents if row["status"] in CLOSED_STATUSES]
    auto_close_rate = _pct(len(closed), total_incidents)

    # ---- KPI：平均确诊时延（MTTR，分钟）----
    # 只统计既已闭环、又有 resolved_at 的样本；无有效样本时为 0.0
    durations = [
        (row["resolved_at"] - row["created_at"]).total_seconds()
        for row in closed
        if row["resolved_at"] and row["created_at"] and row["resolved_at"] >= row["created_at"]
    ]
    mttr_min = round(sum(durations) / len(durations) / 60.0, 1) if durations else 0.0

    # ---- KPI：知识命中率 + 按组件分布 ----
    # 命中 = 该事件至少绑定 1 条 Evidence（F-06 §5.1 的证据落库）
    incident_ids = [row["id"] for row in incidents]
    hit_ids: set[int] = set()
    if incident_ids:
        hit_ids = set(
            await Evidence.filter(incident_id__in=incident_ids)
            .distinct()
            .values_list("incident_id", flat=True)
        )
    knowledge_hit_rate = _pct(len(hit_ids), total_incidents)

    comp_total: dict[str, int] = defaultdict(int)
    comp_hit: dict[str, int] = defaultdict(int)
    for row in incidents:
        comp = row["component"] or "unknown"
        comp_total[comp] += 1
        if row["id"] in hit_ids:
            comp_hit[comp] += 1
    hit_rate_by_component = sorted(
        (
            {"component": comp, "rate": _pct(comp_hit[comp], count)}
            for comp, count in comp_total.items()
        ),
        key=lambda item: item["rate"],
        reverse=True,
    )

    # ---- MTTR 趋势（按分桶粒度）----
    labels = _bucket_sequence(since, now, bucket)
    trend_sum: dict[str, float] = defaultdict(float)
    trend_cnt: dict[str, int] = defaultdict(int)
    for row in incidents:
        if row["status"] not in CLOSED_STATUSES or not row["resolved_at"]:
            continue
        if row["resolved_at"] < row["created_at"]:
            continue
        key = _bucket_key(row["resolved_at"], bucket)
        trend_sum[key] += (row["resolved_at"] - row["created_at"]).total_seconds()
        trend_cnt[key] += 1
    mttr_trend = [
        {
            "date": label,
            "mttr": round(trend_sum[label] / trend_cnt[label] / 60.0, 1)
            if trend_cnt[label]
            else 0.0,
        }
        for label in labels
    ]

    # ---- Agent 性能（平均耗时 ms）----
    agent_rows = (
        await IncidentStep.filter(created_at__gte=since, latency_ms__not_isnull=True)
        .group_by("agent")
        .annotate(total=Sum("latency_ms"), cnt=Count("id"))
        .values("agent", "total", "cnt")
    )
    agent_performance = sorted(
        (
            {
                "agent": row["agent"],
                "avg_ms": round((row["total"] or 0) / row["cnt"]) if row["cnt"] else 0,
            }
            for row in agent_rows
        ),
        key=lambda item: item["avg_ms"],
        reverse=True,
    )

    # ---- 告警趋势（按严重度堆叠）----
    # 告警量是"运维发生了什么"的最直接曲线；堆叠严重度让 P0 尖峰一眼可见。
    # 取 (created_at, severity) 两列在本地归桶：MySQL 侧按小时/天分桶需要
    # DATE_FORMAT，跨库不通用，而区间内的行数在这个量级可接受。
    severity_series = ("P0", "P1", "P2", "P3")
    sev_bucket: dict[str, dict[str, int]] = defaultdict(lambda: defaultdict(int))
    for row in await Alert.filter(created_at__gte=since).values("created_at", "severity"):
        key = _bucket_key(row["created_at"], bucket)
        sev_bucket[key][row["severity"] or "P3"] += 1
    alert_trend = [
        {"date": label, **{sev: sev_bucket[label].get(sev, 0) for sev in severity_series}}
        for label in labels
    ]

    # ---- 置信度分布（仅统计有根因置信度的事件）----
    # 五档对齐 F-04 的判定语义：<0.6 会触发回环，因此 0.6 是分水岭。
    CONF_LABELS = ("0-0.2", "0.2-0.4", "0.4-0.6", "0.6-0.8", "0.8-1.0")
    conf_counts = [0] * len(CONF_LABELS)
    conf_values = await Incident.filter(
        created_at__gte=since, root_cause_confidence__not_isnull=True
    ).values_list("root_cause_confidence", flat=True)
    for value in conf_values:
        idx = min(len(CONF_LABELS) - 1, max(0, int(float(value) * len(CONF_LABELS))))
        conf_counts[idx] += 1
    confidence_distribution = [
        {"bucket": label, "count": count}
        for label, count in zip(CONF_LABELS, conf_counts)
    ]

    # ---- 组件告警 TopN（横向条形，回答"哪个组件最吵"）----
    comp_rows = (
        await Alert.filter(created_at__gte=since)
        .group_by("component")
        .annotate(cnt=Count("id"))
        .values("component", "cnt")
    )
    component_top = sorted(
        ({"component": row["component"] or "unknown", "count": row["cnt"]} for row in comp_rows),
        key=lambda item: item["count"],
        reverse=True,
    )[:8]

    return {
        "range": range,
        "bucket": bucket,
        "since": since.isoformat(),
        "generated_at": now.isoformat(),
        "has_data": total_incidents > 0 or today_alerts > 0,
        "kpi": {
            "today_alerts": today_alerts,
            "auto_close_rate": auto_close_rate,
            "mttr_min": mttr_min,
            "knowledge_hit_rate": knowledge_hit_rate,
        },
        "mttr_trend": mttr_trend,
        "hit_rate_by_component": hit_rate_by_component,
        "agent_performance": agent_performance,
        "alert_trend": alert_trend,
        "confidence_distribution": confidence_distribution,
        "component_top": component_top,
    }
