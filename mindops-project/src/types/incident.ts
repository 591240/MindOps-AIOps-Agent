/**
 * 事件详情视图模型（VM）。
 *
 * 为什么需要这层：后端 `GET /incidents/{id}` 返回的是**存储形态**（拆成
 * incident / alerts / steps / evidence / actions 五个数组，工具调用是 JSON 字符串，
 * 没有 agent 展示名、没有相似案例），而详情页三个子组件消费的是**展示形态**。
 * 这里显式声明一次，后端 DTO 与前端组件都以此为准，不再靠隐式推断对齐。
 */

export type IncidentSeverity = "P0" | "P1" | "P2" | "P3";

export type IncidentStatus = "new" | "diagnosing" | "human_needed" | "resolved" | "closed";

/** 根因来源：规则命中 / 知识库检索 / 大模型推理 */
export type RootCauseSource = "rule" | "rag" | "llm";

export type EvidenceSourceType = "rag" | "incident" | "llm-reasoning";

export type StepStatus = "pending" | "in_progress" | "completed" | "failed";

export interface IncidentToolCallVM {
  name: string;
  args: Record<string, unknown>;
  result: string;
}

export interface IncidentEvidenceVM {
  quote: string;
  source_doc: string;
  score: number;
  source_type: EvidenceSourceType;
}

export interface IncidentStepVM {
  id: string;
  /** 短名（Triage / Context / …），用于图标映射 */
  agent: string;
  /** 中文展示名（分诊智能体 / …） */
  agent_name: string;
  status: StepStatus;
  input_summary: string;
  output_summary: string;
  latency_ms: number;
  confidence: number;
  /** 置信度回环次数 */
  retry: number;
  tool_calls: IncidentToolCallVM[];
  evidence: IncidentEvidenceVM[];
}

export interface IncidentActionVM {
  id: string;
  command: string;
  description: string;
  risk: "low" | "medium" | "high";
  /**
   * 后端处置状态机（F-06 §5.4）：pending / approved / skipped。
   * med/low 建议在 F-06 创建时即自动 approved，只有高危命令停留 pending 等人工确认。
   */
  status: string;
  /** 高危命令需要人工确认（前端弹二次确认框） */
  requires_confirm: boolean;
  /**
   * 分级自动处置回执（F-06 扩展）：
   * - auto_executed：low 风险 + 总闸开启，命令已在平台所在机器执行成功
   * - exec_failed：执行了但退出码非 0（或超时）
   * - skipped：不满足自动执行条件（风险复核非 low / 命中黑名单）
   * - null：总闸未开启或旧数据，维持纯建议语义
   */
  exec_status: "auto_executed" | "exec_failed" | "skipped" | null;
  exec_exit_code: number | null;
  exec_output: string | null;
  exec_duration_ms: number | null;
}

export interface SimilarIncidentVM {
  id: string;
  title: string;
  time: string;
  similarity: number;
}

export interface IncidentVM {
  id: string;
  title: string;
  component: string;
  status: IncidentStatus;
  severity: IncidentSeverity;
  created_at: string;
  last_alert_at: string;
  root_cause: string;
  confidence: number;
  source: RootCauseSource;
  /** 仅当该事件已完成知识回灌时存在（F-07） */
  knowledge_injected?: { version: number; chunk_count: number } | null;
  steps: IncidentStepVM[];
  actions: IncidentActionVM[];
  similar_incidents: SimilarIncidentVM[];
}

export const SEVERITIES: readonly IncidentSeverity[] = ["P0", "P1", "P2", "P3"];

export const INCIDENT_STATUSES: readonly IncidentStatus[] = [
  "new",
  "diagnosing",
  "human_needed",
  "resolved",
  "closed",
];

/** Agent 短名 → 中文展示名（与后端 orchestrator 的 agent 标识对齐） */
export const AGENT_LABELS: Record<string, string> = {
  Triage: "分诊智能体",
  Context: "上下文智能体",
  Diagnose: "诊断智能体",
  Remediate: "处置智能体",
  Ticket: "工单智能体",
  Recap: "复盘智能体",
};

/** 收敛后端返回的任意字符串到受控枚举，避免脏数据把组件打崩 */
export function asSeverity(value: string | null | undefined): IncidentSeverity {
  return SEVERITIES.includes(value as IncidentSeverity)
    ? (value as IncidentSeverity)
    : "P3";
}

export function asIncidentStatus(value: string | null | undefined): IncidentStatus {
  return INCIDENT_STATUSES.includes(value as IncidentStatus)
    ? (value as IncidentStatus)
    : "new";
}

export function asRootCauseSource(value: string | null | undefined): RootCauseSource {
  return value === "rag" || value === "llm" || value === "rule" ? value : "rule";
}
