"""DiagnoseAgent：真实模型调用优先（F-09），启发式桩兜底。

F-04 接入点：
- 记录本次实际使用的 `model_id` / `role`（`state` 由 confidence_loop 注入，
  第 2 次诊断时为 reasoning 角色，§5.3）；
- 补查召回的知识片段（`kb_chunks`，含跨组件兜底结果）作为根因来源之一，
  这也是"补查 → 置信度提升 → 通过阈值"的数据基础。

F-09 接入点（V1.1 §6.1）：
- `state.model_id` 非空时经 model_router.complete_with_degradation 真实调用
  （同 role 失败自动换备用）；全部失败抛 ModelUnavailableError，由
  orchestrator 转 human_needed（reason=model_unavailable）；
- 系统种子（api_key_enc=NULL）走桩路径（gateway 返回 degraded="stub"），
  落回本文件启发式——F-03 既有行为不回归。
"""

from __future__ import annotations

from app.agents import model_router

_PROMPT_SYSTEM = (
    "你是资深 SRE 诊断专家。基于告警现象与知识库片段，给出故障根因判断。"
    "严格按以下两行格式回答，不要输出其他内容：\n"
    "ROOT_CAUSE: <一句话根因>\n"
    "CONFIDENCE: <0-1 的小数，表示你对'该根因就是真实故障原因'的把握>\n"
    "注意：CONFIDENCE 衡量的是你是否真正定位到了故障原因，而不是你对自己措辞的"
    "确定性。当告警信息不足、组件未知或知识库无匹配片段、你只能说'无法定位/需补充"
    "信息'时，说明尚未定位到根因，CONFIDENCE 必须给 0.3 以下（而非高分）。"
)


def _build_messages(state: dict) -> list[dict]:
    alerts = "\n".join(
        f"- [{item.get('severity')}] {item.get('title')}: {(item.get('detail') or '')[:200]}"
        for item in (state.get("alerts") or [])[:10]
    )
    kb = "\n".join(
        f"- {(chunk.get('quote') or chunk.get('content') or '')[:300]}"
        for chunk in (state.get("kb_chunks") or [])[:5]
    )
    user = (
        f"组件: {state.get('component') or 'unknown'}\n"
        f"事件标题: {state.get('title')}\n"
        f"告警现象:\n{alerts or '无'}\n"
        f"知识库片段:\n{kb or '无'}"
    )
    return [{"role": "system", "content": _PROMPT_SYSTEM}, {"role": "user", "content": user}]


def _parse_llm(content: str) -> tuple[str, float]:
    """解析 ROOT_CAUSE/CONFIDENCE 两行格式；缺 CONFIDENCE 视为 0.5（保守）。"""
    cause, confidence = "", 0.5
    for line in (content or "").splitlines():
        text = line.strip()
        upper = text.upper()
        if upper.startswith("ROOT_CAUSE:"):
            cause = text.split(":", 1)[1].strip()
        elif upper.startswith("CONFIDENCE:"):
            try:
                confidence = float(text.split(":", 1)[1].strip())
            except ValueError:
                confidence = 0.5
    return cause, min(max(confidence, 0.0), 1.0)


async def run(state: dict) -> dict:
    llm = None
    if state.get("model_id") is not None:
        # 真实调用（含降级链）；全败抛 ModelUnavailableError → orchestrator 转人工
        llm = await model_router.complete_with_degradation(
            state.get("model_role", "chat"), _build_messages(state)
        )
    if llm is not None and llm.get("degraded") != "stub":
        cause, confidence = _parse_llm(llm.get("content") or "")
        if cause:
            return {
                "root_cause": cause,
                "root_cause_confidence": confidence,
                "root_cause_source": "llm",
                "missing_info": [] if confidence >= 0.6 else ["模型自评置信不足，建议补充指标趋势"],
                "model_id": llm.get("model_id"),
                "model_degraded": llm.get("degraded"),
                "tool_calls": [{"tool": "llm_completion", "args": {"model": llm.get("actual_model")},
                                "result": "llm", "latency_ms": llm.get("latency_ms", 0)}],
            }
    return _heuristic(state)


def _heuristic(state: dict) -> dict:
    """F-03 既有启发式桩（系统种子/模型输出不合规时兜底，行为不变）。"""
    text = " ".join([state["title"], state.get("brief") or ""]).lower()
    similar_cases = state.get("similar_cases") or []
    kb_chunks = state.get("kb_chunks") or []
    if similar_cases:
        cause = similar_cases[0]["root_cause"]
        confidence, source = 0.75, "rag"
    elif kb_chunks:
        # 跨组件补充证据（§10.1 标注为"候选补充"，置信度低于本组件历史案例）
        cause = kb_chunks[0].get("quote") or kb_chunks[0].get("content") or ""
        confidence, source = 0.62, "rag"
    elif any(word in text for word in ("memory", "内存", "oom")):
        cause, confidence, source = "组件存在内存压力，需要核对容量、热点数据和淘汰策略。", 0.68, "llm"
    elif any(word in text for word in ("connection", "连接", "timeout", "超时")):
        cause, confidence, source = "组件连接或上游依赖出现容量压力，需要检查连接池和错误日志。", 0.65, "llm"
    elif any(word in text for word in ("disk", "磁盘", "space")):
        cause, confidence, source = "存储空间或写入链路异常，需要核对磁盘使用率和日志增长。", 0.66, "llm"
    else:
        cause, confidence, source = "现有告警和上下文不足以可靠定位根因。", 0.45, "llm"
    return {
        "root_cause": cause,
        "root_cause_confidence": confidence,
        "root_cause_source": source,
        "missing_info": [] if confidence >= 0.6 else ["30 分钟指标趋势", "相关组件错误日志"],
        "model_id": state.get("model_id"),
        "tool_calls": [{"tool": "finalize_diagnosis", "args": {"role": state.get("model_role", "chat")}, "result": source, "latency_ms": 0}],
    }
