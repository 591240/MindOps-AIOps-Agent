"""Prometheus 指标（文档 §7.4，V1.1 新增）。"""

from prometheus_client import Counter, Gauge, Histogram

alerts_received = Counter(
    "mindops_alerts_received_total", "接收告警数", ["source", "deduped"]
)
alerts_rejected = Counter(
    "mindops_alerts_rejected_total", "被拒告警数", ["reason"]
)
ingest_latency = Histogram(
    "mindops_alert_ingest_latency_seconds",
    "入库延迟",
    buckets=[0.005, 0.01, 0.02, 0.04, 0.08, 0.16, 0.5],
)
dedup_degraded_total = Counter(
    "mindops_alerts_dedup_degraded_total", "去重降级期间落库数", []
)
cluster_decisions_total = Counter(
    "mindops_cluster_decisions_total", "聚类决策数", ["decision"]
)
cluster_latency = Histogram(
    "mindops_cluster_latency_seconds", "聚类延迟", buckets=[0.005, 0.01, 0.02, 0.05, 0.1, 0.5, 1, 5]
)
incidents_open = Gauge("mindops_incidents_open", "开放事件数")

knowledge_search_latency = Histogram("mindops_knowledge_search_seconds", "知识检索耗时", buckets=(.1,.3,.5,1,1.5,3))
knowledge_search_hits = Histogram("mindops_knowledge_search_hits", "知识检索命中数", buckets=(0,1,3,5,10,20))
knowledge_degraded_total = Counter("mindops_knowledge_degraded_total", "知识检索降级次数", ["reason"])
knowledge_ingest_total = Counter("mindops_knowledge_ingest_total", "知识入库次数", ["source_type", "result"])
knowledge_chunks_total = Gauge("mindops_knowledge_chunks_total", "最新知识切片总量")
pattern_rule_hit_total = Counter("mindops_pattern_rule_hit_total", "规则命中次数", ["rule_name"])

# 开放状态集合（与 cluster.OPEN_STATES 保持一致；此处内联以避免循环导入）。
_OPEN_STATES = ("new", "diagnosing", "human_needed")


async def recompute_open_incidents() -> int:
    """重新统计 OPEN 状态事件数并刷新 gauge。

    事件在 resolved/closed 后会脱离 OPEN_STATES，但 incidents_open 原先只在
    cluster_alert 末 set 一次，不随状态收口重算，导致 gauge 单调偏高、误报积压。
    任何把事件推到 resolved/closed 的写操作之后都应调用本函数，确保指标真实。
    """
    from app.models import Incident

    count = await Incident.filter(status__in=_OPEN_STATES).count()
    incidents_open.set(count)
    return count

# ---- F-04 置信度回环（§2.3 NFR / §7.5）----
loop_total = Counter("mindops_loop_total", "回环补查次数", [])
loop_escalation_total = Counter(
    "mindops_loop_escalation_total", "reasoning 模型升级次数", []
)
loop_to_human_total = Counter(
    "mindops_loop_to_human_total", "回环耗尽转人工次数", []
)
loop_conf_delta = Histogram(
    "mindops_loop_conf_delta",
    "回环前后置信度提升",
    buckets=[-0.5, -0.2, -0.1, 0.0, 0.05, 0.1, 0.2, 0.3, 0.5],
)
calibration_error = Gauge(
    "mindops_calibration_error", "置信度校准误差（|mean(reported) − accuracy|）"
)

# ---- P0-2 / P0-3 诊断质量收口（失败模式埋点 + 证据加权置信度）----
# 失败模式命名与 OpenRCA 失败模式研究对齐，便于把学术分类变成可观测指标。
diagnosis_failure_signal_total = Counter(
    "mindops_diagnosis_failure_signal_total",
    "诊断失败模式信号命中次数",
    ["signal"],
)
confidence_adjustment = Histogram(
    "mindops_confidence_adjustment",
    "证据加权对自报置信度的调整量（加权后 − 自报值，负数=被压制）",
    buckets=[-1.0, -0.6, -0.4, -0.3, -0.2, -0.15, -0.1, -0.05, 0.0, 0.05, 0.1, 0.2],
)
confidence_suppressed_total = Counter(
    "mindops_confidence_suppressed_total",
    "因证据不足或失败信号被压制置信度的次数",
    ["reason"],
)

# ---- F-06 处置建议与溯源（§7.5）----
# source_type 归一化：避免把任意文档名写进 label 造成基数爆炸
evidence_total = Counter("mindops_evidence_total", "证据落库总数", ["source_type"])
actions_generated = Counter(
    "mindops_actions_generated_total", "处置建议生成数", ["risk_level", "auto_approved"]
)
action_confirm_rate = Gauge("mindops_action_confirm_rate", "已确认 action 占比")
high_risk_ratio = Gauge("mindops_high_risk_ratio", "高危命令占比")
gate_no_evidence_total = Counter(
    "mindops_gate_no_evidence_total", "门禁触发降级次数", ["source"]
)


def evidence_source_type(source_doc: str | None) -> str:
    """把 source_doc 归一为 3 类 label：similar-incident / llm-reasoning / knowledge。"""
    doc = (source_doc or "").strip()
    if doc.startswith("similar-incident:"):
        return "similar-incident"
    if doc == "llm-reasoning":
        return "llm-reasoning"
    if doc == "pattern-rule":
        return "pattern-rule"
    return "knowledge"


async def recompute_action_stats() -> tuple[float, float]:
    """重算确认率与高危占比（gauge 型指标需显式重算，不能只 inc）。

    与 `recompute_open_incidents` 同构：任何 action 生成/确认之后调用，
    保证 gauge 反映当前真实分布而不是单调累加。
    """
    from app.models import RemediationAction

    rows = await RemediationAction.all().values_list("risk_level", "status")
    if not rows:
        action_confirm_rate.set(0.0)
        high_risk_ratio.set(0.0)
        return 0.0, 0.0
    total = len(rows)
    confirmed = sum(1 for _, status in rows if status in ("approved", "skipped"))
    high = sum(1 for risk, _ in rows if risk == "high")
    rate, ratio = confirmed / total, high / total
    action_confirm_rate.set(rate)
    high_risk_ratio.set(ratio)
    return rate, ratio
