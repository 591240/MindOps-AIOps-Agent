"""F-11 SSRF 五道闸防护（27-F-11 §5.1）。

校验顺序：scheme → userinfo → DNS 解析 → 全 IP 网段白名单 → 端口。
任一不通过抛 SsrfBlocked，路由层转 422 INVALID_BASE_URL。
"""

from __future__ import annotations

import ipaddress
import logging
import socket
from functools import lru_cache
from urllib.parse import urlsplit

logger = logging.getLogger("mindops.ssrf")

ALLOWED_SCHEMES = {"http", "https"}


class SsrfBlocked(Exception):
    """SSRF 校验失败（路由层捕获后返回 422）。"""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


@lru_cache(maxsize=1)
def _parse_cidrs(cidrs_str: str) -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """解析逗号分隔的 CIDR 字符串为 ip_network 列表（lru_cache 缓存）。"""
    networks: list[ipaddress.IPv4Network | ipaddress.IPv6Network] = []
    for part in cidrs_str.split(","):
        part = part.strip()
        if not part:
            continue
        try:
            networks.append(ipaddress.ip_network(part, strict=False))
        except ValueError:
            logger.warning("F-11 SSRF: 无效 CIDR %r，已跳过", part)
    return tuple(networks)


def get_allowed_cidrs() -> tuple[ipaddress.IPv4Network | ipaddress.IPv6Network, ...]:
    """从配置读取允许的网段（缓存）。"""
    from app.config import settings
    return _parse_cidrs(settings.integration_allowed_cidrs)


def validate_base_url(raw: str) -> str:
    """校验并规范化 base_url（§5.1 五道闸）。

    返回去掉尾斜杠的规范化地址；
    任一条不满足抛 SsrfBlocked → 路由层转 422 INVALID_BASE_URL。
    """
    if not raw or not raw.strip():
        raise SsrfBlocked("地址不能为空")

    raw = raw.strip()

    # ① scheme 白名单
    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise SsrfBlocked(f"仅允许 http/https 协议，收到: {parsed.scheme or '(空)'}")

    # ② 禁止 userinfo
    if parsed.username or parsed.password:
        raise SsrfBlocked("地址中不允许包含用户名/密码信息")

    host = parsed.hostname
    if not host:
        raise SsrfBlocked("无法解析主机名")

    # ③ DNS 解析（取全部 IP，IPv4 + IPv6）
    try:
        addrinfos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
    except socket.gaierror:
        raise SsrfBlocked(f"无法解析主机名: {host}")

    if not addrinfos:
        raise SsrfBlocked(f"主机名无可用地址: {host}")

    # ④ 每个解析 IP 必须在允许网段内
    allowed = get_allowed_cidrs()
    resolved_ips = []
    for family, _, _, _, sockaddr in addrinfos:
        ip_str = sockaddr[0]
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            continue
        resolved_ips.append(ip_obj)

    if not resolved_ips:
        raise SsrfBlocked(f"主机名无法解析为有效 IP: {host}")

    for ip in resolved_ips:
        in_allowed = any(ip in net for net in allowed)
        if not in_allowed:
            raise SsrfBlocked(
                f"地址 {ip} 不在允许的内网段内，"
                f"仅允许: {', '.join(str(n) for n in allowed)}"
            )

    # ⑤ 端口合法（urlsplit 对超界端口抛 ValueError，需捕获转 SsrfBlocked）
    try:
        port = parsed.port
    except ValueError:
        raise SsrfBlocked(f"端口号无效: {parsed.netloc.rsplit(':', 1)[-1]}")
    if port is not None and (port < 1 or port > 65535):
        raise SsrfBlocked(f"端口号无效: {port}")

    # 返回规范化地址（去尾斜杠）
    return raw.rstrip("/")


# ---- 出站"公网"校验（P0-3：LLM BYO-Key 场景）----
#
# 与 validate_base_url 的差别：
#   validate_base_url  用于「集成目标」，策略是**内网白名单**（目标本就在内网）；
#   validate_public_url 用于「用户自接大模型」，策略是**内网黑名单**
#   （目标是公网 SaaS，必须挡住 loopback / 内网 / 云元数据地址，否则
#   管理员填一个 http://169.254.169.254/ 就会把真实 API Key 送过去）。

# 云元数据 / 运营商级 NAT 等 is_private 之外的补充黑名单
_EXTRA_DENY_V4 = (
    ipaddress.ip_network("169.254.0.0/16"),    # link-local / 云元数据
    ipaddress.ip_network("169.254.169.254/32"),  # AWS/GCP/Azure metadata
    ipaddress.ip_network("100.64.0.0/10"),     # CGNAT
    ipaddress.ip_network("192.0.0.0/24"),      # IETF protocol assignments
    ipaddress.ip_network("198.18.0.0/15"),     # benchmarking
    ipaddress.ip_network("240.0.0.0/4"),       # reserved
)


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    """是否属于"不可访问的本地/保留地址"。"""
    if (
        ip.is_unspecified
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_private
    ):
        return True
    if ip.version == 4:
        return any(ip in net for net in _EXTRA_DENY_V4)
    # IPv6：唯一本地地址 fc00::/7
    return ip in ipaddress.ip_network("fc00::/7")


# 即使允许私网（本机 Ollama / 内网 vLLM），云元数据端点也一律拒绝：
# 那是"用一个 Key 换主机凭据"的经典 SSRF 目标，没有任何合法大模型用途。
_METADATA_IPS = frozenset({
    ipaddress.ip_address("169.254.169.254"),  # AWS/GCP/Azure
    ipaddress.ip_address("100.100.100.200"),  # 阿里云
    ipaddress.ip_address("fd00:ec2::254"),    # AWS IPv6 IMDS
})
_METADATA_HOSTS = frozenset({"metadata.google.internal", "metadata"})


def validate_public_url(raw: str, *, allow_private: bool = False) -> str:
    """校验"面向公网"的出站地址（P0-3）。

    校验顺序：scheme → userinfo → DNS 解析 → 拒绝内网/本机/元数据 IP → 端口。
    任一不通过抛 SsrfBlocked。返回去掉尾斜杠的规范化地址。

    allow_private=True 用于**管理员显式配置的自托管端点**（Ollama / 内网 vLLM）：
    放开 loopback 与私网，但云元数据地址仍然拒绝。
    """
    if not raw or not raw.strip():
        raise SsrfBlocked("地址不能为空")

    raw = raw.strip()

    parsed = urlsplit(raw)
    if parsed.scheme.lower() not in ALLOWED_SCHEMES:
        raise SsrfBlocked(f"仅允许 http/https 协议，收到: {parsed.scheme or '(空)'}")

    if parsed.username or parsed.password:
        raise SsrfBlocked("地址中不允许包含用户名/密码信息")

    host = parsed.hostname
    if not host:
        raise SsrfBlocked("无法解析主机名")

    if host.lower().rstrip(".") in _METADATA_HOSTS:
        raise SsrfBlocked(f"不允许指向云元数据地址: {host}")

    def _denied(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
        if ip in _METADATA_IPS:
            return True
        return False if allow_private else _is_blocked_ip(ip)

    # 直接以 IP 字面量给出时也走同一套判定
    try:
        literal_ip = ipaddress.ip_address(host)
    except ValueError:
        literal_ip = None

    if literal_ip is not None:
        if _denied(literal_ip):
            raise SsrfBlocked(
                f"不允许指向内网/本机/保留地址: {host}"
                if not allow_private else f"不允许指向云元数据地址: {host}"
            )
    else:
        try:
            addrinfos = socket.getaddrinfo(host, None, socket.AF_UNSPEC, socket.SOCK_STREAM)
        except socket.gaierror:
            raise SsrfBlocked(f"无法解析主机名: {host}")
        if not addrinfos:
            raise SsrfBlocked(f"主机名无可用地址: {host}")

        resolved: list[ipaddress.IPv4Address | ipaddress.IPv6Address] = []
        for _, _, _, _, sockaddr in addrinfos:
            try:
                resolved.append(ipaddress.ip_address(sockaddr[0]))
            except ValueError:
                continue
        if not resolved:
            raise SsrfBlocked(f"主机名无法解析为有效 IP: {host}")

        # 任一解析结果命中黑名单即拒绝（防 DNS 多记录绕过）
        for ip in resolved:
            if _denied(ip):
                raise SsrfBlocked(
                    f"地址 {host} 解析到内网/本机/保留地址 {ip}，"
                    f"不允许作为大模型端点"
                )

    try:
        port = parsed.port
    except ValueError:
        raise SsrfBlocked(f"端口号无效: {parsed.netloc.rsplit(':', 1)[-1]}")
    if port is not None and (port < 1 or port > 65535):
        raise SsrfBlocked(f"端口号无效: {port}")

    return raw.rstrip("/")
