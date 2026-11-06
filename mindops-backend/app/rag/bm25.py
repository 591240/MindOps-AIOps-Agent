"""轻量 BM25 兼容稀疏检索（中文按 bigram，英文按词）。

索引按 `(库, 切片数, 最大 id)` 签名缓存：切片只追加/软删（is_latest 翻转），
任何入库、删除、版本切换都会改变签名，从而触发一次重建。原实现每次检索都
把整张表读出来并对每行跑两遍 jieba，事件循环被分词阻塞；现在分词与 df 统计
只在签名变化时做一次，且放到线程里执行。
"""
from __future__ import annotations

import asyncio
import math
import re
from dataclasses import dataclass, field

from tortoise.functions import Count, Max

from app.config import settings
from app.models import KnowledgeChunk, KnowledgeDoc

try:
    import jieba
except ImportError:  # pragma: no cover - minimal/offline deployments
    jieba = None


def tokenize(text: str) -> list[str]:
    """Tokenize English identifiers as words and Chinese text as bigrams.

    Keep boundaries deterministic across deployments even when ``jieba`` is
    installed in only some environments.
    """
    text = (text or "").lower()
    words = re.findall(r"[a-z0-9_/-]+|[\u4e00-\u9fff]+", text)
    tokens: list[str] = []
    for value in words:
        if re.fullmatch(r"[\u4e00-\u9fff]+", value):
            if jieba is not None:
                tokens.extend(token for token in jieba.lcut(value, HMM=False) if token.strip())
            elif len(value) == 1:
                tokens.append(value)
            else:
                tokens.extend(value[index:index + 2] for index in range(len(value) - 1))
        else:
            tokens.append(value)
    return tokens


@dataclass(frozen=True)
class _Index:
    signature: tuple[str, int, int]
    rows: list[KnowledgeChunk]
    tokenized: dict[int, list[str]] = field(repr=False)
    document_count: int
    avg_len: float
    df: dict[str, int] = field(repr=False)


_INDEX_CACHE: dict[str | None, _Index] = {}
_INDEX_LOCK = asyncio.Lock()


def _component_key(component: str | None) -> str | None:
    return component if component and component != "__global__" else None


def _signature(db: str, count: int, max_id: int) -> tuple[str, int, int]:
    return db, count, max_id


def _build_token_stats(rows: list[KnowledgeChunk]) -> tuple[dict[int, list[str]], dict[str, int], float]:
    tokenized: dict[int, list[str]] = {}
    df: dict[str, int] = {}
    total = 0
    for row in rows:
        tokens = tokenize(row.text)
        tokenized[row.id] = tokens
        total += len(tokens)
        for term in set(tokens):
            df[term] = df.get(term, 0) + 1
    avg_len = total / len(rows) if rows else 1.0
    return tokenized, df, avg_len


async def _load_index(component: str | None, signature: tuple[str, int, int]) -> _Index:
    queryset = KnowledgeChunk.filter(is_latest=True)
    if component:
        queryset = queryset.filter(component=component)
    rows = await queryset
    # 分词是 CPU 密集的，放到线程避免阻塞事件循环
    tokenized, df, avg_len = await asyncio.to_thread(_build_token_stats, rows)
    return _Index(
        signature=signature,
        rows=rows,
        tokenized=tokenized,
        document_count=len(rows),
        avg_len=avg_len,
        df=df,
    )


async def _get_index(component: str | None) -> _Index:
    queryset = KnowledgeChunk.filter(is_latest=True)
    if component:
        queryset = queryset.filter(component=component)
    agg = await queryset.aggregate(count=Count("id"), max_id=Max("id"))
    signature = _signature(
        getattr(settings, "mysql_db", ""),
        int(agg.get("count") or 0),
        int(agg.get("max_id") or 0),
    )
    index = _INDEX_CACHE.get(component)
    if index is not None and index.signature == signature:
        return index
    async with _INDEX_LOCK:
        index = _INDEX_CACHE.get(component)
        if index is None or index.signature != signature:
            index = await _load_index(component, signature)
            _INDEX_CACHE[component] = index
    return index


async def search(query: str, component: str | None, top_n: int = 20) -> list[dict]:
    terms = set(tokenize(query))
    if not terms:
        return []
    index = await _get_index(_component_key(component))
    if not index.rows:
        return []
    doc_ids = {row.doc_id for row in index.rows}
    docs = await KnowledgeDoc.filter(id__in=doc_ids) if doc_ids else []
    docs_by_id = {doc.id: doc for doc in docs}
    scored = []
    k1, b = 1.5, 0.75
    for row in index.rows:
        tokens = index.tokenized.get(row.id) or []
        length = len(tokens)
        score = 0.0
        for term in terms:
            tf = tokens.count(term)
            if not tf:
                continue
            term_df = index.df.get(term, 0)
            idf = math.log(1 + (index.document_count - term_df + 0.5) / (term_df + 0.5))
            score += idf * (tf * (k1 + 1)) / (tf + k1 * (1 - b + b * length / index.avg_len))
        if score <= 0:
            continue
        doc = docs_by_id.get(row.doc_id)
        scored.append((score, row, doc))
    scored.sort(key=lambda x: (-x[0], x[1].id))
    return [
        {"chunk_id": row.id, "text": row.text, "source_doc": doc.title if doc else str(row.doc_id),
         "component": row.component, "doc_type": row.doc_type, "score": score}
        for score, row, doc in scored[:top_n]
    ]
