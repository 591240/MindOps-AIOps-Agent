"""分级自动处置执行器。

策略（与用户确认的口径一致）：
- 总闸 `settings.auto_remediate`（默认 False）——不开就什么都不执行；
- 只有 `risk_level=low` 的动作自动执行；medium/high 永远只生成建议；
- 代码级高危黑名单做**第二道闸**：即使有人把危险命令误标为低风险
  （或未来规则库支持自定义分级），这里也会拦下；
- 执行发生在平台所在机器（私有化同机部署形态），超时/输出截断/审计落库。

本模块只负责「判定 + 跑命令 + 写回 remediation_actions 的回执字段」，
不做 SSE 发布（避免与 orchestrator 循环依赖），由调用方在完成后发布。
"""

from __future__ import annotations

import asyncio
import re
import time

from app.config import model_now, settings
from app.models import RemediationAction
from app.observability.logging import log_event

# 代码级黑名单：与 risk_rules.yaml 无关的第二道闸，永不自动执行。
# 覆盖：递归强删 / 格式化 / 磁盘直写 / 关机重启 / fork 炸弹 / 管道执行远程脚本。
_NEVER_AUTO_PATTERNS = (
    r"rm\s+(-[a-z]*r[a-z]*f|-[a-z]*f[a-z]*r)",
    r"\bmkfs(\.\w+)?\b",
    r"\bdd\s+if=",
    r"\bshutdown\b",
    r"\breboot\b",
    r"\bhalt\b",
    r"\binit\s+[06]\b",
    r">\s*/dev/sd",
    r"\bchmod\s+-R\s+777\s+/\s*$",
    r"\bkill\s+-9\s+1\b",
    r":\(\)\s*\{.*\};\s*:",
    r"\b(curl|wget)\b[^|]*\|\s*(ba|z)?sh\b",
)
_COMPILED_NEVER_AUTO = [re.compile(p, re.IGNORECASE) for p in _NEVER_AUTO_PATTERNS]

_OUTPUT_LIMIT = 4000


def auto_eligible(command: str, risk_level: str) -> tuple[bool, str]:
    """判定一条命令是否允许自动执行。返回 (是否允许, 不允许的原因)。

    risk_level 是 Remediate 定稿的等级（管理员手动指定优先，未指定走风险库）。
    唯一的硬闸是代码级黑名单——管理员把命令标成 low 即代表为该命令担责。
    """
    if risk_level != "low":
        return False, f"风险等级为 {risk_level}（仅 low 允许自动执行）"
    for pattern in _COMPILED_NEVER_AUTO:
        if pattern.search(command):
            return False, "命中代码级高危黑名单"
    return True, ""


async def run_command(command: str, timeout_s: int) -> dict:
    """在平台所在机器执行单条命令，返回 {exit_code, output, duration_ms}。

    超时/进程杀不掉/生成失败都不抛异常——回执里带错误说明，
    由调用方按 exec_failed 落库，不影响诊断主流程。
    """
    start = time.monotonic()
    try:
        proc = await asyncio.create_subprocess_shell(
            command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.STDOUT,
        )
    except Exception as exc:
        return {"exit_code": -1, "output": f"[执行器错误] 无法创建进程：{exc}", "duration_ms": 0}
    try:
        raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout_s)
        exit_code = proc.returncode if proc.returncode is not None else -1
    except asyncio.TimeoutError:
        proc.kill()
        # Windows 坑：shell 的孙进程可能仍握着管道，kill 后 communicate() 会
        # 一直阻塞到孙进程退出。给 1s 收割窗口，收不到就放弃（回执标记超时）。
        raw = b""
        try:
            raw, _ = await asyncio.wait_for(proc.communicate(), timeout=1.0)
        except Exception:
            pass
        exit_code = -1
        raw = (raw or b"") + f"\n[超过 {timeout_s}s 被强制终止]".encode("utf-8")
    duration_ms = int((time.monotonic() - start) * 1000)
    # 子进程输出的编码随宿主环境变：Linux 容器是 UTF-8，中文 Windows 的 cmd
    # 是 GBK（即使 Python 跑在 UTF-8 模式）。先按 UTF-8 严格解，失败回退 GBK。
    text = None
    for enc in ("utf-8", "gbk"):
        try:
            text = (raw or b"").decode(enc)
            break
        except UnicodeDecodeError:
            continue
    if text is None:
        text = (raw or b"").decode("utf-8", "replace")
    if len(text) > _OUTPUT_LIMIT:
        text = text[:_OUTPUT_LIMIT] + "\n…[输出已截断]"
    return {"exit_code": exit_code, "output": text, "duration_ms": duration_ms}


async def auto_execute_for_incident(incident_id: int) -> int:
    """对事件的 low + approved 动作执行自动处置，回执写回原行。

    返回实际执行的条数（skipped 不计入）。总闸关闭时直接返回 0。
    """
    if not settings.auto_remediate:
        return 0
    rows = await RemediationAction.filter(
        incident_id=incident_id, status="approved", risk_level="low", exec_status__isnull=True
    )
    executed = 0
    for row in rows[: settings.auto_remediate_max_per_incident]:
        eligible, reason = auto_eligible(row.command, row.risk_level)
        if not eligible:
            log_event(20, "auto_remediate.skipped", incident_id=incident_id, action_id=row.id, reason=reason)
            await RemediationAction.filter(id=row.id).update(
                exec_status="skipped", exec_output=f"不满足自动执行条件：{reason}", executed_at=model_now(),
            )
            continue
        result = await run_command(row.command, settings.auto_remediate_timeout_s)
        status = "auto_executed" if result["exit_code"] == 0 else "exec_failed"
        await RemediationAction.filter(id=row.id).update(
            exec_status=status,
            exec_exit_code=result["exit_code"],
            exec_output=result["output"],
            exec_duration_ms=result["duration_ms"],
            executed_at=model_now(),
        )
        executed += 1
        log_event(
            20, "auto_remediate.executed", incident_id=incident_id, action_id=row.id,
            command=row.command, exit_code=result["exit_code"], duration_ms=result["duration_ms"],
        )
    return executed
