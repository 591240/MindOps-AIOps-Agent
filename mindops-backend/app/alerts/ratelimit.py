"""限流（文档 §6.2 Redis Token Bucket）。

- 全局 100 QPS、webhook 单 IP 30 QPS（§2.2）
- Lua 保证原子性（§6.2 脚本原样）
- 执行顺序：限流检查放在 JSON 解析**之前**（§6.2 V1.1 新增，被拒请求不消耗解析 CPU）

与 dedup 同理，提供 Redis / 内存两个语义等价实现。
"""

from __future__ import annotations

import asyncio
import time

from app.config import settings
from app.observability.logging import log_event

_degraded_logged = False   # 运行期降级只记一次，避免 Redis 宕机时逐请求刷屏

GLOBAL_KEY = "ratelimit:alerts:global"
IP_KEY = "ratelimit:alerts:ip:{ip}"

# ---- §6.2 Token Bucket Lua（原样）----
# KEYS[1]=bucket, ARGV[1]=capacity, ARGV[2]=rate, ARGV[3]=now_ms, ARGV[4]=cost
# ARGV 在 Redis Lua 里**永远是字符串**：算术运算符会隐式转数字，但 `<` 比较不会，
# 直接写 `tokens < ARGV[4]` 必然抛 "attempt to compare number with string"。
# 因此所有数值参数在入口处一次性 tonumber()。
TOKEN_BUCKET_LUA = """
local capacity, rate = tonumber(ARGV[1]), tonumber(ARGV[2])
local now_ms, cost = tonumber(ARGV[3]), tonumber(ARGV[4])
local b = redis.call('HMGET', KEYS[1], 'tokens', 'ts')
local tokens = tonumber(b[1]) or capacity
local ts = tonumber(b[2]) or now_ms
tokens = math.min(capacity, tokens + (now_ms - ts) * rate / 1000)
if tokens < cost then return 0 end
tokens = tokens - cost
redis.call('HMSET', KEYS[1], 'tokens', tokens, 'ts', now_ms)
redis.call('PEXPIRE', KEYS[1], 2000)
return 1
"""


class RateLimiter:
    """统一限流器：先全局后单 IP，任一不通过即拒绝。"""

    def __init__(self, backend) -> None:
        self._b = backend

    async def acquire(self, key: str, capacity: int, rate: float, cost: int = 1) -> bool:
        return await self._b.acquire(key, capacity, rate, cost)

    async def check_global(self, cost: int = 1) -> bool:
        return await self.acquire(
            GLOBAL_KEY, settings.rate_global_qps, settings.rate_global_qps, cost
        )

    async def check_ip(self, ip: str, cost: int = 1) -> bool:
        return await self.acquire(
            IP_KEY.format(ip=ip), settings.rate_ip_qps, settings.rate_ip_qps, cost
        )


class RedisBackend:
    def __init__(self, redis) -> None:
        self._r = redis
        # 审查 #5：Redis 运行期宕机不再让 webhook 整体 500 —— 失败即降级到
        # 进程内令牌桶（精度降级但语义一致），由该实例持续兜底。
        self._fallback = MemoryBackend()

    async def acquire(self, key: str, capacity: int, rate: float, cost: int) -> bool:
        now_ms = int(time.time() * 1000)
        try:
            ok = await self._r.eval(
                TOKEN_BUCKET_LUA, 1, key, capacity, rate, now_ms, cost
            )
        except Exception as exc:  # noqa: BLE001 - Redis 故障 → 降级，不阻断 webhook
            # 降级必须留痕：本模块此前因 ARGV 未 tonumber 让每次 eval 都抛错，
            # 全程静默走内存桶，语义看起来完全正常，所以一直没人发现限流没走 Redis。
            global _degraded_logged
            if not _degraded_logged:
                _degraded_logged = True
                log_event(40, "ratelimit.redis_degraded",
                          error=f"{type(exc).__name__}: {str(exc)[:200]}")
            return await self._fallback.acquire(key, capacity, rate, cost)
        return bool(int(ok))


class MemoryBackend:
    """进程内 Token Bucket，语义与 Redis Lua 版一致。"""

    _MAX_BUCKETS = 20_000   # 审查 #23：按 IP 建桶无界增长的上限，超出淘汰最早桶

    def __init__(self) -> None:
        self._buckets: dict[str, list[float]] = {}   # key -> [tokens, ts_ms]
        self._lock = asyncio.Lock()

    async def acquire(self, key: str, capacity: int, rate: float, cost: int) -> bool:
        now_ms = time.time() * 1000
        async with self._lock:
            if len(self._buckets) >= self._MAX_BUCKETS and key not in self._buckets:
                for stale in list(self._buckets)[: self._MAX_BUCKETS // 10]:
                    self._buckets.pop(stale, None)
            tokens, ts = self._buckets.get(key, [float(capacity), now_ms])
            tokens = min(capacity, tokens + (now_ms - ts) * rate / 1000)
            if tokens < cost:
                self._buckets[key] = [tokens, now_ms]
                return False
            tokens -= cost
            self._buckets[key] = [tokens, now_ms]
            return True


async def build_rate_limiter(redis_client=None) -> RateLimiter:
    if redis_client is not None:
        return RateLimiter(RedisBackend(redis_client))
    return RateLimiter(MemoryBackend())
