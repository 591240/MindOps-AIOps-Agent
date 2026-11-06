"""Redis 后端真实路径回归：去重认领 / 令牌桶限流 / 分布式锁。

这个文件此前不存在，后果是限流的 Lua 写法错误躲过了整套测试 —— 测试环境不连
Redis，所有断言都跑在进程内降级实现上；而 `RedisBackend.acquire` 把 eval 异常
`except` 掉直接兜回内存桶，语义看起来完全正常，于是"限流走没走 Redis"这件事
既没日志也没测试能证明。

需要真实 Redis：连不上整模块 skip，不伪装成通过。
测试库固定用 db 13，不碰应用实际用的 db。
"""

import os

os.environ.setdefault("MINDOPS_MYSQL_DB", "mindops_redis_test")

import pytest
import pytest_asyncio
import redis.asyncio as aioredis

from app.agents import locks
from app.alerts import context
from app.alerts import dedup as dedup_mod
from app.alerts import ratelimit

pytestmark = pytest.mark.asyncio(loop_scope="module")

# conftest 把应用的 MINDOPS_REDIS_URL 强制置空（M2 异步转发模式与同步契约不兼容，
# 见 conftest 注释），所以这里不能用 settings.redis_url —— 它在本套件里恒为空。
# Redis 分支靠这个**独立**的连接串显式验证；改端口/库用 MINDOPS_REDIS_TEST_URL 覆盖。
TEST_URL = os.environ.get("MINDOPS_REDIS_TEST_URL", "redis://127.0.0.1:16379/13")


def _test_url() -> str:
    return TEST_URL


async def _reachable() -> bool:
    url = _test_url()
    if not url:
        return False
    client = aioredis.from_url(url, socket_connect_timeout=1.5)
    try:
        await client.ping()
        return True
    except Exception:
        return False
    finally:
        await client.aclose()


@pytest_asyncio.fixture(scope="module", loop_scope="module")
async def redis_db():
    if not await _reachable():
        pytest.skip(f"Redis 不可用（{_test_url()}），跳过 Redis 分支用例")
    client = aioredis.from_url(_test_url(), decode_responses=True)
    await client.flushdb()
    yield client
    await client.flushdb()
    await client.aclose()


# ---------------------------------------------------------------------------
# 限流：必须证明桶落在 Redis 里，而不是被静默兜回内存
# ---------------------------------------------------------------------------

async def test_rate_limiter_bucket_lands_in_redis(redis_db):
    limiter = await ratelimit.build_rate_limiter(redis_db)
    assert isinstance(limiter._b, ratelimit.RedisBackend)

    verdicts = [await limiter.acquire("t:bucket", capacity=3, rate=0.0) for _ in range(5)]
    assert verdicts == [True, True, True, False, False]

    # 曾经的 bug 形态就是"这三条全对，但键根本不在 Redis 里"
    assert await redis_db.exists("t:bucket") == 1
    assert float(await redis_db.hget("t:bucket", "tokens")) == 0
    assert await redis_db.pttl("t:bucket") > 0


async def test_rate_limiter_global_and_ip_buckets_are_distinct(redis_db):
    limiter = await ratelimit.build_rate_limiter(redis_db)
    assert await limiter.check_global() is True
    assert await limiter.check_ip("10.1.2.3") is True
    assert set(await redis_db.keys("ratelimit:*")) == {
        ratelimit.GLOBAL_KEY,
        ratelimit.IP_KEY.format(ip="10.1.2.3"),
    }


async def test_rate_limiter_refills_by_rate(redis_db):
    """回填语义：ts 拨到过去，桶应重新可用（证明 rate 参与的是 Redis 侧计算）。"""
    limiter = await ratelimit.build_rate_limiter(redis_db)
    assert await limiter.acquire("t:refill", capacity=1, rate=10.0) is True
    assert await limiter.acquire("t:refill", capacity=1, rate=10.0) is False
    # 10 tokens/s，把 ts 往前挪 500ms → 应回补 5 个 token，够再扣 1
    past = int(await redis_db.hget("t:refill", "ts")) - 500
    await redis_db.hset("t:refill", "ts", past)
    assert await limiter.acquire("t:refill", capacity=1, rate=10.0) is True


# ---------------------------------------------------------------------------
# 去重：claim / bind / peek / release
# ---------------------------------------------------------------------------

async def test_dedup_build_prefers_redis_when_configured(monkeypatch):
    """wiring：配了可达的 redis_url 就必须选 RedisDedup，不能默默用内存实现。"""
    # 这个用例不走 redis_db fixture，但同样需要真实 Redis 才能成立：
    # 无 Redis 时 build_dedup() 本来就该返回 MemoryDedup，断言必然失败。
    # 之前漏了这层判断，导致"没有 Redis 的机器上跑测试套件必红"。
    if not await _reachable():
        pytest.skip(f"Redis 不可用（{_test_url()}），跳过 Redis 分支用例")
    # 必须 patch dedup 自己持有的那个 settings 对象：conftest 会在模块边界重建
    # settings 单例并重新绑定各模块引用，patch 本文件的旧引用会静默失效。
    monkeypatch.setattr(dedup_mod.settings, "redis_url", TEST_URL)
    assert isinstance(await dedup_mod.build_dedup(), dedup_mod.RedisDedup)


async def test_dedup_claim_bind_release(redis_db):
    backend = dedup_mod.RedisDedup(redis_db)
    key = f"alert:fp:lock:{'t-fp'}"

    claimed, owner = await backend.claim("t-fp")
    assert claimed is True and owner == "PENDING"
    # 同一指纹二次认领必须失败 —— 这是 webhook 重复投递的唯一拦截点
    again, _ = await backend.claim("t-fp")
    assert again is False
    assert await redis_db.exists(key) == 1

    await backend.bind("t-fp", 9001)
    assert await backend.peek("t-fp") == "9001"      # 占位符被真实 id 替换
    assert await redis_db.ttl(key) > 0               # bind 用 keepttl，不重置冷却窗口

    await backend.release("t-fp")
    assert await redis_db.exists(key) == 0


# ---------------------------------------------------------------------------
# 分布式锁：获取、互斥、以及"只释放自己那把"
# ---------------------------------------------------------------------------

async def test_distributed_lock_roundtrip(redis_db, monkeypatch):
    monkeypatch.setattr(context, "redis_client", redis_db)
    key = "t:lock"
    async with locks.distributed_lock(key, ttl=5):
        assert await redis_db.get(key) is not None
        assert await redis_db.ttl(key) > 0
        # 持锁期间第二把 SET NX 必须失败
        assert await redis_db.set(key, "intruder", nx=True, ex=5) is None
    assert await redis_db.exists(key) == 0


async def test_lock_release_never_deletes_another_holders_token(redis_db, monkeypatch):
    """compare-and-del 回归：锁超时被抢后，原持有者退出不得删掉新持有者的锁。"""
    monkeypatch.setattr(context, "redis_client", redis_db)
    key = "t:lock2"
    async with locks.distributed_lock(key, ttl=5):
        await redis_db.delete(key)                    # 模拟 TTL 到点、锁被回收
        await redis_db.set(key, "OTHER-TOKEN", ex=30)  # B 随后拿到了锁
    assert await redis_db.get(key) == "OTHER-TOKEN"


async def test_lock_falls_back_to_local_when_redis_gone(redis_db, monkeypatch):
    """Redis 故障不得阻断确认流：降级到进程内锁，且临界区仍然互斥。"""
    import asyncio

    class DeadClient:
        async def set(self, *a, **k):
            raise ConnectionError("redis down")

        async def eval(self, *a):
            raise ConnectionError("redis down")

    monkeypatch.setattr(context, "redis_client", DeadClient())
    order: list[str] = []

    async def worker(tag: str) -> None:
        async with locks.distributed_lock("t:lock3", ttl=5):
            order.append(f"enter:{tag}")
            await asyncio.sleep(0.01)
            order.append(f"exit:{tag}")

    await asyncio.gather(worker("a"), worker("b"))
    assert order in (["enter:a", "exit:a", "enter:b", "exit:b"],
                     ["enter:b", "exit:b", "enter:a", "exit:a"])


async def test_busy_redis_lock_waits_instead_of_using_local_lock(monkeypatch):
    """SET NX 失败 = 别的实例持锁，必须等待重试，不能因本地锁空闲就放行。"""
    import time

    class BusyThenFree:
        def __init__(self) -> None:
            self.calls = 0

        async def set(self, key, token, nx=True, ex=30):
            self.calls += 1
            return None if self.calls < 3 else True

        async def eval(self, *a):
            return 1

    fake = BusyThenFree()
    monkeypatch.setattr(context, "redis_client", fake)
    started = time.monotonic()
    async with locks.distributed_lock("t:busy", ttl=5):
        pass
    assert fake.calls == 3
    assert time.monotonic() - started >= 0.09  # 至少经历两轮 50ms 重试


async def test_busy_redis_lock_raises_timeout(monkeypatch):
    """持续被占用时抛 LockTimeout（路由层转 409），而不是静默并发进入临界区。"""

    class AlwaysBusy:
        async def set(self, *a, **k):
            return None

    monkeypatch.setattr(context, "redis_client", AlwaysBusy())
    with pytest.raises(locks.LockTimeout):
        async with locks.distributed_lock("t:busy2", ttl=5, wait=0.12):
            raise AssertionError("critical section must not be entered")
