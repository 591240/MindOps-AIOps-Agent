import os
import uuid

os.environ.setdefault("MINDOPS_MYSQL_DB", "mindops_f02_test")

from fastapi.testclient import TestClient

from app.db import close_db
from app.main import app


def _client():
    """返回一个 TestClient；用 with 保证 lifespan（init_db/close_db）成对执行。

    `TestClient.__exit__` 会让 lifespan 的 `close_db()` 在**同一个 portal
    事件循环**里收尾，从而避免"循环先关、连接后收"的
    `RuntimeError: Event loop is closed` —— 那个异常会污染后续模块
    （实测会让 test_f06.py 的 20+ 个用例在 setup 阶段报 ERROR）。
    """
    return TestClient(app)


def test_incident_api_contract_end_to_end():
    component = f"f02-api-{uuid.uuid4().hex[:12]}"
    with TestClient(app) as client:
        admin_login = client.post(
            "/api/v1/auth/login",
            json={"username": "sre_admin", "password": "Admin@123"},
        )
        assert admin_login.status_code == 200
        admin_headers = {"Authorization": f"Bearer {admin_login.json()['access_token']}"}

        first = client.post(
            "/api/v1/alerts",
            headers=admin_headers,
            json={
                "component": component,
                "severity": "P1",
                "title": "Redis 内存使用率超过 90%",
                "detail": "first alert",
            },
        )
        second = client.post(
            "/api/v1/alerts",
            headers=admin_headers,
            json={
                "component": component,
                "severity": "P1",
                "title": "Redis 内存使用率达 92%",
                "detail": "second alert",
            },
        )
        assert first.status_code == 200
        assert second.status_code == 200
        assert first.json()["incident_id"] == second.json()["incident_id"]

        listing = client.get(
            "/api/v1/incidents",
            params={"component": component},
            headers=admin_headers,
        )
        assert listing.status_code == 200
        assert listing.json()["total"] == 1
        item = listing.json()["items"][0]
        assert item["alert_count"] == 2
        assert item["last_seen_at"]

        detail = client.get(f"/api/v1/incidents/{item['id']}", headers=admin_headers)
        assert detail.status_code == 200
        assert len(detail.json()["alerts"]) == 2
        assert detail.json()["steps"] == []
        assert detail.json()["evidence"] == []
        assert detail.json()["actions"] == []

        engineer_login = client.post(
            "/api/v1/auth/login",
            json={"username": "sre001", "password": "P@ssw0rd!"},
        )
        engineer_headers = {"Authorization": f"Bearer {engineer_login.json()['access_token']}"}
        dry_run = client.post(
            "/api/v1/incidents/_recluster",
            headers=engineer_headers,
            json={"component": component},
        )
        assert dry_run.status_code == 200
        assert dry_run.json()["scanned"] == 1
        assert "report" in dry_run.json()
