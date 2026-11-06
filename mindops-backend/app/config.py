"""全局配置（pydantic-settings）。

来源：docs/6-MVP技术实现方案.md §2.1（app/config.py pydantic-settings 读取环境变量）
      docs/12-F-01-告警接入专项文档-V1.1.md §2.3 / §6.2 / §6.3（限流、白名单、体积限制）

所有阈值均以 F-01 文档为准：
  - 全局 100 QPS、webhook 单 IP 30 QPS（§2.2）
  - 去重 TTL 1 小时（§6.1）
  - 请求体 ≤ 1MB、单批 ≤ 200 条（§2.3 / §7.2）
  - API 前缀 /api/v1（§9.2 curl 示例 + MVP §2.1 前端 baseURL）
"""

import os
import secrets
from urllib.parse import quote_plus
from datetime import datetime
from functools import lru_cache
from zoneinfo import ZoneInfo

from pydantic import model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# ---- 审查 #12：统一写库时钟 ----
# Tortoise 配置 use_tz=False + timezone="Asia/Shanghai"：auto_now 字段按上海墙上钟
# 落库。手工 new 的 naive datetime 会按**服务器本地时区**解释 —— Docker 默认 UTC
# 部署时两者相差 8 小时（MTTR / recluster since 等比较全部失真）。
# 因此凡手工写库的时间一律用 model_now()，与 auto_now 同口径。
MODEL_TZ = ZoneInfo("Asia/Shanghai")


def model_now() -> datetime:
    """上海墙上钟（naive）——与 Tortoise auto_now 的落库口径一致。"""
    return datetime.now(MODEL_TZ).replace(tzinfo=None)

# 已知不安全的内置默认值（P0-4）：命中即视为"未配置"
_INSECURE_JWT_SECRETS = {
    "",
    "dev-secret-change-me-in-production",
    "change-me",
    "secret",
}
_WEAK_DB_PASSWORDS = {"", "123456", "root", "password", "admin", "mysql", "test"}

# 启动时收集的安全告警（debug=True 只告警，debug=False 直接拒绝启动）
_STARTUP_SECURITY_ERRORS: list[str] = []


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env", env_prefix="MINDOPS_", extra="ignore"
    )

    app_name: str = "MindOps"
    api_prefix: str = "/api/v1"
    # P0-4：默认关闭 debug（避免 /docs 暴露交互式 traceback）
    debug: bool = False

    # ---- MySQL ----
    mysql_host: str = "127.0.0.1"
    mysql_port: int = 3306
    mysql_user: str = "root"
    # P0-4：留空即"未配置"，生产环境启动期拒绝启动
    mysql_password: str = ""
    mysql_db: str = "mindops"

    # ---- 向量库（可选）----
    # 留空 = 不做向量检索，检索自动降级为 BM25 + 已结事件召回。
    # 支持 http(s):// 远端 Milvus，或本地文件路径（milvus-lite）。
    # ⚠️ 本地路径必须是纯 ASCII：Windows 上 faiss 用窄字符 API 写索引，
    #    含中文的路径会让 LoadCollection 必然失败（启动校验会拦这一项）。
    milvus_uri: str = ""

    # ---- JWT ----
    # P0-4：不再内置可被伪造的固定密钥。debug 模式下为空则临时生成（重启失效），
    #       生产（debug=False）为空则拒绝启动。
    jwt_secret: str = ""
    jwt_algorithm: str = "HS256"
    jwt_expire_minutes: int = 120

    # ---- F-01 阈值（§2.2 / §2.3）----
    rate_global_qps: int = 100      # 全模块 100 QPS
    rate_ip_qps: int = 30           # webhook 单 IP 30 QPS
    dedup_ttl_seconds: int = 3600   # 1 小时去重窗口
    # 全局请求体上限。知识入库允许 1MB 正文，JSON 转义后可能膨胀数倍，
    # 1MB 的全局上限会让"合法内容"先撞 413；webhook 另有更严格的独立上限。
    max_body_bytes: int = 8 * 1024 * 1024   # 8MB
    webhook_max_body_bytes: int = 1024 * 1024   # webhook 仍限 1MB
    max_batch_size: int = 200           # 单批最多 200 条

    # ---- M1 同步聚类桩开关（§6.4）----
    m1_stub_triage: bool = False

    # ---- F-03 六智能体编排 ----
    diagnosis_max_concurrent_runs: int = 8
    diagnosis_queue_timeout_seconds: int = 600
    diagnosis_stale_seconds: int = 1800
    # 聚类生成/合并事件后自动触发 F-03 诊断（关掉则仅手动 POST /incidents/{id}/diagnose）
    auto_diagnose_on_cluster: bool = True
    # 自动诊断防抖静默期：新告警持续并入会重置计时，静默满 N 秒才真正开跑
    diagnosis_debounce_seconds: int = 30

    # ---- F-04 置信度回环（§3.2 env 基线；DB runtime_configs 覆盖优先）----
    conf_threshold: float = 0.6            # CONF_THRESHOLD
    max_retry: int = 2                     # MAX_RETRY，0 表示关闭回环
    reasoning_escalation: bool = True      # retry_count==1 时升级 reasoning 角色
    cross_component_fallback: bool = True  # 第 2 次回环跨组件检索
    # expand_recall 参数组（MVP 固定，仅 GET 可读，§3.2 白名单）
    loop_top_k_base: int = 5
    loop_top_k_loop: int = 10
    loop_sim_floor_base: float = 0.7
    loop_sim_floor_loop: float = 0.5

    # ---- P0-2 / P0-3 诊断质量收口（失败模式埋点 + 证据加权置信度）----
    # 关闭后回到"完全采信模型自报置信度"的旧行为（仅用于对照实验）。
    confidence_scoring_enabled: bool = True
    # 证据门限压制：零证据/单条证据时，自报置信度的上限。
    # 单条上限刻意高于默认阈值（0.6），只压制"零证据却高置信"的病态路径，
    # 不改变"一条证据 + 合理自评即可通过"的既有行为。
    conf_cap_zero_evidence: float = 0.35
    conf_cap_single_evidence: float = 0.65

    # ---- CORS（P0-4：禁止 "*" 与 allow_credentials 同时生效）----
    cors_origins: str = "http://localhost:3015,http://127.0.0.1:3015"
    cors_allow_credentials: bool = True

    # ---- 可观测性 ----
    # /metrics 在 debug 档直接开放；生产未设置 token 时只允许回环/私网直连。
    # 经反向代理暴露给 Prometheus 时设置该值，抓取端带 X-Metrics-Token 头。
    metrics_token: str = ""

    # ---- SSE 短票 ----
    # EventSource 不能带 Authorization 头，前端先换一张短票再订阅，
    # 避免把长效 access token 写进 URL 和访问日志。
    stream_ticket_ttl_seconds: int = 300

    # ---- 认证（P0-1）----
    # 是否允许"登录即注册"的 demo 行为。生产必须关闭：
    # 关闭后未知账号登录一律 401，不会创建用户，也不会按用户名推导角色。
    auth_allow_auto_register: bool = False

    # ---- F-09 模型接入（vault 主密钥；未配置时 BYO-Key 保存/解密报 vault_locked）----
    llm_vault_key: str = ""

    # ---- F-11 集成管理（§4.3）----
    integration_allowed_cidrs: str = "10.0.0.0/8,172.16.0.0/12,192.168.0.0/16,127.0.0.0/8"
    integration_probe_timeout_ms: int = 5000

    # ---- 分级自动处置（F-06 扩展）----
    # 总闸默认关：True 时才允许「命中规则且命令被风险库判为 low」的动作自动执行。
    # medium/high 永远只生成建议等人工确认；代码级高危黑名单二次兜底（见 executor.py）。
    # 执行发生在平台所在机器（私有化同机部署形态），生产部署请确认接受这一点再打开。
    auto_remediate: bool = False
    auto_remediate_timeout_s: int = 10
    auto_remediate_max_per_incident: int = 3

    # ---- F-10 验证码登录（密码锁定 / Redis）----
    # ---- 邮箱注册与登录（邮箱验证码 + 自助注册）----
    # register_enabled=False 时 /auth/register 直接 403，等于关闭自助注册入口。
    register_enabled: bool = True
    # 注册角色收口：自助注册一律 engineer，只有本邮箱（规范化后比较）拿到 admin。
    # 留空 = 没人能注册成管理员，建管理员只能靠 MINDOPS_SEED_DEMO_USERS=1 播种。
    bootstrap_admin_email: str = ""
    # F-01 webhook 共享密钥（请求头 X-Webhook-Token）。留空时只剩内网 IP 白名单，
    # 而反代之后白名单可被客户端自填的 X-Forwarded-For 绕过——生产必须设置。
    webhook_token: str = ""
    # 验证码下发按来源 IP 的令牌桶（每分钟条数）。"同目标 60s 冷却"挡不住
    # 随机目标 + 固定 IP 的轰炸，那会同时打爆 SMTP 配额和 email_codes 表。
    code_send_ip_per_min: int = 4
    email_provider: str = "mock"                         # mock（不下发，返回 debug_code）/ smtp
    email_code_ttl: int = 300                            # 验证码有效期（秒）
    email_send_interval: int = 60                        # 同邮箱两次发送间隔（秒）
    email_max_attempts: int = 5                          # 单条验证码最大验证次数
    email_code_length: int = 6                           # 验证码位数
    email_send_timeout_ms: int = 15000                   # SMTP 出网超时（毫秒）
    # smtp 通道凭据：QQ 邮箱用 smtp.qq.com:465(SSL)，密码填「授权码」而非 QQ 密码
    email_smtp_host: str = "smtp.qq.com"
    email_smtp_port: int = 465                           # 465=SSL / 587=STARTTLS
    email_smtp_user: str = ""                            # 完整邮箱地址，如 xxx@qq.com
    email_smtp_password: str = ""                        # QQ 邮箱「授权码」（16 位）
    email_from_name: str = "MindOps"                     # 收件人看到的发件人名称
    # Redis 是可选组件：留空（默认）走内存等价实现，单副本部署完全够用；
    # 本机/内网起了 Redis 后填 redis://host:6379/0 即可启用分布式去重、
    # 跨副本锁与 SSE 广播（见 app/alerts/dedup.py）。
    # ⚠️ 不要把默认值预设成本机 Redis：Windows 上 6379 端口常被 VMware NAT
    #    （vmnat.exe）占用，默认指向它会造成连接超时并拖慢启动。
    redis_url: str = ""
    password_lockout_seconds: int = 1800                 # 密码错误锁定时间（秒，30 分钟）
    password_lockout_threshold: int = 5                  # 密码连续错误次数阈值

    @property
    def database_url(self) -> str:
        return (
            # 审查 #16：密码做 URL 编码，含 @ : / # 等字符时不再破坏连接串
            f"mysql://{self.mysql_user}:{quote_plus(self.mysql_password)}"
            f"@{self.mysql_host}:{self.mysql_port}/{self.mysql_db}"
        )

    @model_validator(mode="after")
    def _apply_security_defaults(self) -> "Settings":
        """P0-4：debug 模式下为未配置的安全项兜底（并告警），生产模式留给启动期拦截。"""
        if self.jwt_secret.strip() in _INSECURE_JWT_SECRETS and self.debug:
            # 开发环境兜底：随机密钥，重启即失效（token 全部过期，符合预期）
            object.__setattr__(self, "jwt_secret", secrets.token_urlsafe(48))
            _STARTUP_SECURITY_ERRORS.append(
                "MINDOPS_JWT_SECRET 未配置，已生成临时随机密钥（重启后 token 全部失效）。"
                "生产环境必须显式配置。"
            )
        return self

    @property
    def cors_origin_list(self) -> list[str]:
        return [o.strip() for o in self.cors_origins.split(",") if o.strip()]


@lru_cache
def get_settings() -> Settings:
    s = Settings()
    # 让 _STARTUP_SECURITY_ERRORS 在首次构造时就填充
    return s


def _collect_startup_issues(s: Settings) -> list[str]:
    """汇总需要在启动期拦截的配置问题（P0-4）。"""
    issues: list[str] = list(_STARTUP_SECURITY_ERRORS)

    if s.jwt_secret.strip() in _INSECURE_JWT_SECRETS:
        issues.append(
            "MINDOPS_JWT_SECRET 未配置或仍为内置占位值（JWT 可被伪造）。"
            "请设置至少 32 位随机字符串。"
        )

    if s.mysql_password.strip() in _WEAK_DB_PASSWORDS:
        issues.append(
            "MINDOPS_MYSQL_PASSWORD 为空或为弱口令（内置默认值）。请设置强密码。"
        )

    origins = s.cors_origin_list
    if "*" in origins:
        if s.cors_allow_credentials:
            issues.append(
                "MINDOPS_CORS_ORIGINS 为 '*' 且 cors_allow_credentials=True："
                "浏览器会拒绝该组合，且等价于向任意站点反射凭证。"
                "请改为显式来源清单。"
            )
        else:
            issues.append("MINDOPS_CORS_ORIGINS 为 '*'，建议改为显式来源清单。")

    # 生产仍保留 localhost 来源 = 前端走公网域名时浏览器直接拒绝跨域，
    # 表现为登录页一直"网络错误"。这个配置错误不会被浏览器报成 CORS 以外的东西，
    # 线上极难定位，因此在启动期直接拦下。
    if not s.debug:
        loopback_origins = [
            o for o in origins
            if "://localhost" in o or "://127.0.0.1" in o or "://[::1]" in o
        ]
        if loopback_origins:
            issues.append(
                f"MINDOPS_CORS_ORIGINS 仍包含本机来源 {loopback_origins}："
                "公网前端的跨域请求会被浏览器全部拒绝。请改为实际访问域名"
                "（同源反代部署可留空）。"
            )

    if s.auth_allow_auto_register and not s.debug:
        issues.append(
            "MINDOPS_AUTH_ALLOW_AUTO_REGISTER=True 但 MINDOPS_DEBUG=False："
            "生产环境禁止开放注册（P0-1）。"
        )
    # 邮箱通道：配错等于"注册/登录功能静默失效"——用户永远收不到码。
    # 与 LLM 凭据不同，这里在启动期就拦（缺项在运行期无法自愈）。
    if s.email_provider == "smtp":
        missing = [
            name
            for name, value in (
                ("MINDOPS_EMAIL_SMTP_HOST", s.email_smtp_host),
                ("MINDOPS_EMAIL_SMTP_USER", s.email_smtp_user),
                ("MINDOPS_EMAIL_SMTP_PASSWORD", s.email_smtp_password),
            )
            if not value.strip()
        ]
        if missing:
            issues.append(
                f"MINDOPS_EMAIL_PROVIDER=smtp 但缺少 {'、'.join(missing)}："
                "验证码邮件无法下发，邮箱注册必然失败"
                "（QQ 邮箱填 smtp.qq.com:465，密码为「授权码」而非登录密码）。"
            )
    elif s.email_provider == "mock":
        if not s.debug:
            issues.append(
                "MINDOPS_EMAIL_PROVIDER=mock 但 MINDOPS_DEBUG=False："
                "注册验证码不会真正发出，仅经接口回传 debug_code。"
                "生产环境请切换为 smtp。"
            )
    else:
        issues.append(
            f"MINDOPS_EMAIL_PROVIDER={s.email_provider} 不是受支持的通道（可选 mock / smtp）。"
        )

    # 自助注册的角色收口：注册默认 engineer，admin 只发给 bootstrap 邮箱。
    # 开着注册但没指定管理员邮箱 = 库里永远不会出现 admin，系统设置页无人可进。
    if (s.register_enabled and not s.debug
            and not s.bootstrap_admin_email.strip()
            and os.environ.get("MINDOPS_SEED_DEMO_USERS") != "1"):
        issues.append(
            "MINDOPS_REGISTER_ENABLED=True 但未设置 MINDOPS_BOOTSTRAP_ADMIN_EMAIL："
            "自助注册只会创建 engineer 账号，系统内不会有任何管理员（/settings 无人可访问）。"
            "请指定管理员邮箱，或关闭注册并设 MINDOPS_SEED_DEMO_USERS=1 播种。"
        )

    if not s.webhook_token.strip() and not s.debug:
        issues.append(
            "MINDOPS_WEBHOOK_TOKEN 为空：/alerts/webhook 只剩内网 IP 白名单这一道门。"
            "经反向代理后所有请求的直连 IP 都是代理地址，任何外网调用方都能伪造告警灌库"
            "并触发自动诊断（顺带烧掉模型额度）。请设置共享密钥，并在 Alertmanager 的 "
            "webhook_configs[].http_config.authorization.credentials 填同值。"
        )

    if s.integration_allowed_cidrs.strip() == "0.0.0.0/0":
        issues.append(
            "MINDOPS_INTEGRATION_ALLOWED_CIDRS 为 0.0.0.0/0，SSRF 白名单形同虚设。"
        )

    # Milvus 本地数据路径含中文/非 ASCII：Windows 上 faiss 用窄字符 API 写索引
    # 文件，中文路径会让 LoadCollection 必然失败。失败表现是「向量检索静默降级
    # 成 BM25 + 知识库上传 500 INGEST_FAILED」——接口层看不出是路径问题，极难
    # 排查。这里在启动期直接点出来，避免重演「以为配了 Milvus 就没事」。
    milvus_uri = (s.milvus_uri or "").strip()
    if milvus_uri and not milvus_uri.startswith(("http://", "https://")):
        milvus_path = os.path.abspath(milvus_uri)
        if not milvus_path.isascii():
            issues.append(
                f"MINDOPS_MILVUS_URI 指向的本地路径含非 ASCII 字符（{milvus_path}）："
                "Windows 上 faiss 无法在该路径写入索引文件，向量检索会静默降级为 BM25、"
                "知识库上传报 500 INGEST_FAILED。请改用纯英文绝对路径"
                "（如 E:/mindops-data/milvus_lite.db）。"
            )

    return issues


def validate_for_startup() -> None:
    """启动期安全校验（P0-4）。

    - debug=True（开发/测试）：仅打印告警，不阻断；
    - debug=False（生产）：任一问题存在即拒绝启动。
    """
    s = get_settings()
    issues = _collect_startup_issues(s)
    if not issues:
        return

    header = f"[config] 检测到 {len(issues)} 项不安全的配置："
    for i, msg in enumerate(issues, 1):
        header += f"\n  {i}. {msg}"

    if s.debug:
        # 开发/测试环境：告警即可
        print(header + "\n[config] debug=True，继续启动（生产环境将拒绝启动）。")
        return

    raise RuntimeError(
        header + "\n[config] 生产环境（debug=False）拒绝启动，请修复上述配置后重试。"
    )


settings = get_settings()
