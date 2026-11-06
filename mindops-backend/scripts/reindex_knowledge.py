"""把 MySQL 里 `is_latest` 的切片重新向量化并写入 Milvus（F-05 的缺失入口）。

为什么需要它：换 Milvus 实例（Lite 文件 → 容器）、换 embedding 模型、或集合被
drop 之后，向量侧不会自动同步，检索会**静默**退化成 `BM25_ONLY`——接口照样 200，
只是 dense 那一路永远空。专项文档里写过 `rebuild_collection()` 这个契约，但代码
里从来没有实现，这个脚本就是补上它。

用法：
    python scripts/reindex_knowledge.py --dry-run     # 只体检：Key 来源 + 引擎 + 待处理条数
    python scripts/reindex_knowledge.py               # 全量重建（幂等，可反复跑）
    python scripts/reindex_knowledge.py --limit 20    # 先跑一小批验证链路

幂等性由主键保证：Milvus 侧 `id` 取 `chunk.embedding_id`（缺省回退 `chunk.id`），
重复执行是同主键 upsert，不会长出重复向量。
"""

from __future__ import annotations

import argparse
import asyncio
import pathlib
import sys

# 直接 `python scripts/reindex_knowledge.py` 时 sys.path[0] 是 scripts/，
# 需要把项目根加进来才能 import app.*
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1]))

from app.config import settings
from app.db import close_db, init_db


async def _report_environment() -> tuple[str, str, str]:
    from app.rag import embedder, milvus_client

    key, source, model = await embedder._resolve_api_key()
    embedder._load_cache()
    cache_entries = len(embedder._cache)
    print(f"  embedding 模型 : {model}")
    print(f"  Key 来源       : {source or '（无）'}")
    print(f"  向量模式       : {'真实模型' if key else '⚠ 本地伪向量（dense 结果不可信，见 embedding.local_vectors 日志）'}")
    print(f"  Milvus URI     : {settings.milvus_uri or '（未配置）'}")
    print(f"  引擎可用       : {milvus_client.engine_available()}（collection: {milvus_client.COLLECTION}）")
    print(f"  向量缓存       : {cache_entries} 条 @ {embedder._CACHE_PATH}")
    if cache_entries:
        print("                   缓存键已改为带模型命名空间，历史条目不会再被命中；"
              "要彻底清掉这份可能掺有伪向量的缓存，加 --clear-cache")
    return key, source, model


async def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description="重建 knowledge_chunks 向量集合")
    parser.add_argument("--dry-run", action="store_true", help="只体检，不请求 embedding、不写 Milvus")
    parser.add_argument("--limit", type=int, default=0, help="最多处理多少条切片（0 = 全部）")
    parser.add_argument("--component", default="", help="只重建指定组件的切片")
    parser.add_argument("--clear-cache", action="store_true",
                        help="先删除 embedding 缓存文件（历史条目可能掺有 sha256 伪向量）")
    args = parser.parse_args(argv)

    await init_db()
    try:
        from app.models import KnowledgeChunk
        from app.rag import embedder, milvus_client

        if args.clear_cache:
            cache_path = embedder._CACHE_PATH
            removed = cache_path.stat().st_size if cache_path.exists() else 0
            if cache_path.exists():
                cache_path.unlink()
            embedder._cache, embedder._cache_loaded = {}, True
            print(f"已清空向量缓存（{removed / 1024:.0f} KB）")

        print("== 环境体检 ==")
        key, _source, _model = await _report_environment()
        if not key and not args.dry_run:
            print("\n拒绝继续：没有 embedding Key 时重建出来的全是 sha256 伪向量，")
            print("          把它们写进集合只会让后续检索长期召回噪声。请先配置槽位，或只跑 --dry-run。")
            return 2

        filt = {"is_latest": True}
        if args.component:
            filt["component"] = args.component
        chunks = await KnowledgeChunk.filter(**filt).order_by("id")
        if args.limit > 0:
            chunks = chunks[: args.limit]
        print(f"\n== 待重建切片 : {len(chunks)} 条（is_latest=True"
              f"{f', component={args.component}' if args.component else ''}）==")
        if not chunks:
            return 0
        if args.dry_run:
            by_component: dict[str, int] = {}
            for chunk in chunks:
                by_component[chunk.component] = by_component.get(chunk.component, 0) + 1
            for component, count in sorted(by_component.items()):
                print(f"  {component:20s} {count} 条")
            print("  （--dry-run：未发起任何网络请求）")
            return 0

        if not milvus_client.engine_available():
            print("\nMilvus 不可用，终止。检查 MINDOPS_MILVUS_URI 与容器状态。")
            return 1

        # embed_batch 内部按 16 条一批请求，这里按 50 条分批只是为了能打印进度
        done = 0
        for offset in range(0, len(chunks), 50):
            batch = chunks[offset:offset + 50]
            vectors = await embedder.embed_batch([chunk.text for chunk in batch])
            await milvus_client.upsert(batch, vectors)
            done += len(batch)
            print(f"  已写入 {done}/{len(chunks)}")

        # 刚 upsert 完立即检索可能是空的（Milvus 的写入可见性有延迟），重试几次
        probe: list = []
        for _attempt in range(3):
            probe = await milvus_client.search(vectors[0], batch[0].component or None, top_n=3)
            if probe:
                break
            await asyncio.sleep(1.0)
        print(f"\n== 完成 : {done} 条 ==")
        if probe:
            print(f"  自检检索（用第 1 条自己的向量）: 命中 {len(probe)} 条，"
                  f"最高分 {probe[0]['score']:.4f}（IP 度量，自匹配应明显高于其他）")
        else:
            print("  ⚠ 自检检索仍为空：集合里确实写入了数据，但查询侧看不见——"
                  "检查 collection 是否 load、或把重试等待加长")
        print("  下一步：GET /api/v1/knowledge/search 应返回 fusion_method=RRF、degraded=false")
        return 0
    finally:
        await close_db()


if __name__ == "__main__":
    sys.exit(asyncio.run(main(sys.argv[1:])))
