"""F-04 递进式补查策略（§5.2）与知识检索（§7.2 / §7.4）。

三种策略按**递进式**使用（V1.1 修订，替代 V1.0"第 1 次全做"的自相矛盾表述）：

- 第 1 次回环：① 查询改写 + ② 扩大召回
- 第 2 次回环：② 扩大召回（范围再扩）+ ③ 跨组件检索

知识检索当前由**已结事件**提供（F-03 §7.6 相似案例 + F-02 已结事件库）；
F-05 向量知识库接入后，`search_knowledge` 只需在其内部追加一路召回即可，
对调用方（回环控制器 / ContextAgent）透明。
"""

from __future__ import annotations

from app.incidents.tokenizer import tokenize
from app.models import Incident
from app.observability.logging import log_event

GLOBAL_SCOPE = "__global__"


def _is_global_scope(component_scope: str | None) -> bool:
    """None/empty scope is the same unrestricted scope as ``__global__``."""
    return not component_scope or component_scope == GLOBAL_SCOPE

# ---- ① 查询改写：现象 → 排错模板（§7.4 规则模板优先，零 LLM 调用、可解释）----
REWRITE_RULES: tuple[tuple[tuple[str, ...], str], ...] = (
    (("内存高", "内存", "oom", "memory"), "OOM 排查 memory leak"),
    (("超时", "timeout", "slow", "缓慢"), "timeout slow query 排查"),
    (("连接", "connection", "连接池"), "connection pool 容量排查"),
    (("磁盘", "disk", "space", "空间"), "disk space 写满排查"),
    (("cpu", "负载", "load"), "cpu load 排查"),
    (("错误率", "error", "5xx", "失败率"), "error rate 排查"),
    (("重启", "restart", "crash", "崩溃"), "crash loop 排查"),
)


def build_strategy(retry_index: int, cross_component_fallback: bool = True) -> list[str]:
    """按回环次序返回策略组合（§5.2 递进式）。

    第 1 次 → `["rewrite", "expand_recall"]`
    第 2 次 → `["expand_recall", "cross_component"]`（关闭跨组件时退化为仅扩大召回）
    """
    if retry_index <= 1:
        return ["rewrite", "expand_recall"]
    if cross_component_fallback:
        return ["expand_recall", "cross_component"]
    return ["expand_recall"]


def rewrite_with_rules(brief: str) -> str | None:
    """规则模板改写：命中则返回追加同义项后的 query，未命中返回 None。"""
    text = (brief or "").strip()
    if not text:
        return None
    lowered = text.lower()
    additions = [
        suffix for keywords, suffix in REWRITE_RULES if any(word in lowered for word in keywords)
    ]
    if not additions:
        return None
    return f"{text} {' '.join(additions)}".strip()


async def rewrite_query(
    brief: str,
    missing_info: list[str] | None = None,
    llm_rewriter=None,
) -> str:
    """① 查询改写（§7.4）：规则模板优先，LLM 兜底。

    - 规则命中 → 直接返回（零成本、可解释）
    - 规则未命中 → 若注入了 `llm_rewriter` 则用其生成改写，结果经
      `_sanitize` 白名单过滤；未注入或调用失败 → 原样返回，不引入噪声。
    """
    text = (brief or "").strip()
    ruled = rewrite_with_rules(text)
    if ruled is not None:
        return ruled
    if llm_rewriter is not None:
        try:
            candidate = await llm_rewriter(text, missing_info or [])
        except Exception as exc:  # noqa: BLE001 - 兜底路径，改写失败不应中断回环
            log_event(30, "loop.rewrite_llm_failed", error=str(exc))
            return text
        return _sanitize(candidate) or text
    return text


def _sanitize(candidate: str) -> str:
    """LLM 改写兜底的白名单过滤（§10.1 噪声缓解）：限长 50 字、禁用危险词。"""
    if not candidate:
        return ""
    text = " ".join(str(candidate).split())
    forbidden = ("rm -rf", "drop ", "delete ", "shutdown", ";", "--")
    lowered = text.lower()
    if any(word in lowered for word in forbidden):
        return ""
    return text[:50]


def _jaccard(a: str, b: str) -> float:
    """纯词面重合度（不引入 severity 偏置，保证 sim_floor 语义干净）。"""
    left, right = tokenize(a or ""), tokenize(b or "")
    if not left or not right:
        return 1.0 if left == right else 0.0
    return len(left & right) / len(left | right)


async def _legacy_incident_recall(
    query: str, top_k: int, sim_floor: float, component_scope: str | None
) -> list[dict]:
    qs = Incident.filter(status__in=("resolved", "closed"), root_cause__not_isnull=True)
    if component_scope and component_scope != GLOBAL_SCOPE:
        qs = qs.filter(component=component_scope)
    rows = await qs.order_by("-created_at").limit(200)
    hits: list[dict] = []
    for row in rows:
        cause = row.actual_cause or row.root_cause or ""
        score = _jaccard(query, f"{row.title} {cause}")
        if score < sim_floor:
            continue
        hits.append(
            {
                "chunk_id": row.id,
                "quote": cause[:500],
                "content": f"{row.title}：{cause}"[:500],
                "source_doc": f"incident-{row.id}",
                "score": round(score, 4),
                "component": row.component,
                "global_scope": _is_global_scope(component_scope),
            }
        )
    hits.sort(key=lambda item: (-item["score"], item["chunk_id"]))
    return hits[:top_k]


async def search_knowledge(
    query: str,
    top_k: int,
    sim_floor: float,
    component_scope: str | None,
) -> list[dict]:
    """②/③ 知识检索：按 query 召回已结且有根因的历史事件。

    - `component_scope == "__global__"`（③ 跨组件）→ 不限组件，召回通用运维经验
    - 否则限定本组件
    - `sim_floor` 为相似度门槛，`top_k` 为召回上限

    返回结构与 F-03 `_persist_evidence` 兼容（`quote` / `source_doc` / `score`）。
    """
    try:
        from app.rag.retriever import retrieve
        result = await retrieve(query, component_scope, top_k, sim_floor)
        hits = [
            {
                "chunk_id": item.get("chunk_id"),
                "quote": item.get("text") or item.get("quote") or "",
                "content": item.get("text") or item.get("content") or "",
                "source_doc": item.get("source_doc") or "knowledge-base",
                # V1.1 B1：retrieve 已按「参与路数」归一化到 [0,1]，此处**不可再除
                # 2/61**——v1.0 的双重归一化会把分数放大约 30 倍，F-06 落 evidence
                # 时前端会显示异常分值。Legacy 路该项本就是 None（量纲不可比）。
                "score": item.get("score"),
                "component": item.get("component"),
                "global_scope": item.get("global_scope", _is_global_scope(component_scope)),
            }
            for item in result.hits
        ]
        # V1.1 B5：Legacy（已结事件）已由 retrieve 作为常驻第三路纳入融合，
        # 此处不再二次调用 _legacy_incident_recall 混排。v1.0 的双重兜底会把
        # Jaccard 分与归一化 RRF 分混合排序（量纲不可比），且重复召回同一事件。
        return hits[:top_k]
    except Exception as exc:
        log_event(30, "knowledge.retrieve_failed", error=str(exc))
    # 最终兜底：仅当 retrieve 自身抛错（按契约不应发生）时退回已结事件召回
    try:
        return await _legacy_incident_recall(query, top_k, sim_floor, component_scope)
    except Exception as exc:
        log_event(30, "knowledge.retrieve_failed", error=str(exc))
        return []
