/** F-11 集成管理接口（对齐 docs/27 与 app/routers/integrations.py）。 */

import { api, ApiError } from "./client";

export type IntegrationStatus = "pending" | "active" | "error" | "disabled";

export interface IntegrationDTO {
  id: number;
  name: string;
  type: string;
  base_url: string;
  scrape_config: string | null;
  status: IntegrationStatus;
  last_heartbeat: string | null;
  last_error: string | null;
  last_latency_ms: number | null;
  targets_total: number | null;
  targets_up: number | null;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
  disabled_at: string | null;
}

export interface IntegrationListOut {
  items: IntegrationDTO[];
  total: number;
  page: number;
  size: number;
}

export interface IntegrationIn {
  name: string;
  type: string;
  base_url: string;
  scrape_config?: string;
}

export interface IntegrationPatch {
  name?: string;
  base_url?: string;
  scrape_config?: string;
  status?: string;
}

export interface ProbeResult {
  ok: boolean;
  status?: IntegrationStatus;
  reason?: string;
  latency_ms?: number;
  targets_total?: number;
  targets_up?: number;
  last_error?: string | null;
  last_heartbeat?: string | null;
}

export interface DraftProbeResult {
  ok: boolean;
  reason: string;
  latency_ms: number;
  targets_total: number | null;
  targets_up: number | null;
  last_error: string | null;
}

export const integrationsApi = {
  list(params: { include_disabled?: boolean; page?: number; size?: number } = {}) {
    const qs = new URLSearchParams();
    if (params.include_disabled) qs.set("include_disabled", "true");
    if (params.page) qs.set("page", String(params.page));
    if (params.size) qs.set("size", String(params.size));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return api.get<IntegrationListOut>(`/integrations${suffix}`);
  },

  create(body: IntegrationIn & { api_key?: string }) {
    return api.post<IntegrationDTO>("/integrations", body);
  },

  update(id: number | string, body: IntegrationPatch) {
    return api.put<IntegrationDTO>(`/integrations/${id}`, body);
  },

  remove(id: number | string) {
    return api.delete<{ message?: string }>(`/integrations/${id}`);
  },

  /** 对已保存条目探测：POST /integrations/{id}/test（会写库并刷新状态） */
  probeById(id: number | string) {
    return api.post<ProbeResult>(`/integrations/${id}/test`);
  },

  /** 保存前探测表单里的地址：POST /integrations/test（只校验 + 探测，不落库） */
  testDraft(body: { type: string; base_url: string; scrape_config?: string }) {
    return api.post<DraftProbeResult>("/integrations/test", body);
  },
};

export { ApiError };
