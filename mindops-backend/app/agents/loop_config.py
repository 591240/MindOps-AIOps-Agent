"""F-04 回环参数：env 基线 + `runtime_configs` 覆盖 + per-run 快照（§3.2）。

生效优先级：**DB > env**。每个诊断 run 启动时调用 `snapshot()` 冻结一份参数进
state（per-run 快照），因此进行中的 run 不受后续调参影响——这是 §3.2 与验收用例
"调参隔离"的实现基础。

可调白名单仅 4 个字段（§3.2）；`expand_recall` 参数组 MVP 固定，仅 GET 可读。
"""

from __future__ import annotations

from app.config import model_now, settings
from app.models import RuntimeConfig

# ---- 可写白名单（§3.2）----
WRITABLE_KEYS: tuple[str, ...] = (
    "conf_threshold",
    "max_retry",
    "reasoning_escalation",
    "cross_component_fallback",
)

# ---- expand_recall 参数组：MVP 固定，仅 GET 可读 ----
EXPAND_RECALL_KEYS: tuple[str, ...] = (
    "top_k_base",
    "top_k_loop",
    "sim_floor_base",
    "sim_floor_loop",
)

_BOOL_TRUE = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"0", "false", "no", "off"}


def _coerce(key: str, raw: str):
    """把 runtime_configs 的字符串值还原为强类型；非法值返回 None（视为未覆盖）。"""
    text = raw.strip()
    if key in ("conf_threshold", "sim_floor_base", "sim_floor_loop"):
        try:
            return float(text)
        except ValueError:
            return None
    if key in ("max_retry", "top_k_base", "top_k_loop"):
        try:
            return int(text)
        except ValueError:
            return None
    lowered = text.lower()
    if lowered in _BOOL_TRUE:
        return True
    if lowered in _BOOL_FALSE:
        return False
    return None


def _env_baseline() -> dict:
    """env 基线（MINDOPS_ 前缀，对齐 F-01 config.py 约定）。"""
    return {
        "conf_threshold": settings.conf_threshold,
        "max_retry": settings.max_retry,
        "reasoning_escalation": settings.reasoning_escalation,
        "cross_component_fallback": settings.cross_component_fallback,
    }


def expand_recall() -> dict:
    """MVP 固定的召回参数组（§3.1 GET 响应中的 expand_recall）。"""
    return {
        "top_k_base": settings.loop_top_k_base,
        "top_k_loop": settings.loop_top_k_loop,
        "sim_floor_base": settings.loop_sim_floor_base,
        "sim_floor_loop": settings.loop_sim_floor_loop,
    }


async def effective_config() -> dict:
    """返回**生效值**：DB 覆盖优先于 env 基线（§3.2）。"""
    values = _env_baseline()
    rows = await RuntimeConfig.filter(key__in=WRITABLE_KEYS)
    for row in rows:
        parsed = _coerce(row.key, row.value)
        if parsed is not None:
            values[row.key] = parsed
    return values


async def snapshot() -> dict:
    """run 启动时的 per-run 快照：阈值四参数 + 固定召回参数组。

    冻结进 state 后，本次 run 全程只读该快照，进行中的调参不影响它。
    """
    values = await effective_config()
    values.update(expand_recall())
    return values


def validate_patch(patch: dict) -> dict:
    """白名单过滤 + 边界校验（§10.1 阈值误配缓解）。

    - `conf_threshold ∈ (0,1)`
    - `max_retry ∈ [0,5]`（0 表示关闭回环，§3.2 特例）
    越界抛 ValueError，由路由层转成 422。
    """
    cleaned: dict = {}
    for key in WRITABLE_KEYS:
        if key not in patch or patch[key] is None:
            continue
        value = patch[key]
        if key == "conf_threshold":
            value = float(value)
            if not 0.0 < value < 1.0:
                raise ValueError("conf_threshold must be in (0,1); max_retry must be in [0,5]")
        elif key == "max_retry":
            value = int(value)
            if not 0 <= value <= 5:
                raise ValueError("conf_threshold must be in (0,1); max_retry must be in [0,5]")
        else:
            value = bool(value)
        cleaned[key] = value
    return cleaned


async def apply_patch(patch: dict, updated_by: str) -> dict:
    """写入 `runtime_configs` 并返回写入后的生效值。"""
    cleaned = validate_patch(patch)
    for key, value in cleaned.items():
        await RuntimeConfig.update_or_create(
            key=key,
            defaults={
                "value": "true" if value is True else "false" if value is False else str(value),
                "updated_by": updated_by,
                "updated_at": model_now(),
            },
        )
    return await effective_config()
