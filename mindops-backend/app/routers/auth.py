"""F-10 认证路由（§3 / §7.1）。

端点：
- POST /auth/login            密码登录（含 5 次锁定 + JWT pair）
- POST /auth/email/login/send 发送邮箱登录验证码（60s 冷却 + IP 限流）
- POST /auth/email/login      邮箱验证码登录（只对已注册邮箱签发 token）
- GET  /auth/register/config  自助注册开关与邮件通道（注册页据此决定是否展示入口）
- POST /auth/email/send       发送注册邮箱验证码（60s 冷却）
- POST /auth/register         邮箱验证码注册（默认 engineer，admin 只给 bootstrap 邮箱）
- POST /auth/refresh          refresh token 轮换（旧 token 拉黑 + 新 pair）
- POST /auth/logout           登出（refresh token jti 拉黑）
- GET  /auth/me               当前用户信息
"""

from __future__ import annotations

import asyncio
import random
import re
import time
from collections import deque

from fastapi import APIRouter, Depends, HTTPException, Request

from app.auth.blacklist import (
    blacklist_token,
    clear_password_failures,
    get_password_failures,
    is_token_blacklisted,
    record_password_failure,
)
from app.auth.dependencies import get_current_user
from app.alerts import context as alerts_context
from app.alerts.webhook import resolve_client_ip
from app.auth.email import (
    EmailSendError,
    is_valid_email,
    mask_email,
    normalize_email,
)
from app.auth.email import send_code as send_email_code
from app.auth.email import verify_code as verify_email_code
from app.auth.jwt import (
    TokenPair,
    create_stream_ticket,
    create_token_pair,
    decode_access_token,
    decode_refresh_token,
)
from app.auth.password import hash_password, verify_password
from app.config import model_now, settings
from app.models import User
from app.observability.logging import log_event

router = APIRouter(prefix="/auth", tags=["auth"])

# 注册产出的角色。**顺序即前端展示顺序**。admin 不由注册产生——
# 只有 MINDOPS_BOOTSTRAP_ADMIN_EMAIL 指定的那一个邮箱例外（见 register()）。
REGISTERABLE_ROLES = ("engineer",)

_USERNAME_RE = re.compile(r"^[A-Za-z0-9_.\-]{3,32}$")
MIN_PASSWORD_LENGTH = 8

# 时序均衡用的固定哈希：未知账号也走一次等价耗时的校验，避免用响应时间
# 反推"这个用户名是否存在"。模块加载时算一次，不在请求路径上重复计算。
_DUMMY_PASSWORD_HASH = hash_password("mindops-timing-equalizer")


def _issue_token_pair(user: User) -> TokenPair:
    """签发 token pair 并更新 last_login。"""
    pair = create_token_pair(user.id, user.username, user.role)
    return pair



# ---- 审查 #8：登录入口按来源 IP 限流（滑动窗口，进程内有界实现）----
_LOGIN_LIMIT_MAX = 30        # 窗口内每 IP 最大尝试次数（含失败）
_LOGIN_LIMIT_WINDOW = 300.0  # 窗口秒数
_login_attempts: dict[str, deque] = {}
_login_lock = asyncio.Lock()


async def _check_login_rate(ip: str) -> bool:
    # debug 档跳过：测试套件上百次登录共用同一 TestClient IP，会误触限流；
    # 生产（debug=False）始终生效。
    if settings.debug:
        return True
    now = time.monotonic()
    async with _login_lock:
        if len(_login_attempts) > 20_000 and ip not in _login_attempts:
            for stale in list(_login_attempts)[:2_000]:
                _login_attempts.pop(stale, None)
        q = _login_attempts.setdefault(ip, deque())
        while q and now - q[0] > _LOGIN_LIMIT_WINDOW:
            q.popleft()
        if len(q) >= _LOGIN_LIMIT_MAX:
            return False
        q.append(now)
        return True


async def _check_code_send_quota(request: Request) -> None:
    """验证码下发按来源 IP 限流（复用 F-01 令牌桶，配 Redis 时跨副本生效）。

    `send_code` 内的 60s 冷却是"同目标"维度，换一个手机号/邮箱就绕过；这里补
    "同来源 IP"维度——否则这是个未鉴权接口，可以拿随机地址无限下发，把 SMTP
    配额（真实授权码）和 email_codes 表一起打爆。
    debug 档跳过：与 `_check_login_rate` 同因，测试套件的请求共用同一 IP。
    """
    if settings.debug:
        return
    per_min = max(settings.code_send_ip_per_min, 1)
    ok = await alerts_context.limiter.acquire(
        f"ratelimit:auth:code_send:{resolve_client_ip(request)}",
        capacity=per_min, rate=per_min / 60.0, cost=1,
    )
    if not ok:
        raise HTTPException(429, detail={
            "code": "CODE_SEND_RATE_LIMITED",
            "message": f"本来源发送验证码过于频繁，请 {60 // per_min} 分钟后重试",
        })


# ---- POST /auth/login（密码登录 + 5 次锁定）----


@router.post("/login")
async def login(request: Request):
    """密码登录（§3.1）。自动注册 + 返回 access_token + refresh_token。"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(422, detail={"code": "INVALID_BODY", "message": "请求体格式错误"})

    username = (body.get("username") or "").strip()
    password = (body.get("password") or "").strip()

    if not username:
        raise HTTPException(422, detail={"code": "USERNAME_REQUIRED", "message": "用户名不能为空"})

    # 审查 #8：登录入口按来源 IP 限流（此前只有告警/集成有限流，登录可被无限爆破）
    client_ip = resolve_client_ip(request)
    if not await _check_login_rate(client_ip):
        log_event(30, "auth.login_rate_limited", ip=client_ip, username=username)
        raise HTTPException(429, detail={"code": "RATE_LIMITED", "message": "尝试过于频繁，请稍后再试"})

    user = await User.get_or_none(username=username)

    # 审查 #4：禁用/锁定账号在任何路径都不得登录
    if user and user.status != "active":
        log_event(30, "auth.login_inactive_user", username=username, status=user.status)
        # 与"用户名或密码错误"同文案同状态码，避免借助本端点探测账号封禁状态
        raise HTTPException(401, detail={"code": "INVALID_CREDENTIALS", "message": "用户名或密码错误"})

    # 密码锁定检查（§5.4）
    if user:
        failures = await get_password_failures(user.id)
        if failures >= settings.password_lockout_threshold:
            raise HTTPException(423, detail={
                "code": "ACCOUNT_LOCKED",
                "message": f"密码连续错误 {failures} 次，{settings.password_lockout_seconds // 60} 分钟后重试",
                "retry_after": settings.password_lockout_seconds,
            })

    # demo 模式：username == password 即通过（现有行为不变）
    # 新模式：bcrypt 校验
    if user:
        # P0-1：demo 明文回退仅在 debug=True 时保留，生产一律走哈希校验
        legacy_plaintext = (
            settings.debug and not user.hashed_password.startswith("$pbkdf2")
        )
        if legacy_plaintext:
            ok = user.hashed_password == password
        else:
            ok = verify_password(password, user.hashed_password)

        if not ok:
            count = await record_password_failure(user.id)
            remaining = settings.password_lockout_threshold - count
            if remaining <= 0:
                raise HTTPException(423, detail={
                    "code": "ACCOUNT_LOCKED",
                    "message": f"密码连续错误 {settings.password_lockout_threshold} 次，账户锁定",
                    "retry_after": settings.password_lockout_seconds,
                })
            # 审查修复：错误响应**不得**携带 remaining_attempts。
            # 原实现对"存在但密码错"返回"还剩 N 次机会"，对"不存在"返回通用文案，
            # 二者可直接区分 → 用户名可被枚举，与本节"防枚举"目标自相矛盾。
            # 剩余次数改由前端按固定阈值本地推算，不再由服务端下发。
            raise HTTPException(401, detail={
                "code": "INVALID_CREDENTIALS",
                "message": "用户名或密码错误",
            })
    else:
        # P0-1：登录入口不再无条件自动注册。
        # 未开启 demo 自动注册时，未知账号返回与"密码错误"完全一致的响应，
        # 避免通过错误码差异枚举用户名。
        if not settings.auth_allow_auto_register:
            # 时序均衡：未知账号也做一次等价耗时的哈希校验。
            # 不做这一步的话，"查无此人"会立即返回，而"密码错误"要等一次
            # pbkdf2（数十毫秒），响应时间本身就是枚举信号。
            verify_password(password, _DUMMY_PASSWORD_HASH)
            log_event(20, "auth.login_unknown_user", username=username)
            raise HTTPException(401, detail={
                "code": "INVALID_CREDENTIALS",
                "message": "用户名或密码错误",
            })

        # P0-1：空密码兜底（用 username 当密码）已移除
        if not password:
            raise HTTPException(422, detail={
                "code": "PASSWORD_REQUIRED",
                "message": "密码不能为空",
            })

        # P0-1：角色固定为 engineer，绝不从用户名推导（sre_admin → admin 提权已封堵）
        user = await User.create(
            username=username,
            hashed_password=hash_password(password),
            role="engineer",
        )
        log_event(20, "auth.auto_register", username=username, user_id=user.id)

    # 登录成功：清空错误计数 + 更新 last_login
    await clear_password_failures(user.id)
    await User.filter(id=user.id).update(last_login=model_now(), last_login_method="password")

    pair = _issue_token_pair(user)
    log_event(20, "auth.login", user_id=user.id, method="password")
    return {
        "access_token": pair.access_token,
        "refresh_token": pair.refresh_token,
        "token_type": "Bearer",
        "expires_in": pair.expires_in,
        "user": {"id": user.id, "username": user.username, "role": user.role},
    }


# ---- POST /auth/email/login/send（发送邮箱登录验证码 + 60s 冷却）----


@router.post("/email/login/send")
async def email_login_send(request: Request):
    """发送邮箱登录验证码。

    与注册共用 `email_codes` 表和 60s 冷却：同一邮箱不会同时存在两种用途的
    有效码，避免「注册码拿去登录」这类混用。通道由 `MINDOPS_EMAIL_PROVIDER`
    决定；下发失败返回 502（不假装成功）。
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(422, detail={"code": "INVALID_BODY", "message": "请求体格式错误"})

    email = normalize_email(body.get("email") or "")
    if not email:
        raise HTTPException(422, detail={"code": "EMAIL_REQUIRED", "message": "邮箱不能为空"})
    if not is_valid_email(email):
        raise HTTPException(422, detail={"code": "INVALID_EMAIL", "message": "邮箱格式不正确"})

    await _check_code_send_quota(request)

    # 只给已注册且启用的邮箱发码：登录场景对未注册地址发信没有业务价值，
    # 却能让这个未鉴权接口变成"向任意邮箱发信"的免费通道（SMTP 配额、风控、骚扰）。
    # 两个分支的响应文案与字段保持一致，避免借响应差异枚举邮箱是否注册。
    user = await User.filter(email=email).order_by("-id").first()
    eligible = user is not None and user.status == "active"
    generic: dict = {
        "message": "如果该邮箱已注册，验证码已发送",
        "ttl": settings.email_code_ttl,
        "interval": settings.email_send_interval,
    }
    if not eligible:
        # 与真实下发保持相近耗时，削弱用响应时间判断"邮箱是否存在"的信号
        await asyncio.sleep(
            random.uniform(1, 2) if settings.email_provider == "mock"
            else random.uniform(0.4, 1.0)
        )
        log_event(20, "auth.email_login_send_skipped")
        return generic

    try:
        result = await send_email_code(email)
    except ValueError:
        raise HTTPException(429, detail={
            "code": "EMAIL_SEND_INTERVAL",
            "message": f"发送过于频繁，请 {settings.email_send_interval} 秒后重试",
        })
    except EmailSendError as exc:
        # 没发出去就如实报错：绝不能返回"验证码已发送"而用户邮箱毫无动静。
        raise HTTPException(502, detail={
            "code": "EMAIL_SEND_FAILED",
            "reason": exc.reason,
            "message": f"验证码邮件发送失败：{exc.message}",
        })

    if settings.email_provider == "mock":
        await asyncio.sleep(random.uniform(1, 2))

    log_event(20, "auth.email_login_send", user_id=user.id, ttl=result["ttl"])

    resp: dict = dict(generic)
    if settings.email_provider == "mock":
        resp["debug_code"] = result["code"]
    return resp


# ---- POST /auth/email/login（邮箱验证码登录）----


@router.post("/email/login")
async def email_login(request: Request):
    """邮箱验证码登录。

    只对**已注册**邮箱签发 token——自动注册只走 `/auth/register`，两个入口
    职责分开。验证码先校验再查用户：没有邮箱控制权的人无法用本接口枚举
    哪些邮箱已注册。错误码：INVALID_CODE(401) / USER_NOT_FOUND(403) /
    ACCOUNT_DISABLED(403)。
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(422, detail={"code": "INVALID_BODY", "message": "请求体格式错误"})

    email = normalize_email(body.get("email") or "")
    code = str(body.get("code") or "").strip()
    if not email:
        raise HTTPException(422, detail={"code": "EMAIL_REQUIRED", "message": "邮箱不能为空"})
    if not is_valid_email(email):
        raise HTTPException(422, detail={"code": "INVALID_EMAIL", "message": "邮箱格式不正确"})
    if not code:
        raise HTTPException(422, detail={"code": "CODE_REQUIRED", "message": "验证码不能为空"})

    if not await verify_email_code(email, code):
        log_event(30, "auth.email_login_bad_code", email=mask_email(email))
        raise HTTPException(401, detail={"code": "INVALID_CODE", "message": "验证码错误或已过期"})

    user = await User.filter(email=email).order_by("-id").first()
    if user is None:
        log_event(20, "auth.email_login_unknown_email", email=mask_email(email))
        raise HTTPException(403, detail={
            "code": "USER_NOT_FOUND",
            "message": "该邮箱尚未注册，请先注册或联系管理员开通账号",
        })
    if user.status != "active":
        log_event(30, "auth.email_login_inactive_user", email=mask_email(email), status=user.status)
        raise HTTPException(403, detail={
            "code": "ACCOUNT_DISABLED",
            "message": "账号已停用，请联系管理员",
        })

    await User.filter(id=user.id).update(
        last_login=model_now(), last_login_method="email", email_verified=True,
    )

    pair = _issue_token_pair(user)
    log_event(20, "auth.email_login", user_id=user.id, email=mask_email(email))
    return {
        "access_token": pair.access_token,
        "refresh_token": pair.refresh_token,
        "token_type": "Bearer",
        "expires_in": pair.expires_in,
        "user": {"id": user.id, "username": user.username, "role": user.role, "email": email},
    }


# ---- GET /auth/register/config（注册页开关）----


@router.get("/register/config")
async def register_config():
    """暴露自助注册的开关与邮件通道，供前端决定是否展示注册入口。

    不需要鉴权：登录页要在未登录状态下就知道该不该显示「注册」。
    只暴露"开关"与"通道名"，不含任何凭据。
    """
    return {
        "enabled": settings.register_enabled,
        "channel": settings.email_provider,
        "roles": list(REGISTERABLE_ROLES),
        "code_length": settings.email_code_length,
        "send_interval": settings.email_send_interval,
    }


# ---- POST /auth/email/send（发送邮箱验证码 + 60s 冷却）----


@router.post("/email/send")
async def email_send(request: Request):
    """发送注册验证码（60s 冷却 + 经通道真实下发）。

    通道由 `MINDOPS_EMAIL_PROVIDER` 决定；下发失败返回 502（不假装成功）。
    mock 通道下验证码经 `debug_code` 回传，真实通道恒不返回。
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(422, detail={"code": "INVALID_BODY", "message": "请求体格式错误"})

    if not settings.register_enabled:
        raise HTTPException(403, detail={
            "code": "REGISTER_DISABLED",
            "message": "自助注册已关闭，请联系管理员开通账号",
        })

    email = normalize_email(body.get("email") or "")
    if not email:
        raise HTTPException(422, detail={"code": "EMAIL_REQUIRED", "message": "邮箱不能为空"})
    if not is_valid_email(email):
        raise HTTPException(422, detail={"code": "INVALID_EMAIL", "message": "邮箱格式不正确"})

    await _check_code_send_quota(request)

    try:
        result = await send_email_code(email)
    except ValueError:
        raise HTTPException(429, detail={
            "code": "EMAIL_SEND_INTERVAL",
            "message": f"发送过于频繁，请 {settings.email_send_interval} 秒后重试",
        })
    except EmailSendError as exc:
        # 没发出去就如实报错：绝不能返回"验证码已发送"而用户邮箱毫无动静。
        raise HTTPException(502, detail={
            "code": "EMAIL_SEND_FAILED",
            "reason": exc.reason,
            "message": f"验证码邮件发送失败：{exc.message}",
        })

    # mock 通道保留 1-2s 延迟，让前端 loading 态与真实 SMTP 耗时观感一致。
    if settings.email_provider == "mock":
        await asyncio.sleep(random.uniform(1, 2))

    log_event(20, "auth.email_send", email=mask_email(email), ttl=result["ttl"])

    resp: dict = {"message": "验证码已发送", "ttl": result["ttl"], "interval": result["interval"]}
    if settings.email_provider == "mock":
        resp["debug_code"] = result["code"]
    return resp


# ---- POST /auth/register（邮箱验证码注册）----


@router.post("/register")
async def register(request: Request):
    """邮箱验证码注册，成功后直接签发 token pair（免二次登录）。

    **权限说明**：注册产出的角色是 `engineer`；只有邮箱等于
    `MINDOPS_BOOTSTRAP_ADMIN_EMAIL` 时才产出 `admin`（用于第一个管理员进场）。
    角色不接受请求体自选——前端也没有角色控件，`REGISTERABLE_ROLES` 只含 engineer。
    每次注册都写 `auth.register` 审计日志，含 role 与 role_choice，事后可追溯。

    错误码：REGISTER_DISABLED(403) / INVALID_EMAIL·USERNAME_*·WEAK_PASSWORD(422) /
    INVALID_CODE(400) / EMAIL_TAKEN·USERNAME_TAKEN(409)。
    注：验证码错误用 400 而非短信登录的 401 —— 这是注册表单的字段校验失败，
    不是"认证失败的已有会话"，语义上不该复用 401。
    """
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(422, detail={"code": "INVALID_BODY", "message": "请求体格式错误"})

    if not settings.register_enabled:
        raise HTTPException(403, detail={
            "code": "REGISTER_DISABLED",
            "message": "自助注册已关闭，请联系管理员开通账号",
        })

    email = normalize_email(body.get("email") or "")
    code = str(body.get("code") or "").strip()
    username = (body.get("username") or "").strip()
    # 审查 #19：login 对密码 .strip()，注册必须同口径，否则首尾带空格的密码永远登录不了
    password = (body.get("password") or "").strip()
    # 角色收口：注册一律创建 engineer；只有 MINDOPS_BOOTSTRAP_ADMIN_EMAIL 指定的
    # 那个邮箱拿到 admin。两边同过 normalize_email，但本地部分大小写敏感（见该函数），
    # 所以配置时要和注册时填的一致。
    admin_email = normalize_email(settings.bootstrap_admin_email or "")
    role = "admin" if admin_email and admin_email == email else "engineer"

    # ---- 字段校验（一次性把所有问题都说清楚，避免用户改一条报一条）----
    if not email:
        raise HTTPException(422, detail={"code": "EMAIL_REQUIRED", "message": "邮箱不能为空"})
    if not is_valid_email(email):
        raise HTTPException(422, detail={"code": "INVALID_EMAIL", "message": "邮箱格式不正确"})
    if not code:
        raise HTTPException(422, detail={"code": "CODE_REQUIRED", "message": "验证码不能为空"})
    if not username:
        raise HTTPException(422, detail={"code": "USERNAME_REQUIRED", "message": "用户名不能为空"})
    if not _USERNAME_RE.match(username):
        raise HTTPException(422, detail={
            "code": "INVALID_USERNAME",
            "message": "用户名需为 3-32 位字母、数字、下划线或短横线",
        })
    if not password:
        raise HTTPException(422, detail={"code": "PASSWORD_REQUIRED", "message": "密码不能为空"})
    if len(password) < MIN_PASSWORD_LENGTH:
        raise HTTPException(422, detail={
            "code": "WEAK_PASSWORD",
            "message": f"密码至少 {MIN_PASSWORD_LENGTH} 位",
        })

    # ---- 验证码优先 ----
    # 查重必须放在验证码校验之后：否则拿任意邮箱 + 一个错误验证码请求注册，
    # 就能从 409/400 的差异判断邮箱或用户名是否已被占用，无需控制邮箱即可枚举。
    # 代价是撞号时验证码已被消费、需要重新发送，这是防枚举的必要成本。
    if not await verify_email_code(email, code):
        log_event(30, "auth.register_bad_code", email=mask_email(email))
        raise HTTPException(400, detail={"code": "INVALID_CODE", "message": "验证码错误或已过期"})

    # ---- 查重 ----
    if await User.filter(email=email).exists():
        raise HTTPException(409, detail={
            "code": "EMAIL_TAKEN",
            "message": "该邮箱已注册，请直接登录或改用其它邮箱",
        })
    if await User.filter(username=username).exists():
        raise HTTPException(409, detail={
            "code": "USERNAME_TAKEN",
            "message": "该用户名已被占用，请换一个",
        })

    # ---- 建号 ----
    # exists() 与 create() 之间存在竞态窗口：username 由 UNIQUE 约束兜底，
    # email 由唯一索引 uk_users_email 兜底，命中哪一个就返回对应的 409。
    from tortoise.exceptions import IntegrityError

    try:
        user = await User.create(
            username=username,
            hashed_password=hash_password(password),
            role=role,
            email=email,
            email_verified=True,
            status="active",
        )
    except IntegrityError as exc:
        if "email" in str(exc).lower() or "uk_users_email" in str(exc).lower():
            raise HTTPException(409, detail={
                "code": "EMAIL_TAKEN",
                "message": "该邮箱已注册，请直接登录或改用其它邮箱",
            })
        raise HTTPException(409, detail={
            "code": "USERNAME_TAKEN",
            "message": "该用户名已被占用，请换一个",
        })

    await User.filter(id=user.id).update(last_login=model_now(), last_login_method="password")

    pair = _issue_token_pair(user)
    # 审计：注册产出的角色一律留痕，事后可追溯谁拿到了 admin。
    log_event(
        20, "auth.register",
        user_id=user.id, username=user.username, email=mask_email(email),
        role=role, role_choice="bootstrap_admin" if role == "admin" else "default_engineer",
    )
    return {
        "access_token": pair.access_token,
        "refresh_token": pair.refresh_token,
        "token_type": "Bearer",
        "expires_in": pair.expires_in,
        "user": {"id": user.id, "username": user.username, "role": user.role, "email": email},
    }


# ---- POST /auth/stream-ticket（SSE 短票）----


@router.post("/stream-ticket")
async def issue_stream_ticket(user: User = Depends(get_current_user)):
    """签发 SSE 短票（type=stream），仅供 EventSource 订阅实时流。

    EventSource 无法携带 Authorization 头，凭证只能放 query。短票不能调用
    任何业务接口、有效期默认 5 分钟，避免长效 access token 进入 URL 和访问日志。
    """
    return {
        "ticket": create_stream_ticket(user.id, user.username, user.role),
        "expires_in": settings.stream_ticket_ttl_seconds,
    }


# ---- POST /auth/refresh（refresh token 轮换）----


@router.post("/refresh")
async def refresh_token(request: Request):
    """Refresh token 轮换（§3.4）。旧 token 拉黑 + 签发新 pair。"""
    try:
        body = await request.json()
    except Exception:
        raise HTTPException(422, detail={"code": "INVALID_BODY", "message": "请求体格式错误"})

    refresh = (body.get("refresh_token") or "").strip()
    if not refresh:
        raise HTTPException(422, detail={"code": "REFRESH_TOKEN_REQUIRED", "message": "refresh_token 不能为空"})

    try:
        payload = decode_refresh_token(refresh)
    except ValueError as exc:
        code = "TOKEN_EXPIRED" if "expired" in str(exc).lower() else "INVALID_TOKEN"
        raise HTTPException(401, detail={"code": code, "message": str(exc)})

    jti = payload.get("jti", "")
    if await is_token_blacklisted(jti):
        raise HTTPException(401, detail={"code": "TOKEN_BLACKLISTED", "message": "refresh token 已失效，请重新登录"})

    # 拉黑旧 token（JTI 黑名单，§5.5）
    exp_seconds = max(int(payload.get("exp", 0) - time.time()), 1)
    await blacklist_token(jti, exp_seconds)

    # 签发新 pair
    user_id = int(payload["sub"])
    user = await User.get_or_none(id=user_id)
    if user is None or user.status != "active":
        raise HTTPException(401, detail={"code": "USER_UNAVAILABLE", "message": "用户不存在或已禁用"})

    pair = _issue_token_pair(user)
    log_event(20, "auth.token_refreshed", user_id=user.id)
    return {
        "access_token": pair.access_token,
        "refresh_token": pair.refresh_token,
        "token_type": "Bearer",
        "expires_in": pair.expires_in,
    }


# ---- POST /auth/logout（登出 + jti 拉黑）----


@router.post("/logout")
async def logout(request: Request, user: User = Depends(get_current_user)):
    """登出（§3.5）。P0-2：access 与 refresh 两个 jti 同时拉黑。

    修复前只拉黑 refresh jti，access token（默认 120 分钟）在有效期内仍可调用
    包括 require_admin 在内的所有接口。
    """
    try:
        body = await request.json()
    except Exception:
        body = {}

    revoked = []

    # 1）access token：从 Authorization 头取回 jti 并拉黑
    auth_header = request.headers.get("Authorization", "")
    if auth_header.startswith("Bearer "):
        access = auth_header.split(" ", 1)[1].strip()
        try:
            payload = decode_access_token(access)
            jti = payload.get("jti", "")
            if jti:
                exp_seconds = max(int(payload.get("exp", 0) - time.time()), 1)
                await blacklist_token(jti, exp_seconds)
                revoked.append("access")
        except ValueError:
            pass  # token 已失效，无需拉黑

    # 2）refresh token：沿用原有行为
    refresh = (body.get("refresh_token") or "").strip()
    if refresh:
        try:
            payload = decode_refresh_token(refresh)
            jti = payload.get("jti", "")
            if jti:
                exp_seconds = max(int(payload.get("exp", 0) - time.time()), 1)
                await blacklist_token(jti, exp_seconds)
                revoked.append("refresh")
        except ValueError:
            pass  # token 已失效，无需拉黑

    log_event(20, "auth.logout", user_id=user.id, revoked=revoked)
    return {"message": "已登出", "revoked": revoked}


# ---- GET /auth/me ----


@router.get("/me")
async def me(user: User = Depends(get_current_user)):
    """当前用户信息（§3.6）。"""
    return {
        "id": user.id,
        "username": user.username,
        "role": user.role,
        "last_login": user.last_login.isoformat() if user.last_login else None,
        "last_login_method": user.last_login_method,
    }
