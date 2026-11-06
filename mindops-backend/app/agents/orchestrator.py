"""F-03 六智能体诊断状态机、运行记录和人工确认。"""

from __future__ import annotations

import asyncio
import hashlib
import json
import time
import uuid
from datetime import timedelta
from typing import Any, TypedDict

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from tortoise.transactions import in_transaction

from app.alerts import sse
from app.config import model_now, settings
from app.knowledge.ingest import ingest_document, sanitize
from app.models import (
    Alert,
    DiagnosisRun,
    Evidence,
    Incident,
    IncidentStep,
    KnowledgeDoc,
    RemediationAction,
)
from app.observability import metrics
from app.observability.logging import log_event
from . import context, diagnose, recap, remediate, ticket, triage
from . import calibration, checkpoint, confidence_loop, evidence_gate, executor, loop_config, locks, model_router

# F-04：阈值/回环上限改由 per-run 快照提供（loop_config.snapshot()，DB > env），
# 这里保留常量仅为向后兼容，不再参与判定（见 confidence_loop.maybe_loop）。
CONF_THRESHOLD = 0.6
MAX_RETRY = 2
# F-07：Recap 病历入库门槛（V1.2 改为多信号判定，见 _maybe_ingest）。
#
# 历史：V1.1 用"通用性自评 > 0.7"，但 recap.generality 当时是桩映射
# （conf>=0.6 -> 0.8，<0.6 -> 0.4），与门槛联立后等价于
# "root_cause_confidence >= 0.6 才回灌"，通用性判断从未真正生效。
#
# V1.2 起改为两条路径分别判定：
#   · 人工收口（有 confirmed_by / actual_cause）—— 人工修正可直接放行；
#     无人工修正时仍需置信度过线，避免"人只是点了个确认"就污染知识库。
#   · 自动收口（无人工参与）—— 维持 confidence >= RECAP_INGEST_MIN_CONFIDENCE。
RECAP_INGEST_MIN_CONFIDENCE = 0.6
# 人工修正过的事件视为高价值样本（"AI 判错、人判对"），允许在置信度不足时
# 仍然回灌——这类样本同时是 F-04 校准的黄金数据，流失代价高于污染代价。
RECAP_INGEST_TRUST_HUMAN = True
# 已废弃：保留常量仅为兼容旧引用（如评测脚本），不参与任何判定。
RECAP_INGEST_MIN_GENERALITY = 0.7
# 启动补发 new 事件的时间窗：只补最近 1 小时，更老的历史积压交人工决定。
RECOVER_WINDOW_SECONDS = 3600
_incident_locks: dict[int, asyncio.Lock] = {}
_tasks: dict[str, asyncio.Task] = {}
_pending_auto: dict[int, asyncio.Task] = {}
# 防抖代数：新一轮调度让旧一轮醒来后自行退出，而不是 cancel 旧任务。
# cancel 可能落在 start_diagnosis 的 await 之间（run 已建、worker 未起），
# 留下永远 queued 的诊断；只有睡眠阶段的任务才该被重置。
_auto_generation: dict[int, int] = {}
# 所有在飞的防抖任务（含被新一轮取代、尚未醒来的），停机时统一取消，
# 避免事件循环关闭时报 "Task was destroyed but it is pending"。
_auto_tasks: set[asyncio.Task] = set()
_run_slots = asyncio.Semaphore(settings.diagnosis_max_concurrent_runs)

# 事件锁字典的容量上限。原实现只 setdefault 不回收，事件 id 单调递增，
# 字典会随运行时长无限膨胀（长跑实例的隐性内存泄漏）。
# 超过阈值时清掉"当前未被持有"的锁：正在诊断的事件锁 locked()=True，
# 不会被误删；删掉一把没人持有的锁，下次 _lock() 会重新创建，语义不变。
_MAX_INCIDENT_LOCKS = 2000


def _lock(incident_id: int) -> asyncio.Lock:
    if len(_incident_locks) >= _MAX_INCIDENT_LOCKS:
        for key, existing in list(_incident_locks.items()):
            if not existing.locked():
                _incident_locks.pop(key, None)
    return _incident_locks.setdefault(incident_id, asyncio.Lock())


async def _publish(event: str, payload: dict) -> None:
    # 走 sse.publish（而非 broker.publish）：多副本下需要转发到其它实例
    await sse.publish(event, payload)


def _summary(value: object) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)[:4000]


async def _heartbeat(run_id: str, status: str | None = None) -> None:
    values = {"heartbeat_at": model_now()}
    if status:
        values["status"] = status
    await DiagnosisRun.filter(run_id=run_id).update(**values)


async def persist_step(
    state: dict, step: str, agent: str, result: dict, started: float
) -> IncidentStep:
    """先保存步骤，再发布 SSE，保证前端收到的轨迹可立即查询。"""
    output = {key: value for key, value in result.items() if key != "tool_calls"}
    row = await IncidentStep.create(
        run_id=state["run_id"],
        incident_id=state["incident_id"],
        step=step,
        agent=agent,
        input_summary=_summary({"brief": state.get("brief"), "retry_count": state["retry_count"]}),
        output_summary=_summary(output),
        tool_calls=_summary(result.get("tool_calls", [])),
        latency_ms=round((time.perf_counter() - started) * 1000),
        confidence=result.get("root_cause_confidence"),
        retry_count=state["retry_count"],
        model_id=result.get("model_id"),
    )
    await _heartbeat(state["run_id"])
    await _publish(
        "incident.step",
        {
            "run_id": state["run_id"], "incident_id": state["incident_id"],
            "step": step, "agent": agent, "output_summary": row.output_summary,
            "confidence": row.confidence, "latency_ms": row.latency_ms,
        },
    )
    log_event(20, "incident.step", run_id=state["run_id"], incident_id=state["incident_id"], step=step, agent=agent, latency_ms=row.latency_ms, confidence=row.confidence, retry_count=state["retry_count"], model_id=row.model_id, degraded=False)
    return row


TRUNC_MARK = "\n...[截断]"
MAX_QUOTE = 65535

# incidents.root_cause 是 VARCHAR(255)，而 Agent 结论可能直接来自召回原文
# （`diagnose._heuristic` 的 rag 分支取 `chunk.quote`），长度没有上界。
# 超长时 MySQL 严格模式会让收口 UPDATE 整条失败，把已经跑完的诊断打成 run failed
# + 事件转人工 —— 所以截断放在写库边界，而不是改 Agent 的结论文本口径。
MAX_CAUSE_TEXT = 255


def _clip_cause(text: str) -> str:
    return text if len(text) <= MAX_CAUSE_TEXT else text[:MAX_CAUSE_TEXT]


async def _persist_evidence(state: dict, step_id: int, result: dict) -> None:
    """将 Agent 返回的知识片段绑定到产生它的 Context/Diagnose 步骤（F-06 §5.1）。

    V1.1 来源约定：三类出处**如实标注、不得互相冒充**——
    - `chunk_id/id` 存在 → 知识库文档（source_doc 为真实文档名）
    - `similar_incident_id` 存在 → `similar-incident:{id}`（chunk_id/score 均 NULL）
    - 其余 → `llm-reasoning`（模型推理摘录，非知识库出处）

    边界：空引用跳过（不落脏数据）；超长截断至 65535 并打 `[截断]` 标记（R3）。
    """
    chunks = result.get("kb_chunks") or []
    for chunk in chunks:
        if not isinstance(chunk, dict):
            continue
        quote = str(
            chunk.get("quote") or chunk.get("content") or chunk.get("text") or ""
        ).strip()
        if not quote:
            continue
        if chunk.get("similar_incident_id"):
            # V1.1：相似案例证据——无知识切片、无相似度分
            source_doc = f"similar-incident:{chunk['similar_incident_id']}"
            chunk_id, score = None, None
        elif chunk.get("chunk_id") or chunk.get("id"):
            source_doc = str(
                chunk.get("source_doc") or chunk.get("source") or "knowledge-base"
            ).strip()
            chunk_id, score = chunk.get("chunk_id") or chunk.get("id"), chunk.get("score")
        else:
            source_doc, chunk_id, score = evidence_gate.LLM_SOURCE_DOC, None, None
        # 多字节安全：TRUNC_MARK 含中文，按字符切会低估字节数（utf8mb4 下 1 汉字 3 字节），
        # 故预留 1000 字符余量，确保不超过 TEXT 的 65535 字节上限。
        if len(quote) > MAX_QUOTE:
            quote = quote[: MAX_QUOTE - 1000] + TRUNC_MARK
        row = await Evidence.create(
            incident_id=state["incident_id"],
            step_id=step_id,
            chunk_id=chunk_id,
            quote=quote,
            source_doc=source_doc[:255],
            score=score,
        )
        metrics.evidence_total.labels(
            source_type=metrics.evidence_source_type(source_doc)
        ).inc()
        log_event(20, "evidence.persisted", incident_id=state["incident_id"], step_id=step_id, run_id=state.get("run_id"), source_doc=source_doc[:255], score=score)
        await _publish(
            "incident.evidence_added",
            {
                "incident_id": state["incident_id"], "run_id": state.get("run_id"),
                "evidence_id": row.id, "step_id": step_id,
                "source_doc": source_doc[:255], "score": score,
            },
        )


async def _emit_evidence(state: dict, step_id: int, source_doc: str, quote: str) -> None:
    """证据落库 + 指标 + 日志 + SSE 的统一出口（避免三处调用各写一遍）。"""
    row = await Evidence.create(
        incident_id=state["incident_id"], step_id=step_id, chunk_id=None,
        quote=quote[:MAX_QUOTE], source_doc=source_doc[:255], score=None,
    )
    metrics.evidence_total.labels(source_type=metrics.evidence_source_type(source_doc)).inc()
    log_event(20, "evidence.persisted", incident_id=state["incident_id"], step_id=step_id, run_id=state.get("run_id"), source_doc=source_doc, score=None)
    await _publish(
        "incident.evidence_added",
        {
            "incident_id": state["incident_id"], "run_id": state.get("run_id"),
            "evidence_id": row.id, "step_id": step_id, "source_doc": source_doc, "score": None,
        },
    )


async def _persist_rule_evidence(state: dict, step_id: int) -> None:
    """规则命中证据（F-06 §5.3 对 F-05 规则短路的衔接）。

    规则短路路径不跑 Context/Diagnose，其"出处"是 `pattern_rules.yaml` 的条目，
    因此在此落一条 `pattern-rule` 证据，使规则结论同样可回溯、且能通过门禁。
    """
    cause = (state.get("rule_root_cause") or state.get("root_cause") or "").strip()
    if not cause:
        return
    await _emit_evidence(state, step_id, evidence_gate.RULE_SOURCE_DOC, cause)


async def _persist_reasoning_evidence(state: dict, step_id: int) -> None:
    """LLM 结论的推理摘录证据（§5.3 (b) / §5.1 llm-reasoning）。

    MVP 桩没有真实推理链，取结论文本作为推理摘录**如实标注**为 llm-reasoning，
    不冒充知识库原文。F-09 接入真实 LLM 后，此处换成模型输出的 reasoning 摘录。
    """
    cause = (state.get("root_cause") or "").strip()
    if not cause:
        return
    await _emit_evidence(state, step_id, evidence_gate.LLM_SOURCE_DOC, cause)


async def _human_needed(state: dict, reason: str) -> None:
    state["status"] = "human_needed"
    state["human_reason"] = reason
    incident_values = {"status": "human_needed"}
    # 低置信或高危暂停时也保留已经得到的诊断结论，供详情页和人工确认复核。
    if state.get("root_cause"):
        incident_values.update(
            root_cause=_clip_cause(state["root_cause"]),
            root_cause_confidence=state.get("root_cause_confidence"),
            root_cause_source=state.get("root_cause_source") or None,
        )
    # 审查 #6：带状态前置条件——仅 diagnosing 可转人工，防止把用户在防抖/
    # 排队期间已显式 closed 的事件"复活"（与 _worker 失败路径的守卫同构）。
    updated = await Incident.filter(
        id=state["incident_id"], status="diagnosing"
    ).update(**incident_values)
    if not updated:
        state["status"] = "closed"
    if state.get("run_id"):
        await _heartbeat(state["run_id"])
    actions = await RemediationAction.filter(incident_id=state["incident_id"]).order_by("id")
    payload = {
        "incident_id": state["incident_id"], "reason": reason,
        "actions": [{"id": action.id, "command": action.command, "risk_level": action.risk_level} for action in actions],
    }
    # F-04 §3.3：reason='low_confidence' 时附回环摘要，供前端展示"AI 已补查 N 次"
    if reason == "low_confidence":
        payload["loop_summary"] = state.get("loop_summary") or (
            await confidence_loop.build_loop_summary(state) if state.get("run_id") else None
        )
    await _publish("incident.human_needed", payload)


async def _store_actions(state: dict) -> bool:
    # R6 修复：只清理 pending。approved/skipped 是人工确认记录（含 confirmed_by），
    # 属审计资产，重诊断时删除会静默撤销人工确认、让审计链断裂（V1.1 铁律）。
    await RemediationAction.filter(incident_id=state["incident_id"], status="pending").delete()
    has_high = False
    for action in state["actions"]:
        risk = action["risk_level"]
        # F-03 V1.1 口径：仅 high 需人工确认；med/low 落库即 approved
        requires_confirm = risk == "high"
        has_high = has_high or requires_confirm
        row = await RemediationAction.create(
            incident_id=state["incident_id"], command=action["command"],
            description=action.get("description"), risk_level=risk,
            requires_confirm=requires_confirm,
            status="pending" if requires_confirm else "approved",
        )
        metrics.actions_generated.labels(risk_level=risk, auto_approved=str(not requires_confirm).lower()).inc()
        log_event(20, "action.generated", incident_id=state["incident_id"], action_id=row.id, risk_level=risk, auto_approved=not requires_confirm)
        await _publish(
            "incident.action_required",
            {
                "incident_id": state["incident_id"], "run_id": state.get("run_id"),
                "action_id": row.id, "command": row.command,
                "risk_level": risk, "requires_confirm": requires_confirm,
            },
        )
    await metrics.recompute_action_stats()
    return has_high


async def _apply_confidence_scoring(state: dict, step_result: dict) -> None:
    """P0-2/P0-3：诊断结论的质量收口（失败模式埋点 + 证据加权置信度）。

    把"模型自报的置信度"替换为"按本轮证据与已知失败模式修正后的置信度"，
    同时把信号打点出来。`state` 与 `step_result` 双写：前者供回环判定与事件
    收口使用，后者供 step 表落库使用 —— 两者必须一致，否则审计链会自相矛盾。

    保留原始自报值在 `root_cause_confidence_raw`，便于对照"模型自评 vs 修正后"
    的偏差，也为评测集校准提供数据。
    """
    scored = calibration.score_diagnosis(
        component=state.get("component"),
        root_cause=state.get("root_cause"),
        self_report=state.get("root_cause_confidence"),
        source=state.get("root_cause_source"),
        rule_hit=bool(state.get("pattern_hit")),
        similar_cases=state.get("similar_cases"),
        kb_chunks=state.get("kb_chunks"),
        threshold=state.get("conf_threshold"),
    )
    state["root_cause_confidence_raw"] = scored["self_report"]
    state["root_cause_confidence"] = scored["confidence"]
    state["diagnosis_signals"] = scored["signals"]
    step_result["root_cause_confidence_raw"] = scored["self_report"]
    step_result["root_cause_confidence"] = scored["confidence"]

    for signal in scored["signals"]:
        metrics.diagnosis_failure_signal_total.labels(signal=signal).inc()
    metrics.confidence_adjustment.observe(
        float(scored["confidence"]) - float(scored["self_report"])
    )
    for reason in scored["caps_applied"]:
        metrics.confidence_suppressed_total.labels(reason=reason).inc()

    if scored["adjusted"] or scored["signals"]:
        log_event(
            20, "diagnosis.confidence_scored",
            run_id=state.get("run_id"), incident_id=state.get("incident_id"),
            retry_count=state.get("retry_count"),
            self_report=scored["self_report"], confidence=scored["confidence"],
            evidence_count=scored["evidence_count"], signals=scored["signals"],
            caps_applied=scored["caps_applied"], model_id=state.get("model_id"),
        )


async def _build_state(incident_id: int, run_id: str) -> dict:
    incident = await Incident.get(id=incident_id)
    alerts = await Alert.filter(incident_id=incident_id).order_by("created_at", "id")
    # F-04 §3.2：run 启动时冻结一份参数快照，本次 run 全程只读它（per-run 快照）
    snapshot = await loop_config.snapshot()
    return {
        "run_id": run_id, "incident_id": incident_id, "title": incident.title,
        "alerts": [{"id": item.id, "title": item.title, "detail": item.detail, "severity": item.severity, "source": item.source, "component": item.component} for item in alerts[:20]],
        "component": incident.component, "severity": incident.severity,
        "brief": "", "pattern_hit": False, "rule_root_cause": "", "rule_playbook": [],
        "context": {}, "similar_cases": [], "kb_chunks": [], "root_cause": "",
        "root_cause_confidence": 0.0, "root_cause_source": "", "missing_info": [],
        "actions": [], "remediation_source": "", "retry_count": 0, "needs_human": False,
        "human_reason": "", "status": "diagnosing",
        "model_role": "chat", "model_id": None, "loop_retrieval": None,
        **snapshot,
    }


class DiagnosisState(TypedDict, total=False):
    """LangGraph 状态模式：键 = `_build_state()` 的产物 + 各 Agent 返回值。

    `total=False` 是因为节点每次只写自己那部分键，未写入的键在首轮不存在。
    """

    run_id: str
    incident_id: int
    title: str
    alerts: list[dict]
    component: str | None
    severity: str
    brief: str
    pattern_hit: bool
    rule_root_cause: str
    rule_playbook: list
    context: dict
    similar_cases: list[dict]
    kb_chunks: list[dict]
    root_cause: str
    root_cause_confidence: float
    root_cause_confidence_raw: float
    root_cause_source: str
    diagnosis_signals: list[str]
    missing_info: list[str]
    actions: list[dict]
    remediation_source: str
    retry_count: int
    needs_human: bool
    human_reason: str
    halt_reason: str
    loop_summary: dict | None
    loop_retrieval: dict | None
    status: str
    model_role: str
    model_id: int | None
    model_degraded: Any
    ticket_summary: str
    recap: str
    generality: float
    generality_kind: str
    case_title: str
    ingest_ready: bool
    knowledge_injected: dict
    tool_calls: list[dict]
    next_step: str
    awaiting_confirm: bool
    pending_actions: bool
    human_decision: Any
    # 人工闸口 resume 回填的审计字段。必须显式声明：不在模式里的键会被
    # LangGraph 静默丢弃，表现为"确认人写不进 incidents"这类无声失败。
    confirmed_by: int | None
    actual_cause: str | None
    correct_actions: str | None
    # per-run 参数快照（loop_config.snapshot()，F-04 §3.2）
    conf_threshold: float
    max_retry: int
    reasoning_escalation: bool
    cross_component_fallback: bool
    top_k_base: int
    top_k_loop: int
    sim_floor_base: float
    sim_floor_loop: float


def _merge(state: DiagnosisState, result: dict) -> dict:
    """等价于旧代码的 `state.update(result)`：合并后整体回写通道。"""
    merged = dict(state)
    merged.update(result)
    return merged


async def _node_triage(state: DiagnosisState) -> dict:
    started = time.monotonic()
    result = await triage.run(state)
    merged = _merge(state, result)
    step = await persist_step(merged, "triage", "TriageAgent", result, started)
    # 规则命中的出处是规则条目本身（短路路径不跑 Context/Diagnose），落证据以便过门禁与溯源
    if merged["pattern_hit"]:
        await _persist_rule_evidence(merged, step.id)
    return merged


def _edge_after_triage(state: DiagnosisState) -> str:
    return "remediate" if state["pattern_hit"] else "pick_model"


async def _node_pick_model(state: DiagnosisState) -> dict:
    # 首次 Diagnose 用 chat 角色模型（F-04 §6.2）
    model_id, role, _, _ = await model_router.pick_model("chat")
    return _merge(state, {"model_id": model_id, "model_role": role})


async def _node_context(state: DiagnosisState) -> dict:
    started = time.monotonic()
    result = await context.run(state)
    merged = _merge(state, result)
    step = await persist_step(merged, "context", "ContextAgent", result, started)
    await _persist_evidence(merged, step.id, result)
    return merged


async def _node_diagnose(state: DiagnosisState) -> dict:
    started = time.monotonic()
    try:
        result = await diagnose.run(state)
    except model_router.ModelUnavailableError as exc:
        # F-09 §5.4：同 role 全部模型失败 → step failed，事件转人工
        log_event(40, "llm.model_unavailable", run_id=state["run_id"],
                  incident_id=state["incident_id"], error=str(exc))
        return _merge(state, {"halt_reason": "model_unavailable"})
    merged = _merge(state, result)
    # P0-2/P0-3：结论落库前的质量收口（失败模式信号 + 证据加权置信度）。
    # 必须在 persist_step 之前 —— step / incident / loop_audit 三处读的都是
    # `root_cause_confidence`，先收口才能保证审计链上是同一个值。
    await _apply_confidence_scoring(merged, result)
    step = await persist_step(merged, "diagnose", "DiagnoseAgent", result, started)
    await _persist_evidence(merged, step.id, result)
    # §5.3 (b)：llm 来源必须有本 run 的推理摘录证据，否则门禁降级
    if merged["root_cause_source"] == "llm" and merged["root_cause"]:
        await _persist_reasoning_evidence(merged, step.id)
    # F-04 §7.3：本轮 Diagnose 产出后，回填上一次回环的 conf_after
    if merged["retry_count"] > 0:
        await confidence_loop.backfill_conf_after(
            merged["run_id"], merged["retry_count"], merged["root_cause_confidence"]
        )
    return merged


def _edge_after_diagnose(state: DiagnosisState) -> str:
    return "human" if state.get("halt_reason") else "loop_route"


async def _node_loop_route(state: DiagnosisState) -> dict:
    """置信度回环的决策 + 副作用节点（旧代码里的 `decision = await maybe_loop(state)`）。

    `maybe_loop` 原地写 retry_count / loop_retrieval / model_* / needs_human，
    因此必须在调用之后才快照 state，早拷贝会丢这些值。
    """
    decision = await confidence_loop.maybe_loop(state)
    merged = dict(state)
    merged["next_step"] = decision
    return merged


def _edge_after_loop_route(state: DiagnosisState) -> str:
    decision = state.get("next_step")
    if decision == confidence_loop.REMEDIATE:
        return "remediate"
    if decision == confidence_loop.CONTEXT:
        return "context"
    return "human"


async def _node_remediate(state: DiagnosisState) -> dict:
    started = time.monotonic()
    result = await remediate.run(state)
    merged = _merge(state, result)
    await persist_step(merged, "remediate", "RemediateAgent", result, started)
    if await _store_actions(merged):
        merged["halt_reason"] = "high_risk"
    # 分级自动处置：总闸开启时，后台执行 low + approved 的动作（medium/high 不动）。
    # create_task 不阻塞诊断主链路；强引用挂 _auto_tasks 防止被 GC。
    if settings.auto_remediate and merged.get("incident_id"):
        exec_task = asyncio.create_task(_auto_remediate_after_store(merged["incident_id"]))
        _auto_tasks.add(exec_task)
        exec_task.add_done_callback(_auto_tasks.discard)
    return merged


async def _auto_remediate_after_store(incident_id: int) -> None:
    """执行 low 风险动作并广播回执；失败只记日志，不影响事件状态机。"""
    try:
        executed = await executor.auto_execute_for_incident(incident_id)
        if executed:
            await _publish(
                "incident.action_executed",
                {"incident_id": incident_id, "executed": executed},
            )
    except Exception as exc:  # 执行器故障绝不能拖垮诊断收尾
        log_event(30, "auto_remediate.failed", incident_id=incident_id, error=str(exc))


def _edge_after_remediate(state: DiagnosisState) -> str:
    return "human" if state.get("halt_reason") == "high_risk" else "ticket"


async def _node_ticket(state: DiagnosisState) -> dict:
    started = time.monotonic()
    result = await ticket.run(state)
    merged = _merge(state, result)
    await persist_step(merged, "ticket", "TicketAgent", result, started)
    return merged


async def _node_finalize(state: DiagnosisState) -> dict:
    incident_id = state["incident_id"]
    now = model_now()
    values: dict[str, Any] = {
        "root_cause": _clip_cause(state["root_cause"]),
        "root_cause_confidence": state["root_cause_confidence"],
        "root_cause_source": state["root_cause_source"],
        "status": "resolved",
        "resolved_at": now,
    }
    # 人工闸口 resume 回来的 run：确认人与人工修正是审计链字段，必须由图落，
    # 否则走 resume 路径会比旧的 confirm_incident 内联收口少写这两列。
    if state.get("confirmed_by") is not None:
        values["confirmed_by"] = state["confirmed_by"]
    if state.get("actual_cause"):
        values["actual_cause"] = state["actual_cause"]
    async with in_transaction() as conn:
        # 审查 #6：closed 事件不得被诊断复活。人工闸口 resume 回来时事件处于
        # human_needed，所以这里放行 diagnosing / human_needed 两态。
        await Incident.filter(
            id=incident_id, status__in=("diagnosing", "human_needed")
        ).using_db(conn).update(**values)
    # P2 修复：事件收口为 resolved 后刷新开放事件数，避免 gauge 误报积压。
    await metrics.recompute_open_incidents()
    await _publish("incident.diagnosed", {"incident_id": incident_id, "root_cause": state["root_cause"], "confidence": state["root_cause_confidence"], "source": state["root_cause_source"]})
    return _merge(state, {"status": "resolved"})


async def _node_recap(state: DiagnosisState) -> dict:
    result = await run_recap(
        state["incident_id"], state["run_id"], state,
        confirmed_by=state.get("confirmed_by"),
    )
    # 病历与回灌结果写回通道：confirm_incident 走 resume 路径时从这里取
    # knowledge_injected 组装响应，否则只能重查库、且拿不到本次 run 的判定。
    return _merge(state, result or {})


async def _node_human(state: DiagnosisState) -> dict:
    """转人工的**副作用**节点：翻事件状态、保留结论、发 SSE。

    必须与 `_node_await_human` 分开：resume 时 `interrupt()` 所在节点会被**整体重放**，
    若副作用与 interrupt 同节点，人工每确认一次就重复推一条 incident.human_needed。
    """
    reason = state.get("halt_reason") or state.get("human_reason") or "low_confidence"
    await _human_needed(state, reason)
    merged = _merge(state, {"halt_reason": reason})
    # 只有"确实有动作等人批"才值得挂起线程：低置信耗尽 / 模型全败这两条路径没有任何
    # pending 动作，挂起只会留下一个永远不会被 resume 的断点。
    merged["awaiting_confirm"] = await RemediationAction.filter(
        incident_id=state["incident_id"], status="pending"
    ).exists()
    return merged


def _edge_after_human(state: DiagnosisState) -> str:
    return "await_human" if state.get("awaiting_confirm") else END


async def _node_await_human(state: DiagnosisState) -> dict:
    """人工闸口：`interrupt()` 暂停，断点随 checkpoint 落库，进程重启后仍可续跑。"""
    decision = interrupt({
        "incident_id": state["incident_id"],
        "run_id": state["run_id"],
        "reason": state.get("halt_reason") or "human_review",
    })
    merged = _merge(state, {"human_decision": decision})
    if isinstance(decision, dict):
        # 人工回填的 confirmed_by / actual_cause / correct_actions 直接进通道，
        # 供 finalize 落审计字段、recap 走"人工修正优先"的回灌门槛。
        merged.update(decision)
    return merged


async def _node_human_gate(state: DiagnosisState) -> dict:
    """resume 后重新读库：仍有 pending 动作就再停一次，全部处理完才放行收尾。"""
    pending = await RemediationAction.filter(
        incident_id=state["incident_id"], status="pending"
    ).exists()
    return _merge(state, {"pending_actions": pending})


def _edge_after_gate(state: DiagnosisState) -> str:
    return "await_human" if state.get("pending_actions") else "ticket"


def _build_graph():
    builder = StateGraph(DiagnosisState)
    builder.add_node("triage", _node_triage)
    builder.add_node("pick_model", _node_pick_model)
    builder.add_node("context", _node_context)
    builder.add_node("diagnose", _node_diagnose)
    builder.add_node("loop_route", _node_loop_route)
    builder.add_node("remediate", _node_remediate)
    builder.add_node("ticket", _node_ticket)
    builder.add_node("finalize", _node_finalize)
    builder.add_node("recap", _node_recap)
    builder.add_node("human", _node_human)
    builder.add_node("await_human", _node_await_human)
    builder.add_node("human_gate", _node_human_gate)

    builder.add_edge(START, "triage")
    builder.add_conditional_edges("triage", _edge_after_triage, ["pick_model", "remediate"])
    builder.add_edge("pick_model", "context")
    builder.add_edge("context", "diagnose")
    builder.add_conditional_edges("diagnose", _edge_after_diagnose, ["loop_route", "human"])
    builder.add_conditional_edges(
        "loop_route", _edge_after_loop_route, ["context", "remediate", "human"]
    )
    builder.add_conditional_edges("remediate", _edge_after_remediate, ["ticket", "human"])
    builder.add_conditional_edges("human", _edge_after_human, ["await_human", END])
    builder.add_edge("await_human", "human_gate")
    builder.add_conditional_edges("human_gate", _edge_after_gate, ["await_human", "ticket"])
    builder.add_edge("ticket", "finalize")
    builder.add_edge("finalize", "recap")
    builder.add_edge("recap", END)
    return builder.compile(checkpointer=checkpoint.get_saver())


_graph_cache: tuple[object, Any] | None = None


def get_graph():
    """按 saver 身份缓存编译结果。

    换库时必须重编译：测试逐模块重绑 MySQL 库，旧池子随上一个事件循环一起销毁，
    复用旧图会把检查点写到已经死掉的连接上。
    """
    global _graph_cache
    saver = checkpoint.get_saver()
    if _graph_cache is None or _graph_cache[0] is not saver:
        _graph_cache = (saver, _build_graph())
    return _graph_cache[1]


def _thread_config(run_id: str, max_retry: int) -> dict:
    """一个 run 独占一个 thread；`recursion_limit` 随该 run 冻结的 max_retry 给足余量。"""
    config = checkpoint.thread_config(run_id)
    config["recursion_limit"] = 3 * (int(max_retry) + 2) + 8
    return config


async def _drive(run_id: str, payload: Any) -> dict:
    """初次运行传完整 state；resume 传 `Command(resume=...)`，回环参数从检查点取。"""
    max_retry = payload.get("max_retry") if isinstance(payload, dict) else None
    if max_retry is None:
        snapshot = await get_graph().aget_state(checkpoint.thread_config(run_id))
        max_retry = (snapshot.values or {}).get("max_retry", settings.max_retry)
    return await get_graph().ainvoke(payload, _thread_config(run_id, int(max_retry)))


async def _run_graph(incident_id: int, run_id: str) -> dict:
    """同步跑完整张图（不经过信号量/锁/run 状态机）。

    **生产路径不调用它**——线上统一走 `_execute()`（含并发闸、事件锁、
    崩溃续跑）。这里是给 tests/test_f03_graph.py 用的轻量入口：
    测试要验证的是图本身的拓扑与分支，不该被队列和锁干扰。
    删之前先看测试，别当死代码清掉。
    """
    state = await _build_state(incident_id, run_id)
    return await _drive(run_id, state)


async def _worker(incident_id: int, run_id: str) -> None:
    await _execute(incident_id, run_id)


async def _execute(incident_id: int, run_id: str, *, mode: str = "start", value: Any = None) -> dict:
    """信号量 + per-incident 锁 + run 状态机的唯一出口。

    三种模式共用同一条收口路径，避免"初次运行"和"续跑"长出两套状态机：
    - `start`：新建 run，出队并抢到锁之后才冻结 per-run 参数快照（与迁移前同点）；
    - `resume`：人工确认回填，从 `await_human` 的 interrupt 处继续；
    - `continue`：进程崩溃后凭检查点从最后完成的节点续跑，不重放已完成节点。

    已知代价：`persist_step` 落库与节点返回不是一个原子动作，崩溃恰好夹在中间
    会留下重复 step —— 检查点只能保证 at-least-once per node。
    """
    acquired = False
    try:
        try:
            await asyncio.wait_for(_run_slots.acquire(), timeout=settings.diagnosis_queue_timeout_seconds)
            acquired = True
        except asyncio.TimeoutError:
            if mode != "start":
                # 断点完好在库里，事件已经处于 human_needed：保持原状态，下一次确认/重启再试。
                log_event(30, "incident.execute_deferred", run_id=run_id, incident_id=incident_id, mode=mode)
                return {}
            await _human_needed({"incident_id": incident_id, "status": "diagnosing", "human_reason": ""}, "queue_timeout")
            await DiagnosisRun.filter(run_id=run_id).update(status="failed", finished_at=model_now(), heartbeat_at=model_now())
            return {}
        async with _lock(incident_id):
            if mode == "start":
                payload: Any = await _build_state(incident_id, run_id)
                await DiagnosisRun.filter(run_id=run_id).update(status="running", started_at=model_now(), heartbeat_at=model_now())
            elif mode == "resume":
                payload = Command(resume=value)
            else:
                payload = None
                await DiagnosisRun.filter(run_id=run_id).update(status="running", heartbeat_at=model_now())
            values = await _drive(run_id, payload)
            # 图停在 interrupt 上时 ainvoke 正常返回并带 `__interrupt__`：这不是失败，
            # 也不能写 finished_at —— 线程还要被 resume。
            paused = bool(values.get("__interrupt__"))
            updates: dict[str, Any] = {"status": "paused" if paused else "finished", "heartbeat_at": model_now()}
            if not paused:
                updates["finished_at"] = model_now()
            await DiagnosisRun.filter(run_id=run_id).update(**updates)
            return values
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        log_event(40, "incident.diagnosis_failed", incident_id=incident_id, run_id=run_id, error=str(exc))
        # P3 修复：先判定事件真实状态，避免"主线已 resolved（仅 run_recap 抛错）"时
        # 被错误翻转为 human_needed 并向前端推送矛盾的双 SSE。
        incident = await Incident.get_or_none(id=incident_id)
        real_status = incident.status if incident else None
        if real_status in ("resolved", "closed"):
            # 诊断主线已成功收口，仅 recap 等后置步骤失败：保留 resolved 结论，
            # 不翻转状态、不发送 human_needed，改用非矛盾事件告知前端 recap 异常。
            await DiagnosisRun.filter(run_id=run_id).update(status="finished", finished_at=model_now(), heartbeat_at=model_now())
            await _publish("incident.recap_failed", {"incident_id": incident_id, "run_id": run_id, "error": str(exc)})
        else:
            await Incident.filter(id=incident_id, status="diagnosing").update(status="human_needed")
            await DiagnosisRun.filter(run_id=run_id).update(status="failed", finished_at=model_now(), heartbeat_at=model_now())
            await _publish("incident.human_needed", {"incident_id": incident_id, "reason": "interrupted", "actions": []})
        return {}
    finally:
        if acquired:
            _run_slots.release()
        _tasks.pop(run_id, None)


async def resume_diagnosis(run_id: str, value: dict | None = None) -> dict:
    """人工确认后从闸口续跑：图接着走 Ticket → 收口 → Recap。"""
    run = await DiagnosisRun.get_or_none(run_id=run_id)
    if run is None:
        raise LookupError(f"run {run_id} not found")
    return await _execute(run.incident_id, run_id, mode="resume", value=value or {"approved": True})


async def start_diagnosis(incident_id: int, triggered_by: int, force: bool = False) -> dict:
    """创建异步 run；锁内完成状态前置，防止同一事件重复跑图。"""
    async with _lock(incident_id):
        incident = await Incident.get_or_none(id=incident_id)
        if incident is None:
            raise LookupError("incident not found")
        active = await DiagnosisRun.filter(incident_id=incident_id, status__in=("queued", "running")).order_by("-heartbeat_at").first()
        if active:
            raise RuntimeError(active.run_id)
        if incident.status not in {"new", "human_needed"}:
            raise ValueError("incident status must be new or human_needed")
        # human_needed 是人工闸口：没有显式 force 就不该被重复触发，
        # 否则会把用户尚未确认的诊断结果直接覆盖掉。
        if incident.status == "human_needed" and not force:
            raise ValueError(
                "incident is awaiting human confirmation; "
                "pass force=true to re-diagnose"
            )
        run_id = f"run_{uuid.uuid4().hex[:20]}"
        now = model_now()
        await DiagnosisRun.create(run_id=run_id, incident_id=incident_id, status="queued", heartbeat_at=now, triggered_by=triggered_by)
        await Incident.filter(id=incident_id).update(status="diagnosing")
        task = asyncio.create_task(_worker(incident_id, run_id))
        _tasks[run_id] = task
    await _publish("incident.queued", {"incident_id": incident_id, "run_id": run_id, "queue_position": max(0, len(_tasks) - settings.diagnosis_max_concurrent_runs)})
    return {"incident_id": incident_id, "status": "diagnosing", "run_id": run_id}


async def schedule_auto_diagnosis(incident_id: int, delay: float | None = None) -> None:
    """防抖调度：静默 delay 秒后再自动诊断，突发告警期间不断重置。

    这样既实现 F-02→F-03 自动贯通，又保留 F-02 的聚类窗口——否则诊断会在毫秒级
    把事件推到 resolved，使其脱离 OPEN_STATES，导致后续同类告警各自新建事件。
    """
    if not settings.auto_diagnose_on_cluster:
        return
    wait = float(settings.diagnosis_debounce_seconds if delay is None else delay)
    generation = _auto_generation.get(incident_id, 0) + 1
    _auto_generation[incident_id] = generation

    async def _fire() -> None:
        try:
            await asyncio.sleep(wait)
        except asyncio.CancelledError:
            return
        if _auto_generation.get(incident_id) != generation:
            return  # 新一轮调度已接管；由它负责触发
        try:
            await start_diagnosis(incident_id, triggered_by=0)
            log_event(20, "incident.auto_diagnose_triggered", incident_id=incident_id)
        except (RuntimeError, ValueError, LookupError) as exc:
            log_event(30, "incident.auto_diagnose_skipped", incident_id=incident_id, reason=str(exc))
        finally:
            # 只清理自己这一代；若期间已被新一轮取代，不能动新任务/新代数
            if _auto_generation.get(incident_id) == generation:
                _auto_generation.pop(incident_id, None)
                _pending_auto.pop(incident_id, None)

    task = asyncio.create_task(_fire())
    _pending_auto[incident_id] = task
    _auto_tasks.add(task)
    task.add_done_callback(_auto_tasks.discard)


async def run_recap(
    incident_id: int,
    run_id: str,
    state: dict | None = None,
    actual_cause: str | None = None,
    correct_actions: str | None = None,
    confirmed_by: int | None = None,
) -> dict | None:
    """复盘并回灌（F-07 收口动作，两条触发路径统一入口）。

    自动收口（_run_graph 直达 resolved）与确认收口（confirm_incident 最后一个
    pending high 被 approve）都汇聚到这里：生成四段式病历，过通用性门槛后
    复用 F-05 ingest_document(source_type="incident") 写回知识库。回灌失败
    不抛出（resolved 不因此翻转，与 _worker P3 修复同构），见 _maybe_ingest。
    """
    incident = await Incident.get(id=incident_id)
    if state is None:
        actions = await RemediationAction.filter(incident_id=incident_id).order_by("id")
        alerts = await Alert.filter(incident_id=incident_id).order_by("created_at", "id").limit(3)
        state = {"run_id": run_id, "incident_id": incident_id, "title": incident.title, "root_cause": incident.root_cause, "root_cause_confidence": incident.root_cause_confidence or 0.0, "root_cause_source": incident.root_cause_source, "actual_cause": actual_cause or incident.actual_cause, "correct_actions": correct_actions, "component": incident.component, "actions": [{"command": row.command, "risk_level": row.risk_level, "description": row.description, "status": row.status} for row in actions], "alerts": [{"source": row.source, "component": row.component, "severity": row.severity, "title": row.title, "detail": row.detail} for row in alerts], "retry_count": 0, "brief": ""}
    elif actual_cause or correct_actions:
        state = {**state, "actual_cause": actual_cause or state.get("actual_cause"), "correct_actions": correct_actions or state.get("correct_actions")}
    started = time.monotonic()
    result = await recap.run(state)
    injected = await _maybe_ingest(state, result, confirmed_by)
    if injected is not None:
        result["knowledge_injected"] = injected
    await persist_step(state, "recap", "RecapAgent", result, started)
    return result


async def _maybe_ingest(state: dict, result: dict, confirmed_by: int | None) -> dict | None:
    """F-07 回灌：门槛过滤 -> 复用 ingest 管线；任何失败不拖垮确认流。

    返回 None 表示未回灌（门槛跳过 / 回灌失败），调用方据此不注入
    knowledge_injected 字段；事件状态与 confirm 主流程不受影响。

    门槛（V1.2 多信号，替代原 generality 假分）：
      1. ingest_ready=False（无根因）-> 一律拒绝，没有内容可沉淀。
      2. 有人工修正（actual_cause 非空）-> 放行（最高价值样本）。
      3. 其余情况（含"人只点了确认"）-> 需 confidence >= RECAP_INGEST_MIN_CONFIDENCE。
    """
    confidence = state.get("root_cause_confidence") or 0.0
    actual_cause = (state.get("actual_cause") or "").strip()
    has_human_correction = bool(actual_cause) and RECAP_INGEST_TRUST_HUMAN

    if not result.get("ingest_ready"):
        log_event(30, "knowledge.feedback_skipped", incident_id=state["incident_id"], run_id=state.get("run_id"), reason="no_root_cause", confidence=confidence, has_human_correction=has_human_correction)
        return None
    if not has_human_correction and confidence < RECAP_INGEST_MIN_CONFIDENCE:
        log_event(30, "knowledge.feedback_skipped", incident_id=state["incident_id"], run_id=state.get("run_id"), reason="low_confidence", confidence=confidence, has_human_correction=False)
        return None
    if has_human_correction and confidence < RECAP_INGEST_MIN_CONFIDENCE:
        # 低置信度但有人工背书：放行并留痕，便于事后统计这类样本占比。
        log_event(30, "knowledge.feedback_ingest_by_human", incident_id=state["incident_id"], run_id=state.get("run_id"), confidence=confidence)
    title, content = result["case_title"], result["recap"]
    # 与 ingest_document 内部同口径（sanitize 后 SHA1(title|content)）的幂等预检，
    # 仅用于给响应打 idempotent 标记；入库判定仍以 ingest 事务内双检为准。
    digest = hashlib.sha1(f"{sanitize(title)}|{sanitize(content)}".encode()).hexdigest()
    idempotent = await KnowledgeDoc.filter(title=title, content_hash=digest).exists()
    try:
        injected = await ingest_document(title=title, content=content, source_type="incident", component=state.get("component") or None, created_by=confirmed_by)
    except Exception as exc:  # noqa: BLE001 —— 回灌失败不翻转 resolved（P3 同构）
        log_event(40, "knowledge.feedback_failed", incident_id=state["incident_id"], run_id=state.get("run_id"), title=title, error=str(exc))
        await _publish("incident.recap_failed", {"incident_id": state["incident_id"], "run_id": state.get("run_id"), "error": str(exc)})
        return None
    return {**injected, "title": title, "idempotent": idempotent}


async def _collect_feedback(
    incident, path: str, user_id: int, actual_cause: str | None, correct_actions: str | None
) -> None:
    """统一采集人工反馈：actual_cause → 校准样本（F-04 §5.5），correct_actions → F-07 素材。

    V1.1 R8：`reject` 与 `approve`、`close`、`feedback` 四条路径**都要采集**——
    reject 意味着"AI 判错"，是价值最高的校准样本，V1.0 只在 approve 采集会丢样本。
    采集失败不影响主流程（记日志后继续）。
    """
    if actual_cause:
        try:
            await calibration.collect_sample(
                incident.id, incident.root_cause_confidence,
                incident.root_cause, actual_cause,
            )
        except Exception as exc:  # noqa: BLE001
            log_event(30, "calibration.collect_failed", incident_id=incident.id, error=str(exc))
    # F-07 尚未实现：correct_actions 先随复盘落库（run_recap），此处仅留痕
    log_event(30, "feedback.collected", incident_id=incident.id, path=path, by=user_id,
              has_actual_cause=bool(actual_cause), has_correct_actions=bool(correct_actions))


async def confirm_incident(incident_id: int, action_id: int, decision: str, user_id: int, actual_cause: str | None, correct_actions: str | None) -> dict:
    """F-06 §5.4 处置建议状态机（V1.1 重写）。

    与 V1.0 的差别：
    - 操作单元从"事件必须是 human_needed"改为"action 必须是 pending"（med/low 已自动 approved）；
    - reject 不再直接关闭事件（V1.0 会让剩余 pending 卡死），而是按 pending/approved 余额自动流转；
    - reject 路径同样采集 actual_cause 校准样本（R8）。
    """
    async with locks.confirm_lock(incident_id):
        incident = await Incident.get_or_none(id=incident_id)
        action = await RemediationAction.get_or_none(id=action_id, incident_id=incident_id)
        if incident is None or action is None:
            raise LookupError("incident or action not found")
        if action.status != "pending":
            raise ValueError("action is not pending")
        action_status = "skipped" if decision == "reject" else "approved"
        await RemediationAction.filter(id=action_id).update(status=action_status, confirmed_by=user_id)
        if actual_cause:
            await Incident.filter(id=incident_id).update(actual_cause=actual_cause)
        await _collect_feedback(incident, decision, user_id, actual_cause, correct_actions)
        log_event(30, "action.confirmed", incident_id=incident_id, action_id=action_id, decision=decision, by=user_id)

        pending = await RemediationAction.filter(incident_id=incident_id, status="pending").exists()
        approved = await RemediationAction.filter(incident_id=incident_id, status="approved").exists()
        status = incident.status
        if not pending and approved:
            # 优先续跑：图还停在人工闸口时，Ticket/收口/Recap 由图完成，
            # 本函数不再自己算一遍结局 —— 两处各算一次正是迁移前"结局逻辑重复"的病根。
            paused = await DiagnosisRun.filter(
                incident_id=incident_id, status="paused"
            ).order_by("-heartbeat_at").first()
            if paused is not None:
                values = await resume_diagnosis(paused.run_id, {
                    "action_id": action_id, "decision": decision,
                    "confirmed_by": user_id,
                    "actual_cause": actual_cause or incident.actual_cause,
                    "correct_actions": correct_actions,
                })
                await metrics.recompute_action_stats()
                fresh = await Incident.get_or_none(id=incident_id)
                return {
                    "incident_id": incident_id,
                    "status": fresh.status if fresh else status,
                    "action_status": action_status,
                    "recap_triggered": bool(values.get("recap")),
                    "knowledge_injected": values.get("knowledge_injected"),
                }
            await Incident.filter(id=incident_id).update(status="resolved", resolved_at=model_now(), confirmed_by=user_id, actual_cause=actual_cause or incident.actual_cause)
            status = "resolved"
            await metrics.recompute_open_incidents()
            last_run = await DiagnosisRun.filter(incident_id=incident_id).order_by("-heartbeat_at").first()
            recap_result = await run_recap(
                incident_id,
                last_run.run_id if last_run else f"run_confirm_{uuid.uuid4().hex[:16]}",
                actual_cause=actual_cause, correct_actions=correct_actions,
                confirmed_by=user_id,
            )
            await _publish(
                "incident.diagnosed",
                {
                    "incident_id": incident_id, "root_cause": incident.root_cause,
                    "confidence": incident.root_cause_confidence,
                    "source": incident.root_cause_source,
                },
            )
            await metrics.recompute_action_stats()
            return {"incident_id": incident_id, "status": status, "action_status": action_status, "recap_triggered": True, "knowledge_injected": (recap_result or {}).get("knowledge_injected")}
        if not pending and not approved:
            # 全部 reject：等人工显式 close 或重诊断，不再自动关闭（V1.1）
            await Incident.filter(id=incident_id).update(status="human_needed")
            status = "human_needed"
            await metrics.recompute_open_incidents()
        await metrics.recompute_action_stats()
        return {"incident_id": incident_id, "status": status, "action_status": action_status, "recap_triggered": False, "knowledge_injected": None}


async def close_incident(incident_id: int, user_id: int, reason: str | None, actual_cause: str | None, correct_actions: str | None) -> dict:
    """F-06 §3.5 显式关闭事件（V1.1 新增）。

    存在 pending action 时拒绝关闭（409）——否则高危命令会被"关事件"悄悄绕过确认。
    关闭不触发 Recap（无有效复盘知识，与 F-03 V1.1 口径一致）。
    """
    async with locks.confirm_lock(incident_id):
        incident = await Incident.get_or_none(id=incident_id)
        if incident is None:
            raise LookupError("incident not found")
        if await RemediationAction.filter(incident_id=incident_id, status="pending").exists():
            raise ValueError("incident has pending actions")
        # 审查 #6：存在排队/运行中的诊断时拒绝关闭——否则诊断完成路径会把
        # closed 事件改写回 human_needed/resolved（复活竞态）。
        active_run = await DiagnosisRun.filter(
            incident_id=incident_id, status__in=("queued", "running")
        ).exists()
        if active_run:
            raise ValueError("incident has an active diagnosis run")
        values: dict = {"status": "closed", "confirmed_by": user_id}
        if actual_cause:
            values["actual_cause"] = actual_cause
        await Incident.filter(id=incident_id).update(**values)
        await _collect_feedback(incident, "close", user_id, actual_cause, correct_actions)
        await metrics.recompute_open_incidents()
        await metrics.recompute_action_stats()
        log_event(30, "incident.closed", incident_id=incident_id, reason=reason or "", by=user_id)
        return {"incident_id": incident_id, "status": "closed", "reason": reason, "recap_triggered": False}


async def feedback_incident(incident_id: int, user_id: int, actual_cause: str | None, correct_actions: str | None) -> dict:
    """F-06 §5.5 resolved 态补录：只补 actual_cause/correct_actions。

    不改变事件状态、不 reopen action（med/low 本就自动 approved，无需补确认——
    V1.0 的"扩展 confirm 支持 resolved 补确认"方向错误，V1.1 改为此端点）。
    """
    async with locks.confirm_lock(incident_id):
        incident = await Incident.get_or_none(id=incident_id)
        if incident is None:
            raise LookupError("incident not found")
        updated = False
        if actual_cause:
            await Incident.filter(id=incident_id).update(actual_cause=actual_cause)
            updated = True
        await _collect_feedback(incident, "feedback", user_id, actual_cause, correct_actions)
        return {"incident_id": incident_id, "status": incident.status, "action_status": None, "updated": updated, "recap_triggered": False}


async def _has_pending_checkpoint(run_id: str) -> bool:
    """检查点里是否还有未跑完的节点（`snapshot.next` 非空即图停在半路）。"""
    try:
        snapshot = await get_graph().aget_state(checkpoint.thread_config(run_id))
    except Exception as exc:  # noqa: BLE001 —— 读不到检查点就只能按"无断点"处理
        log_event(30, "incident.checkpoint_read_failed", run_id=run_id, error=str(exc))
        return False
    return bool(snapshot and snapshot.next)


async def recover_stale_runs() -> int:
    """启动时回收进程崩溃遗留的 diagnosing 运行。

    有断点的 run 从**最后完成的节点**续跑（迁移前只能整图重跑或直接判死）；
    没有断点的沿用旧口径：标记 interrupted 并转人工。
    """
    cutoff = model_now() - timedelta(seconds=settings.diagnosis_stale_seconds)
    stale = await DiagnosisRun.filter(status__in=("queued", "running"), heartbeat_at__lt=cutoff)
    resumed = 0
    for run in stale:
        if await _has_pending_checkpoint(run.run_id):
            resumed += 1
            _tasks[run.run_id] = asyncio.create_task(
                _execute(run.incident_id, run.run_id, mode="continue")
            )
            log_event(30, "incident.diagnosis_resumed", run_id=run.run_id, incident_id=run.incident_id)
            continue
        await DiagnosisRun.filter(run_id=run.run_id).update(status="interrupted", finished_at=model_now())
        await Incident.filter(id=run.incident_id, status="diagnosing").update(status="human_needed")
        await _publish("incident.human_needed", {"incident_id": run.incident_id, "reason": "interrupted", "actions": []})
    if resumed:
        log_event(30, "incident.stale_resumed", count=resumed)
    return len(stale)


async def recover_new_incidents() -> int:
    """P1 修复：补发防抖窗口内遗留的 new 事件。

    F-02→F-03 自动诊断采用防抖（默认 30s）：事件创建后先进入 new，待静默窗口结束
    才自动诊断。若服务在 new 状态、防抖定时器触发前重启，shutdown() 会取消所有
    _pending_auto 定时器，而 recover_stale_runs() 只回收 diagnosing 的运行——这些
    new 事件没有任何 DiagnosisRun，将永久卡死、永不诊断。此处补发诊断。

    **两道闸**（原实现无条件拉起全部 new，会打爆模型额度）：
      1. 自动诊断关闭（auto_diagnose_on_cluster=False）时直接跳过 ——
         此时 new 就是"等人处理"的正常状态，启动钩子不该越权拉起；
      2. 只补发最近 RECOVER_WINDOW 内创建的事件。更老的是历史积压
         （例如长期关着自动诊断），一次性全量诊断既慢又烧钱，应人工决定。
    """
    if not settings.auto_diagnose_on_cluster:
        return 0

    cutoff = model_now() - timedelta(seconds=RECOVER_WINDOW_SECONDS)
    stuck = await Incident.filter(status="new", created_at__gte=cutoff)
    count = 0
    for incident in stuck:
        try:
            await start_diagnosis(incident.id, triggered_by=0)
            count += 1
        except (RuntimeError, ValueError, LookupError) as exc:
            log_event(30, "incident.new_recover_skipped", incident_id=incident.id, reason=str(exc))
    if count:
        log_event(30, "incident.new_recovered", count=count)
    return count


async def startup() -> None:
    await checkpoint.startup()
    recovered = await recover_stale_runs()
    if recovered:
        log_event(30, "incident.diagnosis_recovered", count=recovered)
    # P1 修复：补发防抖窗口内残留的 new 事件，避免永久卡死。
    await recover_new_incidents()
    # P2 修复：启动后重算开放事件数，校正重启间的指标漂移。
    await metrics.recompute_open_incidents()


async def shutdown() -> None:
    # 取消全部在飞的防抖任务（不只是每个事件的最新一轮），否则被取代的
    # 旧任务仍可能在停机后醒来触发一次诊断。
    for task in list(_auto_tasks):
        task.cancel()
    _auto_tasks.clear()
    _pending_auto.clear()
    _auto_generation.clear()
    for task in list(_tasks.values()):
        task.cancel()
    if _tasks:
        await asyncio.gather(*_tasks.values(), return_exceptions=True)
    _tasks.clear()
    # 池子必须最后关：上面 gather 期间还可能有节点在写检查点。
    await checkpoint.shutdown()
