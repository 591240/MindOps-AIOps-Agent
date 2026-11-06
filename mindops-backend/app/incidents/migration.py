"""M1 同步聚类桩迁移到 F-02 规则聚类的存量数据修复。"""

from __future__ import annotations

from app.models import Alert, Incident
from .cluster import OPEN_STATES, event_fingerprint


async def migrate_m1_stub_incidents() -> dict:
    """重建事件指纹和活动时间，并返回需要人工处理的校验问题。"""
    from tortoise import Tortoise

    conn = Tortoise.get_connection("default")
    await conn.execute_query("ALTER TABLE incidents MODIFY fingerprint CHAR(40) NULL")
    await conn.execute_query("UPDATE incidents SET status='new' WHERE status='open'")

    migrated = 0
    without_component: list[int] = []
    for incident in await Incident.all().order_by("id"):
        first_alert = await Alert.filter(incident_id=incident.id).order_by(
            "created_at", "id"
        ).first()
        latest_alert = await Alert.filter(incident_id=incident.id).order_by(
            "-last_seen_at", "-id"
        ).first()
        component = first_alert.component if first_alert else incident.component
        if not component:
            without_component.append(incident.id)
            continue
        last_seen_at = latest_alert.last_seen_at if latest_alert else incident.last_seen_at
        await Incident.filter(id=incident.id).update(
            component=component,
            fingerprint=event_fingerprint(component, incident.title),
            last_seen_at=last_seen_at,
        )
        migrated += 1

    placeholders = ", ".join("%s" for _ in OPEN_STATES)
    duplicate_rows = await conn.execute_query_dict(
        "SELECT fingerprint, GROUP_CONCAT(id ORDER BY id) AS incident_ids, COUNT(*) AS count "
        "FROM incidents "
        f"WHERE status IN ({placeholders}) AND fingerprint IS NOT NULL "
        "GROUP BY fingerprint HAVING COUNT(*) > 1",
        list(OPEN_STATES),
    )
    duplicates = [
        {
            "fingerprint": row["fingerprint"],
            "incident_ids": [int(value) for value in row["incident_ids"].split(",")],
            "count": int(row["count"]),
        }
        for row in duplicate_rows
    ]
    return {
        "migrated": migrated,
        "without_component": without_component,
        "open_fingerprint_duplicates": duplicates,
    }
