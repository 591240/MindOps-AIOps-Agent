"""公网部署前的安全收口回归（注册角色 / webhook 令牌 / 验证码 IP 限流）。

这三处都落在**未鉴权入口**上，此前的 145 条用例一条都没覆盖到，
所以这里的意义不是"补数量"，而是"锁住不能被回退的行为"。

单独运行：`pytest tests/test_sec_hardening.py -p no:cacheprovider`
"""

import os
import subprocess
import sys
import time

os.environ.setdefault("MINDOPS_MYSQL_DB", "mindops_sec_hardening_test")
os.environ.setdefault("MINDOPS_EMAIL_PROVIDER", "mock")
os.environ.setdefault("MINDOPS_REGISTER_ENABLED", "true")

from fastapi.testclient import TestClient

from app.alerts import webhook as webhook_mod
from app.alerts.webhook import check_ip_whitelist
from app.auth.email import MOCK_CODE
from app.main import app
from app.routers import auth as auth_router

_DB = "mindops_sec_hardening_test"
_WEBHOOK = "/api/v1/alerts/webhook"


def _ensure_database() -> None:
    """`init_db()` 只建表不建库，先补上本模块的测试库（与 test_f01 同法）。"""
    import pymysql

    from app.config import settings

    conn = pymysql.connect(
        host=settings.mysql_host, port=settings.mysql_port,
        user=settings.mysql_user, password=settings.mysql_password,
    )
    try:
        with conn.cursor() as cur:
            cur.execute(
                f"CREATE DATABASE IF NOT EXISTS `{settings.mysql_db}` "
                "CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci"
            )
        conn.commit()
    finally:
        conn.close()


def _setup() -> None:
    """建库 + 在子进程里建表（与 test_f10 同因：避免污染 TestClient 的事件循环）。"""
    _ensure_database()
    subprocess.run([sys.executable, "-c", f"""
import asyncio, os
os.environ.setdefault("MINDOPS_MYSQL_DB", "{_DB}")
os.environ.setdefault("MINDOPS_EMAIL_PROVIDER", "mock")
os.environ.setdefault("MINDOPS_REGISTER_ENABLED", "true")
asyncio.run(__import__('app.db', fromlist=['init_db']).init_db())
"""], timeout=60, check=True)


def _uniq(prefix: str) -> str:
    return f"{prefix}{int(time.time() * 1000) % 100000000}"


def _register(client, email: str, username: str) -> dict:
    r = client.post("/api/v1/auth/email/send", json={"email": email})
    assert r.status_code == 200, r.text
    r = client.post("/api/v1/auth/register", json={
        "email": email, "code": MOCK_CODE,
        "username": username, "password": "Passw0rd!x",
    })
    assert r.status_code == 200, r.text
    return r.json()["user"]


def test_register_creates_engineer_not_admin():
    """回归阻断项：自助注册曾一律产出 admin（任何人都能开管理员号）。"""
    _setup()
    with TestClient(app) as c:
        user = _register(c, f"{_uniq('sec')}@example.com", _uniq("secu"))
        assert user["role"] == "engineer", user


def test_bootstrap_admin_email_gets_admin(monkeypatch):
    """关掉"人人管理员"之后必须留下建管理员的通道，否则生产把自己锁死。"""
    _setup()
    email = f"{_uniq('boss')}@example.com"
    monkeypatch.setattr(auth_router.settings, "bootstrap_admin_email", email)
    with TestClient(app) as c:
        user = _register(c, email, _uniq("boss"))
        assert user["role"] == "admin", user


def test_register_config_does_not_offer_admin():
    """前端注册页据此渲染，接口不能再把 admin 列为可注册角色。"""
    _setup()
    with TestClient(app) as c:
        body = c.get("/api/v1/auth/register/config").json()
        assert body["enabled"] is True
        assert "admin" not in body["roles"], body


def test_webhook_rejects_missing_or_wrong_token(monkeypatch):
    """webhook 曾只靠内网 IP 白名单；反代后直连 IP 恒为代理地址，白名单形同虚设。"""
    _setup()
    monkeypatch.setattr(webhook_mod.settings, "webhook_token", "s3cr3t-token")
    app.dependency_overrides[check_ip_whitelist] = lambda: "127.0.0.1"
    try:
        with TestClient(app) as c:
            r = c.post(_WEBHOOK, json={"alerts": []})
            assert r.status_code == 401, r.text
            assert r.json()["detail"]["code"] == "WEBHOOK_UNAUTHORIZED"

            r = c.post(_WEBHOOK, json={"alerts": []}, headers={"X-Webhook-Token": "wrong"})
            assert r.status_code == 401, r.text

            r = c.post(_WEBHOOK, json={"alerts": []}, headers={"X-Webhook-Token": "s3cr3t-token"})
            assert r.status_code != 401, r.text
    finally:
        app.dependency_overrides.pop(check_ip_whitelist, None)


def test_code_send_is_limited_per_source_ip(monkeypatch):
    """同目标 60s 冷却挡不住随机邮箱轰炸；这里验 IP 维度确实生效。

    用两个**不同**邮箱，确保 429 只可能来自 IP 桶而不是邮箱冷却。
    debug 档跳过限流（与登录限流同因），因此需要显式切成生产档。
    """
    _setup()
    with TestClient(app) as c:
        # 切生产档必须**进入 client 之后**：TestClient 进入时会跑 lifespan，
        # 而 validate_for_startup 在 debug=False 下会因测试环境的弱口令/mock 通道拒启动。
        monkeypatch.setattr(auth_router.settings, "debug", False)
        monkeypatch.setattr(auth_router.settings, "code_send_ip_per_min", 1)
        r1 = c.post("/api/v1/auth/email/send", json={"email": f"{_uniq('ip1')}@example.com"})
        assert r1.status_code == 200, r1.text

        r2 = c.post("/api/v1/auth/email/send", json={"email": f"{_uniq('ip2')}@example.com"})
        assert r2.status_code == 429, r2.text
        assert r2.json()["detail"]["code"] == "CODE_SEND_RATE_LIMITED", r2.text
