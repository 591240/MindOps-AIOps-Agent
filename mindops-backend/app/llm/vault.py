"""密钥保险库（F-09 §5.5，PRD §9.4 五措施）。

- 加密落库：Fernet（AES-128-CBC + HMAC），主密钥取 `MINDOPS_LLM_VAULT_KEY`
  （pydantic 字段 llm_vault_key，env 前缀 MINDOPS_，与数据库分置）；
- 永不回显：对外只给 mask() 脱敏串，密文不出本模块；
- 未配置主密钥：encrypt/decrypt 统一抛 RuntimeError("vault_locked")——
  保存被拒（400）、调用走降级链且不烧熔断（gateway 负责转换）。
"""

from __future__ import annotations

from cryptography.fernet import Fernet, InvalidToken

from app.config import settings

VAULT_LOCKED = "vault_locked"


def _fernet() -> Fernet:
    if not settings.llm_vault_key:
        raise RuntimeError(VAULT_LOCKED)
    return Fernet(settings.llm_vault_key.encode())


def encrypt(plain: str) -> str:
    """明文 Key → Fernet 密文（落库唯一形态）。"""
    return _fernet().encrypt(plain.encode()).decode()


def decrypt(enc: str) -> str:
    """密文 → 明文（仅 gateway/test 在请求生命周期内使用，用后即弃）。"""
    try:
        return _fernet().decrypt(enc.encode()).decode()
    except InvalidToken as exc:
        # 主密钥缺失/轮换后旧密文不可解：配置错误，非厂商调用失败
        raise RuntimeError(VAULT_LOCKED) from exc


def mask(plain: str) -> str:
    """脱敏展示：sk-****a1b2（短 Key 全遮）。"""
    return f"{plain[:3]}****{plain[-4:]}" if len(plain) > 8 else "****"
