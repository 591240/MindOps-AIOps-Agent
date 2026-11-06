"""F-10 认证依赖（§3.7 / §7.1）。

- get_current_user：从 Authorization header 解析 JWT → 查 User → 检查黑名单
- require_admin：角色为 admin 放行，否则 403
"""

from __future__ import annotations

from fastapi import Depends, HTTPException, Request

from app.auth.blacklist import is_token_blacklisted
from app.auth.jwt import decode_access_token
from app.models import User


async def get_current_user(request: Request) -> User:
    """从 Authorization header 解析 JWT（§3.7）。"""
    auth_header = request.headers.get("Authorization", "")
    if not auth_header.startswith("Bearer "):
        raise HTTPException(401, detail={"code": "NO_TOKEN", "message": "未提供认证令牌"})
    token = auth_header.split(" ", 1)[1]
    try:
        payload = decode_access_token(token)
    except ValueError as exc:
        code = "TOKEN_EXPIRED" if "expired" in str(exc).lower() else "INVALID_TOKEN"
        raise HTTPException(401, detail={"code": code, "message": str(exc)})

    # 检查 access token 黑名单（登出后拉黑）
    jti = payload.get("jti", "")
    if jti and await is_token_blacklisted(jti):
        raise HTTPException(401, detail={"code": "TOKEN_BLACKLISTED", "message": "令牌已失效"})

    user_id = payload.get("sub")
    if not user_id:
        raise HTTPException(401, detail={"code": "INVALID_TOKEN", "message": "令牌中缺少用户信息"})
    user = await User.get_or_none(id=int(user_id))
    if user is None:
        raise HTTPException(401, detail={"code": "USER_NOT_FOUND", "message": "用户不存在"})
    # 审查 #4：禁用/锁定账号即使持有效 token 也一律拒绝
    #（models 注释声称 active/disabled/locked 三态，此前仅 refresh 校验过）
    if user.status != "active":
        raise HTTPException(403, detail={"code": "ACCOUNT_DISABLED", "message": "账号已被禁用或锁定"})
    return user


async def require_admin(user: User = Depends(get_current_user)) -> User:
    """角色边界：仅 admin 可用（§3.10 / §3.11 / §3.12 / §3.13）。"""
    if user.role != "admin":
        raise HTTPException(403, detail={"code": "FORBIDDEN", "message": "仅管理员可操作"})
    return user
