"""Dense + sparse RRF 检索及永不抛错的降级链。"""
from __future__ import annotations
import asyncio
from dataclasses import dataclass
from time import monotonic
from app.models import Incident
from app.observability import metrics
from app.observability.logging import log_event
from . import bm25, embedder, milvus_client, rrf

@dataclass
class RetrievalResult:
    hits: list[dict]
    fusion: str
    degraded: bool
    latency_ms: int


def _is_global_scope(component: str | None) -> bool:
    return not component or component == "__global__"


# 各路候选池大小。v1.0 用 20，但融合后 top_k 上限也是 20，
# 可选面过窄；V1.1 D8 放宽至 50。
CANDIDATE_POOL = 50


def _public(item: dict) -> dict:
    """移除仅用于内部排序的字段，返回可对外暴露的命中结构。"""
    payload = dict(item)
    payload.pop("_jaccard", None)
    return payload

def _score_legacy_rows(
    query: str, component: str | None, rows: list, floor: float, top_k: int
) -> list[dict]:
    """Legacy 路的纯计算部分（不碰 DB，可安全丢进线程池）。

    为什么要单独拆出来：`_jaccard` 内部走 jieba 分词，是同步 CPU 计算。
    直接在协程里跑 200 条会把事件循环整段卡住——同期所有 SSE 推送、
    其它请求都停摆。用 asyncio.to_thread 把它挪出循环。
    """
    from app.agents.loop_strategies import _jaccard

    hits = []
    for row in rows:
        cause = row.actual_cause or row.root_cause or ""
        score = _jaccard(query, f"{row.title} {cause}")
        if score >= floor:
            hits.append({
                "chunk_id": None,
                "quote": cause[:500],
                "content": f"{row.title}：{cause}"[:500],
                "source_doc": f"similar-incident:{row.id}",
                "score": None,
                "_jaccard": round(score, 4),
                "component": row.component,
                "global_scope": _is_global_scope(component),
            })
    return sorted(hits, key=lambda h: (-h["_jaccard"], h["source_doc"]))[:top_k]


async def _legacy(query: str, component: str | None, top_k: int, floor: float) -> list[dict]:
    """已结事件召回（Legacy 路）——**常驻第三路补充源，不是降级态**（V1.1 B5）。

    输出严格对齐 F-06 V1.1 §5.1 的相似案例证据约定：
    ``chunk_id=None``、``score=None``、``source_doc='similar-incident:{id}'``。
    Jaccard 分仅保存在内部键 ``_jaccard`` 中用于路内排序与门槛过滤，
    **不对外暴露**——它与 RRF 归一化分量纲不同，混排会造成虚假可比（B4）。
    """
    from app.agents.loop_strategies import GLOBAL_SCOPE

    qs = Incident.filter(status__in=("resolved", "closed"), root_cause__not_isnull=True)
    if component and component != GLOBAL_SCOPE:
        qs = qs.filter(component=component)
    rows = list(await qs.order_by("-created_at").limit(200))
    if not rows:
        return []
    return await asyncio.to_thread(_score_legacy_rows, query, component, rows, floor, top_k)

async def retrieve(query: str, component: str | None, top_k: int = 5, sim_floor: float = 0.0) -> RetrievalResult:
    start = monotonic()
    try:
        top_k = max(1, min(int(top_k), 20))
    except (TypeError, ValueError):
        top_k = 5
    try:
        sim_floor = max(0.0, min(float(sim_floor), 1.0))
    except (TypeError, ValueError):
        sim_floor = 0.0
    degraded = False
    reasons: list[str] = []

    # ① Legacy 路：已结事件召回。**常驻补充源，不是降级态**（V1.1 B5）——
    #    不依赖 Milvus 也不依赖知识库，任何档位都参与融合。
    try:
        legacy = await _legacy(query, component, top_k, sim_floor)
    except Exception as exc:  # noqa: BLE001 - retrieval must never break diagnosis
        legacy = []
        reasons.append("legacy")
        log_event(30, "knowledge.retrieve_degraded", reason="legacy", error=str(exc))
        metrics.knowledge_degraded_total.labels(reason="legacy").inc()

    # ② Sparse 路：BM25 内存倒排索引，**不依赖 Milvus**
    try:
        sparse = await bm25.search(query, component, CANDIDATE_POOL)
    except Exception as exc:  # noqa: BLE001
        sparse = []
        degraded = True
        reasons.append("bm25")
        log_event(30, "knowledge.retrieve_degraded", reason="bm25", error=str(exc))
        metrics.knowledge_degraded_total.labels(reason="bm25").inc()

    # ③ Dense 路：Milvus + Embedding。任一不可用仅让本路缺席，
    #    **不再牵连 Sparse 路**（v1.0 在此处整体退 LEGACY 是 B5 所述缺陷）。
    dense: list[dict] = []
    try:
        if milvus_client.engine_available():
            dense = await milvus_client.search(await embedder.embed_one(query), component, CANDIDATE_POOL)
        else:
            reasons.append("milvus_unconfigured")
    except Exception as exc:  # noqa: BLE001
        degraded = True
        reasons.append("embed_or_milvus")
        log_event(30, "knowledge.retrieve_degraded", reason="embed_or_milvus", query=query[:50], error=str(exc), latency_ms=int((monotonic() - start) * 1000))
        metrics.knowledge_degraded_total.labels(reason="embed_or_milvus").inc()

    # ④ 四档判定（F-05 V1.1 §5.5）
    if not dense and not sparse:
        # LEGACY_ONLY（仍有历史事件可召回）或 EMPTY（三路全空）
        fusion = "LEGACY_JACCARD" if legacy else "EMPTY"
        if not legacy:
            reasons.append("all_routes_empty")
            metrics.knowledge_degraded_total.labels(reason="all_routes_empty").inc()
            log_event(30, "knowledge.retrieve_degraded", reason="all_routes_empty", query=query[:50])
        hits = [_public(item) for item in legacy]
        latency = int((monotonic() - start) * 1000)
        metrics.knowledge_search_latency.observe(latency / 1000); metrics.knowledge_search_hits.observe(len(hits))
        log_event(20, "knowledge.search", query=query[:50], component=component, top_k=top_k, hits=len(hits), fusion_method=fusion, degraded=True, latency_ms=latency)
        return RetrievalResult(hits, fusion, True, latency)

    # FULL（Dense+BM25）或 NO_DENSE（仅 BM25）；Legacy 一并参与融合
    routes = [route for route in (dense, sparse, legacy) if route]
    names = [name for route, name in ((dense, "dense"), (sparse, "bm25"), (legacy, "legacy")) if route]
    fused = rrf.fuse(*routes, route_names=names)
    fusion = "RRF" if dense else "BM25_ONLY"
    if not dense:
        degraded = True
        metrics.knowledge_degraded_total.labels(reason="dense_absent").inc()

    hits = []
    for item in fused:
        payload = dict(item.payload)
        if payload.get("chunk_id") is None:
            # Legacy 路：Jaccard 分与 RRF 归一化分量纲不同，置 None 且**不参与
            # sim_floor 过滤**，否则会被归一化门槛全量误杀（V1.1 B4/B1 连带）
            payload["score"] = None
        else:
            normalized = rrf.normalize(item.score, len(routes))
            if normalized < sim_floor:
                continue
            payload["score"] = round(normalized, 6)
        payload.pop("_jaccard", None)  # 内部排序字段不对外暴露
        payload["global_scope"] = _is_global_scope(component)
        hits.append(payload)
    latency = int((monotonic() - start) * 1000)
    metrics.knowledge_search_latency.observe(latency / 1000); metrics.knowledge_search_hits.observe(len(hits))
    log_event(20, "knowledge.search", query=query[:50], component=component, top_k=top_k, hits=len(hits), fusion_method=fusion, degraded=degraded, latency_ms=latency)
    return RetrievalResult(hits[:top_k], fusion, degraded, latency)
