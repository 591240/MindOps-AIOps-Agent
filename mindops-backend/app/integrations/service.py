"""F-11 集成管理业务逻辑（状态机落库）。

状态机规则（§5.3）：
- pending → active / error（仅探测驱动）
- active ⇄ disabled（人工启停）
- error → active（重新探测通过）
- PUT 不可手工设置 pending / error
- disabled 目标不可探测（409）
- 改 base_url → 回 pending + 清空心跳/目标数
"""

from __future__ import annotations

from app.config import model_now
from app.models import Integration
from app.integrations.probes import ProbeResult


def apply_probe_result(integration: Integration, result: ProbeResult) -> dict:
    """根据探测结果构造待更新字段（§5.2 写库规则）。

    返回 dict 供路由层 .update(**fields) 使用。
    """
    if result.ok:
        return {
            "status": "active",
            "last_heartbeat": model_now(),
            "last_error": None,
            "last_latency_ms": result.latency_ms,
            "targets_total": result.targets_total,
            "targets_up": result.targets_up,
        }
    else:
        # 失败：status=error，last_heartbeat 不动（FR-6），只写 last_error
        return {
            "status": "error",
            "last_error": result.error[:255] if result.error else None,
        }


def validate_status_transition(current: str, new: str) -> str | None:
    """校验 PUT 的状态变更合法性。返回 None 表示合法，否则返回错误消息。

    规则（§5.3）：
    - PUT 只接受 active / disabled（pending / error 由探测驱动，不可手工置入）
    - **任意状态 → disabled 一律允许**：停用是安全方向的动作，坏目标（error）
      与未探测目标（pending）都必须能被停用，否则"关不掉一个连不上的目标"
    - active → active 幂等允许；disabled → active 为重新启用
    - pending / error → active 拒绝：未经探测直接标记"已连通"就是状态撒谎
    """
    if new not in {"active", "disabled"}:
        return f"状态只允许设置为 active 或 disabled，收到: {new}"
    if new == "disabled":
        return None
    # new == "active"
    if current == "pending":
        return "待探测状态不可直接置为已连通，请先执行连通性探测"
    if current == "error":
        return "异常状态不可直接置为已连通，请先执行连通性探测重新验证"
    return None
