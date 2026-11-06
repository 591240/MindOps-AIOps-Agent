from __future__ import annotations

from datetime import datetime

from fastapi import APIRouter, Depends, HTTPException, Query
from tortoise.functions import Count

from app.agents.locks import LockTimeout
from app.auth.dependencies import get_current_user
from app.config import MODEL_TZ
from app.models import (
    Alert,
    Evidence,
    Incident,
    IncidentStep,
    LoopAudit,
    RemediationAction,
    User,
)
from app.schemas import (
    ActionListOut,
    CloseIn,
    ConfirmIn,
    DiagnoseIn,
    EvidenceListOut,
    FeedbackIn,
    IncidentItem,
    IncidentsOut,
    LoopAuditsOut,
    ReclusterIn,
)
from app.agents.orchestrator import (
    close_incident,
    confirm_incident,
    feedback_incident,
    start_diagnosis,
)
from app.incidents.recluster import recluster

router = APIRouter(prefix="/incidents", tags=["incidents"])
VALID_STATUS = {"new", "diagnosing", "human_needed", "resolved", "closed"}
VALID_SEV = {"P0", "P1", "P2", "P3"}


def _require_operator(user: User) -> None:
    """写操作（确认/关闭/反馈）必须是运维角色。

    注册默认产出 engineer，但角色模型未来可能扩展出只读账号；读写边界
    在这里显式收口，避免只读账号能确认高危动作并把内容回灌知识库。
    """
    if user.role not in {"engineer", "admin"}:
        raise HTTPException(403, detail={"code": "FORBIDDEN", "message": "engineer role required"})


def _validate(user: User, status: str | None, severity: str | None) -> None:
    if status and status not in VALID_STATUS:
        raise HTTPException(400, detail={"code": "INVALID_STATUS", "message": "invalid status"})
    if severity and severity not in VALID_SEV:
        raise HTTPException(400, detail={"code": "INVALID_SEVERITY", "message": "invalid severity"})


@router.get("", response_model=IncidentsOut)
async def list_incidents(status: str | None = Query(None), severity: str | None = Query(None), component: str | None = Query(None), page: int = Query(1, ge=1), size: int = Query(20, ge=1, le=100), user: User = Depends(get_current_user)):
    _validate(user, status, severity)
    qs = Incident.all()
    if status: qs = qs.filter(status=status)
    if severity: qs = qs.filter(severity=severity)
    if component: qs = qs.filter(component=component)
    total = await qs.count()
    rows = await qs.order_by("-last_seen_at").offset((page - 1) * size).limit(size)

    # P1-3：原先在循环里逐条 `Alert.filter(incident_id=row.id).count()`，
    # 每页最多触发 100 次额外查询（N+1）。改为一次 group_by 聚合后查表。
    row_ids = [row.id for row in rows]
    alert_counts: dict[int, int] = {}
    if row_ids:
        grouped = await (
            Alert.filter(incident_id__in=row_ids)
            .group_by("incident_id")
            .annotate(cnt=Count("id"))
            .values("incident_id", "cnt")
        )
        alert_counts = {r["incident_id"]: r["cnt"] for r in grouped}

    items = []
    for row in rows:
        items.append(IncidentItem(id=row.id, title=row.title, component=row.component, severity=row.severity, status=row.status, alert_count=alert_counts.get(row.id, 0), last_seen_at=row.last_seen_at.isoformat(), root_cause=row.root_cause, root_cause_confidence=row.root_cause_confidence, created_at=row.created_at.isoformat()))
    return {"items": items, "total": total, "page": page, "size": size}


@router.post("/_recluster")
async def recluster_endpoint(body: ReclusterIn, user: User = Depends(get_current_user)):
    if user.role not in {"engineer", "admin"}:
        raise HTTPException(403, detail={"code": "FORBIDDEN", "message": "engineer role required"})
    if not body.dry_run and not body.apply:
        raise HTTPException(400, detail={"code": "APPLY_REQUIRED", "message": "apply=true required"})
    since = None
    if body.since:
        try:
            aware = datetime.fromisoformat(body.since.replace("Z", "+00:00"))
        except ValueError:
            raise HTTPException(400, detail={"code": "INVALID_SINCE", "message": "invalid since"})
        # 审查 #15：aware 值先归一到模型时区再去 tzinfo，否则 UTC 墙钟会被
        # 当作上海墙上钟比较（差 8 小时）。
        since = aware.astimezone(MODEL_TZ).replace(tzinfo=None) if aware.tzinfo else aware
    mode = "apply" if (body.apply and not body.dry_run) else "dry_run"
    result = await recluster(body.component, since, body.dry_run, body.apply, user.username)
    # 审查 #13：响应回显实际执行模式——此前 dry_run=true+apply=true 会被静默
    # 当作 dry-run 执行，且两种模式响应结构相同，调用方无法分辨是否真的落库。
    result["mode"] = mode
    result["applied"] = mode == "apply"
    return result


@router.post("/{inc_id}/diagnose", status_code=202)
async def diagnose_incident(inc_id: int, body: DiagnoseIn, user: User = Depends(get_current_user)):
    if user.role not in {"engineer", "admin"}:
        raise HTTPException(403, detail={"code": "FORBIDDEN", "message": "engineer role required"})
    try:
        return await start_diagnosis(inc_id, user.id, body.force)
    except LookupError:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "incident not found"})
    except RuntimeError as exc:
        raise HTTPException(409, detail={"code": "DIAGNOSIS_RUNNING", "message": "该事件已有诊断在运行中", "run_id": str(exc)})
    except ValueError as exc:
        raise HTTPException(422, detail={"code": "INVALID_STATE", "message": str(exc)})


@router.post("/{inc_id}/confirm")
async def confirm_incident_endpoint(inc_id: int, body: ConfirmIn, user: User = Depends(get_current_user)):
    _require_operator(user)
    try:
        return await confirm_incident(inc_id, body.action_id, body.decision, user.id, body.actual_cause, body.correct_actions)
    except LookupError:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "incident or action not found"})
    except LockTimeout:
        raise HTTPException(409, detail={"code": "LOCK_BUSY", "message": "该事件正被其他确认操作占用，请稍后重试"})
    except ValueError as exc:
        raise HTTPException(422, detail=str(exc))


# ---- F-06 处置建议与溯源（§3.2 / §3.3 / §3.5 / §5.5）----


@router.get("/{inc_id}/evidence", response_model=EvidenceListOut)
async def list_evidence(inc_id: int, step_id: int | None = Query(None), run_id: str | None = Query(None), user: User = Depends(get_current_user)):
    """事件证据链。支持按 step_id 或 run_id 过滤（run_id 供前端按诊断轮次分组）。"""
    if not await Incident.filter(id=inc_id).exists():
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "incident not found"})
    steps = await IncidentStep.filter(incident_id=inc_id).values_list("id", "run_id")
    run_by_step = {sid: rid for sid, rid in steps}
    qs = Evidence.filter(incident_id=inc_id)
    if step_id is not None:
        qs = qs.filter(step_id=step_id)
    elif run_id:
        # Evidence 无 run_id 字段，需经 step_id 反查（与门禁同构）
        qs = qs.filter(step_id__in=[sid for sid, rid in run_by_step.items() if rid == run_id])
    rows = await qs.order_by("created_at", "id")
    return {
        "incident_id": inc_id,
        "items": [
            {
                "id": item.id, "step_id": item.step_id,
                "run_id": run_by_step.get(item.step_id) if item.step_id else None,
                "chunk_id": item.chunk_id, "quote": item.quote,
                "source_doc": item.source_doc, "score": item.score,
                "created_at": item.created_at.isoformat(),
            }
            for item in rows
        ],
    }


@router.get("/{inc_id}/actions", response_model=ActionListOut)
async def list_actions(inc_id: int, user: User = Depends(get_current_user)):
    """处置建议列表（含风险等级与确认状态）。"""
    if not await Incident.filter(id=inc_id).exists():
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "incident not found"})
    rows = await RemediationAction.filter(incident_id=inc_id).order_by("id")
    return {
        "incident_id": inc_id,
        "items": [
            {
                "id": row.id, "command": row.command, "description": row.description,
                "risk_level": row.risk_level, "requires_confirm": row.requires_confirm,
                "status": row.status, "confirmed_by": row.confirmed_by,
                "exec_status": row.exec_status, "exec_exit_code": row.exec_exit_code,
                "exec_output": row.exec_output, "exec_duration_ms": row.exec_duration_ms,
                "executed_at": row.executed_at.isoformat() if row.executed_at else None,
                "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ],
    }


@router.post("/{inc_id}/close")
async def close_incident_endpoint(inc_id: int, body: CloseIn, user: User = Depends(get_current_user)):
    """§3.5 显式关闭事件；存在 pending action 时 409（防止绕过高危确认）。"""
    _require_operator(user)
    try:
        return await close_incident(inc_id, user.id, body.reason, body.actual_cause, body.correct_actions)
    except LookupError:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "incident not found"})
    except LockTimeout:
        raise HTTPException(409, detail={"code": "LOCK_BUSY", "message": "该事件正被其他确认操作占用，请稍后重试"})
    except ValueError as exc:
        raise HTTPException(409, detail={"code": "PENDING_ACTIONS", "message": str(exc)})


@router.post("/{inc_id}/feedback")
async def feedback_incident_endpoint(inc_id: int, body: FeedbackIn, user: User = Depends(get_current_user)):
    """§5.5 resolved 态补录 actual_cause / correct_actions，不改状态。"""
    _require_operator(user)
    if not body.actual_cause and not body.correct_actions:
        raise HTTPException(400, detail={"code": "EMPTY_FEEDBACK", "message": "actual_cause or correct_actions required"})
    try:
        return await feedback_incident(inc_id, user.id, body.actual_cause, body.correct_actions)
    except LookupError:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "incident not found"})
    except LockTimeout:
        raise HTTPException(409, detail={"code": "LOCK_BUSY", "message": "该事件正被其他确认操作占用，请稍后重试"})


@router.get("/{inc_id}/loop-audits", response_model=LoopAuditsOut)
async def list_loop_audits(inc_id: int, user: User = Depends(get_current_user)):
    """F-04 §3.5 回环审计明细：按 retry_index 升序返回。

    事件不存在 → 404；事件存在但无回环 → 空 items（不报错）。
    """
    if user.role not in {"engineer", "admin"}:
        raise HTTPException(403, detail={"code": "FORBIDDEN", "message": "engineer role required"})
    if not await Incident.filter(id=inc_id).exists():
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "incident not found"})
    rows = await LoopAudit.filter(incident_id=inc_id).order_by("retry_index", "id")
    return {
        "incident_id": inc_id,
        "items": [
            {
                "run_id": row.run_id, "retry_index": row.retry_index, "strategy": row.strategy,
                "query_before": row.query_before, "query_after": row.query_after,
                "recall_top_k": row.recall_top_k, "sim_floor": row.sim_floor,
                "component_scope": row.component_scope, "kb_hits_before": row.kb_hits_before,
                "kb_hits_after": row.kb_hits_after, "model_id": row.model_id, "role": row.role,
                "conf_before": row.conf_before, "conf_after": row.conf_after,
                "escalated": row.escalated, "created_at": row.created_at.isoformat(),
            }
            for row in rows
        ],
    }


@router.get("/{inc_id}")
async def incident_detail(inc_id: int, user: User = Depends(get_current_user)):
    incident = await Incident.get_or_none(id=inc_id)
    if incident is None:
        raise HTTPException(404, detail={"code": "NOT_FOUND", "message": "incident not found"})
    alerts = await Alert.filter(incident_id=inc_id).order_by("created_at")
    steps = await IncidentStep.filter(incident_id=inc_id).order_by("created_at", "id")
    evidence = await Evidence.filter(incident_id=inc_id).order_by("created_at", "id")
    actions = await RemediationAction.filter(incident_id=inc_id).order_by("id")
    return {"incident": {"id": incident.id, "title": incident.title, "component": incident.component, "severity": incident.severity, "status": incident.status, "last_seen_at": incident.last_seen_at.isoformat(), "root_cause": incident.root_cause, "root_cause_confidence": incident.root_cause_confidence, "root_cause_source": incident.root_cause_source, "actual_cause": incident.actual_cause, "created_at": incident.created_at.isoformat()}, "alerts": [{"id": a.id, "source": a.source, "title": a.title, "severity": a.severity, "created_at": a.created_at.isoformat()} for a in alerts], "steps": [{"id": step.id, "run_id": step.run_id, "step": step.step, "agent": step.agent, "input_summary": step.input_summary, "output_summary": step.output_summary, "tool_calls": step.tool_calls, "confidence": step.confidence, "latency_ms": step.latency_ms, "retry_count": step.retry_count, "model_id": step.model_id, "created_at": step.created_at.isoformat()} for step in steps], "evidence": [{"id": item.id, "step_id": item.step_id, "quote": item.quote, "source_doc": item.source_doc, "score": item.score} for item in evidence], "actions": [{"id": action.id, "command": action.command, "description": action.description, "risk_level": action.risk_level, "requires_confirm": action.requires_confirm, "status": action.status, "confirmed_by": action.confirmed_by, "exec_status": action.exec_status, "exec_exit_code": action.exec_exit_code, "exec_output": action.exec_output, "exec_duration_ms": action.exec_duration_ms, "executed_at": action.executed_at.isoformat() if action.executed_at else None} for action in actions]}
