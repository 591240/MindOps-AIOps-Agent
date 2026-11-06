"""F-10 Redis 黑名单与限流服务（§5.4 + §5.5）。

三种 Redis 用途（§7.1）：
1. Refresh token 黑名单：登出 / 轮换后拉黑旧 token（jti → 过期时间）
2. 密码错误计数：5 次错误锁定 30 分钟（user_id → count + TTL）
3. 登录入口按来源 IP 的滑动窗口限流（见 `routers/auth.py`）

Redis 不可用时降级为内存 store（功能不受影响，精度降低）。
安全修复（审查 #3）：降级判定不再只在首次调用时做一次——Redis 在运行期宕机时，
每次操作失败都会自动切回内存 store 并就地重试，避免死连接把全部鉴权请求打成 500。
Redis 恢复后的重连交由下次进程重启（黑名单/计数丢失可接受：黑名单缺失的窗口
等于"登出令牌在剩余 TTL 内仍可用"，由 SSE/refresh 侧的 status 校验兜底）。
"""

from __future__ import annotations

import time
from typing import Optional

from app.config import settings

# 内存降级 store 的容量上限：超出后丢弃最早条目（防止长期运行内存无界增长）。
_MEMORY_MAX_ENTRIES = 50_000


class _MemoryStore:
    """Redis 不可用时的内存降级（F-10 §6.3 降级链 ③）。"""

    def __init__(self):
        self._store: dict[str, tuple[str, float]] = {}

    def _evict_if_full(self):
        if len(self._store) >= _MEMORY_MAX_ENTRIES:
            for k in list(self._store)[: _MEMORY_MAX_ENTRIES // 10]:
                self._store.pop(k, None)

    async def get(self, key: str) -> Optional[str]:
        val, exp = self._store.get(key, (None, 0))
        if val and time.time() < exp:
            return val
        self._store.pop(key, None)
        return None

    async def set(self, key: str, value: str, ex: int = 3600):
        self._evict_if_full()
        self._store[key] = (value, time.time() + ex)

    async def incr(self, key: str) -> int:
        val, exp = self._store.get(key, ("0", 0))
        if time.time() >= exp:
            self._evict_if_full()
            self._store[key] = ("1", time.time() + settings.password_lockout_seconds)
            return 1
        new_val = int(val) + 1
        self._store[key] = (str(new_val), exp)
        return new_val

    async def expire(self, key: str, seconds: int):
        # 修复（审查 #23）：key 不存在时不再凭空创建条目
        item = self._store.get(key)
        if item is not None:
            self._store[key] = (item[0], time.time() + seconds)

    async def delete(self, *keys: str):
        for k in keys:
            self._store.pop(k, None)


_store = None          # 活跃 store：Redis 连接或 _MemoryStore
_redis = None          # 最后一次成功连接的 Redis 客户端（用于运行期恢复尝试）
_memory = _MemoryStore()


def _use_memory(reason: str) -> _MemoryStore:
    global _store, _redis
    _redis = None
    _store = _memory
    return _store


async def _get_store():
    global _store, _redis
    if _store is not None:
        return _store
    try:
        import redis.asyncio as aioredis
        _redis = await aioredis.from_url(settings.redis_url, decode_responses=True)
        await _redis.ping()
        _store = _redis  # type: ignore
        return _store
    except Exception:
        return _use_memory("connect_failed")


async def _call(op_name: str, run):
    """统一出口：Redis 操作失败 → 永久切回内存 store 并就地重试（审查 #3）。"""
    store = await _get_store()
    if store is _memory:
        return await run(store)
    try:
        return await run(store)
    except Exception:
        return await run(_use_memory(f"{op_name}_failed"))


# ---- Refresh token 黑名单（§5.5）----


async def blacklist_token(jti: str, exp_seconds: int):
    """将已用 refresh token 的 jti 拉黑（登出 / 轮换后调用）。"""

    async def _run(store):
        await store.set(f"bl:{jti}", "1", ex=exp_seconds)

    await _call("bl_set", _run)


async def is_token_blacklisted(jti: str) -> bool:
    """检查 jti 是否在黑名单中。"""

    async def _run(store):
        return (await store.get(f"bl:{jti}")) is not None

    return await _call("bl_get", _run)


# ---- 密码错误计数（§5.4 双轨锁定）----


async def record_password_failure(user_id: int) -> int:
    """密码错误 +1，返回当前累计次数。满阈值时触发锁定。"""
    key = f"pw_fail:{user_id}"

    async def _run(store):
        count = await store.incr(key)
        if count == 1:
            await store.expire(key, settings.password_lockout_seconds)
        return count

    return await _call("pw_fail", _run)


async def get_password_failures(user_id: int) -> int:
    async def _run(store):
        val = await store.get(f"pw_fail:{user_id}")
        return int(val) if val else 0

    return await _call("pw_get", _run)


async def clear_password_failures(user_id: int):
    async def _run(store):
        await store.delete(f"pw_fail:{user_id}")

    await _call("pw_clear", _run)
