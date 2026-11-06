"""F-12 任务服务：归属推导查询（30-F-12 §5.3，供 alerts 路由复用）。

三条铁律（写在这里防后人改错）：

1. **归属靠 `alerts.integration_id` 显式携带，不做字符串猜测**——早期设想过用
   `alerts.source` 与 `integrations.name` 模糊匹配，被否：名字一改归属就全丢，同名目标会串。
2. **一个告警可以同时属于多个任务**（N:N 的自然结果）。因此「各任务告警数之和」
   会**大于**「全局告警总数」——这不是 bug，是视图归属的固有性质。
3. **未归属告警必须显式可见**（`task_id=none`）。绝不允许"没归属就不显示"，
   否则等于静默丢告警。
"""

from __future__ import annotations

from app.models import Task, TaskTarget


async def integration_ids_for_task(task_id: str) -> list[int]:
    """任务绑定的目标 ID 列表（供 `task_id=<id>` 筛选）。"""
    rows = await TaskTarget.filter(task_id=task_id).values_list("integration_id", flat=True)
    return list(rows)


async def active_task_names_by_integration() -> dict[int, list[str]]:
    """`integration_id` → 归属的 **active** 任务名列表（供看板打「归属任务」标签）。

    只取 active：paused/archived 任务不再是可信归属，但其告警仍留在全局看板。
    """
    links = await TaskTarget.all().values("task_id", "integration_id")
    if not links:
        return {}
    active = {t.id: t.name for t in await Task.filter(status="active").only("id", "name")}
    out: dict[int, list[str]] = {}
    for link in links:
        name = active.get(link["task_id"])
        if name:
            out.setdefault(link["integration_id"], []).append(name)
    return out


async def active_integration_ids() -> set[int]:
    """被至少一个 active 任务绑定的目标 ID 集合（用于 `task_id=none` 判定）。"""
    links = await TaskTarget.all().values("task_id", "integration_id")
    if not links:
        return set()
    active_ids = {t.id for t in await Task.filter(status="active").only("id")}
    return {lk["integration_id"] for lk in links if lk["task_id"] in active_ids}
