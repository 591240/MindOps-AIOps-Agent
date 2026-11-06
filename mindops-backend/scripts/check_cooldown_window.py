"""验证码冷却窗口回归检查（邮箱注册 / 登录共用同一套冷却）。

为什么需要单独一个脚本，而不是并进 `e2e_api_smoke.py`：
冒烟脚本是纯 HTTP 的，改不了**运行中服务**的发送间隔，因此只能验证
"立刻重发被拦"，而**一个恒定返回 429 的坏实现同样能通过那条断言**。
本项目 2026-09-12 就因此把一次「锁死 8 小时」的回归误判成「修好了」。

本脚本把间隔临时压到 1 秒，从而能验证第二件事：
    **过了间隔之后，必须放行。**
这一条才是区分「冷却窗口正常」与「永久锁死」的唯一手段。

通道会被**强制切到 mock**（改的是本进程内的 settings，不影响运行中的服务）：
本脚本测的是时钟逻辑，不是投递；真发会造成邮件退信（见 check_channel 注释）。

用法（后端**不需要**在跑，脚本直连数据库，且会临时改写进程内的 settings）：
    PYTHONPATH=. .venv/Scripts/python.exe scripts/check_cooldown_window.py

退出码：全部通过 0，任一断言失败 1。
"""

from __future__ import annotations

import asyncio
import sys

sys.path.insert(0, ".")

from app.auth.email import (  # noqa: E402
    EmailSendError,
    _model_now,
    send_code,
    verify_code,
)
from app.config import settings  # noqa: E402
from app.models import EmailCode  # noqa: E402
import app.db as db  # noqa: E402

failures: list[str] = []


def check(tag: str, ok: bool, detail: str = "") -> None:
    print(f"[{'PASS' if ok else 'FAIL'}] {tag}" + (f"  -> {detail}" if detail else ""))
    if not ok:
        failures.append(tag)


async def check_channel() -> None:
    print("--- email 冷却窗口 ---")
    contact = "cooldown@qq.com"
    # 本脚本只验证「冷却窗口 + 有效期基准」这条**时钟逻辑**，与"消息怎么送出去"无关。
    # 强制 mock 的另一层原因：`cooldown@qq.com` 是虚构地址，真发会退信砸回管理员邮箱。
    # 真实投递由 `e2e_api_smoke.py`（只发白名单地址）与手工验证覆盖。
    settings.email_provider = "mock"
    # 间隔压到 1 秒是刻意的：只有窗口足够短，才能在几秒内验证"过期后必须放行"。
    # 注意别把它调大——真实 SMTP 单次投递要 1.2s 左右，窗口若小于投递耗时，
    # "间隔内重发"那条断言会因为窗口自然过期而假性失败（2026-09-12 踩过）。
    # 现在强制 mock 后投递瞬时完成，1 秒窗口稳定成立。
    settings.email_send_interval = 1

    await EmailCode.filter(email=contact).delete()

    try:
        await send_code(contact)
        check("email 首次发送", True)
    except (ValueError, EmailSendError) as exc:
        check("email 首次发送", False, f"{type(exc).__name__}: {exc}")
        return

    try:
        await send_code(contact)
        check("email 间隔内重发应被拦", False, "竟然放行了")
    except ValueError:
        check("email 间隔内重发应被拦", True, "429 冷却生效")

    # 关键断言：过了间隔必须放行。缺了这条，坏实现照样全绿。
    await asyncio.sleep(1.4)
    code = None
    try:
        result = await send_code(contact)
        code = result["code"]
        check("email 过间隔后应放行", True)
    except ValueError as exc:
        check("email 过间隔后应放行", False,
              f"仍被拦住 → 冷却窗口写错了（{exc}）；常见原因是用了 naive UTC 当 cutoff")

    row = await EmailCode.filter(email=contact).order_by("-created_at").first()
    if row is None:
        check("email 有效期剩余", False, "查不到刚写入的记录")
    else:
        remaining = (row.expires_at - _model_now()).total_seconds()
        check("email 有效期剩余应为正数", remaining > 0,
              f"剩 {remaining:.0f}s（按 UTC 写入会变成负数，即一生成就过期）")

    if code is not None:
        check("email 验证码应校验通过", await verify_code(contact, code))

    await EmailCode.filter(email=contact).delete()


async def main() -> int:
    await db.init_db()
    try:
        await check_channel()
    finally:
        await db.close_db()

    print()
    if failures:
        print(f"失败 {len(failures)} 项：{'；'.join(failures)}")
        return 1
    print("全部通过")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
