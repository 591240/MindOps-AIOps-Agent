"""诊断侧 TriageAgent：汇总告警并匹配已知模式。"""

from __future__ import annotations

import hashlib
from pathlib import Path
import re

import yaml

from app.incidents.tokenizer import normalize_text
from app.observability import metrics
from app.observability.logging import log_event

_RULE_FILE = Path(__file__).resolve().parents[2] / "config" / "pattern_rules.yaml"
_rules_cache: list[dict] | None = None
_rules_mtime: float | None = None
_rules_digest: str | None = None

# 用户自定义处置剧本（可选字段 `playbook`）最多允许的步骤数，防配置滥用
PLAYBOOK_MAX_STEPS = 8


def normalize_playbook(rule: dict) -> list[dict]:
    """校验并规整规则的 `playbook` 字段（可选）。

    合法条目 = dict 且 `command` 为非空字符串；`description` 可选；
    `risk_level` 可选（low/medium/high）——管理员手动指定的风险等级，
    未指定时由 Remediate 用风险库自动判定。整段非法时记日志并返回空列表，
    由 Remediate 回退到内置模板，不影响诊断主流程。
    """
    name = str(rule.get("name", "unknown"))
    playbook = rule.get("playbook")
    if playbook is None:
        return []
    if not isinstance(playbook, list):
        log_event(30, "pattern_rules.playbook_skipped", rule=name, reason="not a list")
        return []
    steps: list[dict] = []
    for item in playbook[:PLAYBOOK_MAX_STEPS]:
        if not isinstance(item, dict):
            continue
        command = item.get("command")
        if not isinstance(command, str) or not command.strip():
            continue
        step: dict = {
            "command": command.strip(),
            "description": str(item.get("description") or "执行剧本步骤"),
        }
        risk_level = item.get("risk_level")
        if risk_level in ("low", "medium", "high"):
            step["risk_level"] = risk_level
        steps.append(step)
    if playbook and not steps:
        log_event(30, "pattern_rules.playbook_skipped", rule=name, reason="no valid steps")
    return steps


def merge_rule_keywords(rule: dict) -> list[str]:
    """合并 `keywords` 与 `aliases`，归一化后去空去重（保序）。

    P0-4：`aliases` 是本次新增的可选字段，用来登记同一现象的不同叫法
    （如 `redis_pool` / `连接池耗尽`）。归一化让 `cpu 使用率`、`cpu_usage`、
    `CPU使用率` 落到同一个匹配键上，修掉"关键词含空格导致漏命中"的缺陷。
    """
    merged: list[str] = []
    for field in ("keywords", "aliases"):
        value = rule.get(field)
        if isinstance(value, str):
            value = [value]
        if not isinstance(value, list):
            continue
        merged.extend(item for item in value if isinstance(item, str))
    seen: set[str] = set()
    normalized: list[str] = []
    for item in merged:
        key = normalize_text(item)
        if key and key not in seen:
            seen.add(key)
            normalized.append(key)
    return normalized

def _load_rules() -> list[dict]:
    global _rules_cache, _rules_mtime, _rules_digest
    try:
        stat = _RULE_FILE.stat()
        raw = _RULE_FILE.read_bytes()
    except OSError as exc:
        # A missing rules file must not take down the diagnosis path. Keep a
        # previously loaded snapshot when one exists; otherwise match nothing.
        if _rules_cache is None:
            _rules_cache = []
        log_event(30, "pattern_rules.load_failed", error=str(exc))
        return _rules_cache

    # Windows can retain the same mtime for rapid successive writes.  Hash the
    # bytes as well so a valid update is never skipped by the hot-reload cache.
    digest = hashlib.sha256(raw).hexdigest()
    if _rules_cache is not None and _rules_digest == digest:
        return _rules_cache

    try:
        payload = yaml.safe_load(raw.decode("utf-8")) or {}
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        # Do not replace a valid snapshot with a partially edited/invalid file.
        log_event(30, "pattern_rules.reload_failed", error=str(exc))
        return _rules_cache or []

    candidate = payload.get("rules", []) if isinstance(payload, dict) else []
    if not isinstance(candidate, list):
        log_event(30, "pattern_rules.reload_failed", error="rules must be a list")
        return _rules_cache or []

    valid: list[dict] = []
    names: set[str] = set()
    for rule in candidate:
        if not isinstance(rule, dict):
            continue
        name = rule.get("name")
        keywords = rule.get("keywords")
        if not isinstance(name, str) or not re.fullmatch(r"[a-z][a-z0-9_]*", name) or name in names:
            continue
        if not isinstance(keywords, list) or len([k for k in keywords if isinstance(k, str) and k.strip()]) < 2:
            continue
        if not isinstance(rule.get("root_cause"), str) or not rule["root_cause"].strip():
            continue
        if not isinstance(rule.get("component", "*"), str):
            continue
        keywords_norm = merge_rule_keywords(rule)
        if not keywords_norm:
            # 关键词与别名归一后全为空 → 该规则永远匹配不到，直接剔除
            log_event(30, "pattern_rules.skipped", rule=name, reason="empty keywords")
            continue
        rule["_keywords_norm"] = keywords_norm
        names.add(name)
        valid.append(rule)
    _rules_cache = valid
    _rules_mtime = stat.st_mtime_ns
    _rules_digest = digest
    log_event(20, "pattern_rules.reloaded", rule_count=len(_rules_cache), mtime=_rules_mtime)
    return _rules_cache


def match_pattern(brief: str, component: str | None) -> dict | None:
    """Return the first known pattern matching ``brief`` and ``component``.

    Matching normalizes both sides first (case / full-width / separators), then
    does a substring test — so ``cpu 使用率`` also matches ``CPU使用率``.
    A rule with component ``*`` applies to every component.  The public
    argument order follows the F-05/F-03 contract: ``match_pattern(brief, component)``.
    """
    rules = _load_rules()
    haystack = normalize_text(brief)
    component_norm = normalize_text(component)
    for rule in rules:
        # `*` 是通配符，必须先判断再归一化 —— normalize_text 会剔除 `*`，
        # 若先归一化，"通配规则"会被误判成"component 为空"而整条跳过。
        rule_component = str(rule.get("component", "*") or "*")
        if rule_component != "*" and normalize_text(rule_component) != component_norm:
            continue
        keywords = rule.get("_keywords_norm")
        if not keywords:
            # 兼容外部手工构造的规则字典（未经 _load_rules 预计算）
            keywords = merge_rule_keywords(rule)
        if any(keyword in haystack for keyword in keywords):
            metrics.pattern_rule_hit_total.labels(rule_name=str(rule.get("name", "unknown"))).inc()
            return rule
    return None


def _pattern_hit(component: str, text: str) -> dict | None:
    """Backward-compatible wrapper for older internal callers."""
    return match_pattern(text, component)


async def run(state: dict) -> dict:
    alerts = state["alerts"]
    titles = "；".join(alert["title"] for alert in alerts[:5])
    suffix = "" if len(alerts) <= 5 else f"；另有 {len(alerts) - 5} 条聚合告警"
    brief = f"{state['component']} 组件 {state['severity']} 事件：{titles}{suffix}"[:200]
    hit = match_pattern(f"{state['title']} {titles}", state["component"])
    result = {"brief": brief, "pattern_hit": hit is not None, "rule_playbook": [], "tool_calls": []}
    if hit:
        result.update(
            {
                "rule_root_cause": hit["root_cause"],
                "root_cause": hit["root_cause"],
                "root_cause_confidence": 0.95,
                "root_cause_source": "rule",
                "rule_playbook": normalize_playbook(hit),
            }
        )
    return result
