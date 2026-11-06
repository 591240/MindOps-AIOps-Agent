"""F-06 §5.3「无出处不下结论」门禁（V1.1：按 run_id 限定，修 R7）。

铁律：一条 root_cause 只有在能回溯到**当前 run** 的证据时，才允许进入
"已诊断"状态（resolved / 待确认 human_needed）；否则强制降级为
`human_needed`，`human_reason="no_evidence"`。

判定三分支（§5.3）：
- (a) `source in (rag, rule)`：当前 run 的 context/diagnose 步骤存在任意 evidence；
- (b) `source == llm`：当前 run 存在 `llm-reasoning` 证据（模型推理摘录，如实标注）；
- (c) 其它（含空 source）→ 一律降级。

实现要点：
- **R7**：必须按 run_id 限定。旧 run 的历史证据不能为本次结论背书，否则
  回环重诊断（F-04）时旧证据会让"无出处的新结论"骗过门禁。
- `Evidence.step_id` 是 `IntField` 而非外键，无法用 `step__run_id` 跨表过滤，
  因此先按 run_id 取 step ids，再用 `step_id__in` 过滤（两步查询）。
"""

from __future__ import annotations

from app.models import Evidence, IncidentStep
from app.observability import metrics
from app.observability.logging import log_event

# 只有这两步会产出检索/推理证据（Context 检索 / Diagnose 推理）
EVIDENCE_STEPS = ("context", "diagnose")
# rule 来源例外：规则命中的出处是 pattern_rules.yaml 的条目本身（落在 triage 步骤），
# 而非 evidence 表的知识切片。若此处不扩展，F-05「规则层优先」的短路路径
# （命中即跳过 Context/Diagnose）会被门禁无条件降级，与设计意图冲突。
RULE_EVIDENCE_STEPS = ("triage", "context", "diagnose")
LLM_SOURCE_DOC = "llm-reasoning"
RULE_SOURCE_DOC = "pattern-rule"


async def _step_ids(
    incident_id: int, run_id: str, steps: tuple[str, ...] | None
) -> list[int]:
    qs = IncidentStep.filter(incident_id=incident_id, run_id=run_id)
    if steps:
        qs = qs.filter(step__in=steps)
    return list(await qs.values_list("id", flat=True))


async def has_evidence(incident_id: int, run_id: str, source: str | None) -> bool:
    """当前 run 是否具备支撑该来源结论的证据。"""
    if source in ("rag", "rule"):
        # rule 允许 triage 步骤的证据（规则条目本身即出处）
        steps = RULE_EVIDENCE_STEPS if source == "rule" else EVIDENCE_STEPS
        step_ids = await _step_ids(incident_id, run_id, steps)
        if not step_ids:
            return False
        return await Evidence.filter(
            incident_id=incident_id, step_id__in=step_ids
        ).exists()
    if source == "llm":
        step_ids = await _step_ids(incident_id, run_id, None)
        if not step_ids:
            return False
        return await Evidence.filter(
            incident_id=incident_id,
            step_id__in=step_ids,
            source_doc=LLM_SOURCE_DOC,
        ).exists()
    return False


async def gate(incident_id: int, run_id: str, source: str | None) -> bool:
    """通过返回 True；不通过记指标与告警日志，由调用方执行降级。"""
    passed = await has_evidence(incident_id, run_id, source)
    if not passed:
        label = source or "unknown"
        metrics.gate_no_evidence_total.labels(source=label).inc()
        log_event(
            40,
            "gate.no_evidence",
            incident_id=incident_id,
            run_id=run_id,
            source=label,
        )
    return passed
