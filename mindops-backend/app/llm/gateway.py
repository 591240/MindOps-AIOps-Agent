"""统一模型调用入口（F-09 §5.1/§5.3，一套 OpenAI 兼容协议）。

- test_connectivity：16-token ping（独立于 timeout_ms，按角色分档：
  chat 10s / reasoning 45s），保存即测试的数据源；失败抛 LlmCallError
  （reason 为机器原因码）；
- complete：真实调用。api_key_enc 为 NULL（系统种子）时走桩路径返回
  degraded="stub"（§5.7 兜底层，F-03 桩行为由此保持不变）；
- 安全红线（§5.5）：异常只携带机器原因码与 HTTP 状态码，**绝不携带
  请求头/响应体/密钥**——日志与 step 轨迹因此天然无明文 Key。
"""

from __future__ import annotations

import time

import httpx

from app.llm import registry, vault
from app.models import LlmProvider

PING_TIMEOUT_SECONDS = 10.0
# 推理模型（reasoning）"先思考再作答"，16-token ping 实测 7~15s 且抖动，
# 用 10s 会让保存/测试在超时边缘随机失败（reason=timeout）。
PING_TIMEOUT_SECONDS_REASONING = 45.0
PING_MAX_TOKENS = 16


def ping_timeout_for(role: str) -> float:
    """连通测试超时按角色分档：推理模型需放宽，其余保持 10s。"""
    return PING_TIMEOUT_SECONDS_REASONING if role == "reasoning" else PING_TIMEOUT_SECONDS

# HTTP 状态码 → 机器原因码（test_connectivity 400 body 与降级日志共用）
_REASON_BY_STATUS = {
    401: "key_invalid",
    403: "key_invalid",
    404: "model_not_found",
    429: "rate_limited",
}


class LlmCallError(Exception):
    """厂商调用失败；reason ∈ timeout/key_invalid/model_not_found/
    rate_limited/provider_error/endpoint_unreachable/vault_locked。"""

    def __init__(self, reason: str, detail: str = ""):
        self.reason = reason
        # detail 只允许非敏感内容（HTTP 状态码等），见模块级安全红线
        super().__init__(f"{reason}: {detail}" if detail else reason)


def _assert_safe_endpoint(base_url: str, allow_private: bool = False) -> str:
    """P0-3：出站前挡住 SSRF（内网 / 本机 / 云元数据）。

    与 F-11 集成探测共用 ssrf_guard 的判定能力。默认策略是"公网黑名单"，
    因为 BYO 大模型的端点通常本就在公网；`allow_private=True` 仅用于管理员
    显式配置的本地/自托管端点（Ollama、内网 vLLM），元数据地址始终拒绝。
    """
    from app.integrations.ssrf_guard import SsrfBlocked, validate_public_url

    try:
        return validate_public_url(base_url, allow_private=allow_private)
    except SsrfBlocked as exc:
        raise LlmCallError("endpoint_blocked", str(exc)) from exc


async def _post_chat(
    base_url: str,
    api_key: str,
    model_name: str,
    payload: dict,
    timeout: float,
    *,
    allow_private: bool = False,
) -> dict:
    """发 OpenAI 兼容 chat/completions 请求；网络/状态码错误统一转 LlmCallError。"""
    # P0-3：任何出口（含 DB 中历史 provider 行）都先过 SSRF 闸，再做请求
    base_url = _assert_safe_endpoint(base_url, allow_private)
    url = f"{base_url.rstrip('/')}/chat/completions"
    headers = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    try:
        # P0-3：与 app/integrations/probes.py 对齐——禁止跟随跳转（防绕过）、
        # 不读取环境代理（trust_env），避免密钥被发往非预期地址。
        async with httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
        ) as client:
            resp = await client.post(url, headers=headers, json=payload)
    except httpx.TimeoutException:
        raise LlmCallError("timeout", f"timeout {timeout}s")
    except httpx.HTTPError:
        raise LlmCallError("endpoint_unreachable")
    if resp.status_code != 200:
        raise LlmCallError(_REASON_BY_STATUS.get(resp.status_code, "provider_error"), f"HTTP {resp.status_code}")
    try:
        return resp.json()
    except ValueError:
        raise LlmCallError("provider_error", "non-json body")


async def test_connectivity(
    base_url: str,
    api_key: str,
    model_name: str,
    timeout: float = PING_TIMEOUT_SECONDS,
    *,
    allow_private: bool = False,
) -> dict:
    """保存即测试：16-token ping。成功返回 {status, latency_ms, actual_model}。"""
    started = time.monotonic()
    data = await _post_chat(
        base_url, api_key, model_name,
        {"model": model_name, "messages": [{"role": "user", "content": "ping"}], "max_tokens": PING_MAX_TOKENS},
        timeout,
        allow_private=allow_private,
    )
    latency_ms = int((time.monotonic() - started) * 1000)
    return {"status": "success", "latency_ms": latency_ms, "actual_model": data.get("model") or model_name}


def _stub_complete(messages: list[dict], model_name: str) -> dict:
    """系统种子（api_key_enc=NULL）的桩路径：不发起任何外部调用。

    F-03 各 Agent 保持既有启发式行为（diagnose 检测到 degraded=="stub"
    后走本地规则），本函数只提供可用的返回形态。
    """
    last_user = next((m.get("content", "") for m in reversed(messages) if m.get("role") == "user"), "")
    return {
        "content": f"[stub] {last_user}"[:200],
        "prompt_tokens": 0,
        "completion_tokens": 0,
        "latency_ms": 0,
        "actual_model": model_name or "system-stub",
        "degraded": "stub",
    }


async def complete(
    model_id: int,
    messages: list[dict],
    *,
    temperature: float | None = None,
    max_tokens: int | None = None,
) -> dict:
    """按 provider 配置调用模型；失败抛 LlmCallError（降级链在 model_router）。

    返回 {content, prompt_tokens, completion_tokens, latency_ms, actual_model}
    （桩路径额外含 degraded="stub"）。
    """
    provider = await LlmProvider.get_or_none(id=model_id)
    if provider is None:
        raise LookupError(f"provider {model_id} not found")
    if provider.api_key_enc is None:
        return _stub_complete(messages, provider.model_name)
    try:
        api_key = vault.decrypt(provider.api_key_enc)
    except RuntimeError as exc:
        # vault_locked：配置错误，不烧熔断（model_router 按 reason 跳过计数）
        raise LlmCallError(vault.VAULT_LOCKED) from exc

    started = time.monotonic()
    data = await _post_chat(
        provider.base_url, api_key, provider.model_name,
        {
            "model": provider.model_name,
            "messages": messages,
            "temperature": provider.temperature if temperature is None else temperature,
            "max_tokens": provider.max_tokens if max_tokens is None else max_tokens,
        },
        provider.timeout_ms / 1000,
        allow_private=registry.allows_private_endpoint(provider.provider_type),
    )
    latency_ms = int((time.monotonic() - started) * 1000)
    try:
        choice = data["choices"][0]["message"]["content"] or ""
        usage = data.get("usage") or {}
    except (KeyError, IndexError, TypeError):
        raise LlmCallError("provider_error", "unexpected response shape")
    return {
        "content": choice,
        "prompt_tokens": int(usage.get("prompt_tokens") or 0),
        "completion_tokens": int(usage.get("completion_tokens") or 0),
        "latency_ms": latency_ms,
        "actual_model": data.get("model") or provider.model_name,
    }
