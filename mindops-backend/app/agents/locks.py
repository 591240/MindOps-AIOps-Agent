"""F-06 §10.2 / FR-9：分布式确认锁。

处置建议的确认（confirm/close/feedback）必须串行化，否则同一事件的并发请求
可能双写 action 状态或把事件状态改错。优先级：

1. 配置了 `MINDOPS_REDIS_URL` 且连通 → Redis `SETNX` 分布式锁（多实例安全）；
2. Redis 客户端不存在 / 调用抛连接异常 → 进程内 `asyncio.Lock`（单实例安全）。

注意"Redis 在线但 key 被占用"与"Redis 故障"是两件不同的事：
前者说明**别的实例正持锁**，必须等待/重试，绝不能落回本地锁——本地锁对本进程
永远空闲，落回就等于绕过互斥，恰好是这把锁要防的场景。只有 Redis 调用本身
抛异常才允许降级；等待超过 `wait` 秒抛 `LockTimeout`（路由层转 409）。

持有期间按 ttl/3 续期：确认流可能触发复盘与知识回灌，耗时超过初始 ttl 时
若不续期，锁会提前过期并被其他 worker 抢走，互斥同样失效。
"""

from __future__ import annotations

import asyncio
import time
import uuid
from contextlib import asynccontextmanager

_LOCAL_LOCKS: dict[str, asyncio.Lock] = {}

# 确认流是人在页面上点击触发的，等待上限取 5s：足够覆盖另一次确认的
# 正常处理时延，又不会让用户对着转圈无限等。
LOCK_WAIT_SECONDS = 5.0
_RETRY_INTERVAL_SECONDS = 0.05

# 释放锁必须"只删自己持有的那一把"：若持锁期间 ttl 到点被其他 worker 抢到，
# 直接 DELETE 会删掉**别人的锁**，互斥被破坏。Lua 保证"比对 token + 删除"原子。
_RELEASE_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('del', KEYS[1])
else
    return 0
end
"""

_RENEW_SCRIPT = """
if redis.call('get', KEYS[1]) == ARGV[1] then
    return redis.call('expire', KEYS[1], ARGV[2])
else
    return 0
end
"""


class LockTimeout(TimeoutError):
    """等待锁超时（另有实例持锁）。路由层转 409，避免用户误以为确认已生效。"""


def _local_lock(key: str) -> asyncio.Lock:
    return _LOCAL_LOCKS.setdefault(key, asyncio.Lock())


def _redis():
    """惰取 Redis 客户端；未配置或导入失败返回 None。"""
    try:
        from app.alerts.context import redis_client
    except Exception:  # noqa: BLE001 - 环境未初始化时不因锁中断业务
        return None
    return redis_client


async def _renew_until_cancelled(client, key: str, token: str, ttl: int) -> None:
    """持锁期间续期；锁已不属于自己（被抢/被删）时退出。"""
    interval = max(0.5, ttl / 3)
    while True:
        await asyncio.sleep(interval)
        try:
            renewed = await client.eval(_RENEW_SCRIPT, 1, key, token, ttl)
        except Exception:  # noqa: BLE001 - Redis 抖动不再续期，由 ttl 兜底
            return
        if not renewed:
            return


@asynccontextmanager
async def distributed_lock(key: str, ttl: int = 30, wait: float = LOCK_WAIT_SECONDS):
    """获取锁，退出时释放。仅在 Redis 调用异常时降级到进程内锁。"""
    client = _redis()
    if client is not None:
        token = uuid.uuid4().hex
        deadline = time.monotonic() + max(0.0, wait)
        while True:
            try:
                acquired = bool(await client.set(key, token, nx=True, ex=ttl))
            except Exception:  # noqa: BLE001 - Redis 故障 → 降级，不阻断确认流
                break
            if acquired:
                renewer = asyncio.create_task(_renew_until_cancelled(client, key, token, ttl))
                try:
                    yield
                finally:
                    renewer.cancel()
                    try:
                        await renewer
                    except asyncio.CancelledError:
                        pass
                    # 只释放自己持有的锁（比对 token 后再删，避免误删他人的锁）
                    try:
                        await client.eval(_RELEASE_SCRIPT, 1, key, token)
                    except Exception:  # noqa: BLE001 - 释放失败只留日志，不影响业务
                        pass
                return
            # 锁被其他实例持有 = 正常工作状态，等待而不是降级
            if time.monotonic() >= deadline:
                raise LockTimeout(f"lock busy: {key}")
            await asyncio.sleep(_RETRY_INTERVAL_SECONDS)
    lock = _local_lock(key)
    try:
        await asyncio.wait_for(lock.acquire(), timeout=max(0.0, wait))
    except asyncio.TimeoutError as exc:
        raise LockTimeout(f"lock busy: {key}") from exc
    try:
        yield
    finally:
        lock.release()


def confirm_lock(incident_id: int):
    """F-06 §5.4：确认流专用锁键 `confirm:lock:{incident_id}`。"""
    return distributed_lock(f"confirm:lock:{incident_id}", ttl=30)
