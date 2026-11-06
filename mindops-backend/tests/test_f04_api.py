"""F-04 接口验收（文档 §3.1 / §3.2 / §3.5）。

覆盖：GET/PUT /config/confidence（含角色鉴权与 422 越界）、
GET /incidents/{id}/loop-audits（404 / 空 items / 有明细）。
"""

import os
import uuid

os.environ.setdefault("MINDOPS_MYSQL_DB", "mindops_f04_test")

from fastapi.testclient import TestClient

from app.main import app


def _login(client, username: str, password: str) -> dict:
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def test_confidence_config_and_loop_audits_contract():
    with TestClient(app) as client:
        admin = _login(client, "sre_admin", "Admin@123")
        engineer = _login(client, "sre001", "P@ssw0rd!")

        # ---- §3.1 GET /config/confidence：任意已登录角色可读 ----
        read = client.get("/api/v1/config/confidence", headers=engineer)
        assert read.status_code == 200
        body = read.json()
        assert set(body) == {
            "conf_threshold", "max_retry", "expand_recall",
            "reasoning_escalation", "cross_component_fallback",
        }
        assert set(body["expand_recall"]) == {
            "top_k_base", "top_k_loop", "sim_floor_base", "sim_floor_loop",
        }

        # ---- §3.2 PUT：非 admin 被拒 ----
        forbidden = client.put(
            "/api/v1/config/confidence", headers=engineer, json={"conf_threshold": 0.55}
        )
        assert forbidden.status_code == 403

        # ---- §3.2 PUT：越界 422 ----
        too_high = client.put(
            "/api/v1/config/confidence", headers=admin, json={"conf_threshold": 1.5}
        )
        assert too_high.status_code == 422
        bad_retry = client.put(
            "/api/v1/config/confidence", headers=admin, json={"max_retry": 9}
        )
        assert bad_retry.status_code == 422

        # ---- §3.2 PUT：admin 调参成功 ----
        updated = client.put(
            "/api/v1/config/confidence", headers=admin,
            json={"conf_threshold": 0.55, "max_retry": 1},
        )
        assert updated.status_code == 200
        assert updated.json()["conf_threshold"] == 0.55
        assert updated.json()["max_retry"] == 1
        assert updated.json()["updated_by"] == "sre_admin"
        assert "仅对新诊断 run 生效" in updated.json()["note"]

        after = client.get("/api/v1/config/confidence", headers=engineer).json()
        assert after["conf_threshold"] == 0.55
        # expand_recall 为 MVP 固定参数，不随调参变化
        assert after["expand_recall"]["top_k_base"] == 5

        # 还原，避免影响其他用例
        client.put(
            "/api/v1/config/confidence", headers=admin,
            json={"conf_threshold": 0.6, "max_retry": 2},
        )

        # ---- §3.5 审计接口：事件不存在 → 404 ----
        missing = client.get("/api/v1/incidents/99999999/loop-audits", headers=engineer)
        assert missing.status_code == 404

        # ---- §3.5 审计接口：事件存在但无回环 → 空 items ----
        component = f"f04-api-{uuid.uuid4().hex[:12]}"
        created = client.post(
            "/api/v1/alerts", headers=admin,
            json={"component": component, "severity": "P1", "title": "回环接口验证事件",
                  "detail": "no loop yet"},
        )
        assert created.status_code == 200
        inc_id = created.json()["incident_id"]

        empty = client.get(f"/api/v1/incidents/{inc_id}/loop-audits", headers=engineer)
        assert empty.status_code == 200
        assert empty.json() == {"incident_id": inc_id, "items": []}
