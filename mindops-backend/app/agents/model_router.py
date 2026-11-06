"""模型角色解析与降级链（F-04 §6.2/§6.3 + F-09 §5.4）。

边界（§1.3）：本模块**只按 role 取模型**，不感知厂商、不持有密钥；
真实调用统一走 app.llm.gateway。

熔断（§6.2）：10 分钟窗口内同一 role 失败 ≥3 次 → 该 role 置为不可用，
回环不阻塞，退回 `chat` 完成补查，并在审计里记 `escalated=false`（§6.3）。

F-09 降级链（§5.4）：complete_with_degradation 按 role 取 active 模型
（is_default 优先）依次调用，失败（除 vault_locked）记熔断，最多再试
2 个备用；全部失败抛 ModelUnavailableError，由调用方转 human_needed。
"""

from __future__ import annotations

import time
from collections import deque

from app.llm import gateway
from app.llm.gateway import LlmCallError
from app.models import LlmProvider
from app.observability.logging import log_event

BREAKER_WINDOW_SECONDS = 600
BREAKER_FAILURE_THRESHOLD = 3

# role -> 失败时间戳队列（滑动窗口）
_failures: dict[str, deque[float]] = {}


class ModelUnavailableError(Exception):
    """同 role 全部 active 模型调用失败（F-09 §5.4：step failed + 事件转人工）。"""


async def resolve_model_id(role: str) -> int | None:
    """按 role 取默认模型 id（is_default 优先，其次最早注册）；无可用返回 None。"""
    if role not in ("chat", "reasoning", "embedding"):
        return None
    provider = (
        await LlmProvider.filter(role=role, status="active")
        .order_by("-is_default", "id")
        .first()
    )
    return provider.id if provider else None


def is_breaker_open(role: str) -> bool:
    """10 分钟窗口内失败 ≥3 次则熔断打开。"""
    window = _failures.get(role)
    if not window:
        return False
    cutoff = time.monotonic() - BREAKER_WINDOW_SECONDS
    while window and window[0] < cutoff:
        window.popleft()
    if not window:
        _failures.pop(role, None)
        return False
    return len(window) >= BREAKER_FAILURE_THRESHOLD


def record_failure(role: str) -> None:
    _failures.setdefault(role, deque()).append(time.monotonic())
    log_event(30, "model.role_failure", role=role, window_failures=len(_failures[role]))


def record_success(role: str) -> None:
    _failures.pop(role, None)


def reset_breakers() -> None:
    """测试用：清空熔断状态。"""
    _failures.clear()


async def pick_model(requested_role: str) -> tuple[int | None, str, bool, str | None]:
    """为一次 Diagnose 挑选模型。

    返回 `(model_id, actual_role, escalated, skip_reason)`：
    - `requested_role == "reasoning"` 但熔断打开 / 无 reasoning 模型 →
      降级回 `chat`，`escalated=False` 并给出 skip_reason（§6.3）。
    """
    if requested_role == "reasoning":
        if is_breaker_open("reasoning"):
            record_note = "reasoning breaker open"
            chat_id = await resolve_model_id("chat")
            return chat_id, "chat", False, record_note
        reasoning_id = await resolve_model_id("reasoning")
        if reasoning_id is None:
            chat_id = await resolve_model_id("chat")
            return chat_id, "chat", False, "no reasoning model registered"
        return reasoning_id, "reasoning", True, None
    return await resolve_model_id("chat"), "chat", False, None


async def complete_with_degradation(
    requested_role: str,
    messages: list[dict],
    *,
    max_fallbacks: int = 2,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict:
    """F-09 §5.4 降级链：role 内 active 模型（is_default 优先）依次尝试。

    - 成功 → 记录实际生效 model_id 到结果；非首选模型时 degraded=True；
    - 失败 → 按 reason 记 role 级熔断（vault_locked 是配置错误，不计数）；
    - 同 role 最多再试 max_fallbacks 个备用，全败抛 ModelUnavailableError，
      由调用方（orchestrator）转 human_needed（reason=model_unavailable）。
    """
    if requested_role not in ("chat", "reasoning"):
        requested_role = "chat"
    candidates = list(
        await LlmProvider.filter(role=requested_role, status="active").order_by("-is_default", "id")
    )
    last_error: LlmCallError | None = None
    for index, provider in enumerate(candidates[: max_fallbacks + 1]):
        try:
            result = await gateway.complete(
                provider.id, messages, temperature=temperature, max_tokens=max_tokens
            )
        except LlmCallError as exc:
            last_error = exc
            if exc.reason != "vault_locked":
                record_failure(requested_role)
            log_event(30, "llm.degrade_next", role=requested_role, model_id=provider.id,
                      reason=exc.reason, tried=index + 1)
            continue
        record_success(requested_role)
        result["model_id"] = provider.id
        # 桩路径保持 degraded="stub"；真实路径按是否启用备用标记布尔 degraded
        if result.get("degraded") != "stub":
            result["degraded"] = index > 0
        return result
    raise ModelUnavailableError(
        f"no available model for role {requested_role}"
        + (f" (last reason: {last_error.reason})" if last_error else "")
    )
