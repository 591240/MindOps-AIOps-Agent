"""诊断图的检查点后端（LangGraph checkpointer，MySQL 实现）。

为什么单独建 aiomysql 池而不复用 Tortoise 的连接：saver 自带一套
`checkpoint*` 表和迁移游标，走的是原生游标而非 ORM。挂在 Tortoise 上，
测试期反复 `init_db()/close_db()` 会把池子一起带走，图就断在半个超步上。

一次 run = 一个 thread（thread_id = run_id）。这带来两个旧实现没有的能力：
- 进程崩溃后能从**最后一个完成的节点**继续，而不是整图重跑；
- 人工闸口用 `interrupt()` 暂停，确认后再 `Command(resume=...)` 跑完剩余节点。
"""

from __future__ import annotations

import aiomysql
from langgraph.checkpoint.mysql.aio import AIOMySQLSaver

from app.config import settings

_pool: aiomysql.Pool | None = None
_saver: AIOMySQLSaver | None = None


async def startup() -> None:
    """建池并幂等建表；重复调用无副作用。"""
    global _pool, _saver
    if _saver is not None:
        return
    _pool = await aiomysql.create_pool(
        host=settings.mysql_host,
        port=settings.mysql_port,
        user=settings.mysql_user,
        password=settings.mysql_password,
        db=settings.mysql_db,
        autocommit=True,
        charset="utf8mb4",
        minsize=1,
        maxsize=4,
    )
    _saver = AIOMySQLSaver(_pool)
    await _saver.setup()


def get_saver() -> AIOMySQLSaver:
    """图编译时取用；未初始化直接失败，绝不静默退化成"无检查点"。

    退化成 MemorySaver 会让 run 看起来正常，但重启后无法续跑 —— 那种
    "平时看不出问题"的降级比直接报错更糟。
    """
    if _saver is None:
        raise RuntimeError("checkpointer 未初始化：需先 await app.agents.checkpoint.startup()")
    return _saver


def thread_config(run_id: str) -> dict:
    """run 与 thread 一一对应；resume 时必须用同一个 thread_id。"""
    return {"configurable": {"thread_id": run_id}}


async def shutdown() -> None:
    global _pool, _saver
    if _pool is not None:
        _pool.close()
        await _pool.wait_closed()
    _pool = None
    _saver = None
