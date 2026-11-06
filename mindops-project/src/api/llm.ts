/** F-09 模型管理接口（对齐 docs/23 与 app/routers/llm.py）。 */

import { api } from "./client";

export interface LlmProviderDTO {
  id: number;
  name: string;
  provider_type: string;
  base_url: string;
  model_name: string;
  api_key_masked: string | null;
  role: "chat" | "reasoning" | "embedding";
  is_default: boolean;
  status: string;
  temperature: number;
  max_tokens: number;
  last_test_at: string | null;
  last_test_latency_ms: number | null;
  last_test_status: string | null;
}

export interface LlmProviderIn {
  name: string;
  provider_type: string;
  base_url: string;
  model_name: string;
  /** 明文仅存在于本次请求，落库即加密（F-09 §5.5） */
  api_key: string;
  role?: "chat" | "reasoning" | "embedding";
  is_default?: boolean;
  temperature?: number;
  max_tokens?: number;
}

export interface LlmProviderPatch {
  name?: string;
  model_name?: string;
  api_key?: string;
  /** 后端 update_provider 支持改端点与角色（均做 SSRF / 词表校验） */
  base_url?: string;
  role?: "chat" | "reasoning" | "embedding";
  temperature?: number;
  max_tokens?: number;
  status?: string;
  is_default?: boolean;
}

/** POST /llm/test：保存前连通测试（不落库，明文 Key 只在本次请求里）。 */
export interface LlmTestIn {
  provider_type: string;
  base_url: string;
  model_name: string;
  api_key: string;
  role?: "chat" | "reasoning";
}

export interface LlmTestOut {
  status: string;
  latency_ms: number;
  actual_model: string | null;
}

/**
 * GET /llm/presets 的厂商预设（app/llm/registry.py PRESETS）。
 *
 * 注意这与本地 mock 的形状**不同**：后端给的是 `models` 列表 + `key_hint`，
 * 没有 mock 里的 `model_name` / `description`。以前端 mock 的形状为准去写渲染
 * 会在接真接口后拿到 undefined，所以这里以后端契约为准定义类型。
 */
export interface LlmPreset {
  provider_type: string;
  label: string;
  base_url: string;
  models: string[];
  key_hint: string;
  /**
   * 模型 id → 人读名（可选）。官方文档展示的是版本名（如 DeepSeek-V4.1-Flash），
   * 但 API 只认 id（deepseek-flash，传版本名会 400），所以两者必须分开：
   * 展示用 model_labels，请求体永远用 models 里的 id。
   */
  model_labels?: Record<string, string>;
}

export const llmApi = {
  list() {
    return api.get<{ providers: LlmProviderDTO[] }>("/llm/providers");
  },

  /** 仅 admin 可调用（后端 require_admin）。 */
  presets() {
    return api.get<{ presets: LlmPreset[] }>("/llm/presets");
  },

  create(body: LlmProviderIn) {
    return api.post<LlmProviderDTO>("/llm/providers", body);
  },

  update(id: number | string, body: LlmProviderPatch) {
    return api.put<LlmProviderDTO>(`/llm/providers/${id}`, body);
  },

  remove(id: number | string) {
    return api.delete<{ message?: string }>(`/llm/providers/${id}`);
  },

  test(id: number | string) {
    return api.post<LlmTestOut>(`/llm/providers/${id}/test`);
  },

  /** 保存前测试：用表单里尚未落库的端点/Key/模型名发起真实 ping。 */
  testDraft(body: LlmTestIn) {
    return api.post<LlmTestOut>("/llm/test", body);
  },
};
