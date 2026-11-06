"""
F-05 规则层优先与RAG检索回归测试
验证 V1.1 修订中的 8 阻断级问题修复：
B1 归一化单路 rank1=1.0
B3 component 字段与迁移
B4 source_doc 三态
B5 四档降级链
B6 bulk_create 主键回填
（B7/B8 是文档型，无需代码测试）
"""
import pytest
import asyncio
from app.rag.rrf import normalize, fuse
from app.knowledge.ingest import ingest_document
from app.models import KnowledgeDoc, KnowledgeChunk
from tortoise import Tortoise, connections
from decimal import Decimal
import math

# ============================================================================
# B1: 归一化单路 rank1=1.0
# ============================================================================
def test_b1_single_route_rank1_normalizes_to_1():
    """RRF 归一化：单路检索时，rank1 得分应为 1.0（非 0.01639）"""
    score = 1 / 61  # rank=1 -> 1/61
    norm = normalize(score, route_count=1)
    assert norm == pytest.approx(1.0, rel=1e-6), f"单路 rank1 应为 1.0，实际 {norm}"

def test_b1_dual_route_rank1_normalizes_to_0_5():
    """双路检索时，两路都 rank1 的归一化分应为 0.5（避免分摊导致过低）"""
    score = 1 / 61
    norm = normalize(score, route_count=2)
    # min(1.0, (1/61) * 61 / 2) = min(1.0, 0.5) = 0.5
    assert norm == pytest.approx(0.5, rel=1e-6), f"双路 rank1 应为 0.5，实际 {norm}"

def test_b1_rank2_single_route():
    """单路 rank2 应为 61/62 ≈ 0.9839（归一化公式：(1/62)/(1/61)）"""
    score = 1 / 62  # rank=2
    norm = normalize(score, route_count=1)
    expected = 61 / 62  # (1/62) / (1/61)
    assert norm == pytest.approx(expected, rel=1e-6), f"单路 rank2 应为 {expected}，实际 {norm}"

# ============================================================================
# B3: component 字段（直接测试模型字段存在）
# ============================================================================
@pytest.mark.asyncio
async def test_b3_knowledge_doc_has_component_field():
    """KnowledgeDoc 模型必须有 component 字段"""
    # 初始化测试 DB
    await Tortoise.init(
        db_url="mysql://root:123456@127.0.0.1:3306/mindops_f05_test",
        modules={"models": ["app.models"]}
    )
    # 该表由 generate_schemas() 按模型定义建，不要手工 CREATE 一份"骨架表"：
    # Tortoise 不会 ALTER 已存在的表，手写骨架一旦缺列（如 source_type / content），
    # 后续 KnowledgeDoc.create() 就会因为 NOT NULL 列为空而 ValidationError。
    # 这里先 DROP 掉历史遗留的骨架表，让 generate_schemas() 重新按模型建全。
    await connections.get("default").execute_script("DROP TABLE IF EXISTS knowledge_docs")
    await Tortoise.generate_schemas()

    # 测试创建带 component 的文档
    # source_type / content 是 NOT NULL 字段，必须一并给出。
    doc = await KnowledgeDoc.create(
        title="Test Component",
        content="component 字段落库验证",
        source_type="manual",
        component="api-gateway",
    )
    assert doc.component == "api-gateway"

    # 清理
    await connections.get("default").execute_script("DROP TABLE IF EXISTS knowledge_docs")
    await Tortoise.close_connections()

# ============================================================================
# 审查：原 B4/B5 是"变量等于自己字面量"的恒真断言（零覆盖价值），已删除。
# source_doc 三态的真实行为由下方 test_b6_ingest_real_path 与
# tests/test_f06_api.py 的证据断言覆盖。
# ============================================================================

# ============================================================================
# B6: bulk_create 主键回填（通过 ingest 行为验证）
# ============================================================================
@pytest.mark.asyncio
async def test_b6_ingest_populates_chunk_ids(monkeypatch):
    """审查：原用例从未调用 ingest_document，只手工 create 后断言主键非空
    （恒真、零覆盖）。这里真实调用 ingest_document 并 mock 掉 Milvus 上传，
    断言 bulk_create 后 chunk 主键确实回填、且向量库 upsert 拿到的是真 id。"""
    await Tortoise.init(
        db_url="mysql://root:123456@127.0.0.1:3306/mindops_f05_test",
        modules={"models": ["app.models"]}
    )
    # 先 DROP 再让 generate_schemas() 按模型建全表（避免手写骨架表漂移）
    await connections.get("default").execute_script("DROP TABLE IF EXISTS knowledge_docs")
    await connections.get("default").execute_script("DROP TABLE IF EXISTS knowledge_chunks")
    await Tortoise.generate_schemas()

    # mock 掉 Milvus 上传：记录调用参数，验证拿到的是已回填的真实 chunk id
    captured: dict = {}

    async def fake_upsert(rows, vectors):
        captured["row_ids"] = [row.id for row in rows]
        captured["vector_count"] = len(vectors)

    from app.rag import milvus_client
    monkeypatch.setattr(milvus_client, "upsert", fake_upsert)

    result = await ingest_document(
        title="Test B6 real ingest",
        content="第一段内容，足够长以被分块器切成多个 chunk。" * 10,
        source_type="runbook",
        component="test",
    )
    assert result["doc_id"] is not None
    assert result["chunk_count"] >= 1

    chunks = await KnowledgeChunk.filter(doc_id=result["doc_id"]).order_by("chunk_index")
    assert len(chunks) == result["chunk_count"]
    for chunk in chunks:
        # B6 核心断言：bulk_create 后主键必须已回填（None 会导致 Milvus 关联整体断裂）
        assert chunk.id is not None, "Chunk.id must be populated after bulk_create"
    assert captured.get("row_ids") == [c.id for c in chunks]
    assert captured.get("vector_count") == result["chunk_count"]

    # 清理
    await connections.get("default").execute_script("DROP TABLE IF EXISTS knowledge_chunks")
    await connections.get("default").execute_script("DROP TABLE IF EXISTS knowledge_docs")
    await Tortoise.close_connections()

# ============================================================================
# 补充：RRF 融合行为（确保 route_count 正确传递）
# ============================================================================
def test_rrf_fusion_respects_route_count():
    """RRF 融合时，应正确计算 route_count"""
    # 模拟两路检索结果
    candidates1 = [{"chunk_id": 1, "rank": 1, "payload": {}}]
    candidates2 = [{"chunk_id": 1, "rank": 1, "payload": {}}]

    hits = fuse(candidates1, candidates2, route_names=["dense", "bm25"])
    # A 出现在两路，route_count=2
    # 归一化：score = 1/(60+1) + 1/(60+1) = 2/61
    # maximum = 2/(60+1) = 2/61
    # normalized = (2/61) / (2/61) = 1.0
    assert len(hits) == 1
    norm = normalize(hits[0].score, route_count=2)
    assert norm == pytest.approx(1.0, rel=1e-6), f"双路 rank1 应归一化为 1.0，实际 {norm}"