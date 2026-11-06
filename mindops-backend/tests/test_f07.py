"""F-07 知识回灌闭环回归测试（文档 V1.1 §9.1 用例矩阵；V1.2 门槛口径已更新）。

覆盖：四段式病历生成、回灌门槛（多信号：人工修正 > 置信度）、confirm/close/feedback
触发矩阵、幂等跳过、version++ 进化、回灌失败容错（不翻转 resolved）、recap 步骤落库。

V1.2 变更：门槛从 recap.generality 假分（等价 conf>=0.6）迁到 orchestrator
多信号判定——人工修正（actual_cause）可越过置信度门槛。相关用例见
test_low_confidence_with_human_correction_ingests。

回灌成功路径 monkeypatch 掉 milvus_client.upsert（本测试环境无 Milvus），
ingest 管线本体（幂等/事务/补偿）的行为由 F-05 回归测试覆盖。
"""

import os
import uuid

os.environ.setdefault("MINDOPS_MYSQL_DB", "mindops_f07_test")

import pytest
import pytest_asyncio

from app.agents import orchestrator as orch
from app.agents import recap
from app.db import close_db, init_db
from app.models import Incident, IncidentStep, KnowledgeDoc, RemediationAction
from app.rag import milvus_client

pytestmark = pytest.mark.asyncio(loop_scope="module")


@pytest_asyncio.fixture(scope="module", loop_scope="module", autouse=True)
async def initialized_database():
    await init_db()
    yield
    await close_db()


@pytest_asyncio.fixture(autouse=True, loop_scope="module")
async def _clean_tables():
    """本文件回灌的 incident 文档统一按 title 前缀清理，不碰其他用例的数据。"""
    await Incident.all().delete()
    await KnowledgeDoc.filter(source_type="incident").delete()
    yield
    await KnowledgeDoc.filter(source_type="incident").delete()


@pytest.fixture(autouse=True)
def _no_milvus(monkeypatch):
    """回灌成功路径不依赖 Milvus：upsert 置为空操作（向量写失败走补偿再抛错）。"""
    async def fake_upsert(_rows, _vectors):
        return None

    monkeypatch.setattr(milvus_client, "upsert", fake_upsert)


def _uniq(prefix: str = "f07") -> str:
    return f"{prefix}_{uuid.uuid4().hex[:10]}"


async def _new_incident(**kwargs) -> Incident:
    fields = dict(
        title=_uniq("inc"), component="redis", severity="P1", status="human_needed",
        root_cause="内存淘汰策略不当", root_cause_confidence=0.82, root_cause_source="rag",
    )
    fields.update(kwargs)
    return await Incident.create(**fields)


async def _pending_action(incident_id: int, command: str = "systemctl restart redis") -> RemediationAction:
    return await RemediationAction.create(
        incident_id=incident_id, command=command, description="重启服务",
        risk_level="high", requires_confirm=True, status="pending",
    )


# ---- 病历生成（文档 §5.2，TC-2 前置）----


async def test_case_study_four_sections():
    """四段式病历：现象（前 3 条告警）/根因/处置/备注，title = 根因+组件。"""
    alerts = [
        {"source": "prometheus", "component": "redis", "severity": "P2", "title": "Redis内存超限告警", "detail": "used_memory=3.2GB"},
        {"source": "prometheus", "component": "redis", "severity": "P2", "title": "Redis内存超限告警", "detail": "used_memory=3.4GB"},
        {"source": "prometheus", "component": "redis", "severity": "P1", "title": "Redis内存超限告警", "detail": "used_memory=3.5GB"},
        {"source": "prometheus", "component": "redis", "severity": "P1", "title": "第4条不该出现", "detail": ""},
    ]
    state = {
        "title": "Redis内存超限", "component": "redis",
        "root_cause": "内存淘汰策略不当", "root_cause_confidence": 0.82, "root_cause_source": "rag",
        "actual_cause": None, "correct_actions": None,
        "alerts": alerts,
        "actions": [
            {"command": "config set maxmemory-policy allkeys-lru", "risk_level": "medium", "description": "调整淘汰策略", "status": "approved"},
            {"command": "rm -rf /data", "risk_level": "high", "description": "高危清理", "status": "skipped"},
        ],
    }
    result = await recap.run(state)
    assert result["ingest_ready"] is True
    assert result["case_title"] == "内存淘汰策略不当处置方案（redis）"
    assert result["generality"] == 0.8   # conf 0.82 >= 0.6（仅留痕，不参与门槛）
    assert result["generality_kind"] == "heuristic_evidence_sufficiency"
    # 现象段只取前 3 条告警
    assert "used_memory=3.5GB" in result["recap"]
    assert "第4条不该出现" not in result["recap"]
    # 处置段收录 approved、排除 skipped
    assert "allkeys-lru" in result["recap"]
    assert "rm -rf /data" not in result["recap"]
    # 四段结构
    for section in ("故障现象：", "根因：", "处置：", "备注："):
        assert section in result["recap"]


async def test_case_study_actual_cause_and_correct_actions_override():
    """人工修正（actual_cause/correct_actions）优先于 AI 结论。"""
    state = {
        "title": "事件", "component": "mysql", "root_cause": "AI猜的根因",
        "root_cause_confidence": 0.3, "root_cause_source": "llm",
        "actual_cause": "真实根因是连接泄漏", "correct_actions": "set global max_connections=500",
        "alerts": [],
        "actions": [{"command": "old-cmd", "risk_level": "low", "description": "", "status": "approved"}],
    }
    result = await recap.run(state)
    assert result["case_title"] == "真实根因是连接泄漏处置方案（mysql）"
    assert "AI猜的根因" not in result["recap"]
    assert "真实根因是连接泄漏" in result["recap"]
    assert "set global max_connections=500" in result["recap"]
    assert "old-cmd" not in result["recap"]          # correct_actions 整体优先
    assert "人工修正根因：真实根因是连接泄漏" in result["recap"]


async def test_case_study_no_cause_marks_not_ready():
    """actual_cause/root_cause 均空：ingest_ready=False，门槛拒绝。"""
    result = await recap.run({"title": "事件", "component": "redis", "root_cause": None, "actual_cause": None, "alerts": [], "actions": []})
    assert result["ingest_ready"] is False
    assert result["case_title"] == ""
    assert result["generality"] == 0.4   # 无置信度 -> 0.4（仅留痕；门槛由 ingest_ready 拦下）


# ---- 触发矩阵（文档 §5.1，TC-1/3/4）----


async def test_confirm_resolution_ingests_case():
    """确认收口：最后 pending high 被 approve -> resolved 并回灌（created_by=确认人）。"""
    inc = await _new_incident()
    action = await _pending_action(inc.id)
    result = await orch.confirm_incident(inc.id, action.id, "approve", user_id=7, actual_cause="内存淘汰策略不当", correct_actions=None)
    assert result["status"] == "resolved"
    assert result["recap_triggered"] is True
    injected = result["knowledge_injected"]
    assert injected and injected["version"] == 1 and injected["idempotent"] is False
    doc = await KnowledgeDoc.get(id=injected["doc_id"])
    assert doc.source_type == "incident" and doc.created_by == 7
    assert doc.title.startswith("内存淘汰策略不当处置方案（redis）")


async def test_reject_all_does_not_ingest():
    """全 reject：事件保持 human_needed，不回灌。"""
    inc = await _new_incident()
    action = await _pending_action(inc.id, command="rm -rf /data")
    result = await orch.confirm_incident(inc.id, action.id, "reject", user_id=1, actual_cause=None, correct_actions=None)
    assert result["status"] == "human_needed"
    assert result["knowledge_injected"] is None
    assert await KnowledgeDoc.filter(source_type="incident").count() == 0


async def test_close_does_not_ingest():
    """显式 close：无可复盘知识，不回灌（F-03 V1.1 口径）。"""
    inc = await _new_incident(status="resolved")
    await RemediationAction.create(incident_id=inc.id, command="df -h", risk_level="low", requires_confirm=False, status="approved")
    result = await orch.close_incident(inc.id, user_id=1, reason="false_positive", actual_cause="阈值配置错误", correct_actions=None)
    assert result["status"] == "closed" and result["recap_triggered"] is False
    assert await KnowledgeDoc.filter(source_type="incident").count() == 0


async def test_feedback_does_not_ingest():
    """resolved 态补录：只采集素材，不回灌（V1.0 口径）。"""
    inc = await _new_incident(status="resolved")
    await RemediationAction.create(incident_id=inc.id, command="df -h", risk_level="low", requires_confirm=False, status="approved")
    result = await orch.feedback_incident(inc.id, user_id=1, actual_cause="连接泄漏", correct_actions=None)
    assert result["recap_triggered"] is False
    assert await KnowledgeDoc.filter(source_type="incident").count() == 0


# ---- 幂等与版本进化（文档 §5.3，TC-5/6）----


async def test_reingest_identical_content_is_idempotent(monkeypatch):
    """同内容重复回灌（force 重诊断后素材未变，再次收口触发 Recap）：只入库一次。"""
    inc = await _new_incident(title=_uniq("reingest"))
    await _pending_action(inc.id)

    first = await orch.run_recap(inc.id, "run_reingest_a", actual_cause="内存淘汰策略不当", confirmed_by=1)
    second = await orch.run_recap(inc.id, "run_reingest_b", actual_cause="内存淘汰策略不当", confirmed_by=2)
    assert first["knowledge_injected"]["version"] == 1
    injected = second["knowledge_injected"]
    assert injected["doc_id"] == first["knowledge_injected"]["doc_id"]
    assert injected["version"] == 1
    assert injected["idempotent"] is True
    assert await KnowledgeDoc.filter(title=first["knowledge_injected"]["title"]).count() == 1


async def test_corrected_actions_bump_version(monkeypatch):
    """修正 correct_actions 后再收口：version++ 进化，旧版（doc+chunks）is_latest=False。"""
    from app.models import KnowledgeChunk

    inc = await _new_incident(title=_uniq("evolve"))
    action = await _pending_action(inc.id)
    first = await orch.confirm_incident(inc.id, action.id, "approve", user_id=1, actual_cause="内存淘汰策略不当", correct_actions=None)
    old_doc_id = first["knowledge_injected"]["doc_id"]

    action2 = await _pending_action(inc.id)
    second = await orch.confirm_incident(inc.id, action2.id, "approve", user_id=1, actual_cause="内存淘汰策略不当", correct_actions="改用 LRU 淘汰并扩容 maxmemory")
    injected = second["knowledge_injected"]
    assert injected["version"] == 2 and injected["doc_id"] != old_doc_id

    old_doc, new_doc = await KnowledgeDoc.get(id=old_doc_id), await KnowledgeDoc.get(id=injected["doc_id"])
    assert old_doc.is_latest is False and new_doc.is_latest is True
    assert new_doc.content_hash != old_doc.content_hash
    assert not await KnowledgeChunk.filter(doc_id=old_doc_id, is_latest=True).exists()
    assert await KnowledgeChunk.filter(doc_id=injected["doc_id"], is_latest=True).count() == new_doc.chunk_count


# ---- 门槛与容错（文档 §5.4，TC-7/8）----


async def test_low_confidence_skips_ingest():
    """低置信（0.4 < 0.6）且无人工修正：跳过回灌，仅记日志。"""
    inc = await _new_incident(root_cause="拿不准的根因", root_cause_confidence=0.4)
    action = await _pending_action(inc.id)
    result = await orch.confirm_incident(inc.id, action.id, "approve", user_id=1, actual_cause=None, correct_actions=None)
    assert result["status"] == "resolved"
    assert result["knowledge_injected"] is None
    assert await KnowledgeDoc.filter(source_type="incident").count() == 0


async def test_low_confidence_with_human_correction_ingests():
    """V1.2 新口径：低置信 + 人工修正过 -> 仍回灌（人背书的样本价值高于污染风险）。

    这正是旧 generality 门槛误杀的场景：conf=0.4 -> generality=0.4 被拦，
    但事件带了 actual_cause（人工验证过），是最该沉淀的样本。
    """
    inc = await _new_incident(root_cause="拿不准的根因", root_cause_confidence=0.4)
    action = await _pending_action(inc.id)
    result = await orch.confirm_incident(inc.id, action.id, "approve", user_id=1, actual_cause="真实根因是连接池耗尽", correct_actions=None)
    assert result["status"] == "resolved"
    injected = result["knowledge_injected"]
    assert injected is not None
    doc = await KnowledgeDoc.get(id=injected["doc_id"])
    assert doc.title.startswith("真实根因是连接池耗尽处置方案（redis）")
    assert "真实根因是连接池耗尽" in doc.content


async def test_no_root_cause_skips_ingest():
    """无根因（ingest_ready=False）：一律拒绝，即便有置信度也无内容可沉淀。"""
    inc = await _new_incident(root_cause=None, root_cause_confidence=0.9)
    action = await _pending_action(inc.id)
    result = await orch.confirm_incident(inc.id, action.id, "approve", user_id=1, actual_cause=None, correct_actions=None)
    assert result["status"] == "resolved"
    assert result["knowledge_injected"] is None
    assert await KnowledgeDoc.filter(source_type="incident").count() == 0


async def test_ingest_failure_does_not_break_confirm(monkeypatch):
    """TC-8 容错铁律：ingest 抛错 -> confirm 仍 resolved，事件状态不翻转。"""
    async def boom(**_kwargs):
        raise RuntimeError("milvus is unavailable")

    monkeypatch.setattr(orch, "ingest_document", boom)
    published = []

    async def fake_publish(event, payload):
        published.append((event, payload))

    monkeypatch.setattr(orch, "_publish", fake_publish)
    inc = await _new_incident()
    action = await _pending_action(inc.id)
    result = await orch.confirm_incident(inc.id, action.id, "approve", user_id=1, actual_cause="内存淘汰策略不当", correct_actions=None)
    assert result["status"] == "resolved"           # 主流程不受影响
    assert result["knowledge_injected"] is None
    assert (await Incident.get(id=inc.id)).status == "resolved"
    assert published and published[0][0] == "incident.recap_failed"


# ---- 回灌结果可见（文档 §4.2，TC-2/9 前置）----


async def test_recap_step_persists_knowledge_injected():
    """回灌结果随 recap 步骤 output_summary 落库，详情页可追溯。"""
    import json as jsonlib

    inc = await _new_incident()
    action = await _pending_action(inc.id)
    result = await orch.confirm_incident(inc.id, action.id, "approve", user_id=7, actual_cause="内存淘汰策略不当", correct_actions=None)
    step = await IncidentStep.filter(incident_id=inc.id, agent="RecapAgent").order_by("-id").first()
    assert step is not None
    output = jsonlib.loads(step.output_summary)
    assert output["knowledge_injected"]["doc_id"] == result["knowledge_injected"]["doc_id"]
    assert output["case_title"] == result["knowledge_injected"]["title"]
