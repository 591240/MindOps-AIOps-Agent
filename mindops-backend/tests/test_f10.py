"""F-10 双方式登录测试（密码 + 邮箱验证码）。

单独运行：`pytest tests/test_f10.py -p no:cacheprovider`
"""

import asyncio
import os
import uuid

os.environ.setdefault("MINDOPS_MYSQL_DB", "mindops_f10_test")
os.environ.setdefault("MINDOPS_EMAIL_PROVIDER", "mock")

from fastapi.testclient import TestClient
import pytest

from app.auth.email import MOCK_CODE
from app.main import app


def _setup():
    """在子进程里初始化 DB（彻底隔离事件循环）。"""
    import subprocess, sys
    subprocess.run([sys.executable, "-c", """
import asyncio, os
os.environ.setdefault("MINDOPS_MYSQL_DB", "mindops_f10_test")
os.environ.setdefault("MINDOPS_EMAIL_PROVIDER", "mock")
asyncio.run(__import__('app.db', fromlist=['init_db']).init_db())
"""], timeout=30, check=True)


def _login(c, username="sre_admin", password="Admin@123"):
    r = c.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return r.json()["access_token"], r.json()["refresh_token"]


def test_password_login():
    _setup()
    with TestClient(app) as c:
        r = c.post("/api/v1/auth/login", json={"username": "sre_admin", "password": "Admin@123"})
        assert r.status_code == 200
        body = r.json()
        assert "access_token" in body and "refresh_token" in body
        assert body["user"]["role"] == "admin"


def test_email_send_mock():
    with TestClient(app) as c:
        r = c.post("/api/v1/auth/email/send", json={"email": f"{uuid.uuid4().hex[:8]}@example.com"})
        assert r.status_code == 200
        # mock 通道回传纯数字验证码，前端验证码输入框只能敲数字
        assert r.json()["debug_code"] == MOCK_CODE
        assert "ttl" in r.json()


def test_email_login():
    """注册 → 邮箱验证码登录 → /auth/me 记录 last_login_method=email。"""
    tag = uuid.uuid4().hex[:8]
    email = f"login_{tag}@example.com"
    username = f"email_{tag}"
    with TestClient(app) as c:
        r = c.post("/api/v1/auth/email/send", json={"email": email})
        assert r.status_code == 200, r.text

        r = c.post("/api/v1/auth/register", json={
            "email": email, "code": MOCK_CODE,
            "username": username, "password": "Passw0rd!123",
        })
        assert r.status_code == 200, r.text

        # 注册用的码已消费；同邮箱 60s 冷却内不能再发。测试里把冷却压到 0，
        # 只影响当前进程的 settings 对象，退出前恢复。
        import app.auth.email as email_mod

        old_interval = email_mod.settings.email_send_interval
        email_mod.settings.email_send_interval = 0
        try:
            r = c.post("/api/v1/auth/email/login/send", json={"email": email})
            assert r.status_code == 200, r.text
            r = c.post("/api/v1/auth/email/login", json={"email": email, "code": MOCK_CODE})
        finally:
            email_mod.settings.email_send_interval = old_interval

        assert r.status_code == 200, r.text
        body = r.json()
        assert body["user"]["username"] == username
        assert "access_token" in body and "refresh_token" in body

        me = c.get(
            "/api/v1/auth/me",
            headers={"Authorization": f"Bearer {body['access_token']}"},
        )
        assert me.status_code == 200
        assert me.json()["last_login_method"] == "email"


def test_email_login_wrong_code():
    with TestClient(app) as c:
        email = f"wrong_{uuid.uuid4().hex[:8]}@example.com"
        c.post("/api/v1/auth/email/send", json={"email": email})
        r = c.post("/api/v1/auth/email/login", json={"email": email, "code": "000000"})
        assert r.status_code == 401
        assert r.json()["detail"]["code"] == "INVALID_CODE"


def test_email_login_unknown_email():
    """验证码正确但邮箱未注册：403 USER_NOT_FOUND，不自动建号。"""
    with TestClient(app) as c:
        email = f"ghost_{uuid.uuid4().hex[:8]}@example.com"
        c.post("/api/v1/auth/email/send", json={"email": email})
        r = c.post("/api/v1/auth/email/login", json={"email": email, "code": MOCK_CODE})
        assert r.status_code == 403
        assert r.json()["detail"]["code"] == "USER_NOT_FOUND"


def test_email_login_send_does_not_mail_unknown_address():
    """登录发码只对已注册邮箱生效：未注册地址不发信，响应也不暴露是否存在。"""
    with TestClient(app) as c:
        email = f"ghost_{uuid.uuid4().hex[:8]}@example.com"
        r = c.post("/api/v1/auth/email/login/send", json={"email": email})
        assert r.status_code == 200
        assert "debug_code" not in r.json()
        # 没有落库验证码，固定 mock 码也登不进去
        r2 = c.post("/api/v1/auth/email/login", json={"email": email, "code": MOCK_CODE})
        assert r2.status_code == 401
        assert r2.json()["detail"]["code"] == "INVALID_CODE"


def test_register_checks_code_before_duplicate_lookup():
    """未控制邮箱的人不能用注册接口枚举邮箱是否已被占用。"""
    tag = uuid.uuid4().hex[:8]
    email = f"enum_{tag}@example.com"
    username = f"enum_{tag}"
    with TestClient(app) as c:
        c.post("/api/v1/auth/email/send", json={"email": email})
        r = c.post("/api/v1/auth/register", json={
            "email": email, "code": MOCK_CODE,
            "username": username, "password": "Passw0rd!123",
        })
        assert r.status_code == 200, r.text

        # 错误验证码 + 已占用邮箱：必须是 400，不能提前返回 EMAIL_TAKEN
        r = c.post("/api/v1/auth/register", json={
            "email": email, "code": "000000",
            "username": f"{username}_x", "password": "Passw0rd!123",
        })
        assert r.status_code == 400, r.text
        assert r.json()["detail"]["code"] == "INVALID_CODE"

        # 正确验证码 + 已占用邮箱：才返回 EMAIL_TAKEN
        import app.auth.email as email_mod

        old_interval = email_mod.settings.email_send_interval
        email_mod.settings.email_send_interval = 0
        try:
            c.post("/api/v1/auth/email/send", json={"email": email})
            r = c.post("/api/v1/auth/register", json={
                "email": email, "code": MOCK_CODE,
                "username": f"{username}_y", "password": "Passw0rd!123",
            })
        finally:
            email_mod.settings.email_send_interval = old_interval
        assert r.status_code == 409, r.text
        assert r.json()["detail"]["code"] == "EMAIL_TAKEN"


def test_stream_ticket_scoped_and_access_token_rejected_in_query():
    """SSE query 只接受短票；长效 access token 放进 URL 会被拒绝。"""
    from app.auth.jwt import decode_stream_ticket

    with TestClient(app) as c:
        access, _ = _login(c)
        r = c.post(
            "/api/v1/auth/stream-ticket",
            headers={"Authorization": f"Bearer {access}"},
        )
        assert r.status_code == 200, r.text
        payload = decode_stream_ticket(r.json()["ticket"])
        assert payload["type"] == "stream"
        assert payload["sub"]

        # access token 不再作为 query 凭证被接受
        r = c.get("/api/v1/stream/alerts", params={"token": access})
        assert r.status_code == 401
        assert r.json()["detail"]["code"] == "INVALID_TOKEN"


def test_sms_routes_removed():
    """短信登录已整体移除（mock 通道的 debug_code 等于公开后门）。"""
    with TestClient(app) as c:
        assert c.post("/api/v1/auth/sms/send", json={"phone": "13800138000"}).status_code == 404
        assert c.post(
            "/api/v1/auth/sms/login",
            json={"phone": "13800138000", "code": "123456"},
        ).status_code == 404


def test_password_lockout():
    with TestClient(app) as c:
        c.post("/api/v1/auth/login", json={"username": "lockout_test", "password": "pass"})
        for _ in range(5):
            r = c.post("/api/v1/auth/login", json={"username": "lockout_test", "password": "wrong"})
        assert r.status_code == 423
        assert r.json()["detail"]["code"] == "ACCOUNT_LOCKED"

    # 本用例把 lockout_test（id=2）锁死；清理动作统一放在模块级 fixture 里
    # （见文件末尾 `_reset_auth_state`），避免与其他用例的 TestClient 事件循环冲突。


@pytest.fixture(scope="module", autouse=True)
def _reset_auth_state():
    """模块前后清场：邮箱验证码记录 + 密码错误计数。

    1) `email_codes`：发送限流是「同一邮箱 60s 内只允许一条」，
       判定依据是 email_codes 表里该邮箱的最新 created_at。
       表若不清理，**重跑本模块**（或与其它模块连跑）时会命中 429，
       表现为"上一次运行的残留把这一次挡住" —— 典型的测试不可重复。
    2) `pw_fail:{user_id}`：计数存在**进程级全局** store 里
       （Redis 不可用时的内存降级，见 app/auth/blacklist.py）。
       test_password_lockout 把 id=2 锁死，而其他测试库的同 id 用户
       （如 mindops_f11_test 的 sre001）会被同一把键连带锁定。

    放在模块级 fixture（而非用例内）是为了避开 TestClient 的事件循环管理。
    """
    _clear_email_codes()
    yield

    async def _clear():
        import app.auth.blacklist as bl

        store = await bl._get_store()
        inner = getattr(store, "_store", None)
        if isinstance(inner, dict):            # 内存降级
            for k in list(inner):
                if k.startswith("pw_fail:"):
                    inner.pop(k, None)
            return
        keys = await store.keys("pw_fail:*")   # Redis
        if keys:
            await store.delete(*keys)

    asyncio.run(_clear())


def _clear_email_codes():
    """清空本模块测试库的 email_codes（子进程执行，隔离事件循环）。"""
    import subprocess, sys

    subprocess.run([sys.executable, "-c", """
import asyncio, os
os.environ.setdefault("MINDOPS_MYSQL_DB", "mindops_f10_test")
from tortoise import Tortoise
from app.config import settings

async def m():
    await Tortoise.init(config={
        "connections": {"default": settings.database_url},
        "apps": {"models": {"models": ["app.models"], "default_connection": "default"}},
        "use_tz": False, "timezone": "Asia/Shanghai",
    })
    from app.models import EmailCode
    await EmailCode.all().delete()
    await Tortoise.close_connections()

asyncio.run(m())
"""], timeout=60, check=True)


def test_logout():
    with TestClient(app) as c:
        token, refresh = _login(c)
        r = c.post("/api/v1/auth/logout",
                   json={"refresh_token": refresh},
                   headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["message"] == "已登出"


def test_refresh_token_rotation():
    with TestClient(app) as c:
        _, refresh = _login(c)
        r = c.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
        assert r.status_code == 200
        assert "access_token" in r.json()
        r2 = c.post("/api/v1/auth/refresh", json={"refresh_token": refresh})
        assert r2.status_code == 401


def test_email_interval_limit():
    with TestClient(app) as c:
        email = f"interval_{uuid.uuid4().hex[:8]}@example.com"
        c.post("/api/v1/auth/email/send", json={"email": email})
        r = c.post("/api/v1/auth/email/send", json={"email": email})
        assert r.status_code == 429
        assert r.json()["detail"]["code"] == "EMAIL_SEND_INTERVAL"


def test_auth_me():
    with TestClient(app) as c:
        token, _ = _login(c)
        r = c.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert r.status_code == 200
        assert r.json()["username"] == "sre_admin"
        assert r.json()["last_login_method"] == "password"
