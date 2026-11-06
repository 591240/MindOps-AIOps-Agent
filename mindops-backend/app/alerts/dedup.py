"""指纹去重（文档 §6.1）。

语义（写死执行，§6.1）：
  1. 指纹命中且在 1h TTL 内 → 不插入新行；UPDATE alerts SET last_seen_at=NOW() WHERE id=<旧 id>
  2. 未命中（或 TTL 已过）→ 插入新行；落库成功后把锁值从 PENDING 改写为真实 alert_id
  3. 响应 alert_ids 包含本批所有涉及行（新建 + 命中旧行）

实现说明（假设标注）：
  文档要求 Redis + Lua 原子认领。本项目[假设]本地 Redis 暂未启动，
  因此提供两个语义等价的实现：
    - RedisDedup ：走文档 §6.1 的 Lua 脚本（生产/Redis 可用时自动启用）
    - MemoryDedup：进程内 asyncio 实现，语义与 Redis 版一致（本地开发/降级用）
  通过 `MINDOPS_REDIS_URL` 是否配置自动选择，无需改动调用方代码。
"""

from __future__ import annotations

import asyncio
import time
from typing import Protocol

from app.config import settings

# ---- §6.1 claim_fingerprint.lua（原样）----
CLAIM_LUA = """
local v = redis.call('GET', KEYS[1])
if v then
  return {0, v}
end
redis.call('SET', KEYS[1], 'PENDING', 'EX', ARGV[1])
return {1, 'PENDING'}
"""

PENDING = "PENDING"


class DedupBackend(Protocol):
    """去重后端统一接口。"""

    @property
    def degraded(self) -> bool:
        """True 表示处于降级（无 Redis）状态。"""

    async def claim(self, fp: str) -> tuple[bool, str | None]:
        """返回 (is_first, owner)。

        is_first=True  → owner='PENDING'，调用方落库后 bind(真实 id)
        is_first=False → owner=已有 alert_id，调用方刷新该行 last_seen_at
        """

    async def peek(self, fp: str) -> str | None:
        """只读当前 owner（P1-1）。

        与 claim 的区别：不占位、不改 TTL。用于并发窗口内等待持锁方
        把 'PENDING' bind 成真实 alert_id，避免"抢锁失败即释放 + 重插"
        导致的重复告警。
        """

    async def bind(self, fp: str, alert_id: int) -> None:
        """落库成功后回填：PENDING → 真实 id（keepttl）。"""

    async def release(self, fp: str) -> None:
        """落库失败时释放，防悬挂锁。"""


class RedisDedup:
    """Redis + Lua 原子认领（§6.1 原样实现）。"""

    def __init__(self, redis, *, owns_client: bool = False) -> None:
        self._r = redis
        # owns_client=True 表示客户端由本类创建，关闭时一并释放。
        # 应用运行时由 context 传入共享客户端（owns_client=False），
        # 避免 build_dedup 私自再开一个永不关闭的连接。
        self._owns_client = owns_client
        # 审查 #5：Redis 运行期宕机时降级到进程内等价实现，避免 webhook 整体 500
        #（降级后指纹窗口语义一致，仅精度与跨进程能力受限）。
        self._fallback = MemoryDedup()

    @property
    def degraded(self) -> bool:
        return False

    async def aclose(self) -> None:
        if self._owns_client:
            await self._r.aclose()

    def _key(self, fp: str) -> str:
        return f"alert:fp:lock:{fp}"

    async def claim(self, fp: str) -> tuple[bool, str | None]:
        try:
            first, owner = await self._r.eval(
                CLAIM_LUA, 1, self._key(fp), settings.dedup_ttl_seconds
            )
        except Exception:
            return await self._fallback.claim(fp)
        owner = owner.decode() if isinstance(owner, bytes) else owner
        return bool(first), owner

    async def peek(self, fp: str) -> str | None:
        try:
            owner = await self._r.get(self._key(fp))
        except Exception:
            return await self._fallback.peek(fp)
        return owner.decode() if isinstance(owner, bytes) else owner

    async def bind(self, fp: str, alert_id: int) -> None:
        try:
            await self._r.set(self._key(fp), alert_id, xx=True, keepttl=True)
        except Exception:
            await self._fallback.bind(fp, alert_id)

    async def release(self, fp: str) -> None:
        try:
            await self._r.delete(self._key(fp))
        except Exception:
            await self._fallback.release(fp)


class MemoryDedup:
    """进程内等价实现（无 Redis 时使用）。

    语义与 RedisDedup 一致：TTL 1h、claim 原子占位、bind 回填、release 释放。
    用 asyncio.Lock 保证并发下的 claim 原子性（对应 Lua 的原子性）。
    """

    _MAX_ENTRIES = 100_000   # 审查 #23：唯一指纹持续进入，超出后淘汰最早条目

    def __init__(self, ttl: int | None = None) -> None:
        self._ttl = ttl or settings.dedup_ttl_seconds
        self._store: dict[str, tuple[str, float]] = {}   # fp -> (owner, expire_at)
        self._lock = asyncio.Lock()

    @property
    def degraded(self) -> bool:
        return True

    def _purge(self, fp: str) -> None:
        item = self._store.get(fp)
        if item and item[1] < time.monotonic():
            del self._store[fp]

    def _evict_if_full(self) -> None:
        if len(self._store) >= self._MAX_ENTRIES:
            for fp in list(self._store)[: self._MAX_ENTRIES // 10]:
                self._store.pop(fp, None)

    async def claim(self, fp: str) -> tuple[bool, str | None]:
        async with self._lock:
            self._purge(fp)
            self._evict_if_full()
            if fp in self._store:
                return False, self._store[fp][0]
            self._store[fp] = (PENDING, time.monotonic() + self._ttl)
            return True, PENDING

    async def peek(self, fp: str) -> str | None:
        async with self._lock:
            self._purge(fp)
            item = self._store.get(fp)
            return item[0] if item else None

    async def bind(self, fp: str, alert_id: int) -> None:
        async with self._lock:
            self._purge(fp)
            if fp in self._store:
                owner, exp = self._store[fp]
                if owner == PENDING:                      # 仅占位态才回填（对应 xx=True）
                    self._store[fp] = (str(alert_id), exp)

    async def release(self, fp: str) -> None:
        async with self._lock:
            self._store.pop(fp, None)


async def build_dedup(client=None) -> DedupBackend:
    """按配置选择后端：配了 MINDOPS_REDIS_URL 且连通 → Redis；否则内存降级。

    `client` 由调用方（context.startup）传入已连通的共享客户端；未传时
    自建并标记 ownership，由 `aclose()` 负责关闭。
    """
    if client is not None:
        return RedisDedup(client)
    if settings.redis_url:
        try:
            import redis.asyncio as redis_async

            client = redis_async.from_url(
                settings.redis_url, decode_responses=True, socket_connect_timeout=2
            )
            await client.ping()
            return RedisDedup(client, owns_client=True)
        except Exception:                                  # 连不上就降级，不阻断启动
            pass
    return MemoryDedup()
