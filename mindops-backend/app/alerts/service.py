"""告警入库管线（文档 §7.1）。

流程（§7.1 注释，逐条实现）：
  1. 限流（先于解析）
  2. 读 body（限长 1MB）→ JSON 解析（batch ≤ 200）
  3. for alert in batch: normalize → claim_fingerprint
     - 新行 → 插入 + backfill_alert_id
     - 命中 → UPDATE last_seen_at（不插新行）
  4. M1: assign_incident 回填 incident_id（批量取严重度最高者返回）
  5. 入队 alert.raw（Redis Stream，至少一次）+ SSE event: alert
  6. 结构化日志 + metrics
  7. 返回
"""

from __future__ import annotations

import asyncio
import json
import time
from datetime import datetime

from tortoise.expressions import F

from app.alerts import context
from app.alerts import sse
from app.alerts.stub_triage import assign_incident
from app.config import model_now, settings
from app.models import Alert
from app.observability import metrics
from app.observability.logging import Timer, log_event, new_trace_id

# 严重度排序（P0 最高）→ 混合批次取最高者的 incident_id（§3.1）
SEV_ORDER = {"P0": 0, "P1": 1, "P2": 2, "P3": 3}


def _now() -> datetime:
    # 必须与 Tortoise auto_now_add 同口径（上海墙上钟）。用 datetime.now() 会在
    # UTC 容器（Docker 默认）里比库中时间小 8 小时，把聚类/统计窗口整体打偏。
    return model_now()


ALERT_STREAM = "alert.raw"
ALERT_STREAM_MAXLEN = 10000
# M2：告警先入队、由 F-02 异步聚类。这里短暂等待真实 incident_id，使
# webhook/手工上报的响应结构与同步路径一致（此前固定返回 null）。
_M2_WAIT_SECONDS = 1.0
_M2_POLL_SECONDS = 0.02
# 消费端整体失联时不要在每条告警上各等 1 秒（200 条批次会放大成几分钟）；
# 超时一次后静默跳过等待，消息仍在 Stream 中，消费端恢复后会补处理。
_m2_skip_wait_until = 0.0


def _m2_consumer_active() -> bool:
    try:
        from app.incidents import service as incident_service
    except Exception:  # noqa: BLE001 - 循环导入/未初始化时按不可用处理
        return False
    return incident_service.consumer_active()


async def _enqueue_alert_raw(alert: Alert) -> bool:
    """把 alert.raw 写入 Redis Stream；失败返回 False，由调用方同步兜底。"""
    if context.redis_client is None:
        return False
    try:
        await context.redis_client.xadd(
            ALERT_STREAM,
            {
                "payload": json.dumps(
                    {
                        "alert_id": alert.id,
                        "fingerprint": alert.fingerprint,
                        "source": alert.source,
                        "component": alert.component,
                        "severity": alert.severity,
                        "title": alert.title,
                        "incident_id": alert.incident_id,
                        "created_at": str(alert.created_at),
                    },
                    ensure_ascii=False,
                )
            },
            maxlen=ALERT_STREAM_MAXLEN,
            approximate=True,
        )
        return True
    except Exception as exc:  # 入队失败不阻断入库
        log_event(30, "alert.enqueue_failed", error=str(exc))
        return False


async def _wait_for_incident(alert_id: int) -> int | None:
    """短暂轮询本进程消费结果；消费端不可用/超时返回 None（消息仍在队列）。"""
    global _m2_skip_wait_until
    if not _m2_consumer_active():
        return None
    if time.monotonic() < _m2_skip_wait_until:
        return None
    deadline = time.monotonic() + _M2_WAIT_SECONDS
    while True:
        incident_id = await Alert.filter(id=alert_id).values_list("incident_id", flat=True).first()
        if incident_id:
            return incident_id
        if time.monotonic() >= deadline:
            _m2_skip_wait_until = time.monotonic() + 10.0
            return None
        await asyncio.sleep(_M2_POLL_SECONDS)


async def _push_sse(alert: Alert, deduped: bool) -> None:
    await sse.publish(
        "alert",
        {
            "id": alert.id,
            "source": alert.source,
            "component": alert.component,
            "severity": alert.severity,
            "title": alert.title,
            "fingerprint": alert.fingerprint,
            "incident_id": alert.incident_id,
            "deduped": deduped,
            "created_at": str(alert.created_at),
            "last_seen_at": str(alert.last_seen_at),
        },
    )


# P1-1：并发窗口内等待持锁方 bind 出真实 alert_id 的参数
_PENDING_WAIT_SECONDS = 1.0
_PENDING_POLL_SECONDS = 0.02


async def _await_bound_alert_id(fp: str) -> int | None:
    """等待"并发持有者"把 PENDING 回填为真实 alert_id（P1-1）。

    背景：claim 失败时 owner 可能是占位值 'PENDING'（对方刚认领、还没落库）。
    原实现对 `int('PENDING')` 失败的处理是**立刻 release 锁并递归重入**，
    于是自己也可能拿到 PENDING 并插入新行 → 同一指纹出现两条告警。

    这里改为：在有限时间窗内只读轮询 owner（peek，不抢锁、不改 TTL），
    - 拿到真实 id → 走既有"命中"分支刷新 last_seen_at；
    - 期间 owner 消失（对方落库失败已释放）→ 返回 None，交由外层重入；
    - 超时仍为 PENDING → 返回 None（视为持锁方异常，沿用原重入逻辑）。
    """
    peek = getattr(context.dedup, "peek", None)
    if peek is None:
        return None

    deadline = time.monotonic() + _PENDING_WAIT_SECONDS
    while True:
        await asyncio.sleep(_PENDING_POLL_SECONDS)
        try:
            owner = await peek(fp)
        except Exception:                                  # 后端异常：不阻塞主流程
            return None
        if owner is None:
            return None
        try:
            return int(owner)
        except (TypeError, ValueError):
            pass                                           # 仍是 PENDING，继续等
        if time.monotonic() >= deadline:
            return None


async def ingest_one(normalized: dict, *, trigger_pipeline: bool = True) -> dict:
    """处理单条已归一化的告警。返回 {alert_id, is_new, incident_id, deduped}。"""
    fp = normalized["fingerprint"]
    t = Timer()
    trace_id = new_trace_id()

    is_first, owner = await context.dedup.claim(fp)

    # ---- 命中：不插新行，仅刷新 last_seen_at（§6.1 语义 1）----
    if not is_first:
        try:
            old_id = int(owner)
        except (TypeError, ValueError):
            old_id = None

        if not old_id:
            # P1-1：owner 为 'PENDING' —— 等待并发持有者回填，而非立刻释放重插
            old_id = await _await_bound_alert_id(fp)

        if old_id:
            if trigger_pipeline:
                # F-01 不新增 alert 行，但该上报仍代表原事件持续活跃。
                # 事务内同步 last_seen_at 和最高严重度，且不重跑聚类/广播事件更新。
                from app.incidents.service import refresh_deduplicated_alert_incident

                old = await refresh_deduplicated_alert_incident(
                    old_id,
                    normalized["severity"],
                    seen_at=_now(),
                )
            else:
                # 去重命中：刷新命中时间 + 计数自增（F 表达式，避免读改写竞态）
                await Alert.filter(id=old_id).update(
                    last_seen_at=_now(), dedup_count=F("dedup_count") + 1
                )
                old = await Alert.get_or_none(id=old_id)
            if old is None:                       # 锁还在但行已被归档 → 视作首发
                await context.dedup.release(fp)
                return await ingest_one(normalized, trigger_pipeline=trigger_pipeline)
            result = {
                "alert_id": old.id,
                "is_new": False,
                "incident_id": old.incident_id,
                "deduped": True,
            }
        else:
            await context.dedup.release(fp)
            return await ingest_one(normalized, trigger_pipeline=trigger_pipeline)
    else:
        # ---- 首发：插新行（§6.1 语义 2）----
        try:
            alert = await Alert.create(
                source=normalized["source"],
                component=normalized["component"],
                severity=normalized["severity"],
                title=normalized["title"],
                detail=normalized["detail"],
                fingerprint=fp,
                raw_payload=json.dumps(
                    normalized.get("raw_payload") or {}, ensure_ascii=False
                ),
                labels_json=normalized.get("labels_json") or {},
                last_seen_at=_now(),
                created_at=_now(),
                # F-12：显式携带来源被监控目标，作为任务归属推导的唯一依据（30-F-12 §5.3）
                integration_id=normalized.get("integration_id"),
            )
            await context.dedup.bind(fp, alert.id)
        except Exception:
            await context.dedup.release(fp)       # 落库失败释放，防悬挂锁
            raise

        # M1 桩回填或 F-02 同步兜底（§6.4）。
        # Redis 已启用时，F-02 通过 alert.raw 消费；无 Redis 才在进程内同步调用。
        incident_id = None
        if trigger_pipeline and settings.m1_stub_triage:
            incident_id = await assign_incident(normalized)
            await Alert.filter(id=alert.id).update(incident_id=incident_id)
            alert.incident_id = incident_id
        elif trigger_pipeline and context.redis_client is None:
            # 无 Redis 时由 F-01 直接调用 F-02，保证本地链路不中断。
            from app.incidents.service import on_alert

            clustered = await on_alert(alert)
            incident_id = clustered["incident_id"]
            alert.incident_id = incident_id

        result = {
            "alert_id": alert.id,
            "is_new": True,
            "incident_id": incident_id,
            "deduped": False,
        }

    alert_obj = await Alert.get_or_none(id=result["alert_id"])

    # ---- 发布：Redis Stream（可选）+ SSE（§2.2）----
    if alert_obj:
        m2 = (
            trigger_pipeline
            and result["is_new"]
            and context.redis_client is not None
            and not settings.m1_stub_triage
        )
        if m2:
            queued = await _enqueue_alert_raw(alert_obj)
            if queued:
                incident_id = await _wait_for_incident(alert_obj.id)
                if incident_id is not None:
                    result["incident_id"] = incident_id
                    alert_obj.incident_id = incident_id
            else:
                # 入队失败（Redis 掉线）→ 进程内同步聚类，避免事件永远停在 NULL
                from app.incidents.service import on_alert

                try:
                    clustered = await on_alert(alert_obj)
                except Exception as exc:  # noqa: BLE001 - 聚类失败不回滚已入库告警
                    log_event(40, "alert.sync_cluster_failed", alert_id=alert_obj.id, error=str(exc))
                else:
                    result["incident_id"] = clustered["incident_id"]
                    alert_obj.incident_id = clustered["incident_id"]
        # SSE 推送**不**受 trigger_pipeline 约束。
        # trigger_pipeline 控制的是"要不要跑 F-02/F-03 诊断流水线"，
        # 而 SSE 推送回答的是"看板要不要知道来了一条告警"。两者正交：
        # 告警行无论在哪种情况下都已落库、也已经能被 GET /alerts/recent 查到，
        # 若此时不推送，订阅中的看板就会停在旧数据上，表现为
        # "列表里有、页面上没有"的不一致。
        await _push_sse(alert_obj, result["deduped"])

    # ---- 日志 + 指标（§7.3 / §7.4）----
    metrics.alerts_received.labels(
        source=normalized["source"], deduped=str(result["deduped"])
    ).inc()
    metrics.ingest_latency.observe(t.ms / 1000)
    if context.dedup.degraded:
        metrics.dedup_degraded_total.inc()

    log_event(
        20,
        "alert.received",
        trace_id=trace_id,
        alert_id=result["alert_id"],
        fingerprint=fp[:12],
        source=normalized["source"],
        component=normalized["component"],
        severity=normalized["severity"],
        deduped=result["deduped"],
        dedup_degraded=context.dedup.degraded,
        incident_id=result["incident_id"],
        latency_ms=t.ms,
    )
    return result


# 批量入库的并发上限。原实现是串行 `for ... await`，200 条一批就要 200 次
# 串行往返；这里改为并发，但必须设上限——Tortoise 连接池默认只有 10 条，
# 200 个协程同时抢池只会把等待队列拉长，并不更快。
# 同指纹的并发由 dedup 锁串行化，同组件由 cluster 的 component_lock 串行化，
# 因此并发不会破坏"同事件不重复建单"的语义。
_INGEST_CONCURRENCY = 8
_ingest_sem = asyncio.Semaphore(_INGEST_CONCURRENCY)


async def ingest_batch(
    normalized_list: list[dict], *, trigger_pipeline: bool = True
) -> dict:
    """批量入库，返回 §3.1 定义的响应结构。"""

    async def _one(item: dict) -> dict:
        async with _ingest_sem:
            return await ingest_one(item, trigger_pipeline=trigger_pipeline)

    results = list(await asyncio.gather(*(_one(n) for n in normalized_list)))

    alert_ids = [r["alert_id"] for r in results]
    accepted = sum(1 for r in results if r["is_new"])
    deduped = bool(results) and all(r["deduped"] for r in results)

    # 混合批次：取严重度最高那条的 incident_id（§3.1）
    incident_id = None
    best_rank = 99
    for n, r in zip(normalized_list, results):
        rank = SEV_ORDER.get(n["severity"], 9)
        if r["incident_id"] is not None and rank < best_rank:
            best_rank, incident_id = rank, r["incident_id"]
    if incident_id is None:
        incident_id = next((r["incident_id"] for r in results if r["incident_id"]), None)

    return {
        "accepted": accepted,
        "incident_id": incident_id,
        "deduped": deduped,
        "alert_ids": alert_ids,
    }
