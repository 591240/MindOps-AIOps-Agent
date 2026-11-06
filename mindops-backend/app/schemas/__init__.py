"""Pydantic DTO（文档 §3 接口契约）。

来源：docs/12-F-01-告警接入专项文档-V1.1.md §3.1~§3.4
"""

from __future__ import annotations

from pydantic import BaseModel, Field


# ---- §3.1 Prometheus Alertmanager 原生格式 ----
class PrometheusAlert(BaseModel):
    status: str | None = None
    labels: dict = Field(default_factory=dict)
    annotations: dict = Field(default_factory=dict)
    startsAt: str | None = None
    endsAt: str | None = None
    fingerprint: str | None = None
    # F-12（30-F-12 §3.7）：可选，携带则该告警可归属到任务；缺失记为「未归属」
    integration_id: int | None = None


class PrometheusWebhook(BaseModel):
    version: str | None = None
    groupKey: str | None = None
    status: str | None = None
    receiver: str | None = None
    alerts: list[PrometheusAlert]


# ---- §3.2 应用主动上报 ----
class AlertReportIn(BaseModel):
    source: str | None = "app"
    component: str | None = None
    severity: str | None = None
    title: str
    # 审查 #9：detail 落 TEXT（64KB），schema 层限长防超大值打到 DB 500
    detail: str | None = Field(default=None, max_length=65535)
    labels: dict | None = None      # F-12：可选，用于任务归属
    integration_id: int | None = None      # F-12：可选，用于任务归属


# ---- §3.3 手动注入 ----
class ManualAlertIn(BaseModel):
    component: str | None = None
    severity: str | None = None
    title: str
    detail: str | None = None
    trigger_pipeline: bool = True
    integration_id: int | None = None      # F-12：可选，用于任务归属


# ---- 统一响应（§3.1 响应语义表）----
class AlertIngestOut(BaseModel):
    accepted: int = 0
    incident_id: int | None = None
    deduped: bool = False
    alert_ids: list[int] = Field(default_factory=list)


# ---- §3.3 manual 响应 ----
class ManualAlertOut(BaseModel):
    alert_id: int
    incident_id: int | None = None
    pipeline_triggered: bool = True


# ---- §3.4 最近告警 ----
class AlertItem(BaseModel):
    id: int
    source: str
    component: str
    severity: str
    title: str
    fingerprint: str
    incident_id: int | None = None
    # F-12：来源被监控目标 + 归属任务名（仅 active 任务），供告警看板「归属任务」列
    integration_id: int | None = None
    task_names: list[str] = Field(default_factory=list)
    # 第二轮修复：告警看板的「状态」「去重」两列原先无后端数据源，只能前端造假。
    # incident_status = 该告警所属事件的诊断状态；未聚类（incident_id 为空）时为 None。
    # dedup_count    = 同指纹命中次数（含首次入库），真实持久化在 alerts.dedup_count。
    incident_status: str | None = None
    dedup_count: int = 1
    last_seen_at: str | None = None
    created_at: str


class RecentOut(BaseModel):
    items: list[AlertItem] = Field(default_factory=list)
    total: int = 0


class IncidentItem(BaseModel):
    id: int
    title: str
    component: str
    severity: str
    status: str
    alert_count: int
    last_seen_at: str
    root_cause: str | None = None
    root_cause_confidence: float | None = None
    created_at: str


class IncidentsOut(BaseModel):
    items: list[IncidentItem] = Field(default_factory=list)
    total: int
    page: int
    size: int


class ReclusterIn(BaseModel):
    component: str | None = None
    since: str | None = None
    dry_run: bool = True
    apply: bool = False


class DiagnoseIn(BaseModel):
    force: bool = False


# ---- F-04 §3.1 / §3.2：回环参数查询与调参 ----
class ConfidencePatch(BaseModel):
    """PUT /config/confidence 请求体（§3.2 白名单 4 字段）。"""

    conf_threshold: float | None = None
    max_retry: int | None = None
    reasoning_escalation: bool | None = None
    cross_component_fallback: bool | None = None


class ExpandRecall(BaseModel):
    """expand_recall 参数组：MVP 固定，仅 GET 可读（§3.2）。"""

    top_k_base: int
    top_k_loop: int
    sim_floor_base: float
    sim_floor_loop: float


class ConfidenceConfigOut(BaseModel):
    """GET /config/confidence 响应（§3.1）。"""

    conf_threshold: float
    max_retry: int
    expand_recall: ExpandRecall
    reasoning_escalation: bool
    cross_component_fallback: bool


class ConfidencePatchOut(BaseModel):
    """PUT /config/confidence 响应（§3.2）。"""

    conf_threshold: float
    max_retry: int
    updated_by: str
    note: str = "仅对新诊断 run 生效"


# ---- F-04 §3.5：回环审计明细 ----
class LoopAuditItem(BaseModel):
    run_id: str
    retry_index: int
    strategy: str
    query_before: str | None = None
    query_after: str | None = None
    recall_top_k: int | None = None
    sim_floor: float | None = None
    component_scope: str | None = None
    kb_hits_before: int | None = None
    kb_hits_after: int | None = None
    model_id: int | None = None
    role: str
    conf_before: float | None = None
    conf_after: float | None = None
    escalated: bool = False
    created_at: str


class LoopAuditsOut(BaseModel):
    incident_id: int
    items: list[LoopAuditItem] = Field(default_factory=list)


class ConfirmIn(BaseModel):
    action_id: int
    decision: str = Field(pattern="^(approve|reject)$")
    actual_cause: str | None = Field(default=None, max_length=255)
    correct_actions: str | None = None


# ---- F-06 §3.2 / §3.3 / §3.5 / §5.5 ----
class EvidenceItem(BaseModel):
    """证据条目；run_id 由 step_id 反查，供前端按诊断轮次分组（V1.1 §8.1）。"""

    id: int
    step_id: int | None = None
    run_id: str | None = None
    chunk_id: int | None = None
    quote: str
    source_doc: str
    score: float | None = None
    created_at: str


class EvidenceListOut(BaseModel):
    incident_id: int
    items: list[EvidenceItem] = Field(default_factory=list)


class ActionItem(BaseModel):
    id: int
    command: str
    description: str | None = None
    risk_level: str
    requires_confirm: bool
    status: str
    confirmed_by: int | None = None
    created_at: str


class ActionListOut(BaseModel):
    incident_id: int
    items: list[ActionItem] = Field(default_factory=list)


class CloseIn(BaseModel):
    """§3.5 显式关闭事件（reject 不再连带关闭，V1.1）。"""

    reason: str | None = Field(default=None, max_length=64)
    actual_cause: str | None = Field(default=None, max_length=255)
    correct_actions: str | None = None


class FeedbackIn(BaseModel):
    """§5.5 resolved 态补录：只补根因/处置，不改状态、不 reopen action。"""

    actual_cause: str | None = Field(default=None, max_length=255)
    correct_actions: str | None = None


# ---- F-09 模型管理（23-F-09 V1.1 §3；保存即测试）----
class LlmProviderIn(BaseModel):
    """POST /llm/providers 请求体（§3.3）。"""

    name: str = Field(min_length=1, max_length=64)
    provider_type: str = Field(min_length=1, max_length=32)
    base_url: str = Field(min_length=1, max_length=255)
    model_name: str = Field(min_length=1, max_length=64)
    api_key: str  # 明文仅存在于本请求，落库即加密（§5.5）
    role: str = Field(default="chat", pattern="^(chat|reasoning|embedding)$")
    is_default: bool = False
    temperature: float = Field(default=0.2, ge=0.0, le=2.0)
    max_tokens: int = Field(default=4096, ge=1, le=131072)


class LlmProviderPatch(BaseModel):
    """PUT /llm/providers/{id} 请求体（§3.5；全部字段可选，留空即不改）。"""

    name: str | None = Field(default=None, min_length=1, max_length=64)
    model_name: str | None = Field(default=None, min_length=1, max_length=64)
    base_url: str | None = Field(default=None, min_length=1, max_length=255)
    role: str | None = Field(default=None, pattern="^(chat|reasoning)$")
    api_key: str | None = None  # 提交明文则覆盖加密；留空=不更换（FR-4）
    status: str | None = Field(default=None, pattern="^(active|disabled|error)$")
    is_default: bool | None = None
    temperature: float | None = Field(default=None, ge=0.0, le=2.0)
    max_tokens: int | None = Field(default=None, ge=1, le=131072)


class LlmTestIn(BaseModel):
    """POST /llm/test：保存前连通测试（不落库，明文 Key 只存在于本次请求）。"""

    provider_type: str = Field(min_length=1, max_length=32)
    base_url: str = Field(min_length=1, max_length=255)
    model_name: str = Field(min_length=1, max_length=64)
    api_key: str = ""
    role: str = Field(default="chat", pattern="^(chat|reasoning)$")


# ---- F-11 集成管理（27-F-11 §7.4）----
class IntegrationIn(BaseModel):
    """POST /integrations 请求体。"""
    name: str = Field(min_length=1, max_length=64)
    type: str = Field(default="prometheus", max_length=32)
    base_url: str = Field(min_length=1, max_length=255)
    scrape_config: str = Field(default="", max_length=128)


class IntegrationPatch(BaseModel):
    """PUT /integrations/{id} 请求体（全部可选，留空即不改）。"""

    name: str | None = Field(default=None, max_length=64)
    base_url: str | None = Field(default=None, max_length=255)
    scrape_config: str | None = Field(default=None, max_length=128)
    status: str | None = None  # 仅接受 active / disabled


class IntegrationTestIn(BaseModel):
    """POST /integrations/test：新增前的连通性探测（不落库）。"""

    type: str = Field(default="prometheus", max_length=32)
    base_url: str = Field(min_length=1, max_length=255)
    scrape_config: str = Field(default="", max_length=128)


# ---- F-12 监控任务（30-F-12 §7.4）----
class TaskCreate(BaseModel):
    """POST /tasks 请求体。target_ids 允许为空（FR-6 允许空任务）。

    注意 name 不设 min_length：空名/纯空白名交由路由层返回契约约定的
    422 EMPTY_NAME（30-F-12 §3.2），否则会被 Pydantic 抢先返回通用校验错误。
    """

    name: str = Field(max_length=64)
    description: str = Field(default="", max_length=255)
    target_ids: list[int] = Field(default_factory=list)


class TaskUpdate(BaseModel):
    """PUT /tasks/{id} 请求体（全部可选，留空即不改）。

    status 仅接受 active / paused / archived；archived 为终态（服务端守卫）。
    """

    name: str | None = Field(default=None, max_length=64)
    description: str | None = Field(default=None, max_length=255)
    status: str | None = None


class TargetBindIn(BaseModel):
    """POST /tasks/{id}/targets 请求体。"""

    integration_id: int = Field(ge=1)
