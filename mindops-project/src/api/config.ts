/** F-04 置信度回环参数（对齐 app/routers/confidence.py）。
 *
 * **路径坑**：router 的 prefix 是 `/config`，所以真实路径是
 * `/config/confidence` 而不是 `/confidence`。按直觉写成后者只会得到 404。
 *
 * 另一个反直觉点：**GET 与 PUT 的返回结构不同**。
 * - GET 返回完整生效值（含只读的 expand_recall 参数组）
 * - PUT 只回 `conf_threshold` / `max_retry` / `updated_by` / `note`
 * 所以 PUT 之后不能直接拿返回值覆盖整个表单，否则会丢掉两个布尔字段。
 */

import { api } from "./client";

/** GET /config/confidence 响应。 */
export interface ConfidenceConfig {
  conf_threshold: number;
  max_retry: number;
  reasoning_escalation: boolean;
  cross_component_fallback: boolean;
  /** 只读：MVP 固定的召回扩查参数组，后端不允许修改 */
  expand_recall: {
    top_k_base: number;
    top_k_loop: number;
    sim_floor_base: number;
    sim_floor_loop: number;
  };
}

/** PUT /config/confidence 请求体（后端白名单：仅这 4 个字段）。 */
export interface ConfidencePatchInput {
  conf_threshold?: number;
  max_retry?: number;
  reasoning_escalation?: boolean;
  cross_component_fallback?: boolean;
}

/** PUT /config/confidence 响应（注意：不是完整配置，别拿它整体覆盖表单）。 */
export interface ConfidencePatchResult {
  conf_threshold: number;
  max_retry: number;
  updated_by: string;
  note: string;
}

export const configApi = {
  getConfidence() {
    return api.get<ConfidenceConfig>("/config/confidence");
  },

  /** 仅 admin 可调用；engineer 会拿到 403 FORBIDDEN。 */
  updateConfidence(body: ConfidencePatchInput) {
    return api.put<ConfidencePatchResult>("/config/confidence", body);
  },
};
