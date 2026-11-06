"""MindOps 后端入口（F-01 告警接入）。

- 挂载 /api/v1（附录 A ⑪ 统一 API_PREFIX）
- lifespan：初始化 DB → 装配去重/限流后端 → seed 联调账号
"""

from __future__ import annotations

import asyncio
import contextlib
import hmac
import ipaddress
import os
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.alerts import context
from app.auth.email import clear_expired
from app.auth.password import hash_password
from app.config import settings, validate_for_startup
from app.db import close_db, init_db
from app.errors import install_exception_handlers
from app.models import User
from app.incidents import service as incident_service
from app.agents import calibration, orchestrator as diagnosis_orchestrator
from app.observability.logging import log_event, setup_logging
from app.routers import (
    alerts, auth, stream, incidents, confidence, knowledge, llm, integrations, tasks, metrics, rules,
)

# 联调账号（文档 §9.2 验收脚本使用 sre001）
SEED_USERS = [
    ("sre_admin", "Admin@123", "admin"),
    ("sre001", "P@ssw0rd!", "engineer"),
]


async def _seed_users() -> None:
    # 安全修复（审查 #1）：种子账号携带硬编码口令。仅当显式设置
    # MINDOPS_SEED_DEMO_USERS=1 时注入；不再与 debug 绑定——debug 是排障开关，
    # 不应该顺带在库里放进已知口令的管理员账号。已有库不受影响（只补缺号）。
    if os.environ.get("MINDOPS_SEED_DEMO_USERS") != "1":
        log_event(30, "seed.skipped",
                  hint="需要演示账号时显式设置 MINDOPS_SEED_DEMO_USERS=1")
        return
    for username, pwd, role in SEED_USERS:
        if await User.get_or_none(username=username) is None:
            await User.create(
                username=username, hashed_password=hash_password(pwd), role=role
            )


_EMAIL_CLEANUP_INTERVAL_SECONDS = 6 * 3600


async def _email_cleanup_loop() -> None:
    """定期清理过期的邮箱验证码（含"时间到了但没人验证"的 pending）。"""
    while True:
        try:
            removed = await clear_expired()
            if removed:
                log_event(20, "email.cleanup", removed=removed)
        except Exception as exc:  # noqa: BLE001 - 清理失败不能拖垮进程
            log_event(30, "email.cleanup_failed",
                      error=f"{type(exc).__name__}: {str(exc)[:200]}")
        await asyncio.sleep(_EMAIL_CLEANUP_INTERVAL_SECONDS)


@asynccontextmanager
async def lifespan(app: FastAPI):
    setup_logging()
    # P0-4：启动期安全校验（生产环境配置不安全时直接拒绝启动）
    validate_for_startup()
    await init_db()
    await _seed_users()
    await context.startup()
    await incident_service.startup()
    await diagnosis_orchestrator.startup()
    await calibration.start_daily_scheduler()
    cleanup_task = asyncio.create_task(_email_cleanup_loop())

    log_event(
        20,
        "app.started",
        app=settings.app_name,
        api_prefix=settings.api_prefix,
        dedup_backend=type(context.dedup).__name__,
        dedup_degraded=getattr(context.dedup, "degraded", False),
        redis_pubsub=context.redis_pubsub_enabled,
        m1_stub_triage=settings.m1_stub_triage,
    )
    try:
        yield
    finally:
        cleanup_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await cleanup_task
        await calibration.stop_daily_scheduler()
        await diagnosis_orchestrator.shutdown()
        await incident_service.shutdown()
        await context.shutdown()
        await close_db()


app = FastAPI(
    title=f"{settings.app_name} API",
    version="0.1.0-f01",
    description="F-01 告警接入（三源接入 / 归一化 / 去重 / 限流 / SSE）",
    lifespan=lifespan,
)

# P0-4：`*` 与 allow_credentials 同时出现时，Starlette 会把 Origin 原样反射，
# 等价于向任意站点暴露凭证。这里显式禁止该组合（配置层已在启动期拦截）。
_allow_all_origins = "*" in settings.cors_origin_list
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"] if _allow_all_origins else settings.cors_origin_list,
    allow_credentials=settings.cors_allow_credentials and not _allow_all_origins,
    allow_methods=["*"],
    allow_headers=["*"],
)


# 审查 #9：统一请求体上限。此前只有 webhook 在读入后才检查长度，
# JWT/knowledge/事件上报等端点完全无限制。按 Content-Length 早拒（413），
# 未带 Content-Length 的分块请求由各端点自身的流式/读后校验兜底。
@app.middleware("http")
async def _limit_body_size(request: Request, call_next):
    cl = request.headers.get("content-length")
    if cl and cl.isdigit() and int(cl) > settings.max_body_bytes:
        return JSONResponse(
            status_code=413,
            content={"detail": {"code": "BODY_TOO_LARGE",
                                "message": f"body exceeds {settings.max_body_bytes} bytes"}},
        )
    return await call_next(request)

# 业务路由统一挂到 /api/v1。
#
# 原实现是 `api = FastAPI()` + `app.mount(prefix, api)`：mount 出去的是独立
# ASGI 应用，**OpenAPI 不会合并**，导致 /docs 里只剩 /health 与 /metrics，
# 30+ 个业务接口的契约在文档中完全不可见。
# 改用 include_router(prefix=...) 后，路由表与 OpenAPI 都由主应用统一生成。
install_exception_handlers(app)
for _router in (
    alerts.router,
    auth.router,
    stream.router,
    incidents.router,
    confidence.router,
    knowledge.router,
    llm.router,
    integrations.router,
    tasks.router,
    metrics.router,
    rules.router,
):
    app.include_router(_router, prefix=settings.api_prefix)


@app.get("/health")
async def health():
    """存活探针 + DB 可达性。DB 不通返回 503，供编排层直接摘流量。"""
    checks = {"db": True}
    try:
        from tortoise import Tortoise

        await Tortoise.get_connection("default").execute_query("SELECT 1")
    except Exception:  # noqa: BLE001 - 探针不向上抛异常
        checks["db"] = False
    ok = all(checks.values())
    return JSONResponse(
        status_code=200 if ok else 503,
        content={"status": "ok" if ok else "error", "service": settings.app_name, "checks": checks},
    )


@app.get("/metrics")
async def metrics_endpoint(request: Request):
    """Prometheus 抓取端点（F-01 §7.4 / F-04 §2.3 回环与校准指标）。

    生产环境不向公网匿名开放：配置了 `MINDOPS_METRICS_TOKEN` 就校验请求头，
    未配置时只允许回环/私网直连（容器内或宿主机 localhost 抓取）。
    """
    token = settings.metrics_token.strip()
    if token:
        got = request.headers.get("x-metrics-token", "")
        if not hmac.compare_digest(got, token):
            raise HTTPException(404)
    elif not settings.debug:
        host = request.client.host if request.client else ""
        try:
            addr = ipaddress.ip_address(host)
        except ValueError:
            addr = None
        if addr is None or not (addr.is_private or addr.is_loopback):
            raise HTTPException(404)
    return Response(generate_latest(), media_type=CONTENT_TYPE_LATEST)
