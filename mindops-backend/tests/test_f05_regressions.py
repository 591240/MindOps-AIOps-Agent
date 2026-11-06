"""Regression coverage for F-05 rule matching and sparse tokenization."""

from pathlib import Path

import pytest

from app.agents import triage
import app.models as models
from app.rag.bm25 import tokenize
from app.rag import milvus_client, retriever

# 本模块的**异步**用例统一用 module 级事件循环（逐个用例单独加
# `@pytest.mark.asyncio(loop_scope="module")`），与同套件的其他异步模块
# （test_f04.py / test_f06.py / test_f07.py 等）保持一致。
#
# 为什么不用模块级 `pytestmark`：本文件混有同步用例，
# 全局 mark 会让它们收到 "marked with '@pytest.mark.asyncio' but it is not
# an async function" 的告警。
#
# 为什么必须统一 loop_scope：若这里用默认的 function 级循环，其 teardown
# 会关闭共享的 ProactorEventLoop，导致后续 module 级模块（如 F-06）在收尾时
# 报 `RuntimeError: Event loop is closed`（20+ 个 ERROR）。
_ASYNC = pytest.mark.asyncio(loop_scope="module")


def test_bm25_chinese_bigrams_are_hashable_strings():
    tokens = tokenize("Redis 内存高")

    assert "内存" in tokens
    assert all(isinstance(token, str) for token in tokens)
    assert set(tokens)


def test_match_pattern_uses_public_contract_and_component_scope():
    hit = triage.match_pattern("Redis OOM 内存使用率告警", "redis")
    assert hit is not None
    assert hit["name"] == "redis_oom"

    assert triage.match_pattern("Redis OOM 内存使用率告警", "mysql") is None
    assert triage.match_pattern("磁盘空间不足", "mysql")["name"] == "disk_full"
    assert triage.match_pattern("redis OOM", "REDIS")["name"] == "redis_oom"


def test_pattern_rules_hot_reload_and_invalid_file_fallback(monkeypatch):
    # 刻意**不请求** pytest 的临时目录 fixture（tmp_path / tmp_path_factory）：
    # 本机 `%TEMP%\pytest-of-<用户>` 目录存在权限异常（WinError 5），
    # 只要 fixture 被请求，pytest 就会尝试创建/清理该目录并失败，
    # 其 teardown 出错还会连带破坏后续模块的事件循环
    # （表现为 test_f06.py 大量 `RuntimeError: Event loop is closed`）。
    # 因此这里自己开一个可写的临时目录。
    import os
    import tempfile

    base = os.path.join(os.path.dirname(__file__), "..", ".pytest-tmp")
    os.makedirs(base, exist_ok=True)
    tmp = Path(tempfile.mkdtemp(dir=base))

    rule_file = tmp / "pattern_rules.yaml"
    rule_file.write_text(
        "rules:\n  - name: first\n    component: redis\n    keywords: [alpha, first]\n    root_cause: one\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(triage, "_RULE_FILE", rule_file)
    monkeypatch.setattr(triage, "_rules_cache", None)
    monkeypatch.setattr(triage, "_rules_mtime", None)

    assert triage.match_pattern("alpha", "redis")["name"] == "first"

    rule_file.write_text(
        "rules:\n  - name: second\n    component: redis\n    keywords: [beta, second]\n    root_cause: two\n",
        encoding="utf-8",
    )
    assert triage.match_pattern("beta", "redis")["name"] == "second"

    rule_file.write_text("rules: [", encoding="utf-8")
    assert triage.match_pattern("beta", "redis")["name"] == "second"


def test_default_component_scope_is_global():
    from app.rag.retriever import _is_global_scope
    from app.agents.loop_strategies import _is_global_scope as loop_scope

    assert _is_global_scope(None)
    assert _is_global_scope("__global__")
    assert loop_scope(None)
    assert loop_scope("__global__")


@_ASYNC
async def test_milvus_upsert_rejects_wrong_dimension():
    class Row:
        id = 1
        doc_id = 1
        component = "redis"
        text = "test"

    with pytest.raises(ValueError, match="dimension"):
        await milvus_client.upsert([Row()], [[0.0] * 8])


@_ASYNC
async def test_milvus_search_rejects_non_numeric_or_non_finite_vectors():
    with pytest.raises(ValueError, match="dimension"):
        await milvus_client.search([0.0] * 8, "redis")
    with pytest.raises(ValueError, match="numeric"):
        await milvus_client.search([0.0] * 1023 + ["0"], "redis")
    with pytest.raises(ValueError, match="non-finite"):
        await milvus_client.search([0.0] * 1023 + [float("nan")], "redis")


@_ASYNC
async def test_milvus_search_resolves_source_doc_title(monkeypatch):
    class Hit:
        id = 42
        distance = 0.8
        entity = {
            "chunk_id": 42,
            "doc_id": 9,
            "component": "redis",
            "chunk_text": "memory runbook",
            "source_type": "runbook",
            "version": 2,
            "is_latest": True,
        }

    class Collection:
        def search(self, *_args, **_kwargs):
            return [[Hit()]]

    class Doc:
        id = 9
        title = "Redis 运维 Runbook"

    async def fake_filter(**_kwargs):
        return [Doc()]

    monkeypatch.setattr(milvus_client, "_ensure_collection", lambda: Collection())
    class FakeKnowledgeDoc:
        filter = staticmethod(fake_filter)

    class FakeKnowledgeChunk:
        @staticmethod
        async def filter(**_kwargs):
            # Keep the test independent from rows left by earlier integration tests.
            return []

    monkeypatch.setattr(models, "KnowledgeDoc", FakeKnowledgeDoc)
    monkeypatch.setattr(models, "KnowledgeChunk", FakeKnowledgeChunk)

    result = await milvus_client.search([0.0] * 1024, "redis")

    assert result[0]["source_doc"] == "Redis 运维 Runbook"


@_ASYNC
async def test_single_route_rrf_uses_single_route_normalization(monkeypatch):
    async def _vector():
        return [0.0] * 1024

    async def sparse(_query, _component, _top_n):
        return []

    async def dense(_vector, _component, _top_n):
        return [{"chunk_id": 7, "text": "dense hit", "source_doc": "runbook", "score": 0.9}]

    monkeypatch.setattr(retriever.bm25, "search", sparse)
    monkeypatch.setattr(retriever.milvus_client, "engine_available", lambda: True)
    monkeypatch.setattr(retriever.embedder, "embed_one", lambda _query: _vector())
    monkeypatch.setattr(retriever.milvus_client, "search", dense)

    result = await retriever.retrieve("query", "redis", top_k=5, sim_floor=0.9)

    assert [hit["chunk_id"] for hit in result.hits] == [7]
