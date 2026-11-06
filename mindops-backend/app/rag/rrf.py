"""Reciprocal Rank Fusion。

F-05 V1.1 变更：
- 支持**任意多路**融合（Dense / BM25 / Legacy 已结事件），不再固定两路；
- 排名 **rank 从 1 起算**（D2），故单路 rank=1 的 RRF 分为 ``1/(k+1)``；
- 融合键改为**复合键**：知识库切片用 ``chunk:<id>``、已结事件用
  ``doc:similar-incident:<id>``。原因：Legacy 路的 ``chunk_id`` 为 ``None``
  （F-06 V1.1 §5.1 约定），而历史事件的 ``incident_id`` 与知识库切片自增
  ``id`` 同属整数空间，若直接用 ``chunk_id`` 做键会**跨来源撞键**，
  把一条历史事件与一条知识切片错误合并。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

DEFAULT_K = 60


@dataclass
class Hit:
    chunk_id: int | None
    score: float
    payload: dict
    # 各路原始排名，便于调试与评测脚本复盘融合过程
    ranks: dict[str, int] = field(default_factory=dict)


def _fusion_key(item: dict[str, Any]) -> str:
    """复合融合键：跨来源保证唯一（见模块 docstring）。"""
    cid = item.get("chunk_id")
    if cid is None:
        return f"doc:{item.get('source_doc')}"
    return f"chunk:{cid}"


def fuse(*routes: list[dict], k: int = DEFAULT_K, route_names: list[str] | None = None) -> list[Hit]:
    """对任意多路排序结果做 RRF 融合。

    :param routes: 若干已按相关性降序排列的命中列表
    :param k: RRF 平滑常数（默认 60，对齐 PRD §6.4）
    :param route_names: 与 routes 一一对应的路名，仅用于记录 ranks 便于调试
    :return: 按融合分降序的 Hit 列表；同分时按融合键升序保证结果稳定
    """
    scores: dict[str, float] = {}
    payloads: dict[str, dict] = {}
    ranks: dict[str, dict[str, int]] = {}

    for idx, ranked in enumerate(routes):
        if not ranked:
            continue
        name = route_names[idx] if route_names and idx < len(route_names) else f"route{idx}"
        for rank, item in enumerate(ranked, 1):  # rank 从 1 起算（D2）
            key = _fusion_key(item)
            scores[key] = scores.get(key, 0.0) + 1.0 / (k + rank)
            # 同一 key 被多路命中时保留先出现（更相关）的那路载荷
            payloads.setdefault(key, item)
            ranks.setdefault(key, {})[name] = rank

    return [
        Hit(
            chunk_id=payloads[key].get("chunk_id"),
            score=score,
            payload=payloads[key],
            ranks=ranks.get(key, {}),
        )
        for key, score in sorted(scores.items(), key=lambda kv: (-kv[1], kv[0]))
    ]


def normalize(score: float, route_count: int, k: int = DEFAULT_K) -> float:
    """把 RRF 融合分归一化到 [0,1]。

    分母为**本次实际参与融合的路数**的理论最高分（每路均 rank=1）：
    ``route_count / (k + 1)``。这样单路命中也能达到 1.0，避免 F-05 V1.0
    固定取 ``2/61`` 导致单路上限仅 0.5、``sim_floor=0.7`` 永不可达的缺陷（B1）。

    相比 V1.1 文档写的 ``min(1.0, score*61)``，按路数取分母能让多路命中的
    中间名次也保持区分度（双路均 rank=5 → 0.938，而非被 clamp 成 1.0）。
    """
    if route_count <= 0:
        return 0.0
    maximum = route_count / (k + 1)
    return min(1.0, score / maximum) if maximum else 0.0
