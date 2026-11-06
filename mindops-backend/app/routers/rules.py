"""规则库管理：pattern_rules.yaml 的读写接口（admin-only）。

规则以 YAML 文件为唯一事实源：本路由只做「读 → 校验 → 原子写回」，
命中逻辑仍然只走 triage.match_pattern（mtime + sha256 双重热加载，
写回即生效，无需重启）。这里不做第二份内存状态，避免与 Triage 的
缓存互相踩。

字段契约与 triage._load_rules 的校验**严格一致**（写进去的每一条
都必须能被 Triage 加载，否则等于造出一条静默失效的规则）：
- name: `[a-z][a-z0-9_]*`，全库唯一
- component: 字符串，`*` 表示通配所有组件
- keywords: ≥2 个非空项（Triage 会与 aliases 合并归一化）
- root_cause: 非空字符串（命中后的结论，置信度固定 0.95，不可配）
- playbook（可选）: 步骤 ≤8，每步 command 非空、description 可选
"""

from __future__ import annotations

import os
import re
import tempfile
from pathlib import Path

import yaml
from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from app.agents.triage import PLAYBOOK_MAX_STEPS, match_pattern
from app.auth.dependencies import require_admin, get_current_user
from app.models import User

router = APIRouter(prefix="/rules", tags=["rules"])

_RULE_FILE = Path(__file__).resolve().parents[2] / "config" / "pattern_rules.yaml"

# 与 triage._load_rules 完全同名同义
_NAME_RE = re.compile(r"[a-z][a-z0-9_]*")

# YAML 顶层结构 = {"rules": [...]}；写回时只保留这六个白名单字段，
# triage 除此之外不认识任何字段（多写的会被静默忽略，不如写前就拦掉）
_RULE_FIELDS = ("name", "component", "keywords", "aliases", "root_cause", "playbook")


class RulePayload(BaseModel):
    name: str | None = None
    component: str = "*"
    keywords: list[str] = Field(default_factory=list)
    aliases: list[str] = Field(default_factory=list)
    root_cause: str
    playbook: list[dict] | None = None


def _read_rules() -> list[dict]:
    try:
        payload = yaml.safe_load(_RULE_FILE.read_bytes().decode("utf-8")) or {}
    except FileNotFoundError:
        return []
    except (UnicodeDecodeError, yaml.YAMLError) as exc:
        # 规则文件坏了不能静默返回空然后被写回覆盖——那会把故障固化。
        raise HTTPException(500, detail={"code": "RULES_FILE_INVALID", "message": f"规则文件解析失败：{exc}"})
    rules = payload.get("rules", []) if isinstance(payload, dict) else []
    if not isinstance(rules, list):
        raise HTTPException(500, detail={"code": "RULES_FILE_INVALID", "message": "规则文件结构非法：rules 必须是列表"})
    return [r for r in rules if isinstance(r, dict)]


def _write_rules(rules: list[dict]) -> None:
    """原子写回：先写临时文件再 os.replace，避免写一半被 Triage 读到。"""
    _RULE_FILE.parent.mkdir(parents=True, exist_ok=True)
    payload = yaml.safe_dump(
        {"rules": rules},
        allow_unicode=True,
        sort_keys=False,
        default_flow_style=False,
    )
    fd, tmp_path = tempfile.mkstemp(dir=str(_RULE_FILE.parent), suffix=".yaml.tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
        os.replace(tmp_path, _RULE_FILE)
    except OSError:
        # 写失败时清掉残留的临时文件，规则文件本体保持原样
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


def _sanitize(rule: dict) -> dict:
    """只保留白名单字段，剔除内部字段与历史遗留（如空 aliases）。"""
    cleaned: dict = {}
    for field in _RULE_FIELDS:
        value = rule.get(field)
        if field == "aliases" and not value:
            continue
        if field == "playbook" and not value:
            continue
        if value is not None:
            cleaned[field] = value
    return cleaned


def _validate(payload: RulePayload, existing: list[dict], original_name: str | None = None) -> dict:
    """校验 + 归一化，非法直接 422（错误信息里说清哪一项不合规）。"""
    name = (payload.name or original_name or "").strip()
    if not name:
        raise HTTPException(422, detail={"code": "RULE_NAME_REQUIRED", "message": "规则名不能为空"})
    if not _NAME_RE.fullmatch(name):
        raise HTTPException(
            422,
            detail={"code": "RULE_NAME_INVALID", "message": "规则名只能是小写字母开头的小写字母/数字/下划线"},
        )
    if any(r.get("name") == name for r in existing) and name != original_name:
        raise HTTPException(409, detail={"code": "RULE_NAME_EXISTS", "message": f"规则名 {name} 已存在"})

    component = (payload.component or "*").strip() or "*"
    if len(component) > 64:
        raise HTTPException(422, detail={"code": "RULE_COMPONENT_INVALID", "message": "component 过长（≤64）"})

    keywords = [k.strip() for k in payload.keywords if isinstance(k, str) and k.strip()]
    if len(keywords) < 2:
        raise HTTPException(422, detail={"code": "RULE_KEYWORDS_MIN", "message": "关键词至少 2 个（去重归一化前）"})

    aliases = [a.strip() for a in payload.aliases if isinstance(a, str) and a.strip()]

    root_cause = payload.root_cause.strip()
    if not root_cause:
        raise HTTPException(422, detail={"code": "RULE_CAUSE_REQUIRED", "message": "根因结论（root_cause）不能为空"})

    playbook: list[dict] = []
    if payload.playbook:
        if len(payload.playbook) > PLAYBOOK_MAX_STEPS:
            raise HTTPException(
                422,
                detail={"code": "RULE_PLAYBOOK_MAX", "message": f"剧本最多 {PLAYBOOK_MAX_STEPS} 步"},
            )
        for idx, step in enumerate(payload.playbook, start=1):
            if not isinstance(step, dict):
                raise HTTPException(422, detail={"code": "RULE_PLAYBOOK_STEP", "message": f"剧本第 {idx} 步格式非法"})
            command = str(step.get("command", "")).strip()
            if not command:
                raise HTTPException(
                    422,
                    detail={"code": "RULE_PLAYBOOK_STEP", "message": f"剧本第 {idx} 步缺少 command"},
                )
            entry: dict = {
                "command": command,
                "description": str(step.get("description") or "执行剧本步骤"),
            }
            # 可选手动风险等级：指定后覆盖风险库的自动判定（executor 黑名单仍兜底）
            risk_level = step.get("risk_level")
            if risk_level is not None:
                if risk_level not in ("low", "medium", "high"):
                    raise HTTPException(
                        422,
                        detail={"code": "RULE_PLAYBOOK_RISK", "message": f"剧本第 {idx} 步 risk_level 必须为 low|medium|high"},
                    )
                entry["risk_level"] = risk_level
            playbook.append(entry)

    return {
        "name": name,
        "component": component,
        "keywords": keywords,
        "aliases": aliases,
        "root_cause": root_cause,
        "playbook": playbook,
    }


@router.get("")
async def list_rules(user: User = Depends(get_current_user)):
    """规则列表（engineer 可读：规则库是诊断链路的公开事实，管理员才可改）。"""
    rules = [_sanitize(r) for r in _read_rules()]
    return {"rules": rules, "file": str(_RULE_FILE.name)}


@router.post("", status_code=201)
async def create_rule(payload: RulePayload, user: User = Depends(require_admin)):
    existing = _read_rules()
    rule = _validate(payload, existing, original_name=None)
    existing.append(rule)
    _write_rules([_sanitize(r) for r in existing])
    return rule


@router.put("/{name}")
async def update_rule(name: str, payload: RulePayload, user: User = Depends(require_admin)):
    existing = _read_rules()
    if not any(r.get("name") == name for r in existing):
        raise HTTPException(404, detail={"code": "RULE_NOT_FOUND", "message": f"规则 {name} 不存在"})
    rule = _validate(payload, existing, original_name=name)
    updated = [rule if r.get("name") == name else _sanitize(r) for r in existing]
    _write_rules(updated)
    return rule


@router.delete("/{name}")
async def delete_rule(name: str, user: User = Depends(require_admin)):
    existing = _read_rules()
    remaining = [r for r in existing if r.get("name") != name]
    if len(remaining) == len(existing):
        raise HTTPException(404, detail={"code": "RULE_NOT_FOUND", "message": f"规则 {name} 不存在"})
    _write_rules([_sanitize(r) for r in remaining])
    return {"deleted": name}


class RuleTestPayload(BaseModel):
    text: str = Field(min_length=1, max_length=500)
    component: str | None = None


@router.post("/test")
async def test_rule(payload: RuleTestPayload, user: User = Depends(get_current_user)):
    """命中测试器：给一段告警文本，返回会被哪条规则短路（演示/验证两用）。"""
    hit = match_pattern(payload.text, payload.component)
    if hit is None:
        return {"matched": False, "rule": None}
    return {
        "matched": True,
        "rule": {
            "name": hit.get("name"),
            "component": hit.get("component"),
            "root_cause": hit.get("root_cause"),
            "playbook": hit.get("playbook") or [],
        },
    }
