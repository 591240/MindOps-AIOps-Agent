"""F-10 JWT 签发与验证（§5.5 / §7.1）。

- create_access_token：签发 access token（含 jti、exp）
- create_refresh_token：签发 refresh token（含 jti、exp、type="refresh"）
- decode_access_token：验证 access token，不检查黑名单（黑名单检查在 dependencies.py / stream.py 显式进行）（登出 / 轮换后拉黑）
- rotate_refresh_token：轮换 refresh token（旧 token 拉黑 + 签发新 token pair）
"""

from __future__ import annotations

import time
import uuid
from datetime import datetime, timedelta, timezone

import jwt
from pydantic import BaseModel

from app.config import settings


class TokenPair(BaseModel):
    access_token: str
    refresh_token: str
    expires_in: int  # access token 过期秒数


def _sign(payload: dict, expires_delta: timedelta) -> str:
    """内部：签名 JWT。

    `exp` / `iat` 按 JWT 规范是 **Unix 时间戳（UTC）**，与库表里
    `use_tz=False` 的本地时间（Asia/Shanghai）互不影响 —— 两者用途不同，
    不要为了"统一时区"把这里改成 `datetime.now()`，那会让 token 偏移 8 小时。

    这里用 `timezone.utc` 的 aware datetime 取代已废弃的 `datetime.utcnow()`
    （Python 3.12+ 会告警，未来版本将移除）。PyJWT 对 aware datetime
    会自动转成正确的 epoch 秒。
    """
    now = datetime.now(timezone.utc)
    expire = now + expires_delta
    payload.update({"exp": expire, "iat": now})
    return jwt.encode(payload, settings.jwt_secret, algorithm=settings.jwt_algorithm)


def create_access_token(user_id: int, username: str, role: str) -> str:
    """签发 access token（含 jti，§5.5 黑名单最小代价）。"""
    return _sign({
        "sub": str(user_id),
        "username": username,
        "role": role,
        "jti": uuid.uuid4().hex[:16],
        "type": "access",
    }, timedelta(minutes=settings.jwt_expire_minutes))


def create_refresh_token(user_id: int, username: str, role: str) -> str:
    """签发 refresh token（有效期比 access 长 7 倍，§7.1）。"""
    return _sign({
        "sub": str(user_id),
        "username": username,
        "role": role,
        "jti": uuid.uuid4().hex[:16],
        "type": "refresh",
    }, timedelta(minutes=settings.jwt_expire_minutes * 7))


def create_token_pair(user_id: int, username: str, role: str) -> TokenPair:
    """签发 access + refresh token pair。"""
    return TokenPair(
        access_token=create_access_token(user_id, username, role),
        refresh_token=create_refresh_token(user_id, username, role),
        expires_in=settings.jwt_expire_minutes * 60,
    )


def create_stream_ticket(user_id: int, username: str, role: str) -> str:
    """签发 SSE 短票（type=stream）。

    EventSource 无法携带 Authorization 头，只能把凭证放进 query。短票只用于
    订阅实时流，不能调用任何业务接口，且有效期远短于 access token，
    即使被访问日志记录，泄露窗口也可控。
    """
    return _sign({
        "sub": str(user_id),
        "username": username,
        "role": role,
        "jti": uuid.uuid4().hex[:16],
        "type": "stream",
    }, timedelta(seconds=settings.stream_ticket_ttl_seconds))


def decode_access_token(token: str) -> dict:
    """验证 access token；失败抛 ValueError（F-07/F-08 统一契约）。"""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        if payload.get("type") != "access":
            raise ValueError("Invalid token type")
        return payload
    except jwt.ExpiredSignatureError:
        raise ValueError("Token expired")
    except jwt.InvalidTokenError as exc:
        raise ValueError("Invalid token") from exc


def decode_refresh_token(token: str) -> dict:
    """验证 refresh token；失败抛 ValueError。"""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        if payload.get("type") != "refresh":
            raise ValueError("Invalid token type")
        return payload
    except jwt.ExpiredSignatureError:
        raise ValueError("Token expired")
    except jwt.InvalidTokenError as exc:
        raise ValueError("Invalid token") from exc


def decode_stream_ticket(token: str) -> dict:
    """验证 SSE 短票；失败抛 ValueError。access/refresh token 一律不在这里放行。"""
    try:
        payload = jwt.decode(token, settings.jwt_secret, algorithms=[settings.jwt_algorithm])
        if payload.get("type") != "stream":
            raise ValueError("Invalid token type")
        return payload
    except jwt.ExpiredSignatureError:
        raise ValueError("Token expired")
    except jwt.InvalidTokenError as exc:
        raise ValueError("Invalid token") from exc
