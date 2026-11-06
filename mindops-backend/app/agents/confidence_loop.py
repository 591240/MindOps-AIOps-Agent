"""F-04 置信度回环控制器（§3.4 伪契约 / §5.6 边函数 / §7.2 / §7.3）。

职责边界（§1.3）：本模块只"判断要不要回退"和"怎么补查"，不产出根因。

关键不变量：
- `retry_count += 1` **只发生在 Diagnose→Context 这条回环边上**（§5.6 铁律），
  保证幂等、杜绝死循环；
- 阈值来自 run 启动时的 **per-run 快照**（`state["conf_threshold"]`），不是全局实时值；
- `conf_after` 两阶段落库：检索时落 NULL，Diagnose 产出后回填（§7.3）。
"""

from __future__ import annotations

from app.alerts import sse
from app.models import LoopAudit
from app.observability import metrics
from app.observability.logging import log_event
from . import loop_strategies, model_router
from .loop_strategies import GLOBAL_SCOPE

STRATEGY_SEP = ","

REMEDIATE = "remediate"
CONTEXT = "context"
HUMAN_NEEDED = "human_needed"


def _strategy_text(strategy: list[str]) -> str:
    return STRATEGY_SEP.join(strategy)


async def maybe_loop(state: dict, llm_rewriter=None) -> str:
    """Diagnose 之后的条件出口（§3.4）：`remediate` / `context` / `human_needed`。

    判定完全基于 per-run 快照，因此 run 进行中的调参不影响本次判定。
    """
    conf = float(state.get("root_cause_confidence") or 0.0)
    threshold = float(state["conf_threshold"])
    max_retry = int(state["max_retry"])

    if conf >= threshold:
        return REMEDIATE

    if int(state["retry_count"]) >= max_retry:
        state["needs_human"] = True
        state["human_reason"] = "low_confidence"
        state["loop_summary"] = await build_loop_summary(state)
        metrics.loop_to_human_total.inc()
        log_event(
            30, "incident.loop_exhausted", run_id=state.get("run_id"),
            incident_id=state["incident_id"], retry_count=state["retry_count"],
            conf=conf, threshold=threshold, human_reason="low_confidence",
        )
        return HUMAN_NEEDED

    # —— 回环边副作用：唯一 +1 处（§5.6 铁律）——
    state["retry_count"] += 1
    retry_index = int(state["retry_count"])

    # 模型升级定点（§5.3）：retry_count == 1（第二次进 Diagnose）切 reasoning
    requested_role = (
        "reasoning" if (retry_index == 1 and state.get("reasoning_escalation")) else "chat"
    )
    model_id, role, escalated, skip_reason = await model_router.pick_model(requested_role)

    strategy = loop_strategies.build_strategy(
        retry_index, state.get("cross_component_fallback", True)
    )
    audit = await run_retrieval(
        state,
        role=role,
        strategy=strategy,
        model_id=model_id,
        escalated=escalated,
        skip_reason=skip_reason,
        llm_rewriter=llm_rewriter,
    )
    await persist_loop_audit(audit)

    # 回环产出交给 ContextAgent 消费（下一轮 context.run 直接用这些参数检索）
    state["loop_retrieval"] = {
        "query": audit["query_after"],
        "recall_top_k": audit["recall_top_k"],
        "sim_floor": audit["sim_floor"],
        "component_scope": audit["component_scope"],
    }
    state["model_role"] = role
    state["model_id"] = model_id
    return CONTEXT


async def run_retrieval(
    state: dict,
    role: str,
    strategy: list[str],
    model_id: int | None = None,
    escalated: bool = False,
    skip_reason: str | None = None,
    llm_rewriter=None,
) -> dict:
    """执行一次补查检索（§7.2）。

    V1.1 修订：`kb_hits_before` 直接取上一轮 `len(state.kb_chunks)`，
    全程**只检索一次**（删除 V1.0 的双检索浪费）。
    """
    hits_before = len(state.get("kb_chunks") or [])
    brief = state.get("brief") or state.get("title") or ""

    query = brief
    top_k = state.get("top_k_base", 5)
    sim_floor = state.get("sim_floor_base", 0.7)
    scope = state.get("component")

    # ① 查询改写（第 1 次回环）
    if "rewrite" in strategy:
        query = await loop_strategies.rewrite_query(
            brief, state.get("missing_info") or [], llm_rewriter
        )
    # ② 扩大召回（第 1、2 次回环）
    if "expand_recall" in strategy:
        top_k = state.get("top_k_loop", 10)
        sim_floor = state.get("sim_floor_loop", 0.5)
    # ③ 跨组件检索（第 2 次回环兜底）
    if "cross_component" in strategy:
        scope = GLOBAL_SCOPE

    hits = await loop_strategies.search_knowledge(query, top_k, sim_floor, scope)
    return {
        "run_id": state["run_id"],
        "incident_id": state["incident_id"],
        "retry_index": int(state["retry_count"]),
        "strategy": _strategy_text(strategy),
        "query_before": brief,
        "query_after": query,
        "recall_top_k": top_k,
        "sim_floor": sim_floor,
        "component_scope": scope,
        "kb_hits_before": hits_before,
        "kb_hits_after": len(hits),
        "model_id": model_id,
        "role": role,
        "conf_before": state.get("root_cause_confidence"),
        "conf_after": None,
        "escalated": bool(escalated),
        "skip_reason": skip_reason,
        "hits": hits,
    }


def loop_event_payload(audit: dict) -> dict:
    """§3.3 `incident.loop` SSE 载荷（字段统一用 `retry_index`）。"""
    return {
        "incident_id": audit["incident_id"],
        "run_id": audit["run_id"],
        "retry_index": audit["retry_index"],
        "strategy": audit["strategy"],
        "query_before": audit["query_before"],
        "query_after": audit["query_after"],
        "recall_top_k": audit["recall_top_k"],
        "sim_floor": audit["sim_floor"],
        "component_scope": audit["component_scope"],
        "kb_hits_before": audit["kb_hits_before"],
        "kb_hits_after": audit["kb_hits_after"],
        "model_id": audit["model_id"],
        "role": audit["role"],
        "conf_before": audit["conf_before"],
        "conf_after": audit["conf_after"],
    }


async def persist_loop_audit(audit: dict) -> LoopAudit:
    """两阶段落库第一阶段（§7.3）：落行（`conf_after=NULL`）→ 发 SSE → 打指标。"""
    row = await LoopAudit.create(
        run_id=audit["run_id"],
        incident_id=audit["incident_id"],
        retry_index=audit["retry_index"],
        strategy=audit["strategy"],
        query_before=audit["query_before"],
        query_after=audit["query_after"],
        recall_top_k=audit["recall_top_k"],
        sim_floor=audit["sim_floor"],
        component_scope=audit["component_scope"],
        kb_hits_before=audit["kb_hits_before"],
        kb_hits_after=audit["kb_hits_after"],
        model_id=audit["model_id"],
        role=audit["role"],
        conf_before=audit["conf_before"],
        conf_after=None,
        escalated=bool(audit["escalated"]),
    )
    metrics.loop_total.inc()
    if audit["escalated"]:
        metrics.loop_escalation_total.inc()

    payload = loop_event_payload(audit)
    payload["escalated"] = bool(audit["escalated"])
    await sse.publish("incident.loop", payload)
    log_event(
        20, "incident.loop", run_id=audit["run_id"], incident_id=audit["incident_id"],
        retry_index=audit["retry_index"], strategy=audit["strategy"], role=audit["role"],
        model_id=audit["model_id"], kb_hits_before=audit["kb_hits_before"],
        kb_hits_after=audit["kb_hits_after"], conf_before=audit["conf_before"],
        conf_after=None, escalated=bool(audit["escalated"]),
        skip_reason=audit.get("skip_reason"),
    )
    return row


async def backfill_conf_after(run_id: str, retry_index: int, conf_after: float | None) -> None:
    """两阶段落库第二阶段（§7.3）：Diagnose 产出后回填 `conf_after`。"""
    row = await LoopAudit.filter(run_id=run_id, retry_index=retry_index).first()
    if row is None:
        return
    await LoopAudit.filter(run_id=run_id, retry_index=retry_index).update(conf_after=conf_after)
    if row.conf_before is not None and conf_after is not None:
        metrics.loop_conf_delta.observe(float(conf_after) - float(row.conf_before))
    log_event(
        20, "incident.loop_conf_after", run_id=run_id, retry_index=retry_index,
        conf_before=row.conf_before, conf_after=conf_after,
    )


async def build_loop_summary(state: dict) -> dict:
    """§3.3：`human_needed(reason='low_confidence')` 附带的回环摘要。"""
    rows = await LoopAudit.filter(
        run_id=state.get("run_id"), incident_id=state["incident_id"]
    ).order_by("retry_index")
    return {
        "retry_count": int(state.get("retry_count", 0)),
        "audits": [
            {
                "retry_index": item.retry_index,
                "strategy": item.strategy,
                "kb_hits_before": item.kb_hits_before,
                "kb_hits_after": item.kb_hits_after,
                "conf_before": item.conf_before,
                "conf_after": item.conf_after,
            }
            for item in rows
        ],
    }
