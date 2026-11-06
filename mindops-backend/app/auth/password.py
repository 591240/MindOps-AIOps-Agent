"""密码哈希（最小实现，仅供 F-01 鉴权联调）。

文档边界：完整密码策略（bcrypt / 强度校验 / 找回）属 F-10，
本模块只提供 F-01 的 get_current_user 所需的哈希与校验能力。

使用 pbkdf2_sha256：无 72 字节截断问题，纯 Python 实现，免编译依赖。
"""

from passlib.context import CryptContext

_pwd = CryptContext(schemes=["pbkdf2_sha256"], deprecated="auto")


def hash_password(raw: str) -> str:
    return _pwd.hash(raw)


def verify_password(raw: str, hashed: str) -> bool:
    try:
        return _pwd.verify(raw, hashed)
    except Exception:
        return False
