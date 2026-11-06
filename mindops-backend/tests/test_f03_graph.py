"""F-03 编排图（LangGraph）回归测试。

覆盖的是历史用例从未触达的一层：`orchestrator._run_graph` 的图拓扑与状态收口。
迁移前所有相关用例要么手写 `IncidentStep`，要么把 `start_diagnosis` 整个
monkeypatch 掉（`test_linkage_fixes.py:83`），因此"图到底按哪条边走"没有自动化证据。

打桩只打在 LLM 与检索两个外部依赖上，断言全部针对图层的可观察产物：
IncidentStep 轨迹序列、step 上的 retry_count、LoopAudit 两阶段回填、Incident 终态。
"""

import asyncio
import hashlib
import os
import uuid
from datetime import timedelta

os.environ.setdefault("MINDOPS_MYSQL_DB", "mindops_f03_graph_test")

import pytest
import pytest_asyncio

from app.agents import context as context_agent
from app.agents import checkpoint
from app.agents import diagnose as diagnose_agent
from app.agents import model_router
from app.agents import orchestrator as orch
from app.config import model_now, settings
from app.db import close_db, init_db
from app.models import (
    Alert,
    DiagnosisRun,
    Evidence,
    Incident,
    IncidentStep,
    KnowledgeChunk,
    KnowledgeDoc,
    LoopAudit,
    RemediationAction,
    RuntimeConfig,
)
from app.rag import milvus_client

pytestmark = pytest.mark.asyncio(loop_scope="module")

FULL_LLM_TRAJECTORY = ["triage", "context", "diagnose", "remediate", "ticket", "recap"]


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def initialized_database():
    await init_db()
    # 图编译时必须已有检查点后端；池子绑定当前事件循环，故与本模块同生命周期。
    await checkpoint.startup()
    yield
    await checkpoint.shutdown()
    await close_db()


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _clean_tables():
    for model in (
        IncidentStep, Evidence, LoopAudit, RemediationAction,
        DiagnosisRun, Alert, Incident, RuntimeConfig,
    ):
        await model.all().delete()
    # 高分辨率用例会 Recap 回灌病历；docs 与 chunks 必须一起清 —— 只删 docs 会留下
    # 孤儿切片，BM25 稀疏路照样把它召回给下一条用例当根因。
    await KnowledgeChunk.all().delete()
    await KnowledgeDoc.all().delete()
    yield
    await KnowledgeChunk.all().delete()
    await KnowledgeDoc.all().delete()


@pytest.fixture(autouse=True)
def _no_milvus(monkeypatch):
    """Recap 回灌路径不依赖 Milvus（与 test_f07 同口径）。"""

    async def fake_upsert(_rows, _vectors):
        return None

    monkeypatch.setattr(milvus_client, "upsert", fake_upsert)


# ---------------------------------------------------------------------------
# 夹具与驱动
# ---------------------------------------------------------------------------

async def _new_incident(component: str = "ordersvc", title: str | None = None) -> Incident:
    title = title or f"g_{uuid.uuid4().hex[:12]}"
    incident = await Incident.create(
        title=title, component=component, severity="P1", status="new"
    )
    await Alert.create(
        source="demo", component=component, severity="P1", title=title,
        detail="graph regression seed", incident_id=incident.id,
        fingerprint=hashlib.sha1(title.encode()).hexdigest(),
    )
    return incident


async def _drive(incident: Incident) -> None:
    """绕开信号量/锁直接跑图：本文件只验图，调度层由 `test_start_diagnosis_wires_graph` 覆盖。"""
    run_id = f"run_{uuid.uuid4().hex[:20]}"
    await DiagnosisRun.create(
        run_id=run_id, incident_id=incident.id, status="running", triggered_by=0
    )
    await Incident.filter(id=incident.id).update(status="diagnosing")
    await orch._run_graph(incident.id, run_id)


async def _trajectory(incident_id: int) -> list[tuple[str, int]]:
    rows = await IncidentStep.filter(incident_id=incident_id).order_by("id")
    return [(row.step, row.retry_count) for row in rows]


def _stub_context(monkeypatch, chunks: int = 2) -> None:
    """固定 2 条知识片段：够避开 calibration 的零证据/单证据压制（P0-3）。"""

    async def fake_context(state: dict) -> dict:
        kb_chunks = [
            {
                "chunk_id": 1000 + index,
                "quote": f"证据片段 {index}",
                "source_doc": "runbook",
                "score": 0.8,
            }
            for index in range(chunks)
        ]
        return {
            "context": {"component": state["component"]},
            "similar_cases": [],
            "kb_chunks": kb_chunks,
            "tool_calls": [],
        }

    monkeypatch.setattr(context_agent, "run", fake_context)


def _stub_diagnose(monkeypatch, confidence: float, cause: str = "连接池耗尽") -> None:
    async def fake_diagnose(state: dict) -> dict:
        return {
            "root_cause": cause,
            "root_cause_confidence": confidence,
            "root_cause_source": "llm",
            "missing_info": [],
            "model_id": None,
            "tool_calls": [],
        }

    monkeypatch.setattr(diagnose_agent, "run", fake_diagnose)


# ---------------------------------------------------------------------------
# 分支 1：规则短路 —— 不进入 Context/Diagnose
# ---------------------------------------------------------------------------

async def test_rule_hit_skips_diagnosis_branch():
    incident = await _new_incident(component="redis", title="redis 内存使用率告警")
    await _drive(incident)

    steps = [step for step, _ in await _trajectory(incident.id)]
    assert steps == ["triage", "remediate", "ticket", "recap"]

    fresh = await Incident.get(id=incident.id)
    assert fresh.status == "resolved"
    assert fresh.root_cause_source == "rule"
    # 规则结论的出处是规则库条目本身，必须留下可回溯证据
    assert await Evidence.filter(
        incident_id=incident.id, source_doc="pattern-rule"
    ).exists()


# ---------------------------------------------------------------------------
# 分支 2：首轮即过阈值 —— 完整轨迹且不产生回环审计
# ---------------------------------------------------------------------------

async def test_high_confidence_resolves_with_full_trajectory(monkeypatch):
    _stub_context(monkeypatch)
    _stub_diagnose(monkeypatch, confidence=0.95)
    incident = await _new_incident()
    await _drive(incident)

    assert [step for step, _ in await _trajectory(incident.id)] == FULL_LLM_TRAJECTORY
    fresh = await Incident.get(id=incident.id)
    assert fresh.status == "resolved"
    assert fresh.root_cause_source == "llm"
    assert not await LoopAudit.filter(incident_id=incident.id).exists()

    # 证据溯源：知识片段挂 Context 步，推理摘录挂 Diagnose 步（§5.3 b）
    assert await Evidence.filter(
        incident_id=incident.id, source_doc="runbook"
    ).count() == 2
    assert await Evidence.filter(
        incident_id=incident.id, source_doc="llm-reasoning"
    ).exists()


# ---------------------------------------------------------------------------
# 分支 3：低置信耗尽 —— 回环次数、retry_count 自增位置、conf_after 回填
# ---------------------------------------------------------------------------

async def test_low_confidence_loops_until_exhausted_then_human(monkeypatch):
    _stub_context(monkeypatch)
    _stub_diagnose(monkeypatch, confidence=0.05)
    incident = await _new_incident()
    await _drive(incident)

    trajectory = await _trajectory(incident.id)
    steps = [step for step, _ in trajectory]
    # max_retry 默认 2 → Context/Diagnose 各跑 3 轮（首轮 + 2 次补查）
    assert steps.count("context") == 3
    assert steps.count("diagnose") == 3
    # 转人工后不得继续 Remediate/Ticket/Recap
    assert "remediate" not in steps and "ticket" not in steps and "recap" not in steps

    # retry_count 只允许在回环边上 +1（§5.6 铁律）：Diagnose 步依次看到 0/1/2
    assert [retry for step, retry in trajectory if step == "diagnose"] == [0, 1, 2]

    audits = await LoopAudit.filter(incident_id=incident.id).order_by("retry_index")
    assert [audit.retry_index for audit in audits] == [1, 2]
    # 两阶段落库（§7.3）：补查时 NULL，下一轮 Diagnose 产出后必须回填
    assert all(audit.conf_after is not None for audit in audits)

    assert (await Incident.get(id=incident.id)).status == "human_needed"


async def test_recursion_limit_follows_per_run_max_retry(monkeypatch):
    """调高 max_retry 后图必须仍能跑到耗尽，而不是撞上 LangGraph 默认 25 步上限。"""
    await RuntimeConfig.update_or_create(key="max_retry", defaults={"value": "4"})
    _stub_context(monkeypatch)
    _stub_diagnose(monkeypatch, confidence=0.05)
    incident = await _new_incident()
    await _drive(incident)

    trajectory = await _trajectory(incident.id)
    assert [retry for step, retry in trajectory if step == "diagnose"] == [0, 1, 2, 3, 4]
    assert (await Incident.get(id=incident.id)).status == "human_needed"


# ---------------------------------------------------------------------------
# 分支 4：模型全部不可用 —— 不落 Diagnose 步，直接转人工
# ---------------------------------------------------------------------------

async def test_model_unavailable_halts_before_diagnose_step(monkeypatch):
    _stub_context(monkeypatch)

    async def boom(state: dict):
        raise model_router.ModelUnavailableError("no available model for role chat")

    monkeypatch.setattr(diagnose_agent, "run", boom)
    incident = await _new_incident()
    await _drive(incident)

    steps = [step for step, _ in await _trajectory(incident.id)]
    assert "context" in steps
    # F-09 §5.4：模型全败时没有产出可落，Diagnose 步不应存在
    assert "diagnose" not in steps and "remediate" not in steps
    assert (await Incident.get(id=incident.id)).status == "human_needed"


# ---------------------------------------------------------------------------
# 分支 5：高风险命令 —— 停在 Remediate，等人工确认
# ---------------------------------------------------------------------------

async def test_high_risk_action_holds_before_ticket(monkeypatch):
    _stub_context(monkeypatch)
    _stub_diagnose(monkeypatch, confidence=0.95, cause="容器反复重启，需要删除故障 Pod 触发重建")
    incident = await _new_incident()
    await _drive(incident)

    assert [step for step, _ in await _trajectory(incident.id)] == [
        "triage", "context", "diagnose", "remediate"
    ]
    assert (await Incident.get(id=incident.id)).status == "human_needed"

    actions = await RemediationAction.filter(incident_id=incident.id).order_by("id")
    # RemediateAgent 的第一条恒为 low 的 `-cli status`，高风险项按其自身断言
    high = [action for action in actions if action.risk_level == "high"]
    assert len(high) == 1
    assert high[0].status == "pending" and high[0].requires_confirm is True


# ---------------------------------------------------------------------------
# 收口不变量：置信度必须在落库前完成校准
# ---------------------------------------------------------------------------

async def test_step_confidence_equals_incident_confidence(monkeypatch):
    """step / incident / loop_audit 三处读的是同一个收口后的值（P0-2/P0-3）。"""
    _stub_context(monkeypatch)
    _stub_diagnose(monkeypatch, confidence=0.05)
    incident = await _new_incident()
    await _drive(incident)

    step = await IncidentStep.filter(incident_id=incident.id, step="diagnose").order_by("-id").first()
    fresh = await Incident.get(id=incident.id)
    assert step.confidence == fresh.root_cause_confidence
    audit = await LoopAudit.filter(incident_id=incident.id).order_by("-retry_index").first()
    assert audit.conf_after == step.confidence


# ---------------------------------------------------------------------------
# 调度层接线：start_diagnosis → worker → 图 → run 终态
# ---------------------------------------------------------------------------

async def test_start_diagnosis_wires_graph_to_finished_run():
    incident = await _new_incident()
    started = await orch.start_diagnosis(incident.id, triggered_by=0)
    run_id = started["run_id"]

    run = None
    for _ in range(200):
        run = await DiagnosisRun.get_or_none(run_id=run_id)
        if run and run.status in ("finished", "failed"):
            break
        await asyncio.sleep(0.05)

    assert run is not None and run.status == "finished"
    # 未打桩时走系统种子桩路径：低置信必然耗尽回环，事件不会停留在 diagnosing
    assert (await Incident.get(id=incident.id)).status in ("resolved", "human_needed")
    assert await IncidentStep.filter(incident_id=incident.id, step="triage").exists()


# ---------------------------------------------------------------------------
# 检查点：闸口暂停 / 确认续跑 / 崩溃续跑
# ---------------------------------------------------------------------------

async def _start_run(incident: Incident) -> str:
    """走 `_execute` 全出口跑图（含 run 状态机与检查点），返回 run_id。"""
    run_id = f"run_{uuid.uuid4().hex[:20]}"
    await DiagnosisRun.create(
        run_id=run_id, incident_id=incident.id, status="queued",
        triggered_by=0, heartbeat_at=model_now(),
    )
    await Incident.filter(id=incident.id).update(status="diagnosing")
    await orch._execute(incident.id, run_id)
    return run_id


CRASH_CAUSE = "容器反复重启，需要删除故障 Pod 触发重建"


async def test_high_risk_pauses_thread_instead_of_finishing(monkeypatch):
    _stub_context(monkeypatch)
    _stub_diagnose(monkeypatch, confidence=0.95, cause=CRASH_CAUSE)
    incident = await _new_incident()
    run_id = await _start_run(incident)

    run = await DiagnosisRun.get(run_id=run_id)
    assert run.status == "paused"
    assert run.finished_at is None
    # 断点确实落进了检查点存储，而不是只有业务库的一个状态字段
    snapshot = await orch.get_graph().aget_state(checkpoint.thread_config(run_id))
    assert list(snapshot.next) == ["await_human"]


async def test_confirm_approve_resumes_graph_to_resolution(monkeypatch):
    _stub_context(monkeypatch)
    _stub_diagnose(monkeypatch, confidence=0.95, cause=CRASH_CAUSE)
    incident = await _new_incident()
    run_id = await _start_run(incident)
    action = await RemediationAction.filter(incident_id=incident.id, risk_level="high").first()
    assert action is not None

    result = await orch.confirm_incident(
        incident.id, action.id, "approve", user_id=7,
        actual_cause="镜像拉取失败", correct_actions=None,
    )
    assert result["status"] == "resolved"
    assert result["recap_triggered"] is True

    steps = [step for step, _ in await _trajectory(incident.id)]
    # 续跑而非重跑：前序已完成的节点不得再来一遍
    assert steps.count("diagnose") == 1 and steps.count("context") == 1
    assert steps[-2:] == ["ticket", "recap"]

    fresh = await Incident.get(id=incident.id)
    # 走 resume 路径也必须写上确认人与人工修正，否则审计链比旧内联收口少字段
    assert fresh.confirmed_by == 7
    assert fresh.actual_cause == "镜像拉取失败"
    assert (await DiagnosisRun.get(run_id=run_id)).status == "finished"


async def test_partial_approval_keeps_thread_paused(monkeypatch):
    _stub_context(monkeypatch)
    _stub_diagnose(monkeypatch, confidence=0.95, cause=CRASH_CAUSE)
    incident = await _new_incident()
    run_id = await _start_run(incident)
    first = await RemediationAction.filter(incident_id=incident.id, risk_level="high").first()
    await RemediationAction.create(
        incident_id=incident.id, command="kubectl delete deploy ordersvc",
        description="第二条待批高危动作", risk_level="high", requires_confirm=True, status="pending",
    )

    result = await orch.confirm_incident(
        incident.id, first.id, "approve", user_id=7, actual_cause=None, correct_actions=None
    )
    assert result["status"] == "human_needed"
    assert (await DiagnosisRun.get(run_id=run_id)).status == "paused"
    assert not await IncidentStep.filter(incident_id=incident.id, step="ticket").exists()


async def test_exhausted_low_confidence_does_not_pause(monkeypatch):
    """没有待批动作时不能挂线程 —— 那会留下一个永远不会被 resume 的断点。"""
    _stub_context(monkeypatch)
    _stub_diagnose(monkeypatch, confidence=0.05)
    incident = await _new_incident()
    run_id = await _start_run(incident)

    assert (await DiagnosisRun.get(run_id=run_id)).status == "finished"
    assert (await Incident.get(id=incident.id)).status == "human_needed"


async def test_stale_run_with_checkpoint_resumes_instead_of_failing(monkeypatch):
    """崩溃恢复：有断点的 run 续跑回闸口，不再一律判成 interrupted + 转人工。"""
    _stub_context(monkeypatch)
    _stub_diagnose(monkeypatch, confidence=0.95, cause=CRASH_CAUSE)
    incident = await _new_incident()
    run_id = await _start_run(incident)
    assert (await DiagnosisRun.get(run_id=run_id)).status == "paused"

    stale = model_now() - timedelta(seconds=settings.diagnosis_stale_seconds + 60)
    await DiagnosisRun.filter(run_id=run_id).update(status="running", heartbeat_at=stale)

    assert await orch.recover_stale_runs() == 1

    run = await DiagnosisRun.get(run_id=run_id)
    for _ in range(100):
        if run.status == "paused":
            break
        await asyncio.sleep(0.05)
        run = await DiagnosisRun.get(run_id=run_id)
    assert run.status == "paused"
    assert (await Incident.get(id=incident.id)).status == "human_needed"


# ---------------------------------------------------------------------------
# 既有缺陷回归：结论长度越过列宽时收口不能整条失败
# ---------------------------------------------------------------------------

async def test_overlong_root_cause_is_clipped_at_write_boundary(monkeypatch):
    """召回原文当根因时长超 255 会打挂收口：迁移前 run failed，现在必须落库成功。"""
    _stub_context(monkeypatch)
    _stub_diagnose(monkeypatch, confidence=0.95, cause="内存淘汰策略不当导致节点持续换页" * 30)
    incident = await _new_incident()
    run_id = await _start_run(incident)

    fresh = await Incident.get(id=incident.id)
    assert fresh.status == "resolved"
    assert len(fresh.root_cause) == 255
    assert (await DiagnosisRun.get(run_id=run_id)).status == "finished"
