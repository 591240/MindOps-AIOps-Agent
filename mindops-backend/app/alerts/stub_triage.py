"""M1 同步聚类桩（文档 §6.4）。

> 解决 V1.0 中"M1 要求告警落到 incidents，但 F-02 第 2 周才开始做"的矛盾。
> 桩逻辑只在 M1 启用（feature flag M1_STUB_TRIAGE），M2 起下线。

边界（§6.4）：只做"fingerprint 复用 / 占位新建"，不做任何语义聚合——
语义聚类仍是 F-02 的边界，本模块未越界。
"""

from __future__ import annotations

from datetime import timedelta

from app.config import model_now
from app.models import Alert, Incident
from app.incidents.cluster import event_fingerprint


async def assign_incident(normalized: dict) -> int:
    """返回该告警应归属的 incident_id。

    1h 窗口内同 fingerprint 且已有 incident_id → 复用
    否则 → 新建占位 incident（title=告警 title，status='new'），返回其 id
    """
    since = model_now() - timedelta(hours=1)
    row = (
        await Alert.filter(
            fingerprint=normalized["fingerprint"],
            last_seen_at__gte=since,
            incident_id__not_isnull=True,
        )
        .order_by("-last_seen_at")
        .first()
    )
    if row and row.incident_id:
        return row.incident_id

    inc = await Incident.create(
        fingerprint=event_fingerprint(normalized["component"], normalized["title"]),
        title=normalized["title"],
        component=normalized["component"],
        status="new",
        severity=normalized["severity"],
        last_seen_at=model_now(),
    )
    return inc.id
