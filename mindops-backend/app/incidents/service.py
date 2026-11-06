"""F-01 -> F-02 触发适配层。

跨进程交接用 Redis Stream（消费者组）而不是 Pub/Sub：Pub/Sub 是至多一次，
F-02 恰好在重启/抖动时错过一条 `alert.raw`，那条告警就会永远停在
`incident_id IS NULL`，没有任何补偿路径。Stream + XACK 提供至少一次投递，
崩溃消费者的 pending 消息由 XAUTOCLAIM 接管；重复投递由 cluster_alert 幂等处理。
"""

from __future__ import annotations

import asyncio
import json
import os
import time
from datetime import datetime

from redis.exceptions import TimeoutError as RedisTimeoutError
from tortoise.expressions import F
from tortoise.transactions import in_transaction

from app.models import Alert
from .cluster import (
    OPEN_STATES,
    component_lock,
    cluster_alert,
    higher_severity,
)
from app.models import Incident
from app.alerts import context
from app.config import model_now, settings
from app.observability.logging import log_event

ALERT_STREAM = "alert.raw"
ALERT_GROUP = "incidents"
# 与生产者 xadd 的 maxlen 保持一致；消费者只负责读，不裁剪。
STREAM_MAXLEN = 10000
# 单条消息处理超过这个空闲时间视为原消费者已失联，允许接管重试。
_CLAIM_MIN_IDLE_MS = 60_000
_CLAIM_INTERVAL_SECONDS = 30.0

_CONSUMER_NAME = f"incident-worker-{os.getpid()}"

_alert_raw_task: asyncio.Task | None = None


async def on_alert(alert: Alert | int) -> dict:
    """处理一条已入库告警；Redis 不可用时由 F-01 直接 await。"""
    return await cluster_alert(alert)


async def consume_alert_raw(payload: dict) -> dict | None:
    """处理 Redis `alert.raw` 载荷；重复投递由 cluster_alert 幂等处理。"""
    try:
        alert_id = int(payload["alert_id"])
    except (KeyError, TypeError, ValueError):
        log_event(30, "incident.alert_raw_invalid", payload=payload)
        return None
    return await cluster_alert(alert_id)


async def _ensure_group(client) -> None:
    try:
        await client.xgroup_create(ALERT_STREAM, ALERT_GROUP, id="0", mkstream=True)
    except Exception as exc:  # noqa: BLE001 - BUSYGROUP 之外的错误交给外层重连
        if "BUSYGROUP" not in str(exc):
            raise


def _field(fields: dict, name: str):
    return fields.get(name, fields.get(name.encode()))


async def _handle_message(client, message_id: str | bytes, fields: dict) -> None:
    raw = _field(fields, "payload")
    if raw is None:
        payload = {"alert_id": _field(fields, "alert_id")}
    else:
        try:
            payload = json.loads(raw.decode() if isinstance(raw, bytes) else raw)
        except (ValueError, TypeError):
            # 坏载荷永远不会变好，ack 掉避免毒消息无限重试
            log_event(30, "incident.alert_raw_invalid", payload=str(raw)[:200])
            await client.xack(ALERT_STREAM, ALERT_GROUP, message_id)
            return
    try:
        await consume_alert_raw(payload)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 - 保持 pending，交给 XAUTOCLAIM 重试
        log_event(40, "incident.alert_raw_consume_failed", message_id=str(message_id), error=str(exc))
        return
    await client.xack(ALERT_STREAM, ALERT_GROUP, message_id)


async def _claim_stale(client) -> None:
    """接管崩溃/超时消费者遗留的 pending 消息（至少一次投递的兜底）。"""
    cursor = "0-0"
    while True:
        try:
            result = await client.xautoclaim(
                ALERT_STREAM, ALERT_GROUP, _CONSUMER_NAME,
                min_idle_time=_CLAIM_MIN_IDLE_MS, start_id=cursor, count=50,
            )
        except Exception as exc:  # noqa: BLE001 - Redis 版本/权限问题不应拖垮主循环
            log_event(30, "incident.alert_raw_claim_failed", error=str(exc))
            return
        cursor = result[0]
        messages = result[1] or []
        for message_id, fields in messages:
            if fields:
                await _handle_message(client, message_id, fields)
        if not messages or cursor in ("0-0", b"0-0"):
            return


async def _consume_alert_raw_loop() -> None:
    """消费 alert.raw；Redis 抖动时退避重连，而不是永久退出订阅。"""
    backoff = 1.0
    next_claim = 0.0
    while True:
        client = context.redis_client
        if client is None:
            # Redis 未就绪（context.startup 时 ping 失败走了降级）：定期重试，
            # context 恢复后无需重启本任务即可恢复消费。
            await asyncio.sleep(5.0)
            continue
        try:
            await _ensure_group(client)
            while True:
                now = time.monotonic()
                if now >= next_claim:
                    await _claim_stale(client)
                    next_claim = now + _CLAIM_INTERVAL_SECONDS
                try:
                    response = await client.xreadgroup(
                        ALERT_GROUP, _CONSUMER_NAME,
                        {ALERT_STREAM: ">"}, count=10, block=5000,
                    )
                except (RedisTimeoutError, asyncio.TimeoutError):
                    # redis-py asyncio：block 窗口内无新消息会抛 TimeoutError
                    # 而不是返回空列表 —— 这是"没有新数据"，不是故障。
                    # sleep 让路：连接半开时该异常瞬间复现，不 sleep 会烧死事件循环。
                    backoff = 1.0
                    await asyncio.sleep(1.0)
                    continue
                backoff = 1.0
                if not response:
                    continue
                for _stream, messages in response:
                    for message_id, fields in messages:
                        await _handle_message(client, message_id, fields)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - 断线重连
            log_event(40, "incident.alert_raw_subscription_failed", error=str(exc))
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 30.0)



def consumer_active() -> bool:
    """本进程的消费任务是否在跑（供 F-01 决定是否等待 incident_id）。"""
    return _alert_raw_task is not None and not _alert_raw_task.done()


async def startup() -> None:
    """M2 Redis 模式消费 alert.raw；M1 与无 Redis 分别由桩/同步兜底处理。"""
    global _alert_raw_task
    if settings.m1_stub_triage or context.redis_client is None:
        return
    if _alert_raw_task is not None and not _alert_raw_task.done():
        return
    _alert_raw_task = asyncio.create_task(_consume_alert_raw_loop())


async def shutdown() -> None:
    """停止 Redis 消费任务，避免应用关闭时遗留连接。

    停机路径一律带超时上限：`_alert_raw_task` 是**进程级全局**，同一进程内重启
    应用（pytest 逐模块起停 TestClient、uvicorn --reload）时它可能属于一个已经
    关闭的事件循环，裸 `await` 会永久等待一个不再被驱动的 future。
    """
    global _alert_raw_task
    task, _alert_raw_task = _alert_raw_task, None
    if task is not None and not task.done():
        task.cancel()
        try:
            if task.get_loop() is asyncio.get_running_loop():
                await asyncio.wait_for(task, timeout=2)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
        except Exception as exc:  # noqa: BLE001
            log_event(30, "incident.alert_raw_task_close_failed", error=str(exc))


async def refresh_deduplicated_alert_incident(
    alert_id: int,
    severity: str,
    *,
    seen_at: datetime | None = None,
) -> Alert | None:
    """刷新 F-01 指纹命中告警及其开放事件，不重新执行聚类或发事件。"""
    alert = await Alert.get_or_none(id=alert_id)
    if alert is None:
        return None

    seen_at = seen_at or model_now()
    component = alert.component or "unknown"
    async with component_lock(component):
        async with in_transaction() as conn:
            current = await Alert.filter(id=alert_id).using_db(conn).first()
            if current is None:
                return None

            # 去重命中：刷新命中时间 + 计数自增（F 表达式，避免读改写竞态）
            await Alert.filter(id=alert_id).using_db(conn).update(
                last_seen_at=seen_at, dedup_count=F("dedup_count") + 1
            )
            if current.incident_id:
                incident = await Incident.filter(
                    id=current.incident_id,
                    status__in=OPEN_STATES,
                ).using_db(conn).first()
                if incident is not None:
                    await Incident.filter(id=incident.id).using_db(conn).update(
                        severity=higher_severity(severity, incident.severity),
                        last_seen_at=seen_at,
                    )

            # 回写内存对象，让调用方拿到的 dedup_count 与实际库值一致
            current.last_seen_at = seen_at
            current.dedup_count = (current.dedup_count or 0) + 1
            return current
