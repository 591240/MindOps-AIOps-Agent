"""RecapAgent：事件 resolved 后生成四段式病历（F-07 回灌素材）。

V1.1 升级：原三段式（问题/原因/方案）升级为四段式（现象/根因/处置/备注）。

V1.2：回灌门槛从本模块的 `generality` 假分迁至 orchestrator._maybe_ingest
多信号判定（人工修正优先于置信度）。本模块只负责生成病历与基础标记，
不再决定"该不该入库"。
"""

from __future__ import annotations

MAX_CASE_TITLE = 255  # 与 knowledge_docs.title VARCHAR(255) 对齐，超长前置截断


def _phenomenon(state: dict) -> str:
    lines = []
    for alert in (state.get("alerts") or [])[:3]:
        lines.append(
            "- {source} / {component} / {severity} / {title} / {detail}".format(
                source=alert.get("source") or "unknown",
                component=alert.get("component") or "unknown",
                severity=alert.get("severity") or "P3",
                title=alert.get("title") or "",
                detail=alert.get("detail") or "",
            )
        )
    return "\n".join(lines) or "-（无告警记录）"


async def run(state: dict) -> dict:
    cause = (state.get("actual_cause") or state.get("root_cause") or "").strip()
    confidence = state.get("root_cause_confidence") or 0.0
    source = state.get("root_cause_source")

    # 根因段：人工修正（actual_cause）优先于 AI 结论（root_cause）
    if cause:
        root_line = f"{cause}（source: {source}, confidence: {confidence}）" if source else cause
    else:
        root_line = "待人工补充"

    # 处置段：correct_actions 人工修正整体优先；否则只收录非 skipped 的命令
    # （skipped 是被 SRE 否决的命令，回灌它们等于沉淀错误经验）。
    corrected = (state.get("correct_actions") or "").strip()
    if corrected:
        remediation = corrected
    else:
        remediation = "\n".join(
            "- {}（{}）{}".format(a.get("command", ""), a.get("risk_level", ""), a.get("description") or "")
            for a in (state.get("actions") or []) if a.get("status") != "skipped"
        ) or "待补充"

    # 备注段：人工修正说明（自动收口路径无人工参与，为"（无）"）
    notes = []
    if state.get("actual_cause"):
        notes.append(f"人工修正根因：{state['actual_cause']}")
    if corrected:
        notes.append(f"人工修正处置：{corrected}")

    notes_text = "\n".join(notes) or "（无）"

    content = (
        f"故障现象：\n{_phenomenon(state)}\n\n"
        f"根因：\n{root_line}\n\n"
        f"处置：\n{remediation}\n\n"
        f"备注：\n{notes_text}"
    )

    component = state.get("component") or "common"
    case_title = f"{cause}处置方案（{component}）"[:MAX_CASE_TITLE] if cause else ""

    # V1.2：本字段已降级为"证据充分度"启发式，仅供审计留痕，**不参与回灌判定**。
    # 回灌门槛已迁到 orchestrator._maybe_ingest（多信号：人工修正 > 置信度）。
    #
    # 历史（V1.1）：曾以"通用性自评 > 0.7"作门槛，但此处只是 conf 的二值映射，
    # 等价于 conf>=0.6，通用性判断从未真正生效；F-09 接入真 LLM 后也一直没替换。
    # 若将来要恢复真实的通用性自评，应先在此实现模型评分，再回 orchestrator 加门槛。
    generality = 0.8 if confidence >= 0.6 else 0.4

    return {
        "recap": content,
        "generality": generality,
        "generality_kind": "heuristic_evidence_sufficiency",  # 非模型自评，勿当通用性分使用
        "case_title": case_title,
        "ingest_ready": bool(cause),
        "tool_calls": [],
    }
