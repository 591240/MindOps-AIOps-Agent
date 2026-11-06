"""Tortoise-ORM 模型定义（F-01 范围）。

来源：docs/12-F-01-告警接入专项文档-V1.1.md §4.1 / §4.2 / §4.4
      docs/6-MVP技术实现方案.md §2.1（app/models/ 用 Tortoise 模型）

注意：Tortoise 的 generate_schemas 无法表达 CHECK 约束 / MEDIUMTEXT / CHAR(40)，
      因此建表 DDL 走 app/db.py 中的原生 SQL（与文档 §4.2 完全一致），
      这里只做 ORM 映射。字段定义与 DDL 严格对齐。
"""

from tortoise import Model, fields


class Alert(Model):
    """原始告警接入表（§4.2）。"""

    id = fields.BigIntField(pk=True, description="自增 ID")
    source = fields.CharField(max_length=32, description="prometheus / app / demo")
    component = fields.CharField(max_length=64, description="组件：redis / mysql / ...")
    severity = fields.CharField(max_length=8, description="P0 / P1 / P2 / P3")
    title = fields.CharField(max_length=255, description="归一化后标题")
    detail = fields.TextField(description="归一化后详情")
    fingerprint = fields.CharField(max_length=40, description="SHA1 hex(40)")
    incident_id = fields.IntField(null=True, description="聚合事件 ID")
    integration_id = fields.IntField(
        null=True, description="来源被监控目标（F-12 任务归属依据，见 30-F-12 §5.3）"
    )
    raw_payload = fields.TextField(null=True, description="原始 JSON（溯源）")
    labels_json = fields.JSONField(null=True, description="labels 透传")
    dedup_count = fields.IntField(
        default=1, description="同指纹命中次数（含首次入库；前端「去重」列数据源）"
    )
    last_seen_at = fields.DatetimeField(auto_now_add=True, description="最近命中时间")
    created_at = fields.DatetimeField(auto_now_add=True, description="首次入库时间")

    class Meta:
        table = "alerts"
        indexes = [
            ("fingerprint",),      # idx_fingerprint
            ("incident_id",),      # idx_incident
            ("created_at",),       # idx_created
            ("severity", "component"),  # idx_sev_comp 联合索引（最左前缀覆盖 component）
        ]


class Incident(Model):
    """规则聚类事件（F-02 §4.1）。"""

    id = fields.IntField(pk=True)
    fingerprint = fields.CharField(max_length=40, null=True)
    title = fields.CharField(max_length=255)
    component = fields.CharField(max_length=64, default="")
    severity = fields.CharField(max_length=8, default="P3")
    status = fields.CharField(max_length=16, default="new")
    last_seen_at = fields.DatetimeField(auto_now_add=True)
    root_cause = fields.CharField(max_length=255, null=True)
    root_cause_confidence = fields.FloatField(null=True)
    root_cause_source = fields.CharField(max_length=16, null=True)
    actual_cause = fields.CharField(max_length=255, null=True)
    resolved_at = fields.DatetimeField(null=True)
    confirmed_by = fields.IntField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "incidents"
        indexes = [("component", "status", "last_seen_at"), ("created_at",), ("fingerprint",)]


class IncidentReclusterAudit(Model):
    """重聚类迁移审计记录（F-02 §3.3）。"""

    id = fields.BigIntField(pk=True)
    actor = fields.CharField(max_length=64)
    phase = fields.CharField(max_length=16)
    old_incident_id = fields.IntField()
    new_incident_id = fields.IntField()
    details = fields.JSONField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "incident_recluster_audits"
        indexes = [("old_incident_id",), ("new_incident_id",), ("created_at",)]


class IncidentStep(Model):
    """F-03 每个 Agent 的结构化诊断轨迹。"""

    id = fields.IntField(pk=True)
    run_id = fields.CharField(max_length=32, default="")
    incident_id = fields.IntField()
    step = fields.CharField(max_length=16)
    agent = fields.CharField(max_length=32)
    input_summary = fields.TextField(null=True)
    output_summary = fields.TextField(null=True)
    tool_calls = fields.TextField(null=True)
    latency_ms = fields.IntField(null=True)
    confidence = fields.FloatField(null=True)
    retry_count = fields.IntField(default=0)
    model_id = fields.IntField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "incident_steps"
        indexes = [("incident_id",), ("run_id",)]


class DiagnosisRun(Model):
    """F-03 异步诊断运行记录和崩溃恢复锚点。"""

    run_id = fields.CharField(max_length=32, pk=True)
    incident_id = fields.IntField()
    status = fields.CharField(max_length=16)
    heartbeat_at = fields.DatetimeField(null=True)
    started_at = fields.DatetimeField(null=True)
    finished_at = fields.DatetimeField(null=True)
    triggered_by = fields.IntField(null=True)

    class Meta:
        table = "diagnosis_runs"
        indexes = [("incident_id", "status")]


class Evidence(Model):
    id = fields.IntField(pk=True)
    incident_id = fields.IntField()
    step_id = fields.IntField(null=True)
    chunk_id = fields.IntField(null=True)
    quote = fields.TextField()
    source_doc = fields.CharField(max_length=255)
    score = fields.FloatField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "evidence"
        indexes = [("incident_id",), ("step_id",)]


class RemediationAction(Model):
    id = fields.IntField(pk=True)
    incident_id = fields.IntField()
    command = fields.TextField()
    description = fields.CharField(max_length=255, null=True)
    risk_level = fields.CharField(max_length=8)
    requires_confirm = fields.BooleanField(default=True)
    status = fields.CharField(max_length=16, default="pending")
    confirmed_by = fields.IntField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    # 分级自动处置回执（F-06 扩展）：low+approved 且总闸开启时由 executor 写入
    exec_status = fields.CharField(max_length=16, null=True)  # auto_executed / exec_failed / skipped
    exec_exit_code = fields.IntField(null=True)
    exec_output = fields.TextField(null=True)
    exec_duration_ms = fields.IntField(null=True)
    executed_at = fields.DatetimeField(null=True)

    class Meta:
        table = "remediation_actions"
        indexes = [("incident_id",), ("status",)]


class AlertFailedQueue(Model):
    """告警接入死信队列（§4.4）。"""

    id = fields.BigIntField(pk=True)
    source = fields.CharField(max_length=32)
    payload = fields.TextField(description="原始请求体（含 trace_id）")
    error = fields.TextField(description="失败原因与堆栈摘要")
    retry_count = fields.IntField(default=0)
    next_retry_at = fields.DatetimeField()
    resolved = fields.BooleanField(default=False)
    created_at = fields.DatetimeField(auto_now_add=True)
    resolved_at = fields.DatetimeField(null=True)

    class Meta:
        table = "alerts_failed_queue"
        indexes = [("resolved", "next_retry_at")]


class RuntimeConfig(Model):
    """运行时参数覆盖表（F-04 §4.4）。

    通用设计：DB 命中则用 DB 值，否则回退 env 基线（§3.2）；
    后续 F-05 / F-09 的运行时调参可复用本表，避免一参一表。
    """

    key = fields.CharField(max_length=64, pk=True)
    value = fields.CharField(max_length=255)
    updated_by = fields.CharField(max_length=64, null=True)
    updated_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "runtime_configs"


class LoopAudit(Model):
    """置信度回环审计表（F-04 §4.3）。

    `conf_after` 在补查检索时为 NULL，Diagnose 产出后由
    `confidence_loop.backfill_conf_after` 回填（§7.3 两阶段落库）。
    """

    id = fields.IntField(pk=True)
    run_id = fields.CharField(max_length=32)
    incident_id = fields.IntField()
    retry_index = fields.IntField()
    strategy = fields.CharField(max_length=64)
    query_before = fields.TextField(null=True)
    query_after = fields.TextField(null=True)
    recall_top_k = fields.IntField(null=True)
    sim_floor = fields.FloatField(null=True)
    component_scope = fields.CharField(max_length=64, null=True)
    kb_hits_before = fields.IntField(null=True)
    kb_hits_after = fields.IntField(null=True)
    model_id = fields.IntField(null=True)
    role = fields.CharField(max_length=16, default="chat")
    conf_before = fields.FloatField(null=True)
    conf_after = fields.FloatField(null=True)
    escalated = fields.BooleanField(default=False)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "loop_audits"
        indexes = [("run_id",), ("incident_id",)]


class LlmProvider(Model):
    """模型配置表（F-09，PRD §3.11；F-04 只消费 id + role）。

    V1.0 迁移：原最小骨架（id/name/role/enabled）扩展为全量表；
    `enabled` 布尔由 `status` 取代（active/disabled/error），见 db.py 迁移块。
    `api_key_enc` 为 Fernet 密文，永不出 ORM 序列化（F-09 §5.5 永不回显）；
    NULL = 系统种子记录，调用走桩路径（gateway.complete 兜底层）。
    """

    id = fields.IntField(pk=True)
    user_id = fields.IntField(null=True)  # MVP 全局共享模型池，列预留（F-09 §1.4）
    name = fields.CharField(max_length=64)
    provider_type = fields.CharField(max_length=32, default="system")
    base_url = fields.CharField(max_length=255, default="")
    model_name = fields.CharField(max_length=64, default="")
    api_key_enc = fields.TextField(null=True)
    api_key_masked = fields.CharField(max_length=32, default="")
    role = fields.CharField(max_length=16, default="chat")  # chat / reasoning / embedding
    is_default = fields.BooleanField(default=False)  # 同 role 唯一（事务内互斥）
    status = fields.CharField(max_length=16, default="active")  # active / disabled / error
    temperature = fields.FloatField(default=0.2)
    max_tokens = fields.IntField(default=4096)
    timeout_ms = fields.IntField(default=60000)
    last_test_at = fields.DatetimeField(null=True)
    last_test_latency_ms = fields.IntField(null=True)
    last_test_status = fields.CharField(max_length=32, null=True)  # success / failed:<原因码>
    extra_params = fields.TextField(null=True)
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "llm_providers"
        indexes = [("role", "is_default", "status")]


class User(Model):
    """用户表（F-10 V1.1 §4.1）。

    V1.0 骨架（id/username/hashed_password/role）扩展为：
    - phone / phone_verified：联系方式字段（短信登录已移除，保留列以兼容历史数据）；
    - email / email_verified：邮箱验证码注册与登录所需（同样不强制唯一，查重由注册接口显式做，
      避免给已有邮箱的历史行补 UNIQUE 时迁移失败）；
    - status：active / disabled / locked（锁定由密码连续错误触发，§5.4 双轨锁定）；
    - last_login / last_login_method：审计追溯。
    """

    id = fields.IntField(pk=True)
    username = fields.CharField(max_length=64, unique=True)
    hashed_password = fields.CharField(max_length=255)
    role = fields.CharField(max_length=16, default="engineer")  # admin / engineer

    # ---- F-10 扩展 ----
    phone = fields.CharField(max_length=20, null=True, default=None, index=True)
    phone_verified = fields.BooleanField(default=False)
    status = fields.CharField(max_length=16, default="active")  # active / disabled / locked
    last_login = fields.DatetimeField(null=True)
    last_login_method = fields.CharField(max_length=16, null=True)  # password / email

    # ---- 邮箱注册扩展 ----
    email = fields.CharField(max_length=128, null=True, default=None, index=True)
    email_verified = fields.BooleanField(default=False)

    class Meta:
        table = "users"


class EmailCode(Model):
    """邮箱验证码记录（注册 / 登录共用，per-record salt 防彩虹表）。

    code_hash = SHA256(code + salt)；attempts 限爆破；status 状态机
    pending/used/expired。注册与登录共用同一张表与同一套 60s 冷却，
    保证同一邮箱同时只有一条可校验的码。
    """

    id = fields.IntField(pk=True)
    email = fields.CharField(max_length=128, index=True)
    code_hash = fields.CharField(max_length=64)        # SHA256(code + salt)
    salt = fields.CharField(max_length=32)
    attempts = fields.IntField(default=0)               # 已尝试验证次数
    max_attempts = fields.IntField(default=5)
    expires_at = fields.DatetimeField()
    status = fields.CharField(max_length=16, default="pending")  # pending / used / expired
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "email_codes"


class KnowledgeDoc(Model):
    """F-05 知识库文档版本。"""

    id = fields.IntField(pk=True)
    title = fields.CharField(max_length=255)
    content = fields.TextField()
    source_type = fields.CharField(max_length=16)
    version = fields.IntField(default=1)
    chunk_count = fields.IntField(default=0)
    is_latest = fields.BooleanField(default=True)
    created_by = fields.IntField(null=True)
    content_hash = fields.CharField(max_length=40, null=True)
    # F-05 V1.1 B3：文档级组件标签（上传指定或内容推断）。
    # 注意：检索过滤一律以**切片级** knowledge_chunks.component 为准，
    # 此字段仅用于列表展示与粗筛，两者允许不一致。
    component = fields.CharField(max_length=64, default="common")
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)

    class Meta:
        table = "knowledge_docs"
        unique_together = (("title", "version"),)
        indexes = [("title",), ("is_latest",), ("source_type",), ("component",)]


class KnowledgeChunk(Model):
    """F-05 文档切片；文本存 MySQL，向量由 embedding_id 关联。"""

    id = fields.IntField(pk=True)
    doc_id = fields.IntField()
    text = fields.TextField()
    component = fields.CharField(max_length=64, default="common")
    doc_type = fields.CharField(max_length=16)
    chunk_index = fields.IntField()
    chunk_kind = fields.CharField(max_length=16, default="text")
    source_version = fields.IntField(default=1)
    embedding_id = fields.CharField(max_length=64, null=True)
    is_latest = fields.BooleanField(default=True)
    created_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "knowledge_chunks"
        indexes = [("doc_id",), ("component",), ("is_latest",), ("embedding_id",)]


class Integration(Model):
    """F-11 被监控目标（27-F-11 §4.1）。状态四态：pending/active/error/disabled。"""

    id = fields.IntField(pk=True)
    name = fields.CharField(max_length=64)
    type = fields.CharField(max_length=32, default="prometheus")
    base_url = fields.CharField(max_length=255)
    scrape_config = fields.CharField(max_length=128, default="")
    status = fields.CharField(max_length=16, default="pending")
    last_heartbeat = fields.DatetimeField(null=True)
    last_error = fields.CharField(max_length=255, null=True)
    last_latency_ms = fields.IntField(null=True)
    targets_total = fields.IntField(null=True)
    targets_up = fields.IntField(null=True)
    created_by = fields.CharField(max_length=64, default="")
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    disabled_at = fields.DatetimeField(null=True)

    class Meta:
        table = "integrations"
        indexes = [("status", "type")]


class Task(Model):
    """F-12 监控任务（30-F-12 §4.1）。状态三态：active/paused/archived（archived 为终态）。"""

    id = fields.CharField(pk=True, max_length=32, description="task_001 可读 ID")
    name = fields.CharField(max_length=64)
    description = fields.CharField(max_length=255, default="")
    status = fields.CharField(max_length=16, default="active")
    target_count = fields.IntField(default=0, description="冗余计数，绑定/解绑时同事务维护")
    created_by = fields.CharField(max_length=64, default="")
    created_at = fields.DatetimeField(auto_now_add=True)
    updated_at = fields.DatetimeField(auto_now=True)
    archived_at = fields.DatetimeField(null=True)

    class Meta:
        table = "tasks"
        indexes = [("status", "created_at")]


class TaskTarget(Model):
    """F-12 任务 ↔ 被监控目标绑定（N:N，30-F-12 §4.2）。"""

    id = fields.IntField(pk=True)
    task_id = fields.CharField(max_length=32)
    integration_id = fields.IntField()
    added_by = fields.CharField(max_length=64, default="")
    added_at = fields.DatetimeField(auto_now_add=True)

    class Meta:
        table = "task_targets"
        indexes = [("integration_id",)]
