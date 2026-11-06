import asyncio
import hashlib
import os
import uuid
from datetime import datetime, timedelta

os.environ.setdefault("MINDOPS_MYSQL_DB", "mindops_f02_test")

import pytest
import pytest_asyncio

from app.alerts import sse
from app.alerts.normalizer import normalize_app
from app.alerts.service import ingest_one
from app.db import close_db, init_db
from app.incidents.cluster import cluster_alert
from app.incidents.recluster import recluster
from app.models import Alert, Incident, IncidentReclusterAudit

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def initialized_database():
    await init_db()
    yield
    await close_db()


def _component() -> str:
    return f"f02-it-{uuid.uuid4().hex[:12]}"


async def _alert(component: str, title: str, severity: str = "P1") -> Alert:
    fingerprint = hashlib.sha1(f"{component}|{title}|{uuid.uuid4().hex}".encode()).hexdigest()
    return await Alert.create(
        source="app",
        component=component,
        severity=severity,
        title=title,
        detail="integration test",
        fingerprint=fingerprint,
        last_seen_at=datetime.now(),
        created_at=datetime.now(),
    )


async def test_l1_idempotency_and_severity_upgrade():
    component = _component()
    first = await _alert(component, "Redis memory usage > 90%")
    created = await cluster_alert(first)
    assert created["decision"] == "new"

    event_count = sse.broker._seq
    repeated = await cluster_alert(first)
    assert repeated["decision"] == "dedup"
    assert repeated["incident_id"] == created["incident_id"]
    assert sse.broker._seq == event_count

    second = await _alert(component, "Redis memory usage > 90%", "P0")
    merged = await cluster_alert(second)
    incident = await Incident.get(id=created["incident_id"])
    bound = await Alert.get(id=second.id)
    assert merged["decision"] == "dedup"
    assert merged["severity_after"] == "P0"
    assert incident.severity == "P0"
    assert bound.incident_id == incident.id


async def test_l2_chinese_window_and_closed_event_behavior():
    component = _component()
    first = await _alert(component, "Redis 内存使用率超过 90%")
    created = await cluster_alert(first)

    similar = await _alert(component, "Redis 内存使用率达 92%")
    merged = await cluster_alert(similar)
    assert merged["decision"] == "merge"
    assert merged["incident_id"] == created["incident_id"]

    await Incident.filter(id=created["incident_id"]).update(status="closed")
    closed_match = await _alert(component, "Redis 内存使用率超过 90%")
    reopened = await cluster_alert(closed_match)
    assert reopened["decision"] == "new"
    assert reopened["incident_id"] != created["incident_id"]

    await Incident.filter(id=reopened["incident_id"]).update(
        last_seen_at=datetime.now() - timedelta(minutes=6)
    )
    expired = await _alert(component, "Redis 内存使用率达 92%")
    after_window = await cluster_alert(expired)
    assert after_window["decision"] == "new"
    assert after_window["incident_id"] != reopened["incident_id"]


async def test_f01_dedup_refreshes_open_incident_window_without_update_event():
    component = _component()
    first_input = normalize_app(
        {
            "component": component,
            "severity": "P1",
            "title": "Redis 内存使用率超过 90%",
            "detail": "first alert",
        }
    )
    first = await ingest_one(first_input)
    incident_id = first["incident_id"]
    stale_at = datetime.now() - timedelta(minutes=6)
    await Incident.filter(id=incident_id).update(last_seen_at=stale_at)

    event_count = sse.broker._seq
    deduped = await ingest_one({**first_input, "severity": "P0"})
    refreshed = await Incident.get(id=incident_id)

    assert deduped["deduped"] is True
    assert deduped["alert_id"] == first["alert_id"]
    assert refreshed.last_seen_at.replace(tzinfo=None) > stale_at
    assert refreshed.severity == "P0"
    assert sse.broker._seq == event_count + 1
    assert sse.broker._replay[-1][1] == "alert"

    similar = await ingest_one(
        normalize_app(
            {
                "component": component,
                "severity": "P0",
                "title": "Redis 内存使用率达 92%",
                "detail": "similar alert after five minutes",
            }
        )
    )
    assert similar["incident_id"] == incident_id


async def test_manual_ingest_without_pipeline_only_persists_alert():
    component = _component()
    normalized = normalize_app(
        {
            "component": component,
            "severity": "P1",
            "title": "Manual audit alert",
            "detail": "persist only",
        }
    )
    event_count = sse.broker._seq
    result = await ingest_one(normalized, trigger_pipeline=False)

    assert result["is_new"] is True
    assert result["incident_id"] is None
    stored = await Alert.get(id=result["alert_id"])
    assert stored.incident_id is None
    # SSE 推送与诊断流水线**解耦**（见 alerts/service.py）：trigger_pipeline
    # 只决定"跑不跑 F-02 聚类"，不看板推送会出现"列表里有、页面上没有"。
    # 所以这里恰好多一条 alert 事件 —— 断言 +1 而不是 0。
    assert sse.broker._seq == event_count + 1


async def test_same_component_concurrency_creates_one_incident():
    component = _component()
    alerts = [await _alert(component, "Redis connection count increased") for _ in range(20)]

    results = await asyncio.gather(*(cluster_alert(alert) for alert in alerts))
    incident_ids = {result["incident_id"] for result in results}
    assert len(incident_ids) == 1
    assert await Incident.filter(id__in=incident_ids).count() == 1
    assert await Alert.filter(incident_id=next(iter(incident_ids))).count() == 20


async def test_recluster_apply_moves_alerts_and_writes_before_after_audits():
    component = _component()
    now = datetime.now()
    target = await Incident.create(
        fingerprint=hashlib.sha1(f"{component}|target".encode()).hexdigest(),
        title="Redis 内存使用率超过 90%",
        component=component,
        severity="P1",
        status="new",
        last_seen_at=now,
        created_at=now,
    )
    source = await Incident.create(
        fingerprint=hashlib.sha1(f"{component}|source".encode()).hexdigest(),
        title="Redis 内存使用率达 92%",
        component=component,
        severity="P1",
        status="new",
        last_seen_at=now,
        created_at=now + timedelta(seconds=1),
    )
    alert = await _alert(component, "Redis 内存使用率达 92%", "P1")
    await Alert.filter(id=alert.id).update(incident_id=source.id)

    result = await recluster(component, None, dry_run=False, apply=True, actor="pytest")
    migrated = await Alert.get(id=alert.id)
    target_after = await Incident.get(id=target.id)

    assert result["merged"] == 1
    assert any(item["alert_id"] == alert.id for item in result["report"])
    assert migrated.incident_id == target.id
    assert target_after.severity == "P1"
    assert await Incident.get_or_none(id=source.id) is None
    assert await IncidentReclusterAudit.filter(old_incident_id=source.id).count() == 2
