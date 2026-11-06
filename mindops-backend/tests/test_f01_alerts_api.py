"""F-01 告警列表接口验收（`GET /alerts/recent`，第二轮修复）。

覆盖本次新增/修复的契约：
- `dedup_count`：去重命中必须**真实自增**（此前只刷新 last_seen_at，计数无处可查，
  前端「去重」列只能造假）
- `incident_status`：告警没有状态，状态属于它被聚类成的**事件**；未聚类时为 None
- `status` 筛选：支持按事件状态过滤，以及 `unclustered`（仅未聚类）
- `page` / `size` 真实分页：`total` 是全量 count，不是当前页条数
- `q` 标题搜索走服务端（否则分页后只能搜到当前页，结论是错的）

独立测试库 `mindops_f01_api_test`，每次运行先清空相关表。
"""

import asyncio
import os
import uuid

os.environ.setdefault("MINDOPS_MYSQL_DB", "mindops_f01_api_test")

from fastapi.testclient import TestClient

from app.db import close_db, init_db
from app.main import app
from app.models import Alert, Incident

RECENT = "/api/v1/alerts/recent"


def _login(client: TestClient) -> dict:
    resp = client.post("/api/v1/auth/login", json={"username": "sre_admin", "password": "Admin@123"})
    assert resp.status_code == 200, resp.text
    return {"Authorization": f"Bearer {resp.json()['access_token']}"}


def _ensure_database() -> None:
    """`init_db()` 只建表不建库，先补上本模块的测试库。"""
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


async def _reset(orphan_title: str | None = None) -> None:
    await init_db()
    for model in (Alert, Incident):
        await model.all().delete()
    if orphan_title:
        # 直接造一条未聚类的告警（模拟聚类失败/未跑到的场景）。
        # 必须在同一个 asyncio.run 里建，不能塞进 TestClient 块内——
        # TestClient 的 app 跑在另一个事件循环上，close_db() 会把它的连接一起关掉。
        import hashlib

        await Alert.create(
            source="demo", component="nginx", severity="P3", title=orphan_title,
            detail="orphan",
            fingerprint=hashlib.sha1(f"demo|nginx|{orphan_title}".encode()).hexdigest(),
            dedup_count=1,
        )
    await close_db()


def _prepare(orphan_title: str | None = None) -> None:
    _ensure_database()
    asyncio.run(_reset(orphan_title))


def test_dedup_count_increments_on_repeat_report():
    """同一指纹上报 3 次 → 库里 1 行，dedup_count == 3。"""
    _prepare()
    title = f"f01-dedup-{uuid.uuid4().hex[:8]}"
    payload = {"component": "redis", "severity": "P2", "title": title, "detail": "seed"}

    with TestClient(app) as client:
        headers = _login(client)
        first = client.post("/api/v1/alerts", json=payload, headers=headers)
        assert first.status_code == 200, first.text
        assert first.json()["accepted"] == 1 and first.json()["deduped"] is False

        for _ in range(2):
            repeat = client.post("/api/v1/alerts", json=payload, headers=headers)
            assert repeat.status_code == 200, repeat.text
            assert repeat.json()["accepted"] == 0 and repeat.json()["deduped"] is True

        resp = client.get(RECENT, params={"q": title}, headers=headers)
        assert resp.status_code == 200, resp.text
        body = resp.json()
        assert body["total"] == 1, "同指纹必须只落一行"
        item = body["items"][0]
        assert item["dedup_count"] == 3, f"去重计数未自增：{item['dedup_count']}"
        assert item["last_seen_at"] is not None


def test_recent_exposes_incident_status_and_unclustered_filter():
    """incident_status 来自所属事件；未聚类的告警走 unclustered 筛选。"""
    tag = uuid.uuid4().hex[:8]
    clustered_title = f"f01-clustered-{tag}"
    orphan_title = f"f01-orphan-{tag}"
    _prepare(orphan_title)

    with TestClient(app) as client:
        headers = _login(client)
        # 经正常摄取 → 会被聚类桩挂上 incident（status='new'）
        resp = client.post(
            "/api/v1/alerts",
            json={"component": "mysql", "severity": "P1", "title": clustered_title},
            headers=headers,
        )
        assert resp.status_code == 200, resp.text
        assert resp.json()["incident_id"] is not None, "摄取后应已聚类"

        resp = client.get(RECENT, params={"q": tag}, headers=headers)
        assert resp.status_code == 200, resp.text
        by_title = {item["title"]: item for item in resp.json()["items"]}

        assert by_title[clustered_title]["incident_status"] == "new"
        assert by_title[clustered_title]["incident_id"] is not None
        assert by_title[orphan_title]["incident_status"] is None
        assert by_title[orphan_title]["incident_id"] is None

        # unclustered 只留未聚类那条
        resp = client.get(RECENT, params={"q": tag, "status": "unclustered"}, headers=headers)
        titles = {item["title"] for item in resp.json()["items"]}
        assert titles == {orphan_title}

        # 按事件状态筛选：两条都是 'new' 时只应命中已聚类那条
        resp = client.get(RECENT, params={"q": tag, "status": "new"}, headers=headers)
        titles = {item["title"] for item in resp.json()["items"]}
        assert titles == {clustered_title}

        # 该状态不存在任何事件时，必须返回空而不是全量
        resp = client.get(RECENT, params={"q": tag, "status": "human_needed"}, headers=headers)
        assert resp.json() == {"items": [], "total": 0}


def test_recent_pagination_and_server_side_search():
    """total 是全量 count；page 走 offset；q 在服务端过滤。"""
    _prepare()
    tag = uuid.uuid4().hex[:8]

    with TestClient(app) as client:
        headers = _login(client)
        for idx in range(5):
            resp = client.post(
                "/api/v1/alerts",
                json={"component": "nginx", "severity": "P2", "title": f"f01-page-{tag}-{idx}"},
                headers=headers,
            )
            assert resp.status_code == 200, resp.text

        # 第 1 页取 2 条，total 仍须是 5（不是 2）
        page1 = client.get(RECENT, params={"q": tag, "page": 1, "size": 2}, headers=headers).json()
        assert page1["total"] == 5, f"total 应为全量 count，实际 {page1['total']}"
        assert len(page1["items"]) == 2

        # 第 3 页只剩 1 条，且与第 1 页无重叠（offset 真的生效了）
        page3 = client.get(RECENT, params={"q": tag, "page": 3, "size": 2}, headers=headers).json()
        assert len(page3["items"]) == 1
        ids_p1 = {item["id"] for item in page1["items"]}
        ids_p3 = {item["id"] for item in page3["items"]}
        assert not (ids_p1 & ids_p3), "分页出现重叠，offset 未生效"

        # 服务端搜索：命中的只有 1 条
        hit = client.get(RECENT, params={"q": f"f01-page-{tag}-3"}, headers=headers).json()
        assert hit["total"] == 1
        assert hit["items"][0]["title"].endswith("-3")
