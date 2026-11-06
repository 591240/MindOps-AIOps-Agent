from __future__ import annotations
import json
import math
from fastapi import APIRouter, Depends, HTTPException, Query, Request
from tortoise.functions import Count
from app.auth.dependencies import get_current_user
from app.observability.logging import log_event
from app.models import KnowledgeDoc, KnowledgeChunk, User
from app.knowledge.ingest import MAX_CONTENT_BYTES, VALID_SOURCE_TYPES, ingest_document
from app.rag.retriever import retrieve

router = APIRouter(prefix="/knowledge", tags=["knowledge"])

def _role(user: User) -> None:
    if user.role not in {"engineer", "admin"}: raise HTTPException(403, detail="engineer role required")

@router.get("")
async def list_knowledge(q: str | None = Query(None), component: str | None = Query(None), source_type: str | None = Query(None), page: int = Query(1, ge=1), page_size: int = Query(20, ge=1, le=100), user: User = Depends(get_current_user)):
    _role(user); qs = KnowledgeDoc.filter(is_latest=True)
    if q: qs = qs.filter(title__icontains=q)
    if source_type not in (None, "") and source_type not in VALID_SOURCE_TYPES:
        raise HTTPException(422, detail="source_type 必须为 runbook|incident|upload|api_doc|best_practice")
    if component is not None and (not isinstance(component, str) or len(component) > 64):
        raise HTTPException(422, detail="component 参数非法")
    # V1.1 B3：文档级 component 字段已入库，列表筛选直接走 SQL，
    # 不再对每个候选文档查一次切片做众数聚合（原实现是 N+1 查询）。
    if component:
        qs = qs.filter(component=component)
    # 类型标签页计数：与列表同条件（q/component）但**不受 source_type 筛选影响**，
    # 否则选中某类型后其余标签的数量会全部归零。
    count_rows = await qs.annotate(cnt=Count("id")).group_by("source_type").values("source_type", "cnt")
    type_counts = {row["source_type"]: row["cnt"] for row in count_rows}
    if source_type:
        qs = qs.filter(source_type=source_type)
    total = await qs.count()
    rows = await qs.order_by("-updated_at").offset((page - 1) * page_size).limit(page_size)
    # 审查 #21：一次性取回创建人建 map，消除逐行查询（真正的 N+1）
    creator_ids = {doc.created_by for doc in rows if doc.created_by is not None}
    creators = {u.id: u for u in await User.filter(id__in=creator_ids)} if creator_ids else {}
    items = []
    for doc in rows:
        creator = creators.get(doc.created_by) if doc.created_by is not None else None
        items.append({"doc_id": doc.id, "title": doc.title, "source_type": doc.source_type, "component": doc.component, "version": doc.version, "chunk_count": doc.chunk_count, "is_latest": doc.is_latest, "created_by": creator.username if creator else doc.created_by, "created_at": doc.created_at.isoformat()})
    return {"total": total, "items": items, "type_counts": type_counts}

@router.get("/{doc_id}")
async def get_knowledge(doc_id: int, user: User = Depends(get_current_user)):
    """单文档详情（含完整正文、切片列表、同标题版本历史）。

    列表端点刻意只回元数据（避免一次拉回几十份全文），正文此前只能靠检索接口
    间接拿到片段；本端点是"点进文档看全文"的唯一入口，与列表共用同一套可见性
    （engineer 及以上即可读）。

    `doc_id` 允许是**任一历史版本**的 id —— 详情页要能翻旧版本，因此不强制
    is_latest。若传入的是历史版本，额外用 `latest_doc_id` 告诉前端"当前最新是哪个"。
    """
    _role(user)
    doc = await KnowledgeDoc.filter(id=doc_id).first()
    if doc is None:
        raise HTTPException(404, detail={"code": "DOC_NOT_FOUND", "message": "文档不存在"})
    # 切片取该版本自己的，而非该标题最新的 —— 历史版本详情必须显示历史正文，
    # 否则会出现"版本号是 v1、内容是 v2"的错位。
    chunks = await KnowledgeChunk.filter(doc_id=doc.id).order_by("chunk_index")
    # 同标题的版本链（新→旧），用于详情页的版本切换
    versions = await KnowledgeDoc.filter(title=doc.title).order_by("-version")
    latest = next((item for item in versions if item.is_latest), None)
    creator = await User.filter(id=doc.created_by).first() if doc.created_by is not None else None
    return {
        "doc_id": doc.id,
        "title": doc.title,
        "content": doc.content,
        "source_type": doc.source_type,
        "component": doc.component,
        "version": doc.version,
        "chunk_count": doc.chunk_count,
        "is_latest": doc.is_latest,
        "created_by": creator.username if creator else doc.created_by,
        "created_at": doc.created_at.isoformat(),
        "updated_at": doc.updated_at.isoformat(),
        "content_hash": doc.content_hash,
        "latest_doc_id": latest.id if latest else doc.id,
        "chunks": [
            {
                "chunk_id": row.id,
                "chunk_index": row.chunk_index,
                "chunk_kind": row.chunk_kind,
                "component": row.component,
                "text": row.text,
                "embedding_id": row.embedding_id,
                "char_count": len(row.text or ""),
            }
            for row in chunks
        ],
        "versions": [
            {
                "doc_id": item.id,
                "version": item.version,
                "is_latest": item.is_latest,
                "chunk_count": item.chunk_count,
                "created_at": item.created_at.isoformat(),
            }
            for item in versions
        ],
    }

@router.post("/upload")
async def upload_knowledge(request: Request, user: User = Depends(get_current_user)):
    _role(user)
    content_type = request.headers.get("content-type", "")
    if content_type.startswith("application/json"):
        try:
            body = await request.json()
        except (json.JSONDecodeError, UnicodeDecodeError):
            raise HTTPException(422, detail="JSON 请求体非法")
        if not isinstance(body, dict):
            raise HTTPException(422, detail="JSON 请求体必须为对象")
    else:
        try:
            form = await request.form()
        except AssertionError:
            raise HTTPException(422, detail="multipart 上传需要安装 python-multipart")
        file = form.get("file")
        if file is None or not hasattr(file, "read"):
            raise HTTPException(422, detail="file 不能为空")
        raw = await file.read()
        if not isinstance(raw, bytes):
            raise HTTPException(422, detail="file 内容读取失败")
        if len(raw) > MAX_CONTENT_BYTES:
            raise HTTPException(422, detail="file 不能超过 1MB")
        body = {"title": getattr(file, "filename", "upload"), "content": raw.decode("utf-8", "replace"), "source_type": form.get("source_type", "upload"), "component": form.get("component")}
    if not isinstance(body, dict):
        raise HTTPException(422, detail="请求体必须为对象")
    try:
        result = await ingest_document(body.get("title", ""), body.get("content", ""), body.get("source_type", ""), body.get("component"), user.id)
    except ValueError as exc: raise HTTPException(422, detail=str(exc))
    except Exception as exc:
        # 审查 #22：服务端自身错误用 500 + 结构化信封，异常细节只进日志不外泄
        log_event(40, "knowledge.ingest_failed", title=body.get("title"), error=str(exc))
        raise HTTPException(500, detail={"code": "INGEST_FAILED", "message": "知识入库失败，已回滚"})
    return {**result, "ingested": True}

@router.post("/search")
async def search_knowledge(body: dict, user: User = Depends(get_current_user)):
    _role(user)
    query_value = body.get("query", "")
    if not isinstance(query_value, str):
        raise HTTPException(422, detail="query 必须为字符串")
    query = query_value.strip()
    if not query: raise HTTPException(422, detail="query 不能为空")
    component = body.get("component")
    if component is not None and (not isinstance(component, str) or len(component) > 64):
        raise HTTPException(422, detail="component 必须为字符串")
    try:
        top_k_value = body.get("top_k", 5)
        sim_floor_value = body.get("sim_floor", 0.0)
        if isinstance(top_k_value, bool) or isinstance(sim_floor_value, bool):
            raise ValueError
        top_k = int(top_k_value)
        sim_floor = float(sim_floor_value)
    except (TypeError, ValueError):
        raise HTTPException(422, detail="top_k/sim_floor 参数非法")
    if not 1 <= top_k <= 20 or not math.isfinite(sim_floor) or not 0 <= sim_floor <= 1:
        raise HTTPException(422, detail="top_k 应为 1-20，sim_floor 应为 0-1")
    result = await retrieve(query, component, top_k, sim_floor)
    return {"results": [{**item, "text": item.get("text") or item.get("quote", ""), "fusion_rank": i+1} for i, item in enumerate(result.hits)], "fusion_method": result.fusion, "degraded": result.degraded, "latency_ms": result.latency_ms}
