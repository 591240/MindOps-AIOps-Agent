"""Lazy Milvus Lite/remote adapter with a strict 1024-dimensional schema."""
from __future__ import annotations
import math
import os
import time
from numbers import Real
from typing import Any
from app.config import settings

DIM = 1024
COLLECTION = "knowledge_chunks"
_collection: Any = None
_failed = False
_last_failure_at = 0.0
_RETRY_COOLDOWN_SECONDS = 5.0

def _uri() -> str:
    return str(getattr(settings, "milvus_uri", "") or os.getenv("MINDOPS_MILVUS_URI", "")).strip()

def _ensure_collection() -> Any:
    global _collection, _failed, _last_failure_at
    if _collection is not None: return _collection
    uri = _uri()
    if not uri: return None
    # A transient startup/network failure must not permanently pin the process
    # to the legacy retrieval path.  Avoid reconnecting on every query while
    # still allowing a later health check to recover automatically.
    if _failed and time.monotonic() - _last_failure_at < _RETRY_COOLDOWN_SECONDS:
        return None
    try:
        from pymilvus import Collection, CollectionSchema, DataType, FieldSchema, connections, utility
        alias = "mindops"
        connections.connect(alias=alias, uri=uri)
        if utility.has_collection(COLLECTION, using=alias):
            collection = Collection(COLLECTION, using=alias)
            _validate_collection_schema(collection)
        else:
            # ``id`` is the stable embedding id; ``chunk_id`` keeps the
            # externally visible MySQL primary key available in search hits.
            fields = [
                FieldSchema("id", DataType.VARCHAR, max_length=64, is_primary=True, auto_id=False),
                FieldSchema("chunk_id", DataType.INT64),
                FieldSchema("doc_id", DataType.INT64),
                FieldSchema("embedding", DataType.FLOAT_VECTOR, dim=DIM),
                FieldSchema("chunk_text", DataType.VARCHAR, max_length=4096),
                FieldSchema("component", DataType.VARCHAR, max_length=64),
                FieldSchema("source_type", DataType.VARCHAR, max_length=16),
                FieldSchema("version", DataType.INT64),
                FieldSchema("is_latest", DataType.BOOL),
            ]
            collection = Collection(COLLECTION, schema=CollectionSchema(fields, enable_dynamic_field=False), using=alias)
            collection.create_index("embedding", {"index_type": "IVF_FLAT", "metric_type": "IP", "params": {"nlist": 128}})
        collection.load(); _collection = collection; _failed = False; return collection
    except Exception:
        _failed = True
        _last_failure_at = time.monotonic()
        return None


def _validate_collection_schema(collection: Any) -> None:
    """Reject pre-existing collections that cannot satisfy the F-05 contract."""
    schema = getattr(collection, "schema", None)
    fields = getattr(schema, "fields", None)
    if not fields:
        raise ValueError("milvus collection schema is unavailable")
    by_name = {str(getattr(field, "name", "")): field for field in fields}
    required = {
        "id", "chunk_id", "doc_id", "embedding", "chunk_text",
        "component", "source_type", "version", "is_latest",
    }
    missing = required - by_name.keys()
    if missing:
        raise ValueError(f"milvus collection missing fields: {','.join(sorted(missing))}")
    params = getattr(by_name["embedding"], "params", {}) or {}
    try:
        dim = int(params.get("dim"))
    except (TypeError, ValueError):
        dim = 0
    if dim != DIM:
        raise ValueError(f"milvus collection embedding dimension must be {DIM}")

def engine_available() -> bool:
    return _ensure_collection() is not None


def _validate_vector(vector: Any) -> list[float]:
    """Validate the fixed Milvus vector contract at the adapter boundary."""
    if not isinstance(vector, (list, tuple)) or len(vector) != DIM:
        raise ValueError(f"embedding dimension must be {DIM}")
    values: list[float] = []
    for value in vector:
        if isinstance(value, bool) or not isinstance(value, Real):
            raise ValueError("embedding values must be numeric")
        converted = float(value)
        if not math.isfinite(converted):
            raise ValueError("embedding contains non-finite values")
        values.append(converted)
    return values

async def upsert(rows, vectors) -> None:
    rows = list(rows)
    vectors = list(vectors)
    if len(rows) != len(vectors):
        raise ValueError("embedding vector count must match chunk count")
    validated_vectors = [_validate_vector(vector) for vector in vectors]
    collection = _ensure_collection()
    if collection is None:
        raise RuntimeError("milvus is unavailable")
    collection.upsert([
        [str(getattr(r, "embedding_id", None) or r.id) for r in rows],
        [int(r.id) for r in rows],
        [int(r.doc_id) for r in rows],
        validated_vectors,
        [str(r.text)[:4096] for r in rows],
        [str(r.component) for r in rows],
        [str(getattr(r, "doc_type", "upload")) for r in rows],
        [int(getattr(r, "source_version", 1)) for r in rows],
        [bool(getattr(r, "is_latest", True)) for r in rows],
    ])

async def delete_doc(doc_id: int, chunk_ids: list[int] | None = None) -> None:
    collection = _ensure_collection()
    if collection is None: return
    expr = f"chunk_id in [{','.join(str(int(cid)) for cid in chunk_ids)}]" if chunk_ids else f"doc_id == {int(doc_id)}"
    collection.delete(expr)

async def search(vector: list[float], component: str | None, top_n: int = 20) -> list[dict]:
    validated_vector = _validate_vector(vector)
    if isinstance(top_n, bool):
        raise ValueError("top_n must be a positive integer")
    try:
        top_n = int(top_n)
    except (TypeError, ValueError):
        raise ValueError("top_n must be a positive integer") from None
    if top_n < 1:
        raise ValueError("top_n must be a positive integer")
    collection = _ensure_collection()
    if collection is None: raise RuntimeError("milvus is unavailable")
    expr = "is_latest == true"
    if component and component != "__global__":
        escaped = component.replace('"', '\\"')
        expr += f' and component == "{escaped}"'
    raw = collection.search(
        [validated_vector],
        "embedding",
        {"metric_type": "IP", "params": {"nprobe": 16}},
        limit=top_n,
        expr=expr,
        output_fields=["chunk_id", "doc_id", "component", "chunk_text", "source_type", "version", "is_latest"],
    )
    result = []
    raw_hits = raw[0] if raw else []
    doc_ids: set[int] = set()
    parsed_hits: list[tuple[Any, Any, int, int]] = []
    for hit in raw_hits:
        entity = getattr(hit, "entity", hit)
        chunk_id = field_value(entity, "chunk_id")
        if chunk_id is None:
            chunk_id = getattr(hit, "id", 0)
        doc_id = field_value(entity, "doc_id", 0)
        try:
            chunk_id = int(chunk_id)
            doc_id = int(doc_id)
            distance = float(getattr(hit, "distance", entity.get("distance", 0.0) if hasattr(entity, "get") else 0.0))
        except (TypeError, ValueError):
            continue
        if not math.isfinite(distance):
            continue
        doc_ids.add(doc_id)
        parsed_hits.append((hit, entity, chunk_id, doc_id))

    # Milvus stores only the document id. Resolve titles in one MySQL query so
    # evidence links remain stable and match the public knowledge API contract.
    titles: dict[int, str] = {}
    if doc_ids:
        try:
            from app.models import KnowledgeDoc
            docs = await KnowledgeDoc.filter(id__in=list(doc_ids))
            titles = {int(doc.id): str(doc.title) for doc in docs if doc.title}
        except Exception:
            # Search must remain usable when the metadata store is unavailable.
            titles = {}
    chunk_latest_state: dict[int, bool] | None = None
    metadata_lookup_failed = False
    if parsed_hits:
        try:
            from app.models import KnowledgeChunk

            chunk_rows = await KnowledgeChunk.filter(
                id__in=[chunk_id for _, _, chunk_id, _ in parsed_hits],
            )
            # MySQL is authoritative when a row exists.  Keep unknown ids
            # during eventual consistency windows so a newly indexed vector
            # is not dropped just because its metadata row is not visible yet.
            chunk_latest_state = {
                int(row.id): bool(row.is_latest) for row in chunk_rows
            }
        except Exception:
            # Never expose dense evidence without the authoritative version
            # check. The sparse route remains available to the caller.
            chunk_latest_state = {}
            metadata_lookup_failed = True

    for hit, entity, chunk_id, doc_id in parsed_hits:
        if metadata_lookup_failed:
            continue
        if (
            chunk_latest_state is not None
            and chunk_id in chunk_latest_state
            and not chunk_latest_state[chunk_id]
        ):
            continue
        distance = float(getattr(hit, "distance", entity.get("distance", 0.0) if hasattr(entity, "get") else 0.0))
        result.append({"chunk_id": chunk_id, "doc_id": doc_id, "component": field_value(entity, "component"), "text": field_value(entity, "chunk_text", ""), "source_doc": titles.get(doc_id, f"doc-{doc_id}"), "doc_type": field_value(entity, "source_type", "upload"), "source_version": field_value(entity, "version", 1), "is_latest": field_value(entity, "is_latest", True), "score": distance})
    return result


def field_value(entity: Any, name: str, default: Any = None) -> Any:
    """Read a Milvus entity field from dict-like and row-like result types."""
    if hasattr(entity, "get"):
        return entity.get(name, default)
    try:
        return entity[name]
    except (KeyError, TypeError, IndexError):
        return default
