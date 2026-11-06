"""webhook 内网 IP 白名单（文档 §6.3）。

判定规则（§6.3 V1.1 新增，安全必须项）：
  1. 若直连 socket IP 属于 trusted_proxies（网关/Nginx 内网 IP）→ 采信 X-Forwarded-For，
     取"扣除可信代理后"的客户端 IP
  2. 否则**一律使用直连 socket IP**，无视任何 X-Forwarded-For（防外网伪造头部绕过白名单）
  3. 用 ipaddress.ip_network 做 CIDR 判断，依次检查 allowed_cidrs；任一命中即放行

配置：config/webhook_whitelist.yaml
"""

from __future__ import annotations

import hmac
import ipaddress
from pathlib import Path

import yaml
from fastapi import HTTPException, Request, status

from app.config import settings

CONFIG_PATH = Path(__file__).resolve().parents[2] / "config" / "webhook_whitelist.yaml"

# 默认兜底（配置文件缺失时）：仅放行本机与常见内网段
# 审查 #26：默认只信任本机回环——内网代理需显式配置，防止伪造 XFF 绕过白名单
_DEFAULT_TRUSTED = ["127.0.0.1/32"]
_DEFAULT_ALLOWED = ["127.0.0.1/32", "10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16"]


def _load_config() -> tuple[list[str], list[str]]:
    if not CONFIG_PATH.exists():
        return _DEFAULT_TRUSTED, _DEFAULT_ALLOWED
    with CONFIG_PATH.open("r", encoding="utf-8") as f:
        cfg = yaml.safe_load(f) or {}
    trusted = cfg.get("trusted_proxies") or _DEFAULT_TRUSTED
    allowed = cfg.get("allowed_cidrs") or _DEFAULT_ALLOWED
    return list(trusted), list(allowed)


TRUSTED_PROXIES, ALLOWED_CIDRS = _load_config()


def _in_cidrs(ip: str, cidrs: list[str]) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    for c in cidrs:
        try:
            if addr in ipaddress.ip_network(c, strict=False):
                return True
        except ValueError:
            continue
    return False


def resolve_client_ip(request: Request) -> str:
    """按 §6.3 规则解析真实客户端 IP。"""
    socket_ip = request.client.host if request.client else "127.0.0.1"

    # 直连 IP 不是可信代理 → 无视 XFF，直接返回 socket IP
    if not _in_cidrs(socket_ip, TRUSTED_PROXIES):
        return socket_ip

    # 是可信代理 → 从右往左跳过可信条目，取第一个不可信者作为客户端 IP
    xff = request.headers.get("x-forwarded-for", "")
    chain = [p.strip() for p in xff.split(",") if p.strip()]
    for candidate in reversed(chain):
        if not _in_cidrs(candidate, TRUSTED_PROXIES):
            return candidate
    return chain[0] if chain else socket_ip


def is_ip_allowed(ip: str) -> bool:
    return _in_cidrs(ip, ALLOWED_CIDRS)


async def check_ip_whitelist(request: Request) -> str:
    """FastAPI 依赖：不在白名单抛 403 IP_NOT_WHITELIST（§3.1 错误码）。"""
    ip = resolve_client_ip(request)
    if not is_ip_allowed(ip):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail={"code": "IP_NOT_WHITELIST", "message": f"ip {ip} not allowed"},
        )
    return ip


async def check_webhook_secret(request: Request) -> None:
    """共享密钥校验（FastAPI 依赖，纵深防御）。

    只靠 IP 白名单挡不住：经 Nginx 反代后所有请求的直连 IP 都是代理地址，
    而 X-Forwarded-For 完全由客户端自填——外网写一个内网地址就能同时过白名单
    和换取新的限流分桶。配置了 MINDOPS_WEBHOOK_TOKEN 即强制校验；未配置时
    只有 debug 档放行，生产由 config.validate_for_startup 拒绝启动。
    """
    expected = settings.webhook_token.strip()
    if not expected:
        return
    got = request.headers.get("x-webhook-token", "")
    if not hmac.compare_digest(got, expected):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail={"code": "WEBHOOK_UNAUTHORIZED", "message": "webhook 令牌缺失或不正确"},
        )
