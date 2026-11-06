"""SSE 实时流（文档 §2.2 / §8.2 / §9.3）。

- GET /stream/alerts  → text/event-stream，推送 `event: alert`
- GET /stream/stats   → 在线连接数（§3.3 V1.1 修订：stream_subscribers 移出写入接口）

[假设] 事件流用 EventSource 无法携带 Header，因此支持 `?token=<短票>`：前端先
       调用 `POST /auth/stream-ticket` 换一张 5 分钟有效的 stream 票据，
       避免把长效 access token 写进 URL 和访问日志。Authorization 头仍接受
       access token（脚本/测试用）。未带凭证返回 401。
"""

from __future__ import annotations

from fastapi import APIRouter, Depends, HTTPException, Query, Request, status
from fastapi.responses import StreamingResponse

from app.alerts import sse
from app.auth.dependencies import get_current_user
from app.auth.jwt import decode_access_token, decode_stream_ticket
from app.auth.blacklist import is_token_blacklisted
from app.models import User

router = APIRouter(prefix="/stream", tags=["stream"])


async def _authenticate(request: Request, token: str | None) -> dict:
    """解析并校验 SSE 凭证。

    - `Authorization: Bearer <access token>`：脚本与测试入口；
    - `?token=<stream ticket>`：浏览器 EventSource 唯一可行的传参方式。
      query 里只接受短票，长效 access token 不再作为 query 凭证。

    这里必须自己兜住解码函数抛出的 ValueError：
    它是个普通异常，不带 HTTP 语义，任其向上抛会被全局异常处理器兜成
    **500 INTERNAL_ERROR**。前端拿到 500 只会显示"服务异常"，
    而拿不到"该重登还是该重试"的判断依据 —— 令牌失效是客户端问题，
    必须回答 401 而不是 500。
    """
    auth = request.headers.get("authorization", "")
    bearer = auth[7:] if auth.lower().startswith("bearer ") else None
    if not bearer and not token:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "UNAUTHORIZED", "message": "missing token"},
        )
    try:
        payload = decode_access_token(bearer) if bearer else decode_stream_ticket(token or "")
    except ValueError:
        # 无效签名 / 类型不符 / 已过期，一律视作未认证
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "INVALID_TOKEN", "message": "invalid or expired token"},
        )
    # 审查 #7：补上 jti 黑名单检查（此前登出的 access token 在剩余 TTL 内
    # 仍可订阅实时流）。与 get_current_user 的口径一致。
    jti = payload.get("jti", "")
    if jti and await is_token_blacklisted(jti):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "TOKEN_BLACKLISTED", "message": "令牌已失效"},
        )
    return payload


def _last_event_id(request: Request) -> int | None:
    """读取断线重放起点（Header 优先，兼容 query）。"""
    last_id_raw = request.headers.get("last-event-id") or request.query_params.get(
        "last_event_id"
    )
    try:
        return int(last_id_raw) if last_id_raw else None
    except ValueError:
        return None


def _stream(gen) -> StreamingResponse:
    """统一的 SSE 响应包装。

    `X-Accel-Buffering: no` 是给反向代理看的：Nginx 默认会缓冲上游响应，
    不关掉的话 SSE 会被攒着一次性下发，实时性直接失效。
    """
    return StreamingResponse(
        gen,
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "Connection": "keep-alive",
            "X-Accel-Buffering": "no",
        },
    )


@router.get("/alerts")
async def stream_alerts(
    request: Request,
    token: str | None = Query(None, description="stream 短票（EventSource 无法带 Header）"),
):
    await _authenticate(request, token)

    async def gen():
        async for frame in sse.broker.subscribe(_last_event_id(request)):
            if await request.is_disconnected():
                break
            yield frame

    return _stream(gen())


@router.get("/incidents/{incident_id}")
async def stream_incident(
    incident_id: int,
    request: Request,
    token: str | None = Query(None, description="stream 短票（EventSource 无法带 Header）"),
):
    """订阅单个事件的编排进度和终态事件。"""
    await _authenticate(request, token)

    def matches(event: str, payload: dict) -> bool:
        return event.startswith("incident.") and payload.get("incident_id") == incident_id

    async def gen():
        async for frame in sse.broker.subscribe(_last_event_id(request), matches):
            if await request.is_disconnected():
                break
            yield frame

    return _stream(gen())


@router.get("/stats")
async def stream_stats(user: User = Depends(get_current_user)):
    """在线订阅数；与其它业务端点同口径要求登录，不向匿名者泄露运行态。"""
    return {"subscribers": sse.broker.subscriber_count}
