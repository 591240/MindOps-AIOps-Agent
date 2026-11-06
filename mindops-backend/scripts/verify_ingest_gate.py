"""端到端验证 F-07 V1.2 回灌门槛（多信号：人工修正 > 置信度）。

三个场景：
  A. conf=0.4 + 人工修正 actual_cause -> 应回灌（旧 generality 门槛会误杀）
  B. conf=0.4 无人工修正             -> 应跳过
  C. conf=0.85 无人工修正            -> 应回灌（自动收口路径行为不变）

用法：cd mindops-backend && ./.venv/Scripts/python.exe scripts/verify_ingest_gate.py
说明：会向 mindops_f07_test 库写入并随后清理临时数据；不依赖 Milvus（mock upsert）。
"""

import asyncio
import os
import sys

sys.path.insert(0, os.getcwd())
os.environ.setdefault("MINDOPS_MYSQL_DB", "mindops_f07_test")

from app.agents import orchestrator as orch
from app.db import init_db
from app.models import Incident, KnowledgeDoc, RemediationAction
from app.rag import milvus_client


async def main() -> None:
    await init_db()

    # 不依赖 Milvus：向量写入置为空操作（本脚本只验证门槛判定，不验证检索）
    async def fake_upsert(_rows, _vectors):
        return None

    milvus_client.upsert = fake_upsert

    suffix = os.urandom(4).hex()
    created_incidents = []

    async def scenario(tag: str, component: str, confidence: float, actual_cause: str | None):
        inc = await Incident.create(
            title=f"verify_{tag}_{suffix}", component=component, severity="P1",
            status="human_needed", root_cause="拿不准的根因" if confidence < 0.6 else "慢查询拖垮连接池",
            root_cause_confidence=confidence, root_cause_source="llm" if confidence < 0.6 else "rag",
        )
        act = await RemediationAction.create(
            incident_id=inc.id, command="systemctl restart redis", description="重启",
            risk_level="high", requires_confirm=True, status="pending",
        )
        created_incidents.append(inc.id)
        res = await orch.confirm_incident(
            inc.id, act.id, "approve", user_id=1, actual_cause=actual_cause, correct_actions=None
        )
        injected = res.get("knowledge_injected")
        print(f"[{tag}] conf={confidence} 人工修正={'有' if actual_cause else '无'} "
              f"-> status={res['status']}, injected={'有' if injected else '无'}")
        if injected:
            doc = await KnowledgeDoc.get(id=injected["doc_id"])
            print(f"        入库: {doc.title}")
            if actual_cause:
                print(f"        正文含人工根因: {actual_cause in doc.content}")
        return injected

    try:
        a = await scenario("A", "redis", 0.4, "真实根因是连接池耗尽")
        b = await scenario("B", "redis", 0.4, None)
        c = await scenario("C", "mysql", 0.85, None)

        print()
        ok = a is not None and b is None and c is not None
        print("期望 A=有 B=无 C=有 ->", "✅ 门槛按 V1.2 生效" if ok else "❌ 与预期不符")

        await KnowledgeDoc.filter(source_type="incident").filter(title__contains=suffix).delete()
    finally:
        for inc_id in created_incidents:
            await RemediationAction.filter(incident_id=inc_id).delete()
            await Incident.filter(id=inc_id).delete()


if __name__ == "__main__":
    asyncio.run(main())
