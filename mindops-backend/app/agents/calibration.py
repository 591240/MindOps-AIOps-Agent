"""F-04 §5.5 置信度校准（Calibration，M4）。

回环的 gate 完全依赖模型自报的 `confidence`。若模型系统性"过度自信/保守"，
门限会误判，因此本模块采集"自报置信度"与"实际正确性"的偏差。

- **判定方式**（V1.1 明确）：`actually_correct` 由 **LLM-as-judge** 判定——固定
  rubric 的 `chat` 角色模型比对 AI `root_cause` 与人工 confirm 时的 `actual_cause`
  语义一致性，输出 0/1；抽样 20% 人工复核判定质量。
- **计算时机**：每日批任务（`daily_calibration_job`），**不在诊断链路内同步执行**。
- **指标**：`calibration_error = |mean(reported) − accuracy|`（PRD 要求 < 10%）。
- **MVP（M3）边界**：只采集、只出指标，**不自动改写 gate**（§5.5 第 4 条为可选项）。

聚合值复用通用表 `runtime_configs`（§4.4），不新增一参一表。
"""

from __future__ import annotations

import asyncio
import random

from app.config import model_now, settings
from app.incidents.tokenizer import extract_entities, normalize_text, tokenize
from app.models import RuntimeConfig
from app.observability import metrics
from app.observability.logging import log_event

KEY_SUM_REPORTED = "calibration.sum_reported"
KEY_COUNT = "calibration.count"
KEY_SUM_CORRECT = "calibration.sum_correct"
KEY_ERROR = "calibration.error"

# 人工抽检比例（§5.5 抽样 20% 人工复核判定质量）
HUMAN_REVIEW_RATE = 0.2

# 启发式判定阈值（仅在无 LLM-as-judge 时降级使用）
HEURISTIC_MATCH_FLOOR = 0.3


def _jaccard(a: str, b: str) -> float:
    left, right = tokenize(a or ""), tokenize(b or "")
    if not left or not right:
        return 1.0 if left == right else 0.0
    return len(left & right) / len(left | right)


async def _read_float(key: str, default: float = 0.0) -> float:
    row = await RuntimeConfig.get_or_none(key=key)
    if row is None:
        return default
    try:
        return float(row.value)
    except ValueError:
        return default


async def _write(key: str, value: float) -> None:
    await RuntimeConfig.update_or_create(
        key=key,
        defaults={"value": str(value), "updated_by": "calibration", "updated_at": model_now()},
    )


async def judge_actual_correct(
    root_cause: str | None,
    actual_cause: str | None,
    judge=None,
) -> tuple[int, str]:
    """判定诊断结论是否正确，返回 `(0|1, method)`。

    `judge` 为 LLM-as-judge 回调（`(root_cause, actual_cause) -> bool`）；
    未注入（F-09 未接模型）时降级为词面重合度启发式，并如实标注 method。
    """
    if not actual_cause or not root_cause:
        return 0, "skipped"
    if judge is not None:
        try:
            return (1 if await judge(root_cause, actual_cause) else 0), "llm"
        except Exception as exc:  # noqa: BLE001 - judge 失败不应阻塞主流程
            log_event(30, "calibration.judge_failed", error=str(exc))
    score = _jaccard(root_cause, actual_cause)
    return (1 if score >= HEURISTIC_MATCH_FLOOR else 0), "heuristic"


async def collect_sample(
    incident_id: int,
    reported_conf: float | None,
    root_cause: str | None,
    actual_cause: str | None,
    judge=None,
) -> dict:
    """人工 confirm 后采集一条校准样本（§5.5 第 1 步）。

    只做聚合累加（sum_reported / count / sum_correct），不存明细，避免表膨胀。
    """
    if reported_conf is None:
        return {"collected": False, "reason": "no reported confidence"}
    correct, method = await judge_actual_correct(root_cause, actual_cause, judge)
    sum_reported = await _read_float(KEY_SUM_REPORTED) + float(reported_conf)
    count = await _read_float(KEY_COUNT) + 1
    sum_correct = await _read_float(KEY_SUM_CORRECT) + correct
    await _write(KEY_SUM_REPORTED, sum_reported)
    await _write(KEY_COUNT, count)
    await _write(KEY_SUM_CORRECT, sum_correct)

    needs_review = random.random() < HUMAN_REVIEW_RATE
    log_event(
        20, "calibration.sample", incident_id=incident_id, reported_conf=reported_conf,
        actually_correct=correct, method=method, human_review_suggested=needs_review,
    )
    return {
        "collected": True, "actually_correct": correct, "method": method,
        "human_review_suggested": needs_review,
    }


async def daily_calibration_job() -> dict:
    """每日批任务：计算并暴露校准误差（§5.5 第 3 步）。

    `calibration_error = |mean(reported) − accuracy|`，目标 < 10%（PRD §指标）。
    """
    count = await _read_float(KEY_COUNT)
    if count <= 0:
        metrics.calibration_error.set(0.0)
        return {"samples": 0, "calibration_error": 0.0, "status": "no_samples"}
    sum_reported = await _read_float(KEY_SUM_REPORTED)
    sum_correct = await _read_float(KEY_SUM_CORRECT)
    mean_reported = sum_reported / count
    accuracy = sum_correct / count
    error = abs(mean_reported - accuracy)
    await _write(KEY_ERROR, error)
    metrics.calibration_error.set(error)
    log_event(
        20, "calibration.daily", samples=int(count), mean_reported=round(mean_reported, 4),
        accuracy=round(accuracy, 4), calibration_error=round(error, 4),
        within_target=error < 0.1,
    )
    return {
        "samples": int(count),
        "mean_reported": round(mean_reported, 4),
        "accuracy": round(accuracy, 4),
        "calibration_error": round(error, 4),
        "within_target": error < 0.1,
    }


_task: asyncio.Task | None = None


async def start_daily_scheduler(interval_seconds: int = 86400) -> None:
    """启动每日批任务调度（§5.5：计算不在诊断链路内同步执行）。"""
    global _task
    if _task is not None:
        return

    async def _loop() -> None:
        while True:
            try:
                await asyncio.sleep(interval_seconds)
                await daily_calibration_job()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001 - 批任务失败不应拖垮服务
                log_event(40, "calibration.daily_failed", error=str(exc))

    _task = asyncio.create_task(_loop())
    log_event(20, "calibration.scheduler_started", interval_seconds=interval_seconds)


async def stop_daily_scheduler() -> None:
    global _task
    if _task is None:
        return
    _task.cancel()
    try:
        await _task
    except (asyncio.CancelledError, Exception):
        pass
    _task = None


async def current_error() -> float:
    """返回最近一次批任务算出的校准误差（未计算则返回 0）。"""
    return await _read_float(KEY_ERROR)


def calibration_target() -> float:
    """PRD 指标要求：校准误差 < 10%。"""
    return 0.1


def _settings_snapshot() -> dict:
    """供测试/运维查询当前校准相关配置。"""
    return {
        "human_review_rate": HUMAN_REVIEW_RATE,
        "heuristic_match_floor": HEURISTIC_MATCH_FLOOR,
        "conf_threshold": settings.conf_threshold,
        "confidence_scoring_enabled": settings.confidence_scoring_enabled,
        "conf_cap_zero_evidence": settings.conf_cap_zero_evidence,
        "conf_cap_single_evidence": settings.conf_cap_single_evidence,
    }


# =============================================================================
# 在线质量收口（P0-2 / P0-3）
#
# 与上面的「离线校准」分工不同，两件事不要混：
# - 离线（`daily_calibration_job`）：人工 confirm 后统计"自报 vs 实际"的偏差，
#   回答"模型的置信度**准不准**"（`calibration_error`）；
# - 在线（本节的 `score_diagnosis`）：诊断结论**落库之前**，用本轮证据与已知
#   失败模式修正自报置信度，回答"这一条结论该不该被采信"。
#
# 背景：OpenRCA 失败模式研究（1675 次 agent 运行）显示最高发的失败是
# "解读幻觉"（71.2%）与"无交叉验证"（18.6%），且源于**共享的 Agent 架构**
# 而非模型能力差异 —— 说明"模型自报置信度"本身就是最不可靠的信号。
# 本节把这三类失败模式变成可检测、可计数、可修正的在线信号。
# =============================================================================

SYMPTOM_AS_CAUSE = "symptom_as_cause"
NO_CROSS_VALIDATION = "no_cross_validation"
INTERPRETATION_GAP = "interpretation_gap"

# 失败信号扣分（命中即从自报值中扣，绝对值）
FAILURE_SIGNAL_PENALTY: dict[str, float] = {
    SYMPTOM_AS_CAUSE: 0.15,
    NO_CROSS_VALIDATION: 0.05,
    INTERPRETATION_GAP: 0.15,
}

# interpretation_gap：结论实体在证据中找不到对应时的判定门槛。
# 门槛取 0.7（而非 0.5）是刻意的：结论说 "OOM"、证据写 "used_memory 接近
# maxmemory" 属于同义改写，不是解读脱节；只有**大多数**实体都对不上才值得报。
INTERPRETATION_GAP_MIN_ENTITIES = 2
INTERPRETATION_GAP_RATIO = 0.7

# 通用元词不参与"解读脱节"判定：它们出现在结论里不代表引用了具体事实。
ENTITY_STOPWORDS = frozenset(
    {
        "error", "errors", "issue", "issues", "problem", "problems",
        "high", "low", "usage", "value", "level", "count", "total", "rate",
        "service", "server", "system", "status", "data", "time", "result",
    }
)


def _item_text(item: dict) -> str:
    """证据条目取正文：兼容相似案例与知识切片两种结构。"""
    for key in ("quote", "content", "root_cause", "cause", "title", "text"):
        value = item.get(key)
        if value:
            return str(value)
    return ""


def _evidence_bundle(
    similar_cases: list[dict] | None, kb_chunks: list[dict] | None
) -> tuple[int, set[str], list[str]]:
    """汇总本轮模型实际能看到的证据：条数 / 涉及组件 / 正文列表。

    只看 `state` 里已召回的内容（而非回查 evidence 表），因为这里要回答的是
    "模型当时手里有什么"，而不是"数据库里存了什么"。
    """
    count = 0
    components: set[str] = set()
    texts: list[str] = []
    for item in [*(similar_cases or []), *(kb_chunks or [])]:
        if not isinstance(item, dict):
            continue
        count += 1
        component = normalize_text(item.get("component"))
        if component:
            components.add(component)
        text = _item_text(item).strip()
        if text:
            texts.append(text)
    return count, components, texts


def detect_failure_signals(
    *,
    component: str | None,
    root_cause: str | None,
    evidence_count: int,
    evidence_components: set[str],
    evidence_texts: list[str],
    self_report: float,
    threshold: float,
    rule_hit: bool = False,
) -> list[str]:
    """检测失败模式信号；命中表示"可疑"，**不是**对结论对错的终判。

    | 信号 | 判据 | 对治的失败模式 |
    |---|---|---|
    | `symptom_as_cause` | 结论点名了告警自身组件，且本轮证据里没有出现任何其它组件 | Symptom-as-Cause（39.9%）|
    | `no_cross_validation` | 自评已达阈值，但本轮证据不足 2 条 | No Cross-Validation（18.6%）|
    | `interpretation_gap` | 结论中的技术实体半数以上在证据文本里找不到 | Hallucination in Interpretation（71.2%）|

    结论对错由人工 confirm / LLM-as-judge（`collect_sample`）判定，本函数只
    负责把"架构性高风险模式"暴露成可计数指标。
    """
    signals: list[str] = []
    cause = root_cause or ""
    cause_norm = normalize_text(cause)
    component_norm = normalize_text(component)

    # ① 把现象当根因：结论只指向告警组件本身，且证据里没有别组件的踪影。
    # 要求 `evidence_components` 非空 —— 若证据压根没带组件信息，属于"信息不足"，
    # 不应据此判定"没做上溯"，否则会把噪声算成失败模式。
    if (
        evidence_count > 0
        and component_norm
        and component_norm in cause_norm
        and evidence_components
        and not (evidence_components - {component_norm})
    ):
        signals.append(SYMPTOM_AS_CAUSE)

    # ② 无交叉验证：自评已达阈值，但证据撑不住（规则命中的出处是规则条目本身，豁免）
    if not rule_hit and self_report >= threshold and evidence_count <= 1:
        signals.append(NO_CROSS_VALIDATION)

    # ③ 解读与证据脱节：结论里的技术实体在证据中找不到对应
    entities = extract_entities(cause) - ENTITY_STOPWORDS
    if len(entities) >= INTERPRETATION_GAP_MIN_ENTITIES:
        evidence_norm = normalize_text(" ".join(evidence_texts))
        missing = {item for item in entities if item not in evidence_norm}
        if len(missing) / len(entities) >= INTERPRETATION_GAP_RATIO:
            signals.append(INTERPRETATION_GAP)

    return signals


def score_diagnosis(
    *,
    component: str | None,
    root_cause: str | None,
    self_report: float | None,
    source: str | None = None,
    rule_hit: bool = False,
    similar_cases: list[dict] | None = None,
    kb_chunks: list[dict] | None = None,
    threshold: float | None = None,
) -> dict:
    """证据加权置信度（P0-3）：自报值为主，证据不足与失败模式负责压制。

    **为什么不直接做线性重加权**：若把自报值降为一个低权重项，再与"规则命中 /
    证据条数 / 一致性"线性组合，整体分布会系统性下移 —— 原本能通过阈值的健康
    诊断会大批转回环甚至转人工，那是拿"校准"换"误伤"。本实现只压制三类病态
    路径，健康路径保持原有行为：

    1. 零证据 → 自报上限 `conf_cap_zero_evidence`（默认 0.35，必然低于阈值）
    2. 仅一条证据 → 自报上限 `conf_cap_single_evidence`（默认 0.65，仍可通过）
    3. 命中失败模式信号 → 按 `FAILURE_SIGNAL_PENALTY` 扣分

    返回结构同时保留自报值与加权值，便于审计，也便于后续用评测集反推阈值。
    """
    limit = float(settings.conf_threshold if threshold is None else threshold)
    raw = 0.0 if self_report is None else max(0.0, min(1.0, float(self_report)))
    evidence_count, evidence_components, evidence_texts = _evidence_bundle(
        similar_cases, kb_chunks
    )

    if rule_hit:
        # 规则命中是"确定性优先"路径（F-05 短路）：它的出处是规则库条目本身，
        # 只有 1 条 triage 证据是设计如此，不是"证据不足"。因此不套用
        # 幻觉/无交叉验证这套 LLM 治理，直接采信规则结论。
        return {
            "confidence": round(raw, 4),
            "self_report": round(raw, 4),
            "signals": [],
            "evidence_count": evidence_count,
            "caps_applied": [],
            "adjusted": False,
            "scoring_enabled": settings.confidence_scoring_enabled,
            "source": source,
            "rule_hit": True,
        }

    signals = detect_failure_signals(
        component=component,
        root_cause=root_cause,
        evidence_count=evidence_count,
        evidence_components=evidence_components,
        evidence_texts=evidence_texts,
        self_report=raw,
        threshold=limit,
        rule_hit=rule_hit,
    )

    if not settings.confidence_scoring_enabled:
        # 对照实验开关：只观测信号，不修改置信度
        return {
            "confidence": round(raw, 4),
            "self_report": round(raw, 4),
            "signals": signals,
            "evidence_count": evidence_count,
            "caps_applied": [],
            "adjusted": False,
            "scoring_enabled": False,
            "source": source,
            "rule_hit": bool(rule_hit),
        }

    adjusted = raw
    caps: list[str] = []
    if evidence_count == 0:
        cap = float(settings.conf_cap_zero_evidence)
        if adjusted > cap:
            adjusted = cap
            caps.append("zero_evidence")
    elif evidence_count == 1:
        cap = float(settings.conf_cap_single_evidence)
        if adjusted > cap:
            adjusted = cap
            caps.append("single_evidence")

    for signal in signals:
        adjusted = max(0.0, adjusted - FAILURE_SIGNAL_PENALTY.get(signal, 0.0))

    adjusted = round(adjusted, 4)
    return {
        "confidence": adjusted,
        "self_report": round(raw, 4),
        "signals": signals,
        "evidence_count": evidence_count,
        "caps_applied": caps,
        "adjusted": adjusted != round(raw, 4),
        "scoring_enabled": True,
        "source": source,
        "rule_hit": bool(rule_hit),
    }

