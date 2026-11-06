/** F-02 / F-03 事件接口（对齐 app/routers/incidents.py）。 */

import { api } from "./client";
import {
  AGENT_LABELS,
  asIncidentStatus,
  asRootCauseSource,
  asSeverity,
  type EvidenceSourceType,
  type IncidentVM,
  type StepStatus,
} from "@/types/incident";

export interface IncidentSummaryDTO {
  id: number;
  title: string;
  component: string;
  severity: string;
  status: string;
  last_seen_at: string;
  root_cause: string | null;
  root_cause_confidence: number | null;
  root_cause_source: string | null;
  actual_cause: string | null;
  created_at: string;
}

/** `GET /incidents` 列表项（后端 IncidentItem，**不含** steps / evidence）。 */
export interface IncidentItemDTO {
  id: number;
  title: string;
  component: string;
  severity: string;
  status: string;
  alert_count: number;
  last_seen_at: string;
  root_cause: string | null;
  root_cause_confidence: number | null;
  created_at: string;
}

export interface IncidentListOut {
  items: IncidentItemDTO[];
  total: number;
  page: number;
  size: number;
}

export interface IncidentListQuery {
  status?: string;
  severity?: string;
  component?: string;
  page?: number;
  size?: number;
}

export interface IncidentAlertDTO {
  id: number;
  source: string;
  title: string;
  severity: string;
  created_at: string;
}

export interface IncidentStepDTO {
  id: number;
  run_id: string;
  step: string;
  agent: string;
  input_summary: string | null;
  output_summary: string | null;
  /** JSON 字符串（后端按 TextField 存储） */
  tool_calls: string | null;
  confidence: number | null;
  latency_ms: number | null;
  retry_count: number;
  model_id: number | null;
  created_at: string;
}

export interface IncidentEvidenceDTO {
  id: number;
  step_id: number | null;
  quote: string;
  source_doc: string;
  score: number | null;
}

export interface IncidentActionDTO {
  id: number;
  command: string;
  description: string | null;
  risk_level: string;
  requires_confirm: boolean;
  status: string;
  confirmed_by: number | null;
  exec_status: "auto_executed" | "exec_failed" | "skipped" | null;
  exec_exit_code: number | null;
  exec_output: string | null;
  exec_duration_ms: number | null;
  executed_at: string | null;
}

export interface IncidentDetailDTO {
  incident: IncidentSummaryDTO;
  alerts: IncidentAlertDTO[];
  steps: IncidentStepDTO[];
  evidence: IncidentEvidenceDTO[];
  actions: IncidentActionDTO[];
}

/** 后端 agent 标识可能是 `DiagnoseAgent` / `diagnose` / `Diagnose`，统一取短名。 */
function shortAgent(raw: string): string {
  const trimmed = (raw || "").replace(/Agent$/i, "").replace(/_agent$/i, "");
  if (!trimmed) return "Unknown";
  return trimmed.charAt(0).toUpperCase() + trimmed.slice(1).toLowerCase();
}

/** `tool_calls` 是 JSON 字符串，解析失败时降级为空数组（不因脏数据整页报错）。 */
function parseToolCalls(raw: string | null): { name: string; args: Record<string, unknown>; result: string }[] {
  if (!raw) return [];
  try {
    const parsed = JSON.parse(raw);
    if (!Array.isArray(parsed)) return [];
    return parsed.map((item) => ({
      name: String(item?.name ?? "unknown"),
      args: (item?.args && typeof item.args === "object" ? item.args : {}) as Record<string, unknown>,
      result: String(item?.result ?? ""),
    }));
  } catch {
    return [];
  }
}

/** 后端不落库"步骤状态"：有输出即视为完成，末步无输出视为进行中，其余为等待。 */
function deriveStepStatus(output: string | null, index: number, total: number): StepStatus {
  if (output) return "completed";
  return index === total - 1 ? "in_progress" : "pending";
}

function asEvidenceSource(step: string): EvidenceSourceType {
  return step === "context" ? "rag" : step === "diagnose" ? "llm-reasoning" : "rag";
}

function asRisk(value: string | null | undefined): "low" | "medium" | "high" {
  return value === "high" || value === "medium" || value === "low" ? value : "medium";
}

/**
 * 把后端详情响应适配成详情页消费的 VM。
 *
 * 已知的信息损失（如实呈现，不编造）：
 * - `similar_incidents`：详情接口不返回相似案例 → 空数组（面板自带空态判断）；
 * - `knowledge_injected`：详情接口不返回回灌结果 → 置 null，
 *   页面不显示"已回灌知识库"角标（该角标只在 F-07 确认流程的响应里才有真实数据）。
 */
export function toIncidentVM(dto: IncidentDetailDTO): IncidentVM {
  const { incident, steps, evidence, actions } = dto;

  // 证据按 step_id 归位：详情接口给的是扁平数组，时间线需要挂在具体步骤上
  const evidenceByStep = new Map<number, IncidentEvidenceDTO[]>();
  for (const item of evidence) {
    if (item.step_id === null) continue;
    const bucket = evidenceByStep.get(item.step_id);
    if (bucket) bucket.push(item);
    else evidenceByStep.set(item.step_id, [item]);
  }

  const total = steps.length;
  const mappedSteps = steps.map((step, index) => {
    const agent = shortAgent(step.agent);
    return {
      id: String(step.id),
      agent,
      agent_name: AGENT_LABELS[agent] ?? step.agent,
      status: deriveStepStatus(step.output_summary, index, total),
      input_summary: step.input_summary ?? "",
      output_summary: step.output_summary ?? "",
      latency_ms: step.latency_ms ?? 0,
      confidence: step.confidence ?? 0,
      retry: step.retry_count ?? 0,
      tool_calls: parseToolCalls(step.tool_calls),
      evidence: (evidenceByStep.get(step.id) ?? []).map((ev) => ({
        quote: ev.quote,
        source_doc: ev.source_doc,
        score: ev.score ?? 0,
        source_type: asEvidenceSource(step.step),
      })),
    };
  });

  const lastAlertAt =
    dto.alerts.length > 0 ? dto.alerts[dto.alerts.length - 1].created_at : incident.last_seen_at;

  return {
    id: String(incident.id),
    title: incident.title,
    component: incident.component,
    status: asIncidentStatus(incident.status),
    severity: asSeverity(incident.severity),
    created_at: incident.created_at,
    last_alert_at: lastAlertAt,
    root_cause: incident.actual_cause || incident.root_cause || "根因尚未确定",
    confidence: incident.root_cause_confidence ?? 0,
    source: asRootCauseSource(incident.root_cause_source),
    knowledge_injected: null,
    steps: mappedSteps,
    actions: actions.map((action) => ({
      id: String(action.id),
      command: action.command,
      description: action.description ?? "",
      risk: asRisk(action.risk_level),
      status: action.status,
      requires_confirm: action.requires_confirm,
      exec_status: action.exec_status ?? null,
      exec_exit_code: action.exec_exit_code ?? null,
      exec_output: action.exec_output ?? null,
      exec_duration_ms: action.exec_duration_ms ?? null,
    })),
    similar_incidents: [],
  };
}

export const incidentsApi = {
  /**
   * 事件列表。
   *
   * **注意：后端没有 `task_id` 参数**（见 docs/30 §5.3 的归属链路）——
   * 想按任务过滤，必须先用 `alertsApi.recent({ task_id })` 拿到归属告警，
   * 再取其 `incident_id` 集合，然后在本列表里筛。
   */
  list(params: IncidentListQuery = {}) {
    const qs = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value === undefined || value === null || value === "") continue;
      qs.set(key, String(value));
    }
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return api.get<IncidentListOut>(`/incidents${suffix}`);
  },

  detail(id: number | string) {
    return api.get<IncidentDetailDTO>(`/incidents/${id}`);
  },

  /**
   * F-06 §5.4 处置确认（对齐 ConfirmIn）：action 必须处于 pending；
   * approve 后若无剩余 pending 且有 approved，事件转 resolved 并触发复盘回灌。
   */
  confirm(
    id: number | string,
    body: {
      action_id: number;
      decision: "approve" | "reject";
      actual_cause?: string;
      correct_actions?: string;
    },
  ) {
    return api.post<{
      incident_id: number;
      status: string;
      action_status: string;
      recap_triggered: boolean;
      knowledge_injected: Record<string, unknown> | null;
    }>(`/incidents/${id}/confirm`, body);
  },

  /**
   * 人工触发重诊断（202 异步）：仅 new / human_needed 可触发，
   * human_needed 必须 force=true；已有运行中诊断时后端 409。
   */
  diagnose(id: number | string, body: { force?: boolean }) {
    return api.post<{ incident_id: number; status: string; run_id: string }>(
      `/incidents/${id}/diagnose`,
      body,
    );
  },

  /** 显式关闭事件（§3.5）：存在 pending action 时后端 409，防绕过高危确认。 */
  close(id: number | string, body: { reason?: string; actual_cause?: string; correct_actions?: string }) {
    return api.post<{ incident_id: number; status: string }>(`/incidents/${id}/close`, body);
  },

  /** resolved 态补录真实根因 / 正确处置（§5.5）：喂置信度校准，至少一项。 */
  feedback(id: number | string, body: { actual_cause?: string; correct_actions?: string }) {
    return api.post<{ incident_id: number }>(`/incidents/${id}/feedback`, body);
  },
};
