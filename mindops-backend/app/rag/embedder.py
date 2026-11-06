"""可替换 embedding 适配器；MVP 无 Key 时使用稳定本地向量。

The adapter keeps the same contract as a remote embedding service: requests are
batched, successful vectors are cached by text hash, and transient failures are
retried before the caller is allowed to decide whether to degrade.
"""
from __future__ import annotations
import asyncio
import hashlib
import json
import math
import os
import time
import httpx
from pathlib import Path

from app.observability.logging import log_event

DIM = 1024
BATCH_SIZE = 16
MAX_RETRIES = 2
DEFAULT_EMBEDDING_MODEL = "text-embedding-v3"
_KEY_TTL_SECONDS = 60.0
_key_cache: tuple[float, tuple[str, str, str]] | None = None
_local_logged = False
_CACHE_PATH = Path(os.getenv("MINDOPS_EMBED_CACHE_PATH", Path(__file__).resolve().parents[2] / "data" / "embedding_cache.json"))
_cache: dict[str, list[float]] = {}
_cache_loaded = False
_last_request_at = 0.0
_qps_lock = asyncio.Lock()


def _load_cache() -> None:
    global _cache_loaded, _cache
    if _cache_loaded:
        return
    _cache_loaded = True
    try:
        data = json.loads(_CACHE_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            valid: dict[str, list[float]] = {}
            for key, value in data.items():
                if not isinstance(value, list) or len(value) != DIM:
                    continue
                try:
                    vector = [float(item) for item in value]
                except (TypeError, ValueError):
                    continue
                if all(math.isfinite(item) for item in vector):
                    valid[str(key)] = vector
            _cache = valid
    except (OSError, ValueError, TypeError, UnicodeError):
        _cache = {}


def _persist_cache() -> None:
    try:
        _CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
        _CACHE_PATH.write_text(json.dumps(_cache, ensure_ascii=False), encoding="utf-8")
    except OSError:
        # A read-only deployment may still use the in-memory cache.
        pass


async def _resolve_api_key() -> tuple[str, str, str]:
    """embedding Key 的取值优先级：F-09 embedding 槽位（vault 解密）> 环境变量 > 无。

    返回 `(key, source, model)`。槽位查到的 Key 只出现在返回值里，绝不写日志、
    绝不进任何接口响应；模型名跟随槽位配置，避免 UI 改了模型、代码还在打 v3。
    结果缓存 60 秒——ingest 按批发请求，不该每批打一次 DB。
    """
    global _key_cache
    now = time.monotonic()
    if _key_cache is not None and now - _key_cache[0] < _KEY_TTL_SECONDS:
        return _key_cache[1]

    resolved = ("", "", os.getenv("MINDOPS_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL))
    try:
        from app.llm import vault
        from app.models import LlmProvider

        providers = await LlmProvider.filter(role="embedding", status="active").order_by("-is_default", "-id")
        for provider in providers:
            if not provider.api_key_enc:
                continue  # 系统种子记录：没有 Key，调用方自行退到本地向量
            try:
                model = (provider.model_name or "").strip() or resolved[2]
                resolved = (vault.decrypt(provider.api_key_enc), f"provider:{provider.id}", model)
                break
            except RuntimeError:
                # vault_locked（主密钥缺失或已轮换）：这条密文不可解，继续找下一条
                log_event(30, "embedding.provider_vault_locked", provider_id=provider.id)
    except Exception as exc:  # noqa: BLE001 —— 表缺失等异常不该阻断本地开发
        log_event(30, "embedding.provider_lookup_failed", error=f"{type(exc).__name__}: {exc}")

    if not resolved[0]:
        env_key = os.getenv("DASHSCOPE_API_KEY") or os.getenv("MINDOPS_DASHSCOPE_API_KEY")
        if env_key:
            resolved = (env_key, "env", os.getenv("MINDOPS_EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL))

    _key_cache = (now, resolved)
    return resolved


def invalidate_key_cache() -> None:
    """F-09 改动 embedding 槽位后调用，让新 Key 立即生效而不必等 60 秒。"""
    global _key_cache
    _key_cache = None


def _log_local_fallback(count: int) -> None:
    """本地向量是文本 sha256 派生的伪向量，dense 相似度没有物理意义，必须留痕。

    静默降级是这套系统最坏的失败模式：入库会"成功"、检索会报融合结果，
    但召回的邻居只由文本哈希决定，而且这些向量还会被写进 embedding 缓存，
    接上真实模型后继续被复用。至少要让日志里有一条能看懂的 WARNING。
    """
    global _local_logged
    if _local_logged:
        return
    _local_logged = True
    log_event(
        40, "embedding.local_vectors", sample_count=count,
        hint="未配置 embedding Key：向量由文本 sha256 派生，dense 检索结果不可信。"
             "请在系统设置为 embedding 槽位填 Key，或设置 MINDOPS_DASHSCOPE_API_KEY。",
    )


async def _request_vectors(texts: list[str]) -> list[list[float]]:
    """Call DashScope when configured, with a deterministic dev fallback."""
    api_key, _source, model = await _resolve_api_key()
    if api_key:
        global _last_request_at
        async with _qps_lock:
            delay = 0.1 - (time.monotonic() - _last_request_at)
            if delay > 0:
                await asyncio.sleep(delay)
            async with httpx.AsyncClient(timeout=30) as client:
                response = await client.post(
                    "https://dashscope.aliyuncs.com/api/v1/services/embeddings/text-embedding/text-embedding",
                    headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
                    json={"model": model, "input": {"texts": texts}, "parameters": {"dimension": DIM}},
                )
                response.raise_for_status()
                payload = response.json()
            _last_request_at = time.monotonic()
            output = payload.get("output", {}).get("embeddings", [])
            vectors = [item.get("embedding") for item in sorted(output, key=lambda item: item.get("text_index", 0))]
            _validate_vectors(vectors, len(texts))
            norms = [math.sqrt(sum(value * value for value in vector)) or 1.0 for vector in vectors]
            return [[value / norm for value in vector] for vector, norm in zip(vectors, norms)]
    # Deterministic local vector keeps development and offline tests usable.
    _log_local_fallback(len(texts))
    vectors = []
    for text in texts:
        digest = hashlib.sha256(text.encode("utf-8")).digest()
        values = [((digest[i % len(digest)] / 255.0) - .5) for i in range(DIM)]
        norm = math.sqrt(sum(v * v for v in values)) or 1.0
        vectors.append([v / norm for v in values])
    return vectors


def _validate_vectors(vectors: list[list[float]], expected: int) -> None:
    if len(vectors) != expected or any(len(vector) != DIM for vector in vectors):
        raise ValueError(f"embedding dimension must be {DIM}")
    try:
        finite = all(math.isfinite(float(value)) for vector in vectors for value in vector)
    except (TypeError, ValueError):
        raise ValueError("embedding values must be numeric") from None
    if not finite:
        raise ValueError("embedding contains non-finite values")


async def embed_batch(texts: list[str]) -> list[list[float]]:
    _load_cache()
    # 缓存键必须带上"这批向量是谁产的"：本地伪向量和真实模型的输出都是 1024 维
    # float 数组，读的时候完全分不出来。只按文本哈希做键的话，接上真实 embedding
    # Key 之后仍会命中之前 sha256 派生的假向量并永久复用——这是最难查的一类污染。
    api_key, _source, model = await _resolve_api_key()
    namespace = "local-vector" if not api_key else f"{model}@{DIM}"
    vectors = [None] * len(texts)
    missing: list[tuple[int, str, str]] = []
    missing_by_key: dict[str, tuple[str, list[int]]] = {}
    for index, text in enumerate(texts):
        key = hashlib.sha1(f"{namespace}|{text}".encode("utf-8")).hexdigest()
        if key in _cache:
            vectors[index] = _cache[key]
        else:
            entry = missing_by_key.setdefault(key, (text or "", []))
            entry[1].append(index)
    missing = [(indices[0], text, key) for key, (text, indices) in missing_by_key.items()]
    for offset in range(0, len(missing), BATCH_SIZE):
        batch = missing[offset:offset + BATCH_SIZE]
        batch_texts = [text for _, text, _ in batch]
        last_error: Exception | None = None
        for attempt in range(MAX_RETRIES + 1):
            try:
                generated = await _request_vectors(batch_texts)
                _validate_vectors(generated, len(batch_texts))
                for (index, _, key), vector in zip(batch, generated):
                    _cache[key] = vector
                    for duplicate_index in missing_by_key[key][1]:
                        vectors[duplicate_index] = vector
                last_error = None
                break
            except Exception as exc:  # noqa: BLE001 - adapter boundary
                last_error = exc
                if attempt < MAX_RETRIES:
                    await asyncio.sleep(0.05 * (2 ** attempt))
        if last_error is not None:
            raise last_error
    if missing:
        _persist_cache()
    return vectors

async def embed_one(text: str) -> list[float]:
    return (await embed_batch([text]))[0]
