"""F-06 接口验收（文档 V1.1 §3.2 / §3.3 / §3.4 / §3.5 / §5.5）。

覆盖：
- GET  /incidents/{id}/evidence（按 run_id 过滤）
- GET  /incidents/{id}/actions
- POST /incidents/{id}/confirm（approve 后事件 resolved）
- POST /incidents/{id}/close（有 pending → 409；无 pending → 200）
- POST /incidents/{id}/feedback（补录不改状态）
"""

import asyncio
import os
import uuid

# 审查：原复制粘贴写成 f04 库——conftest 会强制覆盖，但声明必须名实相符
os.environ.setdefault("MINDOPS_MYSQL_DB", "mindops_f06_api_test")

from fastapi.testclient import TestClient

from app.db import close_db, init_db
from app.main import app
from app.models import Evidence, Incident, IncidentStep, RemediationAction


def _login(client, username: str = "sre_admin", password: str = "Admin@123") -> dict:
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _ensure_database() -> None:
    """审查：原实现蹭 f04 的库；独立建库后 `init_db()` 只建表不建库，这里补上。"""
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


async def _prepare() -> dict:
    _ensure_database()
    await init_db()
    inc = await Incident.create(
        title=f"f06-api-{uuid.uuid4().hex[:8]}", component="redis",
        severity="P1", status="human_needed", root_cause="组件存在内存压力",
        root_cause_confidence=0.68, root_cause_source="llm",
    )
    step_a = await IncidentStep.create(
        run_id=f"runA{uuid.uuid4().hex[:6]}", incident_id=inc.id,
        step="context", agent="ContextAgent",
    )
    await Evidence.create(
        incident_id=inc.id, step_id=step_a.id, chunk_id=1,
        quote="当 used_memory > maxmemory 时新写入会被淘汰",
        source_doc="Redis运维Runbook-v3", score=0.82,
    )
    step_b = await IncidentStep.create(
        run_id=f"runB{uuid.uuid4().hex[:6]}", incident_id=inc.id,
        step="diagnose", agent="DiagnoseAgent",
    )
    await Evidence.create(
        incident_id=inc.id, step_id=step_b.id,
        quote="组件存在内存压力，需要核对容量与淘汰策略", source_doc="llm-reasoning",
    )
    high = await RemediationAction.create(
        incident_id=inc.id, command="rm -rf /data/cache", description="高危清理",
        risk_level="high", requires_confirm=True, status="pending",
    )
    await RemediationAction.create(
        incident_id=inc.id, command="df -h", description="确认磁盘",
        risk_level="low", requires_confirm=False, status="approved",
    )
    payload = {
        "incident_id": inc.id, "run_a": step_a.run_id,
        "run_b": step_b.run_id, "action_id": high.id,
    }
    await close_db()
    return payload


def test_f06_evidence_actions_confirm_close_feedback():
    data = asyncio.run(_prepare())
    inc_id = data["incident_id"]
    base = f"/api/v1/incidents/{inc_id}"

    with TestClient(app) as client:
        headers = _login(client)

        # ---- §3.2 证据链：全量 2 条，且带 run_id 便于前端分组 ----
        resp = client.get(f"{base}/evidence", headers=headers)
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        assert len(items) == 2
        assert {item["run_id"] for item in items} == {data["run_a"], data["run_b"]}
        assert any(item["source_doc"] == "llm-reasoning" for item in items)

        # ---- §3.2 按 run_id 过滤 ----
        resp = client.get(f"{base}/evidence?run_id={data['run_a']}", headers=headers)
        assert resp.status_code == 200
        filtered = resp.json()["items"]
        assert len(filtered) == 1 and filtered[0]["source_doc"] == "Redis运维Runbook-v3"

        # ---- §3.3 处置建议列表 ----
        resp = client.get(f"{base}/actions", headers=headers)
        assert resp.status_code == 200
        actions = {item["command"]: item for item in resp.json()["items"]}
        assert actions["rm -rf /data/cache"]["status"] == "pending"
        assert actions["rm -rf /data/cache"]["requires_confirm"] is True
        assert actions["df -h"]["status"] == "approved"   # low 自动批准

        # ---- §3.5 close：存在 pending 时 409（防止绕过高危确认）----
        resp = client.post(f"{base}/close", headers=headers, json={"reason": "false_positive"})
        assert resp.status_code == 409, resp.text

        # ---- §3.4 confirm approve → action approved 且事件 resolved ----
        resp = client.post(
            f"{base}/confirm", headers=headers,
            json={"action_id": data["action_id"], "decision": "approve",
                  "actual_cause": "内存淘汰策略配置不当"},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["action_status"] == "approved"
        assert body["status"] == "resolved"

        # ---- §5.5 feedback：补录不改状态 ----
        resp = client.post(f"{base}/feedback", headers=headers, json={"actual_cause": "真实原因是连接泄漏"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "resolved"

        # ---- §3.5 close：无 pending 后关闭成功 ----
        resp = client.post(f"{base}/close", headers=headers, json={"reason": "false_positive"})
        assert resp.status_code == 200, resp.text
        assert resp.json()["status"] == "closed"
