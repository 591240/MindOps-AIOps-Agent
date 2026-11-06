"""SSE 实时推送（文档 §2.2 / §8.2 / §9.3）。

- 入库后向所有已连接前端推送 `event: alert`
- 支持 Last-Event-ID 断线重放（附录 A ⑬）：保留最近 _REPLAY_SIZE 条事件
"""

from __future__ import annotations

import asyncio
import json
from collections.abc import Awaitable, Callable
from typing import AsyncIterator

_REPLAY_SIZE = 200


class SSEBroker:
    def __init__(self) -> None:
        self._subscribers: set[asyncio.Queue] = set()
        self._filters: dict[asyncio.Queue, Callable[[str, dict], bool] | None] = {}
        self._seq = 0
        self._replay: list[tuple[int, str, str]] = []   # (seq, event, data_json)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)

    async def publish(self, event: str, data: dict) -> int:
        self._seq += 1
        payload = json.dumps(data, ensure_ascii=False, default=str)
        self._replay.append((self._seq, event, payload))
        if len(self._replay) > _REPLAY_SIZE:
            self._replay.pop(0)

        frame = f"id: {self._seq}\nevent: {event}\ndata: {payload}\n\n"
        dead: list[asyncio.Queue] = []
        for q in self._subscribers:
            event_filter = self._filters.get(q)
            if event_filter is not None and not _matches(event, data, event_filter):
                continue
            try:
                q.put_nowait(frame)
            except asyncio.QueueFull:
                dead.append(q)
        for q in dead:
            self._subscribers.discard(q)
        return self._seq

    def replay_since(self, last_event_id: int | None) -> list[str]:
        """返回 last_event_id 之后的事件帧（断线重放）。"""
        if not last_event_id:
            return []
        return [
            f"id: {seq}\nevent: {ev}\ndata: {data}\n\n"
            for seq, ev, data in self._replay
            if seq > last_event_id
        ]

    async def subscribe(
        self,
        last_event_id: int | None = None,
        event_filter: Callable[[str, dict], bool] | None = None,
    ) -> AsyncIterator[str]:
        """订阅事件，可按事件名和 JSON 载荷筛选。

        过滤在回放和实时队列两条路径统一执行，避免事件级订阅收到其他事件的
        进度，同时保留 Last-Event-ID 的断线重放语义。
        """
        q: asyncio.Queue = asyncio.Queue(maxsize=100)
        self._subscribers.add(q)
        self._filters[q] = event_filter
        try:
            for frame in self.replay_since(last_event_id):
                if event_filter is None or _frame_matches(frame, event_filter):
                    yield frame
            while True:
                frame = await q.get()
                if event_filter is None or _frame_matches(frame, event_filter):
                    yield frame
        finally:
            self._subscribers.discard(q)
            self._filters.pop(q, None)


def _matches(event: str, payload: dict, event_filter: Callable[[str, dict], bool]) -> bool:
    try:
        return bool(event_filter(event, payload))
    except (TypeError, ValueError, KeyError):
        return False


def _frame_matches(frame: str, event_filter: Callable[[str, dict], bool]) -> bool:
    """从 SSE 帧提取事件和 JSON 数据；坏帧直接丢弃而不影响订阅。"""
    try:
        event = next(line[7:] for line in frame.splitlines() if line.startswith("event: "))
        raw = next(line[6:] for line in frame.splitlines() if line.startswith("data: "))
        return _matches(event, json.loads(raw), event_filter)
    except (StopIteration, ValueError, TypeError, json.JSONDecodeError):
        return False


broker = SSEBroker()

# ---- 跨副本广播（可选）----
# 单进程的 broker 只能推给"连在本进程"的客户端：多副本部署时，诊断在 A 副本跑、
# 前端连在 B 副本，事件就永远到不了页面。这里留一个远端发布钩子，由
# alerts/context.py 在 Redis 可用时注入；未注入时退化为纯本地广播。
_remote_publisher: Callable[[str, dict], Awaitable[None]] | None = None


def set_remote_publisher(fn: Callable[[str, dict], Awaitable[None]] | None) -> None:
    """注入/清除远端发布函数（Redis Pub/Sub）。传 None 即关闭跨副本广播。"""
    global _remote_publisher
    _remote_publisher = fn


async def publish(event: str, data: dict) -> int:
    """对外统一入口：先本地广播，再尽力转发到其它副本。

    远端失败不影响本地推送——SSE 是"尽力而为"的通道，不能因为 Redis 抖动
    让业务写入路径抛错。
    """
    seq = await broker.publish(event, data)
    if _remote_publisher is not None:
        try:
            await _remote_publisher(event, data)
        except Exception:  # noqa: BLE001 - 跨副本广播失败必须降级而非中断
            pass
    return seq


async def publish_local(event: str, data: dict) -> int:
    """只广播给本进程订阅者（relay 收到其它副本的事件后调用，避免回环）。"""
    return await broker.publish(event, data)
