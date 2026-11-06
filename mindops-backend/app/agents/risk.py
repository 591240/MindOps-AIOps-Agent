"""F-03 处置建议风险库。"""

from __future__ import annotations

import re
from functools import lru_cache
from pathlib import Path

import yaml

_RULE_FILE = Path(__file__).resolve().parents[2] / "config" / "risk_rules.yaml"


@lru_cache(maxsize=1)
def _rules() -> dict[str, list[str]]:
    with _RULE_FILE.open(encoding="utf-8") as source:
        loaded = yaml.safe_load(source) or {}
    return {level: list(loaded.get(level, [])) for level in ("high", "medium", "low")}


def check_risk(command: str) -> str:
    """用服务器端规则覆盖 Agent 自报风险，未知写操作按 medium 保守处理。"""
    for level in ("high", "medium", "low"):
        if any(re.search(pattern, command) for pattern in _rules()[level]):
            return level
    return "medium"
