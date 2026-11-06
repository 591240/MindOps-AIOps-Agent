"""F-08 统计大盘 JSON 接口验收（`GET /metrics/dashboard`，docs/22 §2.1）。

覆盖：
- 权限边界：统计大盘按 docs/22 §2.1 为 **admin+**，engineer 必须 403
- range 白名单：非法值 422 INVALID_RANGE（不是静默回退默认值）
- 区间语义：today 不含 10 天前样本，30d 含；分桶粒度 today→hour / 30d→day
- 指标口径：自动闭环率、MTTR（分钟）、知识命中率（有 Evidence 即命中）、
  Agent 平均耗时（来自 incident_steps.latency_ms）
- 空态：无样本时 has_data=False 且各项为 0 / 空数组，而不是 500

本模块独立测试库 `mindops_f08_test`，每次运行先清空相关表，保证可重复。
"""

import asyncio
import hashlib
import os
from datetime import datetime, timedelta

os.environ.setdefault("MINDOPS_MYSQL_DB", "mindops_f08_test")

from fastapi.testclient import TestClient

from app.db import close_db, init_db
from app.main import app
from app.models import Alert, Evidence, Incident, IncidentStep

BASE = "/api/v1/metrics/dashboard"


def _login(client: TestClient, username: str, password: str) -> dict:
    resp = client.post("/api/v1/auth/login", json={"username": username, "password": password})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


async def _seed() -> None:
    """清空并写入可预测的样本，覆盖今日 / 10 天前两个时间桶。

    注意：`Incident.created_at` 是 `auto_now_add`，create 时无法指定，
    因此「10 天前」的样本用 create 后 update 改写；而 `resolved_at` 必须
    以**真实的 created_at 为基准**推算，否则会造出 resolved_at < created_at
    的负时长样本，被指标口径直接过滤掉。
    """
    await init_db()
    for model in (IncidentStep, Evidence, Alert, Incident):
        await model.all().delete()

    now = datetime.now()

    # ---- 告警：今日 3 条（fingerprint 受 chk_fpr 约束，必须为 40 位 SHA1 hex）----
    for idx in range(3):
        await Alert.create(
            source="demo", component="redis", severity="P2",
            title=f"f08-seed-{idx}", detail="seed",
            fingerprint=hashlib.sha1(f"f08-seed-{idx}".encode()).hexdigest(),
        )

    # ---- 今日事件：2 个闭环（1 个带证据）+ 1 个未闭环 ----
    hit = await Incident.create(
        title="f08-closed-hit", component="redis", severity="P1", status="resolved",
        root_cause="连接池耗尽", root_cause_confidence=0.9, root_cause_source="llm",
    )
    # 4 分钟闭环 → 期望 MTTR 参与值之一
    await Incident.filter(id=hit.id).update(
        resolved_at=hit.created_at + timedelta(minutes=4)
    )

    plain = await Incident.create(
        title="f08-closed-miss", component="mysql", severity="P2", status="closed",
        root_cause="慢查询", root_cause_confidence=0.8, root_cause_source="llm",
    )
    # 2 分钟闭环 → 期望 MTTR = (4 + 2) / 2 = 3.0min
    await Incident.filter(id=plain.id).update(
        resolved_at=plain.created_at + timedelta(minutes=2)
    )

    await Incident.create(
        title="f08-open", component="redis", severity="P3", status="human_needed",
    )
    await Evidence.create(
        incident_id=hit.id, quote="连接池参数需上调",
        source_doc="redis-tuning.md", score=0.88,
    )

    # ---- 10 天前的闭环事件：只应出现在 30d 窗口 ----
    old = await Incident.create(
        title="f08-old", component="nginx", severity="P2", status="resolved",
        root_cause="上游超时", root_cause_confidence=0.7, root_cause_source="llm",
    )
    old_created = now - timedelta(days=10)
    await Incident.filter(id=old.id).update(
        created_at=old_created, resolved_at=old_created + timedelta(minutes=30)
    )

    # ---- Agent 步骤：同 agent 两条，平均耗时 (200+400)/2 = 300ms ----
    await IncidentStep.create(
        run_id="f08run", incident_id=hit.id, step="diagnose",
        agent="DiagnoseAgent", latency_ms=200,
    )
    await IncidentStep.create(
        run_id="f08run", incident_id=plain.id, step="diagnose",
        agent="DiagnoseAgent", latency_ms=400,
    )

    await close_db()


def _prepare() -> None:
    _ensure_database()
    asyncio.run(_seed())


def _ensure_database() -> None:
    """`init_db()` 只建表不建库，先补上本模块的测试库。

    其他模块依赖"库已由人工/前次运行创建"，本模块自带这一步，
    避免换机/清库后出现一串与断言无关的 DBConnectionError。
    """
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


def test_metrics_dashboard_requires_admin():
    """统计大盘是 admin+ 页面：engineer 403，匿名 401。"""
    _prepare()
    with TestClient(app) as client:
        assert client.get(BASE).status_code == 401
        engineer = _login(client, "sre001", "P@ssw0rd!")
        assert client.get(BASE, headers=engineer).status_code == 403


def test_metrics_dashboard_rejects_unknown_range():
    """非法 range 必须显式 422，而不是静默回退成 today。"""
    _prepare()
    with TestClient(app) as client:
        admin = _login(client, "sre_admin", "Admin@123")
        resp = client.get(BASE, params={"range": "90d"}, headers=admin)
        assert resp.status_code == 422, resp.text
        assert resp.json()["detail"]["code"] == "INVALID_RANGE"


def test_metrics_dashboard_today_aggregates_real_data():
    """today 窗口：KPI / 分桶 / Agent 性能全部来自真实表。"""
    _prepare()
    with TestClient(app) as client:
        admin = _login(client, "sre_admin", "Admin@123")
        resp = client.get(BASE, params={"range": "today"}, headers=admin)
        assert resp.status_code == 200, resp.text
        body = resp.json()

        assert body["range"] == "today" and body["bucket"] == "hour"
        assert body["has_data"] is True

        kpi = body["kpi"]
        assert kpi["today_alerts"] == 3
        # 今日 3 个事件，2 个闭环 → 66.7%
        assert kpi["auto_close_rate"] == 66.7
        # 今日仅 1 个事件带证据 → 33.3%
        assert kpi["knowledge_hit_rate"] == 33.3
        # MTTR = (4min + 2min) / 2 = 3.0min
        assert kpi["mttr_min"] == 3.0

        # 按小时分桶：X 轴连续（覆盖 0 点到当前小时）
        assert body["mttr_trend"], "趋势不得为空"
        assert all(entry["date"].endswith(":00") for entry in body["mttr_trend"])

        # 按组件命中率：redis 1/2=50.0、mysql 0/1=0.0；nginx 属 10 天前，不入今日
        rates = {item["component"]: item["rate"] for item in body["hit_rate_by_component"]}
        assert rates == {"redis": 50.0, "mysql": 0.0}

        perf = {item["agent"]: item["avg_ms"] for item in body["agent_performance"]}
        assert perf == {"DiagnoseAgent": 300}


def test_metrics_dashboard_range_boundaries():
    """10 天前的事件：7d 不可见、30d 可见；分桶粒度随 range 变化。"""
    _prepare()
    with TestClient(app) as client:
        admin = _login(client, "sre_admin", "Admin@123")

        week = client.get(BASE, params={"range": "7d"}, headers=admin).json()
        assert week["bucket"] == "day"
        # 与 today 同口径（10 天前样本被排除）
        assert week["kpi"]["auto_close_rate"] == 66.7
        assert "nginx" not in {i["component"] for i in week["hit_rate_by_component"]}

        month = client.get(BASE, params={"range": "30d"}, headers=admin).json()
        assert month["bucket"] == "day"
        # 30d 纳入第 4 个事件，3 个闭环 → 75.0%
        assert month["kpi"]["auto_close_rate"] == 75.0
        assert month["kpi"]["knowledge_hit_rate"] == 25.0
        assert "nginx" in {i["component"] for i in month["hit_rate_by_component"]}
        # 趋势按天分桶，标签为 MM-DD
        assert len(month["mttr_trend"]) >= 2
        assert all(len(entry["date"]) == 5 for entry in month["mttr_trend"])
