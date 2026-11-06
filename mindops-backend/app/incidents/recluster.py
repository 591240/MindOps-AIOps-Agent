from __future__ import annotations

from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass
from datetime import datetime
from typing import Iterable

from tortoise import Tortoise
from tortoise.transactions import in_transaction

from app.config import model_now
from app.models import Alert, Incident, IncidentReclusterAudit
from app.observability.logging import log_event
from .cluster import SIM_THRESHOLD, component_lock, higher_severity
from .tokenizer import similarity

_PROTECTED_TABLES = ("incident_steps", "evidence", "remediation_actions")


@dataclass(frozen=True)
class ReclusterOperation:
    old_incident_id: int
    new_incident_id: int
    old_severity: str


def build_recluster_plan(
    incidents: Iterable[Incident], protected_ids: set[int]
) -> tuple[list[ReclusterOperation], list[dict], dict[int, str]]:
    """Plan migrations only toward earlier stable roots, so no target is deleted."""
    ordered = sorted(incidents, key=lambda item: (item.created_at, item.id))
    roots: list[Incident] = []
    root_severity: dict[int, str] = {}
    report: list[dict] = []
    operations: list[ReclusterOperation] = []

    for source in ordered:
        if (
            source.id in protected_ids
            or source.status != "new"
            or source.root_cause is not None
        ):
            report.append(
                {
                    "old_incident": source.id,
                    "new_incident": None,
                    "decision": "skipped=diagnosed",
                }
            )
            continue

        best: Incident | None = None
        best_score = -1.0
        for candidate in roots:
            if candidate.component != source.component:
                continue
            candidate_severity = root_severity[candidate.id]
            score = similarity(
                source.title,
                candidate.title,
                source.severity == candidate_severity,
            )
            if score > best_score or (
                score == best_score
                and best is not None
                and candidate.last_seen_at > best.last_seen_at
            ):
                best, best_score = candidate, score

        if best is None or best_score < SIM_THRESHOLD:
            roots.append(source)
            root_severity[source.id] = source.severity
            report.append(
                {
                    "old_incident": source.id,
                    "new_incident": source.id,
                    "decision": "unchanged",
                }
            )
            continue

        root_severity[best.id] = higher_severity(
            root_severity[best.id], source.severity
        )
        operations.append(
            ReclusterOperation(source.id, best.id, source.severity)
        )
        report.append(
            {
                "old_incident": source.id,
                "new_incident": best.id,
                "decision": "merge",
                "similarity": round(best_score, 4),
            }
        )
    return operations, report, root_severity


async def protected_incident_ids(
    incident_ids: list[int], *, conn=None, lock_rows: bool = False
) -> set[int]:
    """Find F-03/F-05/F-06 artifacts without requiring their tables in F-02."""
    if not incident_ids:
        return set()

    connection = conn or Tortoise.get_connection("default")
    try:
        rows = await connection.execute_query_dict(
            "SELECT table_name FROM information_schema.tables "
            "WHERE table_schema = DATABASE() "
            "AND table_name IN ('incident_steps', 'evidence', 'remediation_actions')"
        )
    except Exception:
        # F-02 owns none of these future tables. On non-MySQL test backends,
        # their absence is equivalent to no downstream records.
        return set()

    # aiomysql preserves MySQL's uppercase information_schema column names
    # (`TABLE_NAME`), while other drivers may return the SQL spelling.
    tables = {
        str(row.get("table_name", row.get("TABLE_NAME", ""))).lower()
        for row in rows
    } & set(_PROTECTED_TABLES)
    protected: set[int] = set()
    placeholders = ", ".join("%s" for _ in incident_ids)
    suffix = " FOR UPDATE" if lock_rows else ""
    for table in tables:
        try:
            rows = await connection.execute_query_dict(
                f"SELECT incident_id FROM `{table}` "
                f"WHERE incident_id IN ({placeholders}){suffix}",
                incident_ids,
            )
        except Exception:
            # A discovered table that cannot be read must fail closed: moving
            # such an incident could break a downstream diagnostic chain.
            return set(incident_ids)
        protected.update(
            int(row.get("incident_id", row.get("INCIDENT_ID"))) for row in rows
        )
    return protected


def _incident_query(component: str | None, since: datetime | None, conn=None):
    query = Incident.all()
    if component:
        query = query.filter(component=component)
    if since:
        query = query.filter(created_at__gte=since)
    return query.using_db(conn) if conn is not None else query


async def _load_incidents(
    component: str | None,
    since: datetime | None,
    *,
    conn=None,
    lock_rows: bool = False,
) -> list[Incident]:
    query = _incident_query(component, since, conn)
    if lock_rows:
        query = query.select_for_update()
    return await query.order_by("created_at", "id")


async def _scope_components(component: str | None, since: datetime | None) -> list[str]:
    if component:
        return [component]
    components = await _incident_query(None, since).values_list("component", flat=True)
    return sorted({value or "unknown" for value in components})


@asynccontextmanager
async def _component_locks(components: Iterable[str]):
    """按固定顺序持锁，避免跨组件重聚类与实时聚类交叉修改。"""
    async with AsyncExitStack() as stack:
        for component in sorted(set(components)):
            await stack.enter_async_context(component_lock(component))
        yield


async def _alert_report(
    incident_report: list[dict], *, conn=None
) -> list[dict]:
    """将事件级计划展开为接口契约要求的逐告警迁移明细。"""
    by_incident = {entry["old_incident"]: entry for entry in incident_report}
    if not by_incident:
        return []
    query = Alert.filter(incident_id__in=list(by_incident)).order_by("id")
    alerts = await (query.using_db(conn) if conn is not None else query)
    report: list[dict] = []
    for alert in alerts:
        report.append({"alert_id": alert.id, **by_incident[alert.incident_id]})
    return report


async def _build_recluster_plan(
    component: str | None,
    since: datetime | None,
    *,
    conn=None,
    lock_rows: bool = False,
) -> tuple[list[Incident], list[ReclusterOperation], list[dict], dict[int, str]]:
    incidents = await _load_incidents(
        component, since, conn=conn, lock_rows=lock_rows
    )
    protected = await protected_incident_ids(
        [incident.id for incident in incidents], conn=conn, lock_rows=lock_rows
    )
    operations, incident_report, target_severities = build_recluster_plan(
        incidents, protected
    )
    return (
        incidents,
        operations,
        await _alert_report(incident_report, conn=conn),
        target_severities,
    )


async def recluster(
    component: str | None,
    since: datetime | None,
    dry_run: bool,
    apply: bool,
    actor: str,
) -> dict:
    if dry_run or not apply:
        incidents, operations, report, _ = await _build_recluster_plan(component, since)
        return {
            "scanned": len(incidents),
            "incidents_created": 0,
            "merged": len(operations),
            "errors": 0,
            "report": report,
        }

    components = await _scope_components(component, since)
    async with _component_locks(components):
        async with in_transaction() as conn:
            # Build only after component and incident rows are locked. This prevents a
            # real-time cluster or diagnosis update from invalidating the plan.
            incidents, operations, report, target_severities = await _build_recluster_plan(
                component, since, conn=conn, lock_rows=True
            )
            if operations:
                log_event(
                    20, "incident.recluster_started", actor=actor, operations=len(operations)
                )
                source_ids = [operation.old_incident_id for operation in operations]
                # Evaluate the queryset while the transaction is open; merely
                # constructing a ``select_for_update`` queryset does not issue
                # SQL and therefore would leave alert rows unlocked.
                await Alert.filter(incident_id__in=source_ids).using_db(conn).select_for_update()
                moved_at = model_now()
                target_ids = {operation.new_incident_id for operation in operations}
                for operation in operations:
                    # Keep the protection rule in a final transaction-time guard.
                    source_rows = await Incident.filter(
                        id=operation.old_incident_id,
                        status="new",
                        root_cause__isnull=True,
                    ).using_db(conn).count()
                    if source_rows != 1:
                        raise RuntimeError(
                            f"incident {operation.old_incident_id} changed during recluster"
                        )

                    alert_count = await Alert.filter(
                        incident_id=operation.old_incident_id
                    ).using_db(conn).count()
                    await IncidentReclusterAudit.create(
                        actor=actor,
                        phase="before",
                        old_incident_id=operation.old_incident_id,
                        new_incident_id=operation.new_incident_id,
                        details={
                            "alert_count": alert_count,
                            "old_severity": operation.old_severity,
                        },
                        using_db=conn,
                    )
                    await Alert.filter(incident_id=operation.old_incident_id).using_db(conn).update(
                        incident_id=operation.new_incident_id
                    )

                await Incident.filter(id__in=source_ids).using_db(conn).delete()

                for incident_id, severity in target_severities.items():
                    if incident_id in target_ids:
                        await Incident.filter(id=incident_id).using_db(conn).update(
                            severity=severity, last_seen_at=moved_at
                        )

                for operation in operations:
                    await IncidentReclusterAudit.create(
                        actor=actor,
                        phase="after",
                        old_incident_id=operation.old_incident_id,
                        new_incident_id=operation.new_incident_id,
                        details={
                            "severity_after": target_severities[operation.new_incident_id],
                            "migrated_at": moved_at.isoformat(),
                        },
                        using_db=conn,
                    )
    if operations:
        log_event(20, "incident.recluster_completed", actor=actor, merged=len(operations))

    return {
        "scanned": len(incidents),
        "incidents_created": 0,
        "merged": len(operations),
        "errors": 0,
        "report": report,
    }
