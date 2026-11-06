"""邮箱验证码服务（注册 / 登录共用）。

- normalize_email()：去空白 + 域名小写
- send_code()：60s 冷却 → 生成 code → 落 MySQL（email_codes）→ **经通道下发**
- verify_code()：按 email 查最新 pending → attempts++ → SHA256 比对 → 状态更新
- clear_expired()：清理过期记录（可定期调度）

**通道（`MINDOPS_EMAIL_PROVIDER`）**：`mock`（只记日志不下发，验证码经 `debug_code`
回传，供开发/演示）/ `smtp`（标准库 smtplib，SSL 465 或 STARTTLS 587）。选标准库
而非第三方 SDK 是有意的：这是私有化内网部署，每多一个依赖都是运维负债，而
"smtp + ssl + login + send_message" 标准库已经完整覆盖。SMTP 是阻塞 API，
统一下沉到 `asyncio.to_thread` 执行，避免卡住事件循环。

下发失败抛 `EmailSendError`，由路由层转成明确错误码——**绝不在没发出去的情况下假装成功**。
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import secrets
import random
import re
import smtplib
import ssl
import string
from datetime import datetime, timedelta
from email.message import EmailMessage
from email.utils import formataddr
from zoneinfo import ZoneInfo

from app.config import settings
from tortoise.expressions import F
from app.models import EmailCode
from app.observability.logging import log_event

# 模型时区，与 db.tortoise_orm()['timezone'] 保持一致。
MODEL_TZ = ZoneInfo("Asia/Shanghai")

# 宽松但不放水：本地部分允许常见符号，域名须含点且 TLD ≥2 位。
# 不做 RFC 5322 完全校验——那需要正则爆炸或第三方库，而真正的有效性由"能否收到码"证明。
_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$")

# mock 通道的固定验证码。**必须是纯数字**：前端验证码输入框会过滤非数字字符，
# 若这里回 "debug_code" 这类字符串，用户界面上根本敲不进去，注册流程直接死锁。
MOCK_CODE = "123456"


class EmailSendError(RuntimeError):
    """通道下发失败（凭据错误 / SMTP 不可达 / 收件人被拒 / 超时 …）。

    `reason` 为机器原因码，供路由层映射中文提示：
    auth_failed / recipient_refused / endpoint_unreachable / timeout / provider_error / provider_unknown。
    """

    def __init__(self, reason: str, message: str = "") -> None:
        super().__init__(message or reason)
        self.reason = reason
        self.message = message or reason


def _timeout() -> float:
    """出网超时（秒）。默认 15s：SMTP 握手 + 登录 + 投递比短信 HTTP 慢。"""
    return max(1.0, settings.email_send_timeout_ms / 1000)


def _model_now() -> datetime:
    """模型时区（Asia/Shanghai）的当前时间，**带时区信息**。

    实测结论：Tortoise 的 `DatetimeField` 按 `db.tortoise_orm()['timezone']`
    解释读写——**落库取上海墙上钟，读回附 Asia/Shanghai**。因此：
    - 写入的时间必须是上海墙上钟（写 UTC 墙上钟 → 读回即"8 小时前" → 验证码一生成就过期）
    - 比较时传 aware 时间（传 naïve UTC 会被当墙上钟比 → 冷却恒命中 → 锁死 8 小时）
    """
    return datetime.now(MODEL_TZ)


def _as_model_tz(value: datetime) -> datetime:
    """把读回的 datetime 归一到模型时区（naive 值按上海墙上钟解释）。"""
    return value if value.tzinfo is not None else value.replace(tzinfo=MODEL_TZ)


def normalize_email(raw: str) -> str:
    """归一化邮箱。

    只做两件事：去首尾空白 + 域名小写（DNS 大小写不敏感）。
    本地部分（@ 之前）**保持原样**——RFC 5321 规定它大小写敏感，
    强行 lower 会让 `Abc@qq.com` 和 `abc@qq.com` 被当成同一个人的邮箱。
    """
    value = (raw or "").strip()
    if "@" not in value:
        return value.lower()
    local, _, domain = value.rpartition("@")
    return f"{local}@{domain.lower()}"


def is_valid_email(value: str) -> bool:
    return len(value) <= 128 and bool(_EMAIL_RE.match(value))


def mask_email(value: str) -> str:
    """日志脱敏：保留本地部分前两位与完整域名，其余打码。"""
    local, sep, domain = (value or "").partition("@")
    if not sep:
        return "***"
    return f"{local[:2]}***@{domain}"


class _MockProvider:
    """开发/演示通道：只记日志，不下发。验证码由路由层经 `debug_code` 回传。"""

    name = "mock"

    async def send(self, email: str, code: str) -> None:
        log_event(20, "auth.email_mock_send", email=mask_email(email), code=code)


class _SmtpProvider:
    """标准库 SMTP 通道。

    - 465 端口走 `SMTP_SSL`（QQ 邮箱默认，隐式 TLS）
    - 其它端口走 `SMTP` + `starttls()`（587 显式 TLS）
    - 证书一律校验（`create_default_context`），不提供"跳过校验"开关：
      为了内网自签证书而关掉校验，等于把邮件内容交给中间人。
    """

    name = "smtp"

    def _build_message(self, email: str, code: str) -> EmailMessage:
        message = EmailMessage()
        message["Subject"] = "MindOps 验证码"
        message["From"] = formataddr((settings.email_from_name, settings.email_smtp_user))
        message["To"] = email
        ttl_minutes = max(1, settings.email_code_ttl // 60)
        message.set_content(
            "你正在登录或注册 MindOps 智能运维诊断工作台。\n\n"
            f"验证码：{code}\n\n"
            f"验证码 {ttl_minutes} 分钟内有效，请勿转发给他人。\n"
            "如果这不是你本人的操作，忽略本邮件即可。\n"
        )
        return message

    def _send_sync(self, email: str, code: str) -> None:
        """阻塞式投递，由 `send()` 放进线程池执行。"""
        message = self._build_message(email, code)
        host = settings.email_smtp_host
        port = settings.email_smtp_port
        timeout = _timeout()
        context = ssl.create_default_context()

        if port == 465:
            server: smtplib.SMTP = smtplib.SMTP_SSL(host, port, timeout=timeout, context=context)
        else:
            server = smtplib.SMTP(host, port, timeout=timeout)
        try:
            if port != 465:
                server.starttls(context=context)
            server.login(settings.email_smtp_user, settings.email_smtp_password)
            server.send_message(message)
        finally:
            # 退出失败不该盖掉真正的异常（如认证失败），所以单独吞掉。
            try:
                server.quit()
            except Exception:  # noqa: BLE001 - 收尾清理，失败无副作用
                pass

    async def send(self, email: str, code: str) -> None:
        try:
            await asyncio.to_thread(self._send_sync, email, code)
        except smtplib.SMTPAuthenticationError as exc:
            # QQ 邮箱最常见的坑：密码位填了登录密码而不是「授权码」。
            raise EmailSendError(
                "auth_failed",
                f"SMTP 认证失败（{exc.smtp_code}）：请确认 MINDOPS_EMAIL_SMTP_PASSWORD "
                "填的是邮箱「授权码」而不是登录密码。",
            ) from exc
        except smtplib.SMTPRecipientsRefused as exc:
            raise EmailSendError("recipient_refused", f"收件人被 SMTP 拒绝：{exc}") from exc
        except TimeoutError as exc:
            raise EmailSendError("timeout", f"SMTP 超时（{_timeout()}s）") from exc
        except smtplib.SMTPException as exc:
            raise EmailSendError("provider_error", f"SMTP 投递失败：{exc}") from exc
        except OSError as exc:
            # 含 DNS 解析失败、连接被拒、被防火墙丢包
            raise EmailSendError(
                "endpoint_unreachable",
                f"无法连接 {settings.email_smtp_host}:{settings.email_smtp_port}：{exc}",
            ) from exc

    # 说明：Exception 兜底不做。未知异常应当向上冒泡成 500，
    # 而不是被伪装成一个"发送失败"的业务错误码——隐藏 bug 的代价更大。


_PROVIDERS: dict[str, object] = {
    "mock": _MockProvider(),
    "smtp": _SmtpProvider(),
}


async def _dispatch(email: str, code: str) -> None:
    """把验证码交给当前配置的通道下发；未知通道直接报错（不静默降级为不下发）。"""
    provider = _PROVIDERS.get(settings.email_provider)
    if provider is None:
        raise EmailSendError(
            "provider_unknown",
            f"未知邮件通道 {settings.email_provider!r}（可选 {'/'.join(_PROVIDERS)}）",
        )
    await provider.send(email, code)  # type: ignore[attr-defined]


def _generate_code(length: int | None = None) -> str:
    """生成纯数字验证码。mock 模式下固定返回 MOCK_CODE（方便测试与演示）。"""
    length = length or settings.email_code_length
    if settings.email_provider == "mock":
        return MOCK_CODE[:length]
    # 审查 #10：验证码是安全凭据，必须用加密随机源（Mersenne Twister 可预测）
    return "".join(secrets.choice(string.digits) for _ in range(length))


def _hash_code(code: str, salt: str) -> str:
    """SHA256(code + salt)，per-record salt 防彩虹表。"""
    return hashlib.sha256(f"{code}{salt}".encode()).hexdigest()


def _generate_salt() -> str:
    """随机 salt（32 字符 hex）。"""
    # 审查 #10：盐同样改用加密随机源
    return secrets.token_hex(16)


async def send_code(email: str) -> dict:
    """生成、落库并**下发**验证码。

    返回 {"code": 明文, "ttl": 秒, "interval": 秒}。明文 Code 仅供路由层在 mock
    模式下经 `debug_code` 回传；真实通道下路由层不会外泄它。
    - 冷却：`email_send_interval` 秒内重复发送抛 ValueError（路由层转 429）
    - 下发失败：抛 EmailSendError（路由层转 502），并作废刚落库的 pending 记录
    """
    # 冷却基准必须用模型时区 aware 时间——理由见 _model_now()。
    recent = (
        await EmailCode.filter(
            email=email,
            created_at__gte=_model_now() - timedelta(seconds=settings.email_send_interval),
        )
        .order_by("-created_at")
        .first()
    )
    if recent is not None:
        raise ValueError("email_send_interval")

    code = _generate_code()
    salt = _generate_salt()
    # 有效期必须是上海墙上钟（写 UTC 会让读回值等于"8 小时前"，即一生成就过期）。
    expires_at = _model_now() + timedelta(seconds=settings.email_code_ttl)

    # 同一邮箱旧的 pending 记录作废：只保留最新一条可校验，避免多码同时有效。
    await EmailCode.filter(email=email, status="pending").update(status="expired")

    await EmailCode.create(
        email=email, code_hash=_hash_code(code, salt), salt=salt,
        attempts=0, max_attempts=settings.email_max_attempts,
        expires_at=expires_at, status="pending",
    )

    try:
        await _dispatch(email, code)
    except EmailSendError:
        # 没发出去就不能留一条"可被校验的 pending"——否则用户手里没码，界面却在等验证码。
        # 记录保留为 expired：冷却按 created_at 判定，网关故障时不会被重试风暴打穿。
        await EmailCode.filter(email=email, status="pending").update(status="expired")
        log_event(40, "auth.email_send_failed", email=mask_email(email), provider=settings.email_provider)
        raise

    return {"code": code, "ttl": settings.email_code_ttl, "interval": settings.email_send_interval}


async def verify_code(email: str, code: str) -> bool:
    """校验验证码（邮箱注册用）。

    按 email 取最新 pending 记录：
    - 不存在 / 过期 → False（过期时置 expired）
    - attempts >= max_attempts → status=expired，返回 False
    - code 不匹配 → attempts++，返回 False
    - 匹配 → status=used，返回 True
    """
    record = (
        await EmailCode.filter(email=email, status="pending")
        .order_by("-created_at")
        .first()
    )
    if record is None:
        return False

    # 两侧都归一到模型时区（aware）再比，避免 naive/aware 混比或差 8 小时。
    if _as_model_tz(record.expires_at) < _model_now():
        record.status = "expired"
        await record.save()
        return False
    if record.attempts >= record.max_attempts:
        record.status = "expired"
        await record.save()
        return False

    if not hmac.compare_digest(_hash_code(code, record.salt), record.code_hash):
        # 审查 #27：F 表达式原子自增，并发验证不再依赖读改写
        await EmailCode.filter(id=record.id).update(attempts=F("attempts") + 1)
        await record.refresh_from_db(fields=["attempts"])
        return False

    record.status = "used"
    await record.save()
    return True


async def clear_expired() -> int:
    """清理过期记录（定期调度）。

    除了已标记 expired 的行，还要清掉"时间到了但没人来验"的 pending ——
    后者不会被 verify_code 触碰，长期只增不减。
    """
    removed = await EmailCode.filter(status="expired").delete()
    removed += await EmailCode.filter(expires_at__lt=_model_now()).delete()
    return removed
