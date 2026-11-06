"""ContextAgent：收集局部指标、已结历史案例和可用知识片段。

F-04 接入点：回环时消费 `state["loop_retrieval"]`（由 confidence_loop 写入的
补查参数：改写后 query / top_k / sim_floor / component_scope），使
`kb_chunks` 真实反映本次补查结果——这也是下一轮回环 `kb_hits_before` 的来源
（§7.2：取 `len(state.kb_chunks)`，不重复检索）。
"""

from __future__ import annotations

from app.models import Incident
from .loop_strategies import search_knowledge


async def run(state: dict) -> dict:
    retry = state["retry_count"]
    retrieval = state.get("loop_retrieval")
    if retrieval:
        query = retrieval["query"]
        top_k = retrieval["recall_top_k"]
        sim_floor = retrieval["sim_floor"]
        scope = retrieval["component_scope"]
    else:
        query = state.get("brief") or state.get("title") or ""
        top_k = state.get("top_k_base", 5)
        sim_floor = state.get("sim_floor_base", 0.7)
        scope = state["component"]

    limit = 10 if retry else 5
    cases = await Incident.filter(
        component=state["component"],
        status__in=("resolved", "closed"),
        root_cause__not_isnull=True,
    ).order_by("-created_at").limit(limit)
    similar_cases = [
        {
            "id": item.id,
            "title": item.title,
            "root_cause": item.actual_cause or item.root_cause,
            "created_at": item.created_at.isoformat(),
        }
        for item in cases
    ]
    kb_chunks = await search_knowledge(query, top_k, sim_floor, scope)
    context = {
        "metrics_window_minutes": 30,
        "component": state["component"],
        "alert_count": len(state["alerts"]),
        "query_mode": "expanded" if retry else "standard",
        # F-04 追溯字段：本轮实际使用的检索参数
        "retrieval": {
            "query": query, "top_k": top_k, "sim_floor": sim_floor, "component_scope": scope,
        },
    }
    return {
        "context": context,
        "similar_cases": similar_cases,
        "kb_chunks": kb_chunks,
        "tool_calls": [
            {"tool": "get_recent_metrics", "args": {"component": state["component"], "minutes": 30}, "result": "metrics snapshot unavailable in MVP", "latency_ms": 0},
            {"tool": "list_similar", "args": {"component": state["component"], "top_k": limit}, "result": f"{len(similar_cases)} cases", "latency_ms": 0},
            {"tool": "search_kb", "args": {"query": query, "top_k": top_k, "sim_floor": sim_floor, "component": scope}, "result": f"{len(kb_chunks)} hits", "latency_ms": 0},
        ],
    }
