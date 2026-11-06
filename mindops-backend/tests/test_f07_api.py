"""F-07 接口验收（文档 V1.1 §3.1 / §3.2 / §9.1 TC-2/9/10）。

覆盖：
- POST /incidents/{id}/confirm（收口 resolved，响应透传 knowledge_injected）
- GET  /incidents/{id}（recap 步骤 output_summary 含回灌结果，详情页可见）
- GET  /knowledge?source_type=incident（回灌文档列表：version/created_by 可追溯）
- POST /knowledge/search（回灌病历可被检索命中——"下次命中"闭环）
- 同内容二次收口（第二个同类事件）→ 幂等命中，不新增文档

回灌成功路径在模块级替换 milvus_client.upsert（测试环境无 Milvus）；
检索断言走真实 BM25 路（不依赖 Milvus）。
"""

import asyncio
import json
import os
import uuid

os.environ.setdefault("MINDOPS_MYSQL_DB", "mindops_f07_api_test")

import pytest
from fastapi.testclient import TestClient

from app.db import close_db, init_db
from app.main import app
from app.models import Incident, RemediationAction
from app.rag import milvus_client

CAUSE = f"连接池耗尽{uuid.uuid4().hex[:6]}"   # 唯一根因，避免命中历史测试数据
COMMAND = "set global max_connections=500"


async def _fake_upsert(_rows, _vectors):
    return None


@pytest.fixture
def _no_milvus(monkeypatch):
    """回灌成功路径不依赖 Milvus（测试环境无 Milvus；monkeypatch 隔离，不污染其他测试）。"""
    monkeypatch.setattr(milvus_client, "upsert", _fake_upsert)


def _login(client, username: str = "sre_admin", password: str = "Admin@123") -> dict:
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _prepare_incident() -> int:
    inc = await Incident.create(
        title=f"f07-api-{uuid.uuid4().hex[:8]}", component="mysql",
        severity="P1", status="human_needed",
        root_cause="数据库连接数达到上限", root_cause_confidence=0.85, root_cause_source="llm",
    )
    await RemediationAction.create(
        incident_id=inc.id, command=COMMAND, description="调大连接数上限",
        risk_level="high", requires_confirm=True, status="pending",
    )
    return inc.id


def test_f07_confirm_visible_searchable(_no_milvus):
    async def _prepare():
        # 两个同素材事件：①走首次回灌断言 ②走幂等重放断言
        await init_db()
        inc1 = await _prepare_incident()
        inc2 = await _prepare_incident()
        await close_db()
        return inc1, inc2

    inc1_id, inc2_id = asyncio.run(_prepare())
    base = f"/api/v1/incidents/{inc1_id}"

    with TestClient(app) as client:
        headers = _login(client)

        # ---- §3.1 confirm approve → resolved + knowledge_injected 透传 ----
        resp = client.get(f"{base}/actions", headers=headers)
        assert resp.status_code == 200
        action_id = next(a["id"] for a in resp.json()["items"] if a["status"] == "pending")
        resp = client.post(
            f"{base}/confirm", headers=headers,
            json={"action_id": action_id, "decision": "approve",
                  "actual_cause": CAUSE, "correct_actions": COMMAND},
        )
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["status"] == "resolved" and body["recap_triggered"] is True
        injected = body["knowledge_injected"]
        assert injected and injected["version"] == 1
        assert injected["title"] == f"{CAUSE}处置方案（mysql）"
        assert injected["idempotent"] is False

        # ---- §4.2 事件详情：recap 步骤 output_summary 含回灌结果 ----
        resp = client.get(base, headers=headers)
        assert resp.status_code == 200
        recap_steps = [s for s in resp.json()["steps"] if s["agent"] == "RecapAgent"]
        assert recap_steps, "recap 步骤未落库"
        output = json.loads(recap_steps[-1]["output_summary"])
        assert output["knowledge_injected"]["doc_id"] == injected["doc_id"]
        # 四段式病历内容可追溯
        assert "故障现象：" in output["recap"] and f"根因：\n{CAUSE}" in output["recap"]

        # ---- §3.2 知识列表：source_type=incident 可见，created_by 可追溯 ----
        resp = client.get("/api/v1/knowledge", params={"source_type": "incident", "q": CAUSE[:8]}, headers=headers)
        assert resp.status_code == 200, resp.text
        items = resp.json()["items"]
        assert len(items) == 1
        assert items[0]["doc_id"] == injected["doc_id"]
        assert items[0]["version"] == 1 and items[0]["is_latest"] is True
        assert items[0]["created_by"] == "sre_admin"   # created_by=确认用户 → 用户名展示

        # ---- TC-10 下次命中：回灌病历可被检索召回（BM25 路）----
        resp = client.post(
            "/api/v1/knowledge/search", headers=headers,
            json={"query": f"{CAUSE} 处置方案", "component": "mysql", "top_k": 5},
        )
        assert resp.status_code == 200, resp.text
        hits = resp.json()["results"]
        assert hits, "回灌病历未被检索命中"
        assert any(injected["doc_id"] == hit.get("doc_id") or CAUSE in (hit.get("text") or "") for hit in hits)

        # ---- 幂等重放：第二个同类事件同素材收口 → 命中既有版本 ----
        base2 = f"/api/v1/incidents/{inc2_id}"
        resp = client.get(f"{base2}/actions", headers=headers)
        action2 = next(a["id"] for a in resp.json()["items"] if a["status"] == "pending")
        resp = client.post(
            f"{base2}/confirm", headers=headers,
            json={"action_id": action2, "decision": "approve",
                  "actual_cause": CAUSE, "correct_actions": COMMAND},
        )
        assert resp.status_code == 200, resp.text
        replay = resp.json()["knowledge_injected"]
        assert replay["doc_id"] == injected["doc_id"]
        assert replay["version"] == 1 and replay["idempotent"] is True
