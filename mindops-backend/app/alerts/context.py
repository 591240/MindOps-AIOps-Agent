"""F-01 运行期单例（由 main.py lifespan 初始化）。"""

from __future__ import annotations

import asyncio
import json

from app.alerts import sse
from app.alerts.dedup import DedupBackend, MemoryDedup
from app.alerts.ratelimit import MemoryBackend, RateLimiter

redis_client = None                      # redis.asyncio.Redis | None
dedup: DedupBackend = MemoryDedup()
limiter: RateLimiter = RateLimiter(MemoryBackend())
redis_pubsub_enabled = False

# SSE 跨副本转发频道与后台任务
SSE_CHANNEL = "mindops:sse"
_relay_task: asyncio.Task | None = None


async def _relay_sse(client) -> None:
    """订阅 Redis 频道，把其它副本发出的 SSE 事件转投给本进程的订阅者。

    本进程 publish 时走 `sse.publish`（本地 + 远端），relay 收到消息后走
    `sse.publish_local`（只本地），因此不会形成回环。
    """
    pubsub = client.pubsub()
    try:
        await pubsub.subscribe(SSE_CHANNEL)
        async for message in pubsub.listen():
            if message.get("type") != "message":
                continue
            try:
                payload = json.loads(message["data"])
                event = payload.get("event")
                data = payload.get("data")
                if isinstance(event, str) and isinstance(data, dict):
                    await sse.publish_local(event, data)
            except (ValueError, TypeError, KeyError):
                # 坏消息直接丢弃：一个副本的序列化问题不该拖垮其它副本的订阅
                continue
    except asyncio.CancelledError:
        raise
    except Exception:
        # Redis 断开：放弃跨副本广播，本地推送不受影响
        return
    finally:
        try:
            await pubsub.unsubscribe(SSE_CHANNEL)
            await pubsub.aclose()
        except Exception:
            pass


async def startup() -> None:
    """按配置装配去重 / 限流后端；Redis 可用则一并启用 Pub/Sub（§2.2）。"""
    global redis_client, dedup, limiter, redis_pubsub_enabled, _relay_task

    from app.alerts.dedup import build_dedup
    from app.alerts.ratelimit import build_rate_limiter
    from app.config import settings

    redis_client = None
    if settings.redis_url:
        import redis.asyncio as redis_async
        from app.observability.logging import log_event

        client = redis_async.from_url(
            settings.redis_url, decode_responses=True, socket_connect_timeout=2
        )
        # 容器编排虽等 redis healthy 才起 backend，但健康检查与真正可服务
        # 之间仍有窗口；ping 失败重试 10 次（共 ~20s），仍失败才降级并大声记录——
        # 静默吞掉会让 M2 消费循环永远走 client-None 分支且无人知晓。
        for attempt in range(1, 11):
            try:
                await client.ping()
                redis_client = client
                break
            except Exception as exc:
                log_event(30, "redis.startup_ping_failed", attempt=attempt, error=str(exc))
                await asyncio.sleep(2.0)
        if redis_client is None:
            log_event(40, "redis.startup_failed_degraded",
                      hint="去重/限流降级为内存实现，alert.raw 消费循环将空转等待")

    dedup = await build_dedup(redis_client)
    limiter = await build_rate_limiter(redis_client)
    redis_pubsub_enabled = redis_client is not None

    # SSE 跨副本：有 Redis 就注入远端发布钩子 + 起转发任务
    if redis_client is not None:
        async def _remote_publish(event: str, data: dict) -> None:
            await redis_client.publish(
                SSE_CHANNEL,
                json.dumps({"event": event, "data": data}, ensure_ascii=False, default=str),
            )

        sse.set_remote_publisher(_remote_publish)
        _relay_task = asyncio.create_task(_relay_sse(redis_client))
    else:
        sse.set_remote_publisher(None)


async def shutdown() -> None:
    global redis_client, _relay_task
    sse.set_remote_publisher(None)
    if _relay_task is not None:
        _relay_task.cancel()
        try:
            await _relay_task
        except (asyncio.CancelledError, Exception):
            pass
        _relay_task = None
    if redis_client is not None:
        await redis_client.aclose()
        redis_client = None
    # 自建客户端（tests/build_dedup 无参调用）需要显式关闭；共享客户端已在上面关过
    close_dedup = getattr(dedup, "aclose", None)
    if close_dedup is not None:
        try:
            await close_dedup()
        except Exception:
            pass
