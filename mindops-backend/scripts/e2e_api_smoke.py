"""前后端联调 E2E 冒烟脚本。

用途：在真实 HTTP 层验证 mindops-backend 全部对外接口可用，
并核对响应字段与前端 `src/api/*.ts` 的 DTO 是否一致。

用法（后端需已在 8010 启动）：
    .venv/Scripts/python.exe scripts/e2e_api_smoke.py

退出码：全部通过 0，任一用例失败 1。
"""

from __future__ import annotations

import json
import os
import sys
import uuid

import httpx

BASE = "http://127.0.0.1:8010/api/v1"
ADMIN = ("sre_admin", "Admin@123")
ENGINEER = ("sre001", "P@ssw0rd!")

# 判定为"可用"的状态码：2xx 与 409（业务冲突说明接口已正确执行到业务层）
OK = {200, 201, 202, 204, 409}

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    results.append((name, ok, detail))
    flag = "PASS" if ok else "FAIL"
    print(f"[{flag}] {name}" + (f"  -> {detail}" if detail and not ok else ""))


def login(client: httpx.Client, username: str, password: str) -> str:
    r = client.post(f"{BASE}/auth/login", json={"username": username, "password": password})
    if r.status_code != 200:
        raise SystemExit(f"登录失败 {username}: HTTP {r.status_code} {r.text[:300]}")
    data = r.json()
    for key in ("access_token", "refresh_token", "expires_in", "user"):
        assert key in data, f"登录响应缺字段 {key}"
    assert data["user"]["username"] == username
    return data["access_token"]


def _cleanup_smoke_user(username: str) -> None:
    """删掉本轮注册用例造的测试账号，避免每次冒烟都往 users 表里堆垃圾。

    直接用 ORM 而不是走接口：项目没有删用户的 API（也不该有）。
    导入失败时只提示、不算用例失败——本脚本的主体是 HTTP 冒烟，不该被环境耦合拖死。
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        import asyncio

        import app.db as db
        from app.models import EmailCode, User
    except Exception as exc:  # noqa: BLE001
        print(f"      提示：未能导入 ORM 清理测试账号（{type(exc).__name__}），"
              f"请手动删除 {username}")
        return

    async def _run() -> int:
        await db.init_db()
        try:
            deleted = await User.filter(username=username).delete()
            # 本轮用过的邮箱验证码也一并清掉
            await EmailCode.filter(email__startswith="smoke").delete()
            return deleted
        finally:
            await db.close_db()

    try:
        deleted = asyncio.run(_run())
        print(f"      已清理测试账号 {username}（删除 {deleted} 行）")
    except Exception as exc:  # noqa: BLE001
        print(f"      提示：清理测试账号失败（{type(exc).__name__}: {exc}），"
              f"请手动删除 {username}")


def _env_value(key: str) -> str:
    """读配置项（优先进程环境，其次 .env）。"""
    if os.environ.get(key):
        return os.environ[key].strip()
    env_path = os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env"
    )
    try:
        with open(env_path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(f"{key}="):
                    return line.split("=", 1)[1].strip()
    except OSError:
        pass
    return ""


def _smtp_owner() -> str:
    """真实通道下唯一允许发送的收件地址——即服务端自持的发件邮箱本身。"""
    return _env_value("MINDOPS_EMAIL_SMTP_USER")


def _reset_email_rows(email: str) -> None:
    """删掉某个邮箱的全部验证码记录，顺带清掉它的发送冷却。

    冒烟需要可重复执行：上一轮留下的记录会让本轮首封发送直接撞 60s 冷却。
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        import asyncio

        import app.db as db
        from app.models import EmailCode
    except Exception as exc:  # noqa: BLE001
        print(f"      提示：未能导入 ORM（{type(exc).__name__}），冷却未清理")
        return

    async def _run() -> None:
        await db.init_db()
        try:
            await EmailCode.filter(email=email).delete()
        finally:
            await db.close_db()

    try:
        asyncio.run(_run())
    except Exception as exc:  # noqa: BLE001
        print(f"      提示：清理 {email} 的验证码失败（{type(exc).__name__}）")


def _inject_email_code(email: str) -> str | None:
    """直接落库一枚已知验证码，供真实通道下的注册用例使用。

    为什么需要它：smtp 通道下接口**不返回**验证码（安全要求），而冒烟用的
    `smoke_*@qq.com` 是虚构地址、绝不能真发——否则退信会砸回管理员真实邮箱。
    于是把"验证码从哪来"这一步绕过去，仍让注册链路本身走真实 HTTP。

    哈希必须复用 `app.auth.email` 的真实实现，自己另写一套就等于没测校验逻辑。
    """
    sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
    try:
        import asyncio
        from datetime import timedelta

        import app.db as db
        from app.auth.email import _generate_salt, _hash_code, _model_now
        from app.config import settings
        from app.models import EmailCode
    except Exception as exc:  # noqa: BLE001
        print(f"      提示：未能导入 ORM（{type(exc).__name__}），无法注入验证码")
        return None

    code = "654321"
    salt = _generate_salt()

    async def _run() -> None:
        await db.init_db()
        try:
            await EmailCode.filter(email=email).delete()
            await EmailCode.create(
                email=email,
                code_hash=_hash_code(code, salt),
                salt=salt,
                attempts=0,
                max_attempts=settings.email_max_attempts,
                expires_at=_model_now() + timedelta(seconds=settings.email_code_ttl),
                status="pending",
            )
        finally:
            await db.close_db()

    try:
        asyncio.run(_run())
        return code
    except Exception as exc:  # noqa: BLE001
        print(f"      提示：注入 {email} 的验证码失败（{type(exc).__name__}: {exc}）")
        return None


def main() -> int:
    with httpx.Client(timeout=20.0, trust_env=False, follow_redirects=False) as client:
        # ---------------- F-10 认证 ----------------
        admin_token = login(client, *ADMIN)
        eng_token = login(client, *ENGINEER)
        check("POST /auth/login（admin + engineer）", True)

        ah = {"Authorization": f"Bearer {admin_token}"}
        eh = {"Authorization": f"Bearer {eng_token}"}

        r = client.get(f"{BASE}/auth/me", headers=ah)
        check("GET /auth/me", r.status_code == 200 and r.json().get("username") == ADMIN[0],
              f"HTTP {r.status_code} {r.text[:200]}")

        # 错误密码必须拒绝（P0-1：未开放自动注册）。
        # 用户名必须每次随机：早期版本曾在 .env 里开过 AUTH_ALLOW_AUTO_REGISTER，
        # 历史测试跑出来的账号会留在库里，用固定用户名会被"这个账号已存在"
        # 误写成"自动注册仍然开着"。
        ghost_user = f"ghost_{uuid.uuid4().hex[:12]}"
        r = client.post(f"{BASE}/auth/login",
                        json={"username": ghost_user, "password": "Whatever@123"})
        check("未知账号登录被拒绝（无自动注册）", r.status_code == 401,
              f"HTTP {r.status_code} {r.text[:200]}")

        # 已知账号用错密码也必须 401
        r = client.post(f"{BASE}/auth/login",
                        json={"username": ADMIN[0], "password": "WrongPassword@123"})
        check("密码错误返回 401", r.status_code == 401,
              f"HTTP {r.status_code} {r.text[:200]}")

        # refresh → logout
        rl = client.post(f"{BASE}/auth/login", json={"username": ADMIN[0], "password": ADMIN[1]})
        old_refresh = rl.json()["refresh_token"]
        r = client.post(f"{BASE}/auth/refresh", json={"refresh_token": old_refresh})
        ok_refresh = r.status_code == 200 and bool(r.json().get("access_token"))
        check("POST /auth/refresh", ok_refresh, f"HTTP {r.status_code} {r.text[:200]}")
        if ok_refresh:
            new_rt = r.json()["refresh_token"]
            r = client.post(f"{BASE}/auth/logout",
                            json={"refresh_token": new_rt}, headers=ah)
            check("POST /auth/logout", r.status_code in OK,
                  f"HTTP {r.status_code} {r.text[:200]}")
            # 轮换后旧 refresh 必须失效
            r = client.post(f"{BASE}/auth/refresh", json={"refresh_token": new_rt})
            check("登出后旧 refresh 失效（黑名单）", r.status_code == 401,
                  f"HTTP {r.status_code} {r.text[:200]}")

        # 重新登录，后续用例用新 token
        admin_token = login(client, *ADMIN)
        ah = {"Authorization": f"Bearer {admin_token}"}

        # ---------------- 短信登录已移除 ----------------
        # mock 短信通道会把 debug_code 直接回传给任何调用方，等于公开后门；
        # 登录入口已收敛为密码 + 邮箱验证码。这里锁住"不能悄悄加回来"。
        r = client.post(f"{BASE}/auth/sms/send", json={"phone": "13900000000"})
        check("POST /auth/sms/send 已下线 404", r.status_code == 404,
              f"HTTP {r.status_code} {r.text[:200]}")
        r = client.post(f"{BASE}/auth/sms/login", json={"phone": "13900000000", "code": "123456"})
        check("POST /auth/sms/login 已下线 404", r.status_code == 404,
              f"HTTP {r.status_code} {r.text[:200]}")

        # ---------------- 邮箱验证码注册 ----------------
        smoke_email = f"smoke_{uuid.uuid4().hex[:8]}@qq.com"
        smoke_user = f"smoke_{uuid.uuid4().hex[:8]}"

        r = client.get(f"{BASE}/auth/register/config")
        channel = None
        ok = r.status_code == 200
        if ok:
            cfg = r.json()
            channel = cfg.get("channel")
            ok = {"enabled", "channel", "roles", "code_length"} <= set(cfg)
        check("GET /auth/register/config（注册开关）", ok,
              f"HTTP {r.status_code} {r.text[:200]}")

        # 通道决定"验证码从哪来"，两条路都保持同一批注册用例的覆盖：
        # - mock：接口回传 debug_code，直接取用；
        # - smtp：接口【不】返回验证码（安全要求）。此时严禁拿 smoke_*@qq.com 去真发
        #   ——地址不存在，退信会砸回管理员真实邮箱。故只对自持邮箱真发一封验证通道，
        #   其余用例所需的验证码改为直接落库注入（_inject_email_code）。
        if channel != "smtp":
            r = client.post(f"{BASE}/auth/email/send", json={"email": smoke_email})
            ok = r.status_code == 200
            code = r.json().get("debug_code") if ok else None
            if ok:
                ok = code is not None      # mock 通道必须回传，否则本地无法联调
            check("POST /auth/email/send 下发验证码（mock 回传 debug_code）", ok,
                  f"HTTP {r.status_code} {r.text[:200]}")

            r = client.post(f"{BASE}/auth/email/send", json={"email": smoke_email})
            check("邮箱冷却期内重复发送被限流 429", r.status_code == 429,
                  f"HTTP {r.status_code} {r.text[:200]}")
        else:
            real_to = _smtp_owner()
            if not real_to:
                check("POST /auth/email/send 真实通道下发且不回传验证码", False,
                      "未找到 MINDOPS_EMAIL_SMTP_USER，无法在 smtp 通道下安全冒烟")
            else:
                _reset_email_rows(real_to)   # 清上一轮残留，保证连跑两次也能过
                r = client.post(f"{BASE}/auth/email/send", json={"email": real_to})
                ok = r.status_code == 200 and "debug_code" not in r.json()
                check("POST /auth/email/send 真实通道下发且不回传验证码", ok,
                      f"HTTP {r.status_code} {r.text[:200]}")

                r = client.post(f"{BASE}/auth/email/send", json={"email": real_to})
                check("邮箱冷却期内重复发送被限流 429（真实通道）", r.status_code == 429,
                      f"HTTP {r.status_code} {r.text[:200]}")

                # 注意：这里**不**清掉刚发出的那条记录。它是真的发到了自持邮箱，
                # 留着才能在收件箱里点开用；清掉会让人"收到了码但用不了"。
                # 记录数由本轮开头的那次 _reset_email_rows 兜住，不会累积。

            code = _inject_email_code(smoke_email)
            check("smtp 通道下注入已知验证码供注册用例使用", bool(code))

        r = client.post(f"{BASE}/auth/email/send", json={"email": "not-an-email"})
        check("非法邮箱发送被拒 422", r.status_code == 422,
              f"HTTP {r.status_code} {r.text[:200]}")

        # role 字段由服务端忽略：注册一律 engineer，admin 只发给
        # MINDOPS_BOOTSTRAP_ADMIN_EMAIL 指定的邮箱。仍传值是为了验证
        # "旧客户端带 role 也不报错"，下方断言按实际返回的 engineer 校验。
        payload = {"email": smoke_email, "code": code, "username": smoke_user,
                   "password": "Smoke@12345", "role": "engineer"}
        r = client.post(f"{BASE}/auth/register", json={**payload, "code": "000000"})
        check("错误验证码注册被拒 400", r.status_code == 400,
              f"HTTP {r.status_code} {r.text[:200]}")

        r = client.post(f"{BASE}/auth/register", json={**payload, "password": "123"})
        check("弱密码注册被拒 422", r.status_code == 422,
              f"HTTP {r.status_code} {r.text[:200]}")

        # V1.1（2026-09-12）注册已移除角色自选：请求里的 role 被直接忽略，一律
        # 创建 engineer 账号，因此不存在"非法角色 422"这条分支。这里刻意不再发第二次
        # 注册请求——验证码单次使用，多打一次会消耗掉下面「正常注册」用例的 code。

        # 正常注册：应直接拿到 token pair（不需要再登一次）
        r = client.post(f"{BASE}/auth/register", json=payload)
        ok = r.status_code == 200
        new_token = None
        if ok:
            body = r.json()
            ok = (bool(body.get("access_token")) and bool(body.get("refresh_token"))
                  and body.get("user", {}).get("role") == "engineer"
                  and body.get("user", {}).get("email") == smoke_email)
            new_token = body.get("access_token")
        check("POST /auth/register 注册成功并直接签发 token", ok,
              f"HTTP {r.status_code} {r.text[:300]}")

        if new_token:
            r = client.get(f"{BASE}/auth/me", headers={"Authorization": f"Bearer {new_token}"})
            ok = r.status_code == 200 and r.json().get("username") == smoke_user
            check("注册返回的 token 可直接调用 /auth/me", ok,
                  f"HTTP {r.status_code} {r.text[:200]}")

        # 查重：必须"新邮箱 + 该邮箱的新验证码"，因为验证码是单次使用且按邮箱绑定的。
        # 拿 A 邮箱的码去注册 B 邮箱只会撞 400 INVALID_CODE，走不到查重分支。
        other_email = f"smoke2_{uuid.uuid4().hex[:8]}@qq.com"
        if channel != "smtp":
            r = client.post(f"{BASE}/auth/email/send", json={"email": other_email})
            other_code = r.json().get("debug_code") if r.status_code == 200 else None
        else:
            other_code = _inject_email_code(other_email)
        r = client.post(f"{BASE}/auth/register",
                        json={"email": other_email, "code": other_code,
                              "username": smoke_user, "password": "Smoke@12345",
                              "role": "engineer"})
        ok = r.status_code == 409 and r.json().get("detail", {}).get("code") == "USERNAME_TAKEN"
        check("重复用户名注册被拒 409 USERNAME_TAKEN", ok,
              f"HTTP {r.status_code} {r.text[:200]}")

        r = client.post(f"{BASE}/auth/login",
                        json={"username": smoke_user, "password": "Smoke@12345"})
        check("新注册账号可用密码登录", r.status_code == 200,
              f"HTTP {r.status_code} {r.text[:200]}")

        _cleanup_smoke_user(smoke_user)

        # ---------------- F-01 告警 ----------------
        r = client.get(f"{BASE}/alerts/recent", params={"minutes": 1440, "page": 1, "size": 5},
                       headers=ah)
        ok = r.status_code == 200
        if ok:
            body = r.json()
            need = {"items", "total"}
            ok = need <= set(body)
            if ok and body["items"]:
                it = body["items"][0]
                missing = [
                    f for f in ("id", "source", "component", "severity", "title",
                                "fingerprint", "incident_id", "integration_id",
                                "task_names", "incident_status", "dedup_count",
                                "last_seen_at", "created_at")
                    if f not in it
                ]
                ok = not missing
                if missing:
                    print(f"      告警字段缺失: {missing}")
        check("GET /alerts/recent（字段对齐 AlertDTO）", ok, f"HTTP {r.status_code} {r.text[:200]}")

        # 筛选参数
        for label, params in [
            ("status=unclustered", {"minutes": 1440, "status": "unclustered"}),
            ("task_id=none", {"minutes": 1440, "task_id": "none"}),
            ("severity=P1", {"minutes": 1440, "severity": "P1"}),
            ("q=关键字", {"minutes": 1440, "q": "Redis"}),
        ]:
            r = client.get(f"{BASE}/alerts/recent", params=params, headers=ah)
            check(f"GET /alerts/recent 筛选 {label}", r.status_code == 200,
                  f"HTTP {r.status_code} {r.text[:200]}")

        # 手动注入告警（admin-only）
        title = f"e2e-smoke-{__import__('time').time():.0f}"
        r = client.post(f"{BASE}/alerts/manual", headers=ah,
                        json={"component": "e2e-smoke", "severity": "P2",
                              "title": title, "detail": "联调冒烟", "trigger_pipeline": False})
        ok = r.status_code == 200 and "alert_id" in r.json()
        check("POST /alerts/manual（admin）", ok, f"HTTP {r.status_code} {r.text[:200]}")
        new_alert_id = r.json().get("alert_id") if ok else None

        # engineer 不允许手动注入
        r = client.post(f"{BASE}/alerts/manual", headers=eh,
                        json={"component": "x", "severity": "P2", "title": title})
        check("engineer 调 /alerts/manual 被拒 403", r.status_code == 403,
              f"HTTP {r.status_code} {r.text[:200]}")

        # 主动上报
        r = client.post(f"{BASE}/alerts", headers=ah,
                        json={"component": "e2e-smoke", "severity": "P2",
                              "title": title + "-app", "source": "app"})
        check("POST /alerts（主动上报）", r.status_code == 200,
              f"HTTP {r.status_code} {r.text[:200]}")

        # ---------------- F-02 事件 ----------------
        r = client.get(f"{BASE}/incidents", params={"page": 1, "size": 5}, headers=ah)
        ok = r.status_code == 200
        inc_id = None
        if ok:
            body = r.json()
            ok = {"items", "total"} <= set(body)
            if body.get("items"):
                it = body["items"][0]
                inc_id = it.get("id")
                missing = [f for f in ("id", "title", "component", "severity", "status",
                                       "alert_count", "last_seen_at", "root_cause",
                                       "root_cause_confidence", "created_at")
                           if f not in it]
                if missing:
                    ok = False
                    print(f"      事件列表字段缺失: {missing}")
        check("GET /incidents（字段对齐 IncidentItemDTO）", ok,
              f"HTTP {r.status_code} {r.text[:200]}")

        if inc_id:
            r = client.get(f"{BASE}/incidents/{inc_id}", headers=ah)
            ok = r.status_code == 200
            if ok:
                d = r.json()
                need = {"incident", "alerts", "steps", "evidence", "actions"}
                ok = need <= set(d)
                if not ok:
                    print(f"      详情缺少键: {need - set(d)}")
                else:
                    inc = d["incident"]
                    missing = [f for f in ("id", "title", "component", "severity", "status",
                                           "root_cause", "root_cause_confidence",
                                           "root_cause_source", "actual_cause")
                               if f not in inc]
                    if missing:
                        ok = False
                        print(f"      incident 字段缺失: {missing}")
            check("GET /incidents/{id}（字段对齐 IncidentDetailDTO）", ok,
                  f"HTTP {r.status_code} {r.text[:200]}")

            r = client.get(f"{BASE}/incidents/{inc_id}/evidence", headers=ah)
            check("GET /incidents/{id}/evidence", r.status_code == 200,
                  f"HTTP {r.status_code} {r.text[:200]}")

            r = client.get(f"{BASE}/incidents/{inc_id}/actions", headers=ah)
            ok = r.status_code == 200
            action_id = None
            if ok:
                # 只挑 pending 的 action：已批准/已驳回的再 confirm 会得到
                # 422 "action is not pending"，那是正确的业务响应，
                # 不该被算成接口故障。
                pending = [a for a in r.json().get("items", [])
                           if a.get("status") == "pending"]
                target = pending[0] if pending else None
                if target:
                    action_id = target.get("id")
                    missing = [f for f in ("id", "command", "risk_level",
                                           "requires_confirm", "status") if f not in target]
                    if missing:
                        ok = False
                        print(f"      action 字段缺失: {missing}")
            check("GET /incidents/{id}/actions", ok, f"HTTP {r.status_code} {r.text[:200]}")

            r = client.get(f"{BASE}/incidents/{inc_id}/loop-audits", headers=ah)
            check("GET /incidents/{id}/loop-audits", r.status_code == 200,
                  f"HTTP {r.status_code} {r.text[:200]}")

            r = client.post(f"{BASE}/incidents/{inc_id}/diagnose", headers=ah,
                            json={"force": True})
            check("POST /incidents/{id}/diagnose", r.status_code in OK,
                  f"HTTP {r.status_code} {r.text[:200]}")

            if action_id:
                r = client.post(f"{BASE}/incidents/{inc_id}/confirm", headers=ah,
                                json={"action_id": action_id, "decision": "approve"})
                check("POST /incidents/{id}/confirm", r.status_code in OK,
                      f"HTTP {r.status_code} {r.text[:200]}")

            r = client.post(f"{BASE}/incidents/_recluster", headers=ah,
                            json={"component": "e2e-smoke", "dry_run": True})
            check("POST /incidents/_recluster（dry_run）", r.status_code in OK,
                  f"HTTP {r.status_code} {r.text[:200]}")

        # ---------------- F-04 置信度 ----------------
        # 注意：真实路径是 /config/confidence（confidence 路由的 prefix 是 /config），
        # 不是 /confidence。写错会得到 404，曾一度被误判成后端故障。
        r = client.get(f"{BASE}/config/confidence", headers=ah)
        ok = r.status_code == 200 and "conf_threshold" in r.json()
        check("GET /confidence", ok, f"HTTP {r.status_code} {r.text[:200]}")

        r = client.put(f"{BASE}/config/confidence", headers=ah, json={"conf_threshold": 0.6})
        check("PUT /config/confidence（admin）", r.status_code in OK,
              f"HTTP {r.status_code} {r.text[:200]}")

        r = client.put(f"{BASE}/config/confidence", headers=eh, json={"conf_threshold": 0.6})
        check("engineer 调 PUT /config/confidence 被拒 403", r.status_code == 403,
              f"HTTP {r.status_code} {r.text[:200]}")

        # ---------------- F-09 大模型 ----------------
        r = client.get(f"{BASE}/llm/providers", headers=ah)
        check("GET /llm/providers", r.status_code == 200 and "providers" in r.json(),
              f"HTTP {r.status_code} {r.text[:200]}")

        r = client.get(f"{BASE}/llm/presets", headers=ah)
        check("GET /llm/presets", r.status_code == 200 and "presets" in r.json(),
              f"HTTP {r.status_code} {r.text[:200]}")

        r = client.get(f"{BASE}/llm/presets", headers=eh)
        check("engineer 调 /llm/presets 被拒 403", r.status_code == 403,
              f"HTTP {r.status_code} {r.text[:200]}")

        # ---------------- F-11 集成 ----------------
        r = client.get(f"{BASE}/integrations", headers=ah)
        ok = r.status_code == 200
        if ok and r.json().get("items"):
            it = r.json()["items"][0]
            missing = [f for f in ("id", "name", "type", "base_url", "status") if f not in it]
            if missing:
                ok = False
                print(f"      集成字段缺失: {missing}")
        check("GET /integrations", ok, f"HTTP {r.status_code} {r.text[:200]}")

        r = client.post(f"{BASE}/integrations", headers=eh,
                        json={"name": "x", "base_url": "http://127.0.0.1:1"})
        check("engineer 调 POST /integrations 被拒 403", r.status_code == 403,
              f"HTTP {r.status_code} {r.text[:200]}")

        # SSRF 守护：公网地址必须被拒（体现白名单生效，而非接口坏了）
        r = client.post(f"{BASE}/integrations", headers=ah,
                        json={"name": "e2e-public", "base_url": "http://8.8.8.8:9090"})
        check("POST /integrations 公网地址被 SSRF 拦截", r.status_code in (400, 403, 422),
              f"HTTP {r.status_code} {r.text[:200]}")

        # ---------------- F-12 任务 ----------------
        r = client.get(f"{BASE}/tasks", params={"page": 1, "size": 5}, headers=ah)
        ok = r.status_code == 200 and {"items", "total", "page", "size"} <= set(r.json())
        check("GET /tasks（字段对齐 TaskListOut）", ok, f"HTTP {r.status_code} {r.text[:200]}")

        task_name = f"e2e-task-{__import__('time').time():.0f}"
        r = client.post(f"{BASE}/tasks", headers=ah,
                        json={"name": task_name, "description": "联调冒烟"})
        ok = r.status_code == 201
        task_id = r.json().get("id") if ok else None
        if ok:
            missing = [f for f in ("id", "name", "status", "target_count") if f not in r.json()]
            if missing:
                ok = False
                print(f"      任务字段缺失: {missing}")
        check("POST /tasks（admin）", ok, f"HTTP {r.status_code} {r.text[:200]}")

        if task_id:
            r = client.get(f"{BASE}/tasks/{task_id}", headers=ah)
            ok = r.status_code == 200 and "targets" in r.json()
            check("GET /tasks/{id}", ok, f"HTTP {r.status_code} {r.text[:200]}")

            r = client.put(f"{BASE}/tasks/{task_id}", headers=ah, json={"status": "paused"})
            check("PUT /tasks/{id}（改状态）", r.status_code in OK,
                  f"HTTP {r.status_code} {r.text[:200]}")

            r = client.delete(f"{BASE}/tasks/{task_id}", headers=ah)
            check("DELETE /tasks/{id}（归档）", r.status_code in OK,
                  f"HTTP {r.status_code} {r.text[:200]}")

        r = client.post(f"{BASE}/tasks", headers=eh, json={"name": "x"})
        check("engineer 调 POST /tasks 被拒 403", r.status_code == 403,
              f"HTTP {r.status_code} {r.text[:200]}")

        # ---------------- F-12 / FR-10 任务归属与告警筛选 ----------------
        # 告警**不持有** task_id：归属关系由 `alerts.integration_id` 经
        # `task_targets` 反查得出。因此这条用例覆盖三段真实链路——
        # 带 integration_id 入库 → 按任务筛出 → 不泄漏进"未归属"。
        # 前端 AlertsView 的「任务」下拉（docs/30 §8.5）直接依赖它。
        r = client.get(f"{BASE}/integrations", headers=ah)
        active_int = None
        if r.status_code == 200:
            active_int = next(
                (i for i in r.json().get("items", []) if i.get("status") == "active"), None
            )

        if active_int is None:
            check("FR-10 前置：存在 active 集成目标可供绑定", False,
                  "没有 active 的集成目标，无法验证归属链路")
        else:
            iid = active_int["id"]
            owner_name = f"e2e-owner-{uuid.uuid4().hex[:8]}"
            r = client.post(f"{BASE}/tasks", headers=ah,
                            json={"name": owner_name, "description": "FR-10 归属验证"})
            owner_task = r.json().get("id") if r.status_code == 201 else None
            check("FR-10 前置：创建归属测试任务", owner_task is not None,
                  f"HTTP {r.status_code} {r.text[:200]}")

            if owner_task is not None:
                r = client.post(f"{BASE}/tasks/{owner_task}/targets", headers=ah,
                                json={"integration_id": iid})
                check("FR-10 前置：任务绑定目标", r.status_code in (200, 201),
                      f"HTTP {r.status_code} {r.text[:200]}")

                own_title = f"e2e-owner-alert-{uuid.uuid4().hex[:8]}"
                r = client.post(f"{BASE}/alerts/manual", headers=ah,
                                json={"component": "e2e-owner", "severity": "P2",
                                      "title": own_title, "integration_id": iid,
                                      "trigger_pipeline": False})
                check("FR-10 带 integration_id 的告警可入库", r.status_code == 200,
                      f"HTTP {r.status_code} {r.text[:200]}")

                def _fetch(**params):
                    rr = client.get(f"{BASE}/alerts/recent",
                                    params={"minutes": 60, "size": 200, **params}, headers=ah)
                    if rr.status_code != 200:
                        return None
                    return {a["title"]: a["task_names"] for a in rr.json()["items"]}

                by_task = _fetch(task_id=owner_task)
                check("FR-10 task_id=<id> 能筛出该任务下的告警",
                      by_task is not None and own_title in by_task,
                      f"HTTP 非 200 或未命中（task_id={owner_task}）")

                names = (_fetch(task_id=owner_task) or {}).get(own_title)
                # task_names 回填的是该告警所属 integration 下**全部** active 任务名，
                # 而非"仅本脚本创建的那个任务"。运行库中 integration 138 通常还被
                # 「日常巡检」等长期任务绑定，故断言"包含"而非"相等"。
                check("FR-10 task_names 回填归属任务名",
                      names is not None and owner_name in names,
                      f"task_names={names!r}，期望包含 {owner_name!r}")

                unassigned = _fetch(task_id="none")
                check("FR-10 已归属告警不泄漏进 task_id=none",
                      unassigned is not None and own_title not in unassigned,
                      "未归属列表里混入了已归属告警")

                # 清理：归档测试任务，避免污染演示环境的任务列表
                client.delete(f"{BASE}/tasks/{owner_task}", headers=ah)

        # ---------------- F-05 知识库 ----------------
        r = client.get(f"{BASE}/knowledge", params={"page": 1, "page_size": 5}, headers=ah)
        check("GET /knowledge", r.status_code == 200, f"HTTP {r.status_code} {r.text[:200]}")

        r = client.post(f"{BASE}/knowledge/search", headers=ah,
                        json={"query": "Redis 内存", "top_k": 3})
        check("POST /knowledge/search", r.status_code in OK,
              f"HTTP {r.status_code} {r.text[:200]}")

        # ---------------- 看板 / SSE ----------------
        r = client.get(f"{BASE}/metrics/dashboard", params={"range": "7d"}, headers=ah)
        ok = r.status_code == 200
        if ok:
            body = r.json()
            if not isinstance(body, dict) or not body:
                ok = False
                print("      看板返回空")
        check("GET /metrics/dashboard", ok, f"HTTP {r.status_code} {r.text[:200]}")

        r = client.get(f"{BASE}/metrics/dashboard", headers=eh)
        check("engineer 调 /metrics/dashboard 被拒 403", r.status_code == 403,
              f"HTTP {r.status_code} {r.text[:200]}")

        r = client.get(f"{BASE}/stream/stats", headers=ah)
        check("GET /stream/stats", r.status_code == 200 and "subscribers" in r.json(),
              f"HTTP {r.status_code} {r.text[:200]}")

        r = client.get(f"{BASE}/stream/alerts", params={"token": "__bad__"})
        check("SSE 无效 token 被拒 401", r.status_code == 401,
              f"HTTP {r.status_code} {r.text[:200]}")

        # SSE 真实连通：连上后立即 POST 一条告警，10s 内应收到 event: alert
        received: list[str] = []
        title_sse = f"e2e-sse-{__import__('time').time():.0f}"
        try:
            with client.stream("GET", f"{BASE}/stream/alerts",
                               params={"token": admin_token}, timeout=12.0) as resp:
                if resp.status_code != 200:
                    check("GET /stream/alerts（SSE）", False,
                          f"HTTP {resp.status_code}")
                else:
                    import threading

                    def push() -> None:
                        import time as _t
                        _t.sleep(1.0)
                        # 必须用**独立**的 client：httpx.Client 在有一个 stream
                        # 连接打开时，同一实例再发起请求会互相阻塞，导致这里
                        # 推不出去、外层读不到帧（看起来像"SSE 坏了"）。
                        try:
                            with httpx.Client(timeout=10.0, trust_env=False) as c:
                                c.post(f"{BASE}/alerts/manual", headers=ah,
                                       json={"component": "e2e-sse", "severity": "P2",
                                             "title": title_sse, "trigger_pipeline": False})
                        except Exception:
                            pass

                    threading.Thread(target=push, daemon=True).start()
                    buf = ""
                    for chunk in resp.iter_text():
                        buf += chunk
                        if title_sse in buf:
                            received.append(buf)
                            break
                        if len(buf) > 65536:
                            buf = buf[-4096:]
                    check("GET /stream/alerts（SSE 收到实时告警）", bool(received),
                          "10s 内未收到推送")
        except Exception as exc:  # 超时属于预期（SSE 长连接不会主动关闭）
            if not received:
                check("GET /stream/alerts（SSE）", False, f"{type(exc).__name__}: {exc}")

        # 未认证访问必须 401（前端 401 兜底依赖此行为）
        r = client.get(f"{BASE}/alerts/recent")
        check("无 token 访问受保护接口 401", r.status_code == 401,
              f"HTTP {r.status_code} {r.text[:200]}")

    total = len(results)
    failed = [n for n, ok, _ in results if not ok]
    print("\n" + "=" * 60)
    print(f"用例总数 {total} / 通过 {total - len(failed)} / 失败 {len(failed)}")
    if failed:
        print("失败项：")
        for n in failed:
            print(f"  - {n}")
    print("=" * 60)
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
