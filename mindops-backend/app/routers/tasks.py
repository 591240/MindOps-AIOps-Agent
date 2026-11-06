"""F-12 监控任务接口（30-F-12 §3 接口契约）。

七个端点：
  GET    /tasks                          — 列表（admin + engineer 可读）
  POST   /tasks                          — 新建（admin-only）
  GET    /tasks/{id}                     — 详情（含绑定项目摘要）
  PUT    /tasks/{id}                     — 编辑 / 状态流转（admin-only）
  DELETE /tasks/{id}                     — 软删除 = 归档（admin-only）
  POST   /tasks/{id}/targets             — 绑定项目（admin-only）
  DELETE /tasks/{id}/targets/{iid}       — 解绑项目（admin-only）

设计立场（30-F-12 §5.5）：
- 任务只做**视图归属**，不采集、不产生告警、不参与诊断流程；
- 告警归属靠 alerts.integration_id 显式携带，不做字符串猜测；
- paused **不改变告警采集**——告警照旧进全局看板，只是不再归属本任务；
- 未归属告警必须显式可见（?task_id=none），绝不允许"没归属就不显示"。
"""

from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Depends, HTTPException, Query
from tortoise.exceptions import IntegrityError
from tortoise.transactions import in_transaction
from tortoise.functions import Count

from app.config import model_now
from app.alerts import context
from app.auth.dependencies import get_current_user, require_admin
from app.models import Alert, Integration, Task, TaskTarget, User
from app.observability.logging import log_event
from app.schemas import TargetBindIn, TaskCreate, TaskUpdate

router = APIRouter(prefix="/tasks", tags=["tasks"])

TASK_STATUSES = {"active", "paused", "archived"}
TERMINAL_STATUS = "archived"
ARCHIVE_REASON = "保留历史任务与其告警/诊断记录用于审计"


def _err(code: str, http: int, message: str) -> HTTPException:
    """与 F-01 / F-11 同款错误体：{"detail": {"code", "message"}}。"""
    return HTTPException(status_code=http, detail={"code": code, "message": message})


async def _rate_limit(key: str, capacity: int) -> None:
    """复用 F-01 RateLimiter（token bucket）；rate 单位为 token/秒，故 capacity/60。"""
    if not await context.limiter.acquire(key, capacity=capacity, rate=capacity / 60.0):
        raise _err("RATE_LIMITED", 429, "操作过于频繁，请稍后再试")


def _serialize(row: Task, alerts_24h: int = 0) -> dict:
    return {
        "id": row.id,
        "name": row.name,
        "description": row.description,
        "status": row.status,
        "target_count": row.target_count,
        "alerts_24h": alerts_24h,
        "created_by": row.created_by,
        "created_at": row.created_at.isoformat() if row.created_at else None,
        "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        "archived_at": row.archived_at.isoformat() if row.archived_at else None,
    }


async def _alerts_24h_by_integration() -> dict[int, int]:
    """近 24h 告警数，按 integration_id 聚合（供列表页批量算 alerts_24h）。"""
    since = model_now() - timedelta(hours=24)
    rows = await (
        Alert.filter(created_at__gte=since, integration_id__not_isnull=True)
        .group_by("integration_id")
        .annotate(cnt=Count("id"))
        .values("integration_id", "cnt")
    )
    return {r["integration_id"]: r["cnt"] for r in rows}


async def _binds_by_task(task_ids: list[str]) -> dict[str, set[int]]:
    """一次取回这批任务的绑定关系，避免 N+1。"""
    if not task_ids:
        return {}
    rows = await TaskTarget.filter(task_id__in=task_ids).values("task_id", "integration_id")
    out: dict[str, set[int]] = {}
    for r in rows:
        out.setdefault(r["task_id"], set()).add(r["integration_id"])
    return out


async def _next_task_id() -> str:
    """生成 task_%03d 可读 ID（§5.2）。取现有最大序号 +1。"""
    ids = await Task.all().values_list("id", flat=True)
    nums: list[int] = []
    for rid in ids:
        parts = str(rid).split("_")
        if len(parts) == 2 and parts[1].isdigit():
            nums.append(int(parts[1]))
    return f"task_{max(nums, default=0) + 1:03d}"


async def _get_task_or_404(task_id: str) -> Task:
    task = await Task.get_or_none(id=task_id)
    if task is None:
        raise _err("TASK_NOT_FOUND", 404, "任务不存在")
    return task


async def _validate_targets(target_ids: list[int]) -> list[int]:
    """校验待绑定目标：存在性 + 非停用。返回去重后的 ID 列表。"""
    ids = list(dict.fromkeys(target_ids))          # 去重保序
    if not ids:
        return []
    found = {i.id: i for i in await Integration.filter(id__in=ids)}
    missing = [i for i in ids if i not in found]
    if missing:
        raise _err("TARGET_NOT_FOUND", 404, f"目标不存在: {missing}")
    disabled = [found[i].name for i in ids if found[i].status == "disabled"]
    if disabled:
        raise _err("TARGET_DISABLED", 422, f"目标「{disabled[0]}」已停用，无法绑定")
    return ids


# ---------------------------------------------------------------- §3.1 列表
@router.get("")
async def list_tasks(
    status: str | None = Query(None),
    q: str | None = Query(None),
    include_archived: bool = Query(False),
    page: int = Query(1, ge=1),
    size: int = Query(20, ge=1, le=100),
    user: User = Depends(get_current_user),
):
    qs = Task.all()
    if status:
        if status not in TASK_STATUSES:
            raise _err("INVALID_STATUS", 422, "status 仅接受 active / paused / archived")
        qs = qs.filter(status=status)          # 显式筛选优先于 include_archived
    elif not include_archived:
        qs = qs.exclude(status=TERMINAL_STATUS)
    if q:
        qs = qs.filter(name__icontains=q)

    total = await qs.count()
    rows = await qs.order_by("-created_at").offset((page - 1) * size).limit(size)

    binds = await _binds_by_task([r.id for r in rows])
    cnt_map = await _alerts_24h_by_integration()

    items = []
    for r in rows:
        n = sum(cnt_map.get(i, 0) for i in binds.get(r.id, ()))
        items.append(_serialize(r, n))

    return {"total": total, "page": page, "size": size, "items": items}


# ---------------------------------------------------------------- §3.2 新建
@router.post("", status_code=201)
async def create_task(payload: TaskCreate, user: User = Depends(require_admin)):
    await _rate_limit(f"task:user:{user.id}", 20)

    name = (payload.name or "").strip()
    if not name:
        raise _err("EMPTY_NAME", 422, "任务名不能为空")
    if await Task.filter(name=name).exists():
        raise _err("DUPLICATE_TASK_NAME", 409, "已存在同名任务")

    ids = await _validate_targets(payload.target_ids)

    # task_%03d 是"取最大序号 +1"生成的，两个并发创建会取到同一个号；
    # 撞号时靠主键唯一约束兜底，重查后重新取号重试（名冲突则如实报 409）。
    # 审查 #25：建号 + 绑定包进同一事务，中途失败不留"半绑定任务"。
    task = None
    for _ in range(5):
        task_id = await _next_task_id()
        try:
            async with in_transaction() as conn:
                task = await Task.create(
                    id=task_id,
                    name=name,
                    description=payload.description or "",
                    status="active",
                    target_count=len(ids),
                    created_by=user.username,
                    using_db=conn,
                )
                for iid in ids:
                    await TaskTarget.create(
                        task_id=task.id, integration_id=iid,
                        added_by=user.username, using_db=conn,
                    )
            break
        except IntegrityError:
            if await Task.filter(name=name).exists():
                raise _err("DUPLICATE_TASK_NAME", 409, "已存在同名任务")
    if task is None:
        raise _err("CREATE_CONFLICT", 500, "任务创建冲突，请稍后重试")

    log_event(20, "task.created", task_id=task.id, operator=user.username, targets=len(ids))
    return {
        "id": task.id,
        "name": task.name,
        "status": task.status,
        "target_count": task.target_count,
    }


# ---------------------------------------------------------------- §3.3 详情
@router.get("/{task_id}")
async def get_task(task_id: str, user: User = Depends(get_current_user)):
    task = await _get_task_or_404(task_id)

    links = await TaskTarget.filter(task_id=task_id).order_by("added_at")
    ig_ids = [l.integration_id for l in links]
    ints = {i.id: i for i in await Integration.filter(id__in=ig_ids)} if ig_ids else {}

    targets = []
    for l in links:
        ig = ints.get(l.integration_id)
        base = {"integration_id": l.integration_id, "added_at": l.added_at.isoformat() if l.added_at else None}
        if ig is None:
            # 目标被物理删除（不应发生，F-11 是软删除）——保留绑定痕迹，标记缺失
            targets.append({**base, "name": "(目标记录缺失)", "type": None, "base_url": None,
                            "scrape_config": None, "status": None, "targets_total": None,
                            "targets_up": None, "last_heartbeat": None})
            continue
        targets.append({
            **base,
            "name": ig.name,
            "type": ig.type,
            "base_url": ig.base_url,
            "scrape_config": ig.scrape_config,
            "status": ig.status,
            "targets_total": ig.targets_total,
            "targets_up": ig.targets_up,
            "last_heartbeat": ig.last_heartbeat.isoformat() if ig.last_heartbeat else None,
        })

    cnt_map = await _alerts_24h_by_integration()
    alerts_24h = sum(cnt_map.get(i, 0) for i in ig_ids)

    # §4.3：详情不信冗余列，以 task_targets 实时结果为准
    data = _serialize(task, alerts_24h)
    data["target_count"] = len(links)
    data["targets"] = targets
    return data


# ---------------------------------------------------------------- §3.4 编辑 / 状态流转
@router.put("/{task_id}")
async def update_task(
    task_id: str, payload: TaskUpdate, user: User = Depends(require_admin)
):
    await _rate_limit(f"task:user:{user.id}", 20)
    task = await _get_task_or_404(task_id)

    # 状态守卫：archived 是终态（§5.1），任何变更一律拒绝，避免"归档=删除/暂停"语义混用
    if task.status == TERMINAL_STATUS:
        raise _err("ALREADY_ARCHIVED", 409, "已归档的任务不可再变更状态")

    values: dict = {}

    if payload.name is not None:
        nm = payload.name.strip()
        if not nm:
            raise _err("EMPTY_NAME", 422, "任务名不能为空")
        if nm != task.name and await Task.filter(name=nm).exists():
            raise _err("DUPLICATE_TASK_NAME", 409, "已存在同名任务")
        values["name"] = nm

    if payload.description is not None:
        values["description"] = payload.description

    if payload.status is not None:
        if payload.status not in TASK_STATUSES:
            raise _err("INVALID_STATUS", 422, "status 仅接受 active / paused / archived")
        values["status"] = payload.status
        if payload.status == TERMINAL_STATUS:
            values["archived_at"] = model_now()

    if not values:
        return _serialize(task)

    await Task.filter(id=task_id).update(**values)
    refreshed = await Task.get(id=task_id)
    log_event(20, "task.updated", task_id=task_id, operator=user.username, fields=list(values))
    return _serialize(refreshed)


# ---------------------------------------------------------------- §3.5 软删除=归档
@router.delete("/{task_id}")
async def archive_task(task_id: str, user: User = Depends(require_admin)):
    await _rate_limit(f"task:user:{user.id}", 20)
    task = await _get_task_or_404(task_id)

    if task.status != TERMINAL_STATUS:
        await Task.filter(id=task_id).update(
            status=TERMINAL_STATUS, archived_at=model_now()
        )
        log_event(20, "task.archived", task_id=task_id, operator=user.username)

    return {
        "deleted": False,
        "soft": True,
        "status": TERMINAL_STATUS,
        "reason": ARCHIVE_REASON,
    }


# ---------------------------------------------------------------- §3.6 绑定
@router.post("/{task_id}/targets", status_code=201)
async def bind_target(
    task_id: str, payload: TargetBindIn, user: User = Depends(require_admin)
):
    await _rate_limit(f"task:bind:{user.id}", 30)
    task = await _get_task_or_404(task_id)
    if task.status == TERMINAL_STATUS:
        raise _err("TASK_ARCHIVED", 409, "已归档任务不可变更绑定")

    ig = await Integration.get_or_none(id=payload.integration_id)
    if ig is None:
        raise _err("TARGET_NOT_FOUND", 404, "目标不存在")
    if ig.status == "disabled":
        raise _err("TARGET_DISABLED", 422, "该目标已停用，无法绑定")
    if await TaskTarget.filter(task_id=task_id, integration_id=payload.integration_id).exists():
        raise _err("ALREADY_BOUND", 409, "该目标已绑定此任务")

    try:
        await TaskTarget.create(
            task_id=task_id, integration_id=payload.integration_id, added_by=user.username
        )
    except IntegrityError:
        # 审查 #25：exists() 与 create() 之间的并发窗口由唯一键兜底，转 409
        raise _err("ALREADY_BOUND", 409, "该目标已绑定此任务")
    # 同事务语义下校准冗余计数（§4.3）
    count = await TaskTarget.filter(task_id=task_id).count()
    await Task.filter(id=task_id).update(target_count=count)

    log_event(20, "task.target.bound", task_id=task_id, integration_id=payload.integration_id,
              operator=user.username)
    return {"task_id": task_id, "integration_id": payload.integration_id, "target_count": count}


# ---------------------------------------------------------------- §3.6 解绑
@router.delete("/{task_id}/targets/{integration_id}")
async def unbind_target(
    task_id: str, integration_id: int, user: User = Depends(require_admin)
):
    await _rate_limit(f"task:bind:{user.id}", 30)
    task = await _get_task_or_404(task_id)
    if task.status == TERMINAL_STATUS:
        raise _err("TASK_ARCHIVED", 409, "已归档任务不可变更绑定")

    deleted = await TaskTarget.filter(
        task_id=task_id, integration_id=integration_id
    ).delete()
    if not deleted:
        raise _err("BINDING_NOT_FOUND", 404, "绑定关系不存在")

    count = await TaskTarget.filter(task_id=task_id).count()
    await Task.filter(id=task_id).update(target_count=count)

    log_event(20, "task.target.unbound", task_id=task_id, integration_id=integration_id,
              operator=user.username)
    return {"task_id": task_id, "integration_id": integration_id, "target_count": count}
