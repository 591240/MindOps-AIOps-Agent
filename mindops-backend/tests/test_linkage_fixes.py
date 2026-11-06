"""跨模块联动审查三处硬伤（P1/P2/P3）的回归测试。

P1：防抖窗口内残留的 new 事件在重启后由 recover_new_incidents() 补发诊断。
P2：事件收口为 resolved/closed 后 incidents_open gauge 必须随之回落。
P3：诊断主线已 resolved 但 run_recap 抛错时，_worker 不应翻转状态 / 推送矛盾 SSE
    （P3 依赖完整编排，此处以代码审查 + 既有 F-04 套件覆盖为主，不在此单测）。
"""

import os
import uuid

os.environ.setdefault("MINDOPS_MYSQL_DB", "mindops_f04_test")

import pytest
import pytest_asyncio

from app.agents import orchestrator as orch
from app.db import close_db, init_db
from app.models import Incident
from app.observability import metrics

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def initialized_database():
    await init_db()
    yield
    await close_db()


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _clean_incidents():
    await Incident.all().delete()
    yield
    await Incident.all().delete()


def _make_incident(status: str) -> Incident:
    return Incident(
        fingerprint=uuid.uuid4().hex[:40],
        title=f"inc-{uuid.uuid4().hex[:8]}",
        component="linkage-test",
        severity="P3",
        status=status,
    )


async def test_p2_recompute_follows_open_states():
    """P2：新建/收口后 incidents_open 必须反映真实 OPEN 数，不能单调偏高。"""
    await Incident.bulk_create([_make_incident("new") for _ in range(3)])
    rows = await Incident.filter(component="linkage-test").order_by("id")
    a, b, c = rows[0], rows[1], rows[2]

    assert await metrics.recompute_open_incidents() == 3
    assert metrics.incidents_open._value.get() == 3

    await Incident.filter(id=a.id).update(status="resolved")
    assert await metrics.recompute_open_incidents() == 2
    assert metrics.incidents_open._value.get() == 2

    await Incident.filter(id=b.id).update(status="human_needed")
    # human_needed 仍属 OPEN，计数不变
    assert await metrics.recompute_open_incidents() == 2

    await Incident.filter(id=c.id).update(status="closed")
    # 仅 b(human_needed) 仍 OPEN
    assert await metrics.recompute_open_incidents() == 1
    assert metrics.incidents_open._value.get() == 1


async def test_p1_recover_new_incidents_triggers_diagnosis(monkeypatch):
    """P1：启动时所有残留 new 事件都应被补发诊断（triggered_by=0 系统哨兵）。"""
    await Incident.bulk_create([_make_incident("new") for _ in range(3)])
    rows = await Incident.filter(component="linkage-test").order_by("id")

    calls = []

    async def fake_start(incident_id: int, triggered_by: int):
        calls.append((incident_id, triggered_by))
        return {"incident_id": incident_id, "status": "diagnosing", "run_id": "run_x"}

    monkeypatch.setattr(orch, "start_diagnosis", fake_start)

    recovered = await orch.recover_new_incidents()
    assert recovered == 3
    assert len(calls) == 3
    # 全部以系统哨兵 0 触发
    assert all(trig == 0 for _, trig in calls)
    # 覆盖到每一个 new 事件
    triggered_ids = {iid for iid, _ in calls}
    assert triggered_ids == {r.id for r in rows}
