from __future__ import annotations
import hashlib, re
from time import monotonic
from tortoise.transactions import in_transaction
from app.alerts import sse
from app.models import KnowledgeDoc, KnowledgeChunk
from app.observability import metrics
from app.observability.logging import log_event
from app.rag.chunker import split
from app.rag.embedder import embed_batch
from app.rag import milvus_client

VALID_SOURCE_TYPES = {"runbook", "incident", "upload", "api_doc", "best_practice"}
MAX_CONTENT_BYTES = 1024 * 1024
def infer_component(content: str) -> str:
    low = content.lower()
    for name in ("redis", "mysql", "nginx", "docker"):
        if name in low: return name
    return "common"
def sanitize(value: str) -> str:
    return re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", " ", value or "").strip()

async def ingest_document(title: str, content: str, source_type: str, component: str | None = None, created_by: int | None = None) -> dict:
    started = monotonic()
    if not isinstance(title, str) or not isinstance(content, str):
        raise ValueError("title/content 必须为字符串")
    if not isinstance(source_type, str):
        raise ValueError("source_type 必须为字符串")
    if component is not None and not isinstance(component, str):
        raise ValueError("component 必须为字符串")
    title, content = sanitize(title), sanitize(content)
    if not title or not content: raise ValueError("title/content 不能为空")
    if len(title) > 255: raise ValueError("title 长度不能超过 255 个字符")
    if len(content.encode("utf-8")) > MAX_CONTENT_BYTES: raise ValueError("content 不能超过 1MB")
    if source_type not in VALID_SOURCE_TYPES: raise ValueError("source_type 必须为 runbook|incident|upload|api_doc|best_practice")
    digest = hashlib.sha1(f"{title}|{content}".encode()).hexdigest()
    # Idempotency is content based across the full version history.  A
    # previously superseded version must not be inserted again if the same
    # document is replayed by an uploader or the F-07 re-ingest job.
    existing = await KnowledgeDoc.filter(title=title, content_hash=digest).order_by("-version").first()
    if existing: return {"doc_id": existing.id, "chunk_count": existing.chunk_count, "version": existing.version}
    component = component or infer_component(content)
    chunks = split(content, component)
    if not chunks: raise ValueError("content 不能为空")
    try: vectors = await embed_batch([chunk.text for chunk in chunks])
    except Exception as exc:
        metrics.knowledge_ingest_total.labels(source_type=source_type, result="failed").inc(); log_event(40, "knowledge.ingest_failed", title=title, stage="embed", error=str(exc)); raise
    previous_id = None
    rows = []
    doc = None
    try:
        async with in_transaction() as conn:
            # Lock all versions for this title so concurrent uploads cannot
            # allocate the same version or leave two latest documents.
            existing_locked = await KnowledgeDoc.filter(title=title, content_hash=digest).order_by("-version").using_db(conn).first()
            if existing_locked:
                return {"doc_id": existing_locked.id, "chunk_count": existing_locked.chunk_count, "version": existing_locked.version}
            previous = await KnowledgeDoc.filter(title=title, is_latest=True).using_db(conn).select_for_update().first()
            highest = await KnowledgeDoc.filter(title=title).order_by("-version").using_db(conn).select_for_update().first()
            version = highest.version + 1 if highest else 1
            previous_id = previous.id if previous else None
            if previous:
                await KnowledgeDoc.filter(id=previous.id).using_db(conn).update(is_latest=False)
                await KnowledgeChunk.filter(doc_id=previous.id).using_db(conn).update(is_latest=False)
            doc = await KnowledgeDoc.create(using_db=conn, title=title, content=content, source_type=source_type, version=version, chunk_count=len(chunks), is_latest=True, created_by=created_by, content_hash=digest, component=component)
            await KnowledgeChunk.bulk_create(
                [
                    KnowledgeChunk(
                        doc_id=doc.id, text=chunk.text[:4096], component=chunk.component,
                        doc_type=source_type, chunk_index=i, chunk_kind=chunk.kind,
                        source_version=version,
                        embedding_id=hashlib.sha1(f"{doc.id}:{version}:{i}".encode()).hexdigest(),
                        is_latest=True,
                    )
                    for i, chunk in enumerate(chunks)
                ],
                using_db=conn,
            )
            # F-05 V1.1 B6：Tortoise bulk_create 返回 None 且**不回填自增主键**。
            # 必须重新查询取回，否则下游 milvus_client.upsert(rows, ...) 拿到的
            # chunk_id 全为 None —— 向量库与 MySQL 的关联会整体断裂，检索命中后
            # 无法回表取原文。实测 bulk_create(...) is None，len(rows) 会 TypeError。
            rows = await (
                KnowledgeChunk.filter(doc_id=doc.id, source_version=version)
                .using_db(conn)
                .order_by("chunk_index")
            )
        await milvus_client.upsert(rows, vectors)
    except Exception:
        if doc is not None:
            chunk_ids = [row.id for row in rows if row.id is not None]
            try:
                await milvus_client.delete_doc(doc.id, chunk_ids)
            except Exception as compensation_error:
                log_event(40, "knowledge.ingest_compensation_failed", doc_id=doc.id, stage="milvus_delete", error=str(compensation_error))
            try:
                await KnowledgeChunk.filter(doc_id=doc.id).delete()
                await KnowledgeDoc.filter(id=doc.id).delete()
            except Exception as compensation_error:
                log_event(40, "knowledge.ingest_compensation_failed", doc_id=doc.id, stage="db_delete", error=str(compensation_error))
        if previous_id is not None:
            try:
                await KnowledgeDoc.filter(id=previous_id).update(is_latest=True)
                await KnowledgeChunk.filter(doc_id=previous_id).update(is_latest=True)
            except Exception as compensation_error:
                # Preserve the original ingest error while making a failed
                # restore visible for operators; a repair job can then
                # re-establish one latest version from the audit log.
                log_event(40, "knowledge.ingest_compensation_failed", doc_id=previous_id, stage="db_restore", error=str(compensation_error))
        try:
            metrics.knowledge_chunks_total.set(await KnowledgeChunk.filter(is_latest=True).count())
        except Exception:
            pass
        metrics.knowledge_ingest_total.labels(source_type=source_type, result="failed").inc()
        raise
    try:
        metrics.knowledge_chunks_total.set(await KnowledgeChunk.filter(is_latest=True).count())
    except Exception:
        pass
    metrics.knowledge_ingest_total.labels(source_type=source_type, result="success").inc()
    try:
        await sse.publish("knowledge.ingested", {"doc_id": doc.id, "title": title, "source_type": source_type, "version": version, "chunk_count": len(rows), "is_reingest": previous is not None})
    except Exception as exc:
        # Notification is best effort after both stores have committed.
        log_event(30, "knowledge.ingest_sse_failed", doc_id=doc.id, error=str(exc))
    log_event(20, "knowledge.ingest", doc_id=doc.id, title=title, version=version, chunk_count=len(rows), created_by=created_by, latency_ms=int((monotonic() - started) * 1000))
    return {"doc_id": doc.id, "chunk_count": len(rows), "version": version}
