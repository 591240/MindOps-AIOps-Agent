"""F-06 处置建议与溯源回归测试（文档 V1.1 §9.1 用例矩阵）。

覆盖：证据落库四类边界、风险判定、处置建议状态机、R6/R7/R8 三个代码级 bug、
门禁三分支、close/feedback 端点语义。
"""

import os
import uuid

os.environ.setdefault("MINDOPS_MYSQL_DB", "mindops_f06_test")

import pytest
import pytest_asyncio

from app.agents import evidence_gate, orchestrator as orch
from app.agents.risk import check_risk
from app.db import close_db, init_db
from app.models import Evidence, Incident, IncidentStep, RemediationAction

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


def _uniq(prefix: str = "f06") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


async def _new_incident(status: str = "human_needed"):
    return await Incident.create(
        title=_uniq("inc"), component="redis", severity="P1", status=status
    )


async def _new_step(incident_id: int, step: str = "context") -> IncidentStep:
    return await IncidentStep.create(
        run_id=_uniq("run"), incident_id=incident_id, step=step, agent=f"{step}Agent"
    )


# ---- 证据落库（FR-1 / §5.1）----


async def test_evidence_persist_normal():
    inc = await _new_incident()
    step = await _new_step(inc.id)
    await orch._persist_evidence(
        {"incident_id": inc.id, "run_id": step.run_id},
        step.id,
        {"kb_chunks": [{"quote": "当 used_memory > maxmemory 时新写入会被淘汰", "source_doc": "Redis运维Runbook-v3", "score": 0.82, "chunk_id": 7}]},
    )
    rows = await Evidence.filter(incident_id=inc.id)
    assert len(rows) == 1
    assert rows[0].step_id == step.id
    assert rows[0].source_doc == "Redis运维Runbook-v3"
    assert rows[0].score == pytest.approx(0.82)


async def test_evidence_similar_incident_source():
    """V1.1：相似案例证据 → similar-incident:{id}，chunk_id/score 均 NULL。"""
    inc = await _new_incident()
    step = await _new_step(inc.id)
    await orch._persist_evidence(
        {"incident_id": inc.id, "run_id": step.run_id},
        step.id,
        {"kb_chunks": [{"quote": "历史同类根因：连接池耗尽", "similar_incident_id": 40}]},
    )
    row = (await Evidence.filter(incident_id=inc.id))[0]
    assert row.source_doc == "similar-incident:40"
    assert row.chunk_id is None and row.score is None


async def test_evidence_empty_quote_skipped():
    inc = await _new_incident()
    step = await _new_step(inc.id)
    await orch._persist_evidence(
        {"incident_id": inc.id, "run_id": step.run_id},
        step.id,
        {"kb_chunks": [{"content": "   "}, {"quote": ""}]},
    )
    assert await Evidence.filter(incident_id=inc.id).count() == 0


async def test_evidence_truncated():
    inc = await _new_incident()
    step = await _new_step(inc.id)
    await orch._persist_evidence(
        {"incident_id": inc.id, "run_id": step.run_id},
        step.id,
        {"kb_chunks": [{"quote": "x" * 70000, "source_doc": "big-doc"}]},
    )
    row = (await Evidence.filter(incident_id=inc.id))[0]
    assert len(row.quote) <= 65535
    assert row.quote.endswith("[截断]")


# ---- 风险判定（FR-3 / §5.2）----


async def test_risk_high_command():
    assert check_risk("rm -rf /data") == "high"


async def test_risk_unknown_write_defaults_medium():
    """未知写操作保守判 medium（安全优先，§10.1 R5 显式取舍）。"""
    assert check_risk("alter table x add col") == "medium"


# ---- 处置建议落库状态（§5.2 / F-03 V1.1 口径）----


async def test_store_actions_auto_approves_non_high():
    inc = await _new_incident()
    state = {
        "incident_id": inc.id,
        "actions": [
            {"command": "redis-cli config set maxmemory-policy allkeys-lru", "description": "调整淘汰策略", "risk_level": "medium"},
            {"command": "rm -rf /data/cache", "description": "高危清理", "risk_level": "high"},
        ],
    }
    has_high = await orch._store_actions(state)
    assert has_high is True
    rows = {row.command: row for row in await RemediationAction.filter(incident_id=inc.id)}
    assert rows["redis-cli config set maxmemory-policy allkeys-lru"].status == "approved"
    assert rows["rm -rf /data/cache"].status == "pending"
    assert rows["rm -rf /data/cache"].requires_confirm is True


async def test_r6_rediagnosis_keeps_approved_and_skipped():
    """R6：重诊断只清 pending，approved/skipped 永不删（人工确认是审计资产）。"""
    inc = await _new_incident()
    await RemediationAction.create(incident_id=inc.id, command="old-approved", risk_level="medium", requires_confirm=False, status="approved", confirmed_by=9)
    await RemediationAction.create(incident_id=inc.id, command="old-skipped", risk_level="high", requires_confirm=True, status="skipped", confirmed_by=9)
    await RemediationAction.create(incident_id=inc.id, command="old-pending", risk_level="high", requires_confirm=True, status="pending")

    await orch._store_actions({"incident_id": inc.id, "actions": [{"command": "new-low", "description": "", "risk_level": "low"}]})

    rows = {row.command: row for row in await RemediationAction.filter(incident_id=inc.id)}
    assert "old-pending" not in rows          # pending 被清理重建
    assert rows["old-approved"].status == "approved"   # 审计记录保留
    assert rows["old-approved"].confirmed_by == 9
    assert rows["old-skipped"].status == "skipped"
    assert rows["new-low"].status == "approved"        # low 自动批准


# ---- 确认执行流（§5.4 / R8）----


async def test_confirm_approve_last_action_resolves():
    inc = await _new_incident()
    action = await RemediationAction.create(incident_id=inc.id, command="systemctl restart redis", risk_level="high", requires_confirm=True, status="pending")
    result = await orch.confirm_incident(inc.id, action.id, "approve", user_id=1, actual_cause="内存淘汰策略不当", correct_actions=None)
    assert result["action_status"] == "approved"
    assert result["status"] == "resolved"
    assert (await Incident.get(id=inc.id)).actual_cause == "内存淘汰策略不当"


async def test_confirm_reject_does_not_close_event():
    """V1.1：reject 不再连带关闭事件；仍有 pending 时保持 human_needed。"""
    inc = await _new_incident()
    a1 = await RemediationAction.create(incident_id=inc.id, command="cmd-1", risk_level="high", requires_confirm=True, status="pending")
    a2 = await RemediationAction.create(incident_id=inc.id, command="cmd-2", risk_level="high", requires_confirm=True, status="pending")
    result = await orch.confirm_incident(inc.id, a1.id, "reject", user_id=2, actual_cause="真实根因是连接泄漏", correct_actions=None)
    assert result["action_status"] == "skipped"
    assert result["status"] == "human_needed"
    assert (await RemediationAction.get(id=a1.id)).status == "skipped"
    assert (await RemediationAction.get(id=a2.id)).status == "pending"   # 未卡死，仍可确认


async def test_r8_reject_collects_calibration_sample(monkeypatch):
    """R8：reject 是"AI 判错"的最高价值样本，必须采集。"""
    inc = await _new_incident()
    action = await RemediationAction.create(incident_id=inc.id, command="cmd-x", risk_level="high", requires_confirm=True, status="pending")
    calls = []

    async def fake_collect(incident_id, reported_conf, root_cause, actual_cause, judge=None):
        calls.append((incident_id, actual_cause))
        return {}

    monkeypatch.setattr(orch.calibration, "collect_sample", fake_collect)
    await orch.confirm_incident(inc.id, action.id, "reject", user_id=3, actual_cause="连接泄漏", correct_actions=None)
    assert calls and calls[0][1] == "连接泄漏"


async def test_confirm_rejects_non_pending_action():
    inc = await _new_incident()
    action = await RemediationAction.create(incident_id=inc.id, command="cmd-y", risk_level="medium", requires_confirm=False, status="approved")
    with pytest.raises(ValueError):
        await orch.confirm_incident(inc.id, action.id, "approve", user_id=1, actual_cause=None, correct_actions=None)


# ---- 门禁（§5.3 / R7）----


async def test_gate_llm_without_reasoning_evidence_fails():
    inc = await _new_incident()
    step = await _new_step(inc.id, step="diagnose")
    assert await evidence_gate.gate(inc.id, step.run_id, "llm") is False


async def test_gate_llm_with_reasoning_evidence_passes():
    inc = await _new_incident()
    step = await _new_step(inc.id, step="diagnose")
    await orch._persist_reasoning_evidence(
        {"incident_id": inc.id, "run_id": step.run_id, "root_cause": "组件存在内存压力"}, step.id
    )
    assert await evidence_gate.gate(inc.id, step.run_id, "llm") is True


async def test_r7_gate_ignores_other_run_evidence():
    """R7：旧 run 的证据不能为本次结论背书。"""
    inc = await _new_incident()
    old_step = await _new_step(inc.id, step="diagnose")
    await orch._persist_reasoning_evidence(
        {"incident_id": inc.id, "run_id": old_step.run_id, "root_cause": "旧结论"}, old_step.id
    )
    assert await evidence_gate.gate(inc.id, old_step.run_id, "llm") is True
    assert await evidence_gate.gate(inc.id, "run_another_brand_new", "llm") is False


async def test_gate_rag_zero_evidence_fails():
    inc = await _new_incident()
    step = await _new_step(inc.id, step="context")
    assert await evidence_gate.gate(inc.id, step.run_id, "rag") is False


async def test_gate_rule_uses_triage_evidence():
    """规则命中：出处是 pattern_rules 条目（落在 triage 步骤），应通过门禁。"""
    inc = await _new_incident()
    step = await _new_step(inc.id, step="triage")
    await orch._persist_rule_evidence(
        {"incident_id": inc.id, "run_id": step.run_id, "rule_root_cause": "Redis 内存达到上限"}, step.id
    )
    assert await evidence_gate.gate(inc.id, step.run_id, "rule") is True


# ---- close / feedback（§3.5 / §5.5）----


async def test_close_rejects_when_pending_exists():
    inc = await _new_incident()
    await RemediationAction.create(incident_id=inc.id, command="rm -rf /tmp/x", risk_level="high", requires_confirm=True, status="pending")
    with pytest.raises(ValueError):
        await orch.close_incident(inc.id, user_id=1, reason="false_positive", actual_cause=None, correct_actions=None)


async def test_close_without_pending_succeeds():
    inc = await _new_incident()
    await RemediationAction.create(incident_id=inc.id, command="df -h", risk_level="low", requires_confirm=False, status="approved")
    result = await orch.close_incident(inc.id, user_id=1, reason="false_positive", actual_cause="监控阈值配置错误", correct_actions=None)
    assert result["status"] == "closed"
    assert (await Incident.get(id=inc.id)).actual_cause == "监控阈值配置错误"


async def test_feedback_does_not_change_status_or_actions():
    inc = await _new_incident(status="resolved")
    action = await RemediationAction.create(incident_id=inc.id, command="df -h", risk_level="low", requires_confirm=False, status="approved")
    result = await orch.feedback_incident(inc.id, user_id=1, actual_cause="真实原因是磁盘配额", correct_actions=None)
    assert result["status"] == "resolved"
    assert (await Incident.get(id=inc.id)).actual_cause == "真实原因是磁盘配额"
    assert (await RemediationAction.get(id=action.id)).status == "approved"   # action 未被 reopen
