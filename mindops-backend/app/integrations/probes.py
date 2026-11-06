"""F-11 Prometheus 只读连通性探测（27-F-11 §5.2）。

安全铁律：
- 只允许 GET，只访问 /api/v1/status/runtimeinfo 与 /api/v1/targets 两个只读端点
- follow_redirects=False（禁止 30x 跳转绕过 SSRF）
- trust_env=False（忽略 HTTP_PROXY/HTTPS_PROXY）
- 不携带任何 Authorization/Cookie header
- 响应体上限 256KB（流式读取后截断）
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass

import httpx

from app.config import settings

logger = logging.getLogger("mindops.probes")

MAX_RESPONSE_BYTES = 256 * 1024  # 256KB（§5.1：流式读取后截断，防大响应打爆内存）


@dataclass
class ProbeResult:
    """探测结果（路由层根据此结构决定 status/last_heartbeat/last_error）。"""

    ok: bool
    reason: str = ""
    latency_ms: int = 0
    targets_total: int | None = None
    targets_up: int | None = None
    error: str = ""  # "原因码: 摘要" 格式，写入 last_error


async def _fetch_json(url: str, timeout_s: float) -> tuple[int, object | None, int]:
    """单次只读 GET，返回 (status_code, json_or_None, latency_ms)。

    安全铁律（§5.1）：禁重定向、忽略代理环境变量、无任何凭据；
    响应体流式读取并在 MAX_RESPONSE_BYTES 处截断。

    网络异常（超时/连接失败）不在此处吞掉，由调用方分类。
    """
    t0 = time.perf_counter()
    body = bytearray()
    async with httpx.AsyncClient(
        timeout=timeout_s,
        follow_redirects=False,
        trust_env=False,
    ) as client:
        async with client.stream("GET", url) as resp:
            status_code = resp.status_code
            if status_code == 200:
                async for chunk in resp.aiter_bytes():
                    body.extend(chunk)
                    if len(body) >= MAX_RESPONSE_BYTES:
                        del body[MAX_RESPONSE_BYTES:]
                        logger.warning(
                            "F-11 probe: %s 响应体超过 %d 字节，已截断",
                            url, MAX_RESPONSE_BYTES,
                        )
                        break
    latency_ms = int((time.perf_counter() - t0) * 1000)

    if status_code != 200:
        return status_code, None, latency_ms
    try:
        return status_code, json.loads(bytes(body)), latency_ms
    except Exception:
        return status_code, None, latency_ms


async def probe_prometheus(base_url: str, timeout_ms: int | None = None) -> ProbeResult:
    """探测 Prometheus 端点连通性（§5.2）。

    GET {base_url}/api/v1/status/runtimeinfo → 校验 status=='success'
    GET {base_url}/api/v1/targets → 统计 activeTargets[] 的 health=='up'
    """
    if timeout_ms is None:
        timeout_ms = settings.integration_probe_timeout_ms
    timeout_s = timeout_ms / 1000.0

    # ① GET /api/v1/status/runtimeinfo（真实 Prometheus 只有 /api/v1/status/* 子端点，
    #    裸 /api/v1/status 并不存在 → 会 404 导致探测永远失败）
    status_url = f"{base_url}/api/v1/status/runtimeinfo"
    try:
        code, status_data, latency_ms = await _fetch_json(status_url, timeout_s)
    except httpx.TimeoutException:
        return _fail("timeout", f"连接超时 ({timeout_ms}ms)")
    except httpx.HTTPError as exc:
        return _fail("connection_refused", f"连接失败: {exc}")

    # 非 200 → http_error（302 也是走这里：探测不跟随重定向）
    if code != 200:
        return _fail("http_error", f"HTTP {code}")

    # 200 但非 JSON 或缺少 status 字段
    if not isinstance(status_data, dict):
        return _fail("bad_response", "响应非 JSON 格式")

    if status_data.get("status") != "success":
        return _fail("bad_response", "status 字段非 success")

    # ② GET /api/v1/targets（附加信息，失败不阻断探测）
    targets_total = None
    targets_up = None
    targets_url = f"{base_url}/api/v1/targets"
    try:
        code2, targets_data, _ = await _fetch_json(targets_url, timeout_s)
        if code2 == 200 and isinstance(targets_data, dict):
            active_targets = targets_data.get("data", {}).get("activeTargets", [])
            targets_total = len(active_targets)
            targets_up = sum(1 for t in active_targets if t.get("health") == "up")
        else:
            logger.warning(
                "F-11 probe: /targets 返回 %s（降级为无目标数）", code2
            )
    except Exception:
        logger.warning("F-11 probe: /targets 请求失败（降级为无目标数）")

    return ProbeResult(
        ok=True,
        latency_ms=latency_ms,
        targets_total=targets_total,
        targets_up=targets_up,
    )


def _fail(reason: str, detail: str) -> ProbeResult:
    """构造失败结果（§5.2 写库规则：reason + 摘要截断到 255）。"""
    error_str = f"{reason}: {detail}"[:255]
    return ProbeResult(ok=False, reason=reason, error=error_str)
