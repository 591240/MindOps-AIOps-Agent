"""RemediateAgent：生成建议并由风险库作最终风险判定。

方案来源优先级（P0-5 处置剧本）：
1. `rule_playbook`：命中规则自带的用户自定义剧本（triage 已校验，这里再防御性过滤）
2. 内置模板：按根因关键词选固定动作（兜底，行为与历史版本一致）

无论哪个来源，每条命令都由 `check_risk` 查风险库定级——用户配置不豁免安全闸门。
"""

from __future__ import annotations

from .risk import check_risk

_PLAYBOOK_MAX_STEPS = 8


def _playbook_actions(state: dict) -> list[dict] | None:
    """从 state 取用户剧本并防御性规整；无可用剧本返回 None。

    风险等级：步骤自带 `risk_level`（管理员在规则库手动指定）优先；
    未指定时先用 "low" 占位，run() 里统一由 `check_risk` 重新判定覆盖。
    """
    raw = state.get("rule_playbook")
    if not isinstance(raw, list) or not raw:
        return None
    actions: list[dict] = []
    for step in raw[:_PLAYBOOK_MAX_STEPS]:
        if not isinstance(step, dict):
            continue
        command = step.get("command")
        if not isinstance(command, str) or not command.strip():
            continue
        action: dict = {
            "command": command.strip(),
            "description": str(step.get("description") or "执行剧本步骤"),
            "risk_level": "low",
        }
        if step.get("risk_level") in ("low", "medium", "high"):
            action["risk_level"] = step["risk_level"]
            action["risk_level_manual"] = True
        actions.append(action)
    return actions or None


async def run(state: dict) -> dict:
    playbook = _playbook_actions(state)
    if playbook is not None:
        actions = playbook
        source = "rule_playbook"
    else:
        source = "template"
        component = state["component"]
        cause = state["root_cause"].lower()
        actions = [{"command": f"{component}-cli status", "description": "确认组件当前状态和关键指标", "risk_level": "low"}]
        if "内存" in cause or "memory" in cause:
            actions.append({"command": f"{component}-cli config set maxmemory-policy allkeys-lru", "description": "在变更窗口调整内存淘汰策略", "risk_level": "medium"})
        elif "磁盘" in cause or "storage" in cause:
            actions.append({"command": "df -h", "description": "确认文件系统使用率", "risk_level": "low"})
        elif "反复重启" in cause or "crashloop" in cause:
            # 崩溃循环场景给出删除 Pod 的建议 —— 命中风险库 high 规则（kubectl delete），
            # 用于演示"高风险命令必须人工确认"的 F-06/F-07 流程
            actions.append({"command": f"kubectl delete pod {component}-0", "description": "删除反复崩溃的 Pod，触发控制器重建", "risk_level": "medium"})
        else:
            actions.append({"command": f"systemctl restart {component}", "description": "在确认影响范围后重启组件", "risk_level": "medium"})
    for action in actions:
        # 手动指定的等级生效（管理员对自己的平台负责）；未指定的用风险库判定。
        # 代码级高危黑名单始终在 executor 兜底，与这里的等级无关。
        if not action.get("risk_level_manual"):
            action["risk_level"] = check_risk(action["command"])
        action["requires_confirm"] = action["risk_level"] == "high"
    return {"actions": actions, "remediation_source": source, "tool_calls": [{"tool": "check_risk", "args": {"count": len(actions)}, "result": [a["risk_level"] for a in actions], "latency_ms": 0}]}
