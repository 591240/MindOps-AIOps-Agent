"""告警归一化（文档 §5）。

来源：docs/12-F-01-告警接入专项文档-V1.1.md §5.1 字段映射 / §5.2 异常处理 / §5.3 代码骨架
函数在命名与 Semantics 上与文档 §5.3 保持一致。
"""

from __future__ import annotations

import hashlib
import re

# ---- §5.3 常量 ----
SEV_MAP = {"critical": "P0", "warning": "P2", "info": "P3"}
SEV_VALID = {"P0", "P1", "P2", "P3"}
ANSI = re.compile(r"\x1b\[[0-9;]*m")
COMP_RE = re.compile(r"[^\w-]+")

# CHECK 约束要求的最终形态：^[a-z0-9_-]{1,64}$（§4.2 chk_comp）
COMP_STRICT = re.compile(r"[^a-z0-9_-]+")


def _norm_severity(raw: str | None) -> str:
    """两级处理：映射表优先，其次白名单直传，其余兜底 P3（§5.2）。"""
    s = (raw or "P3").strip().lower()
    if s in SEV_MAP:
        return SEV_MAP[s]
    s = s.upper()
    return s if s in SEV_VALID else "P3"


def _norm_component(raw: str | None) -> str:
    r"""转小写，[^\w-] → '-'，首尾 '-' 切除，截断 64（§5.2）。

    实现补充（假设标注）：文档 COMP_RE 用 r"[^\w-]+"，Python 下 \\w 含 Unicode，
    中文组件名（如 "Redis 服务"）会保留汉字，进而违反 §4.2 的
    CHECK (component REGEXP '^[a-z0-9_-]{1,64}$') 导致 500。
    这里在文档逻辑之后追加一道 ASCII 收敛，保证一定能通过 CHECK。
    """
    c = (raw or "unknown").strip().lower()
    c = COMP_RE.sub("-", c)
    c = COMP_STRICT.sub("-", c)      # 兜底：非 ASCII（汉字等）同样收敛为 '-'
    c = c.strip("-")[:64].strip("-")
    return c or "unknown"


def _norm_title(raw: str | None) -> str:
    """超 255 截断为 252 + '...'；空标题抛 ValueError（→ 上层 400）。"""
    t = (raw or "").strip()
    t = (t[:252] + "...") if len(t) > 255 else t
    if not t:
        raise ValueError("title empty after normalization")
    return t


def _fingerprint(source: str, component: str, title: str) -> str:
    """三源统一指纹公式（V1.1）：SHA1(f"{source}|{component}|{title}")。

    带 '|' 分隔符，避免 ("app","le","x") 与 ("ap","ple","x") 碰撞（§5.1 修订说明）。
    """
    return hashlib.sha1(f"{source}|{component}|{title}".encode()).hexdigest()


def normalize_prometheus(alert: dict) -> dict:
    """Prometheus Alertmanager 单条告警 → 统一结构（§5.1）。"""
    labels = alert.get("labels", {}) or {}
    ann = alert.get("annotations", {}) or {}
    component = _norm_component(labels.get("component") or labels.get("job"))
    title = _norm_title(ann.get("summary") or labels.get("alertname"))
    detail = ANSI.sub("", ann.get("description") or "")
    return {
        "source": "prometheus",
        "component": component,
        "severity": _norm_severity(labels.get("severity")),
        "title": title,
        "detail": detail,
        "labels_json": labels,
        "raw_payload": alert,
        "fingerprint": _fingerprint("prometheus", component, title),
        # F-12 任务归属：由上报方显式携带，缺失即「未归属」（30-F-12 §5.3）
        "integration_id": alert.get("integration_id"),
    }


def normalize_app(body: dict) -> dict:
    """应用主动上报 → 统一结构（§5.1）。"""
    component = _norm_component(body.get("component"))
    title = _norm_title(body.get("title"))
    return {
        "source": "app",
        "component": component,
        "severity": _norm_severity(body.get("severity")),
        "title": title,
        "detail": body.get("detail") or "",
        "labels_json": body.get("labels") or {},
        "raw_payload": body,
        "fingerprint": _fingerprint("app", component, title),
        # F-12 任务归属（同 prometheus 口径）
        "integration_id": body.get("integration_id"),
    }


def normalize_manual(body: dict) -> dict:
    """手动注入 → 统一结构，强制 source='demo'（§5.1 / §3.3）。"""
    n = normalize_app(body)
    n["source"] = "demo"
    n["fingerprint"] = _fingerprint("demo", n["component"], n["title"])
    n["labels_json"] = {}
    return n
