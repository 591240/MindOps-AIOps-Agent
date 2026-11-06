from __future__ import annotations

import asyncio
import hashlib
import re
import secrets
import time
from contextlib import asynccontextmanager
from datetime import timedelta

from tortoise.transactions import in_transaction

from app.alerts import context, sse
from app.agents.orchestrator import schedule_auto_diagnosis
from app.config import model_now, settings
from app.models import Alert, Incident
from app.observability import metrics
from app.observability.logging import log_event
from .tokenizer import similarity

OPEN_STATES = ("new", "diagnosing", "human_needed")
WINDOW_SEC = 300
SIM_THRESHOLD = 0.6
SEV_RANK = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}
_locks: dict[str, asyncio.Lock] = {}


def canonicalize(title: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[^\w\u4e00-\u9fff]+", " ", title.lower())).strip()


def event_fingerprint(component: str, title: str) -> str:
    return hashlib.sha1(f"{component}|{canonicalize(title)}".encode()).hexdigest()


def higher_severity(a: str, b: str) -> str:
    return a if SEV_RANK.get(a, 99) <= SEV_RANK.get(b, 99) else b


_canonicalize = canonicalize
_event_fingerprint = event_fingerprint
_higher_severity = higher_severity


@asynccontextmanager
async def component_lock(component: str):
    """组件级锁；Redis 可用时 SET NX，异常时退回进程内锁。"""
    redis = context.redis_client
    key = f"incident:lock:{component}"
    token = secrets.token_hex(12)
    if redis is not None:
        deadline = time.monotonic() + 10
        acquired = False
        while time.monotonic() < deadline:
            try:
                acquired = bool(await redis.set(key, token, nx=True, ex=11))
            except Exception:
                log_event(30, "incident.component_lock_redis_failed", component=component)
                redis = None
                break
            if acquired:
                break
            await asyncio.sleep(0.02)
        if acquired:
            try:
                yield
            finally:
                try:
                    await redis.eval("if redis.call('get',KEYS[1])==ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end", 1, key, token)
                except Exception:
                    pass
            return
        if redis is not None:
            log_event(40, "incident.component_lock_timeout", component=component)
            raise TimeoutError("component lock timeout")
    lock = _locks.setdefault(component, asyncio.Lock())
    try:
        await asyncio.wait_for(lock.acquire(), timeout=10)
    except asyncio.TimeoutError:
        log_event(40, "incident.component_lock_timeout", component=component)
        raise TimeoutError("component lock timeout")
    try:
        yield
    finally:
        lock.release()


async def _publish(event: str, payload: dict) -> None:
    await sse.publish(event, payload)
    if context.redis_client is not None:
        try:
            import json
            await context.redis_client.publish(
                "incident.events",
                json.dumps({"event": event, "payload": payload}, ensure_ascii=False, default=str),
            )
        except Exception as exc:
            log_event(30, "incident.publish_failed", event=event, error=str(exc))


async def _count(incident_id: int, conn=None) -> int:
    query = Alert.filter(incident_id=incident_id)
    return await (query.using_db(conn) if conn is not None else query).count()


async def _cluster_in_transaction(alert: Alert, component: str) -> tuple[dict, str | None, dict | None, int, float | None]:
    """Return the decision and deferred event after the database commit succeeds."""
    async with in_transaction() as conn:
        latest = await Alert.filter(id=alert.id).using_db(conn).first()
        if latest is None:
            raise ValueError(f"alert {alert.id} does not exist")
        if latest.incident_id:
            incident = await Incident.filter(id=latest.incident_id).using_db(conn).first()
            return (
                {
                    "alert_id": latest.id,
                    "decision": "dedup",
                    "incident_id": latest.incident_id,
                    "incident_status": incident.status if incident else "new",
                    "is_new_incident": False,
                    "severity_after": incident.severity if incident else latest.severity,
                },
                None,
                None,
                0,
                None,
            )

        if latest.component != component:
            await Alert.filter(id=latest.id).using_db(conn).update(component=component)

        fp = event_fingerprint(component, latest.title)
        hit = await Incident.filter(
            fingerprint=fp, status__in=OPEN_STATES
        ).using_db(conn).first()
        candidates_count = 0
        best_score: float | None = None

        if hit is not None:
            decision = "dedup"
            target = hit
        else:
            cutoff = model_now() - timedelta(seconds=WINDOW_SEC)
            candidates = await Incident.filter(
                component=component,
                status__in=OPEN_STATES,
                last_seen_at__gte=cutoff,
            ).using_db(conn)
            candidates_count = len(candidates)
            target = None
            score_floor = -1.0
            for candidate in candidates:
                score = similarity(
                    latest.title, candidate.title, latest.severity == candidate.severity
                )
                if score > score_floor or (
                    score == score_floor
                    and target is not None
                    and candidate.last_seen_at > target.last_seen_at
                ):
                    target, score_floor = candidate, score
            best_score = score_floor if target is not None else None
            decision = "merge" if target is not None and score_floor >= SIM_THRESHOLD else "new"

        now = model_now()
        if decision != "new":
            severity = higher_severity(latest.severity, target.severity)
            await Incident.filter(id=target.id).using_db(conn).update(
                severity=severity, last_seen_at=now
            )
            await Alert.filter(id=latest.id).using_db(conn).update(incident_id=target.id)
            alert_count = await _count(target.id, conn)
            result = {
                "alert_id": latest.id,
                "decision": decision,
                "incident_id": target.id,
                "incident_status": target.status,
                "is_new_incident": False,
                "severity_after": severity,
            }
            event = "incident.updated"
            payload = {
                "incident_id": target.id,
                "alert_count": alert_count,
                "severity": severity,
                "last_seen_at": now.isoformat(),
                "added": 1,
            }
        else:
            incident = await Incident.create(
                fingerprint=fp,
                title=latest.title[:255],
                component=component,
                severity=latest.severity,
                status="new",
                last_seen_at=now,
                created_at=now,
                using_db=conn,
            )
            await Alert.filter(id=latest.id).using_db(conn).update(incident_id=incident.id)
            result = {
                "alert_id": latest.id,
                "decision": "new",
                "incident_id": incident.id,
                "incident_status": "new",
                "is_new_incident": True,
                "severity_after": latest.severity,
            }
            event = "incident.created"
            payload = {
                "incident_id": incident.id,
                "alert_count": 1,
                "severity": latest.severity,
                "last_seen_at": now.isoformat(),
                "added": 1,
            }
    return result, event, payload, candidates_count, best_score


async def cluster_alert(alert: Alert | int) -> dict:
    if isinstance(alert, int):
        alert = await Alert.get(id=alert)
    started = time.perf_counter()
    owned = await Alert.get_or_none(id=alert.id)
    if owned and owned.incident_id:
        incident = await Incident.get_or_none(id=owned.incident_id)
        result = {
            "alert_id": alert.id,
            "decision": "dedup",
            "incident_id": owned.incident_id,
            "incident_status": incident.status if incident else "new",
            "is_new_incident": False,
            "severity_after": incident.severity if incident else alert.severity,
        }
        result["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
        metrics.cluster_decisions_total.labels(decision="dedup").inc()
        metrics.cluster_latency.observe(result["latency_ms"] / 1000)
        log_event(
            20,
            "incident.clustered",
            **result,
            component=alert.component or "unknown",
            fingerprint=event_fingerprint(alert.component or "unknown", alert.title)[:12],
            candidates=0,
            best_sim=None,
        )
        return result
    component = alert.component or "unknown"
    candidates = 0
    best_sim: float | None = None
    try:
        async with component_lock(component):
            result, event, payload, candidates, best_sim = await _cluster_in_transaction(
                alert, component
            )
    except TimeoutError:
        raise
    if event is not None and payload is not None:
        await _publish(event, payload)
    # F-02→F-03 自动贯通（防抖）：事件创建/合并后静默 N 秒再诊断，
    # 预留突发告警继续聚类合并的时间，避免诊断立刻把事件推到 resolved 而脱离 OPEN_STATES。
    if event is not None:
        await schedule_auto_diagnosis(result["incident_id"])
    # P2 修复：以重算代替一次性 set，确保后续 resolved/closed 后 gauge 能回落。
    # 新建/合并事件后事件仍处于 OPEN，recompute 即唯一真相来源。
    await metrics.recompute_open_incidents()
    result["latency_ms"] = round((time.perf_counter() - started) * 1000, 3)
    metrics.cluster_decisions_total.labels(decision=result["decision"]).inc()
    metrics.cluster_latency.observe(result["latency_ms"] / 1000)
    log_event(
        20,
        "incident.clustered",
        **result,
        component=component,
        fingerprint=event_fingerprint(component, alert.title)[:12],
        candidates=candidates,
        best_sim=best_sim,
    )
    return result
