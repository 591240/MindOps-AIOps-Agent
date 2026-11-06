/** F-01 告警接口（对齐 app/routers/alerts.py）。
 *
 * 注意告警看板的两个字段语义（第二轮修复后才有真实数据源）：
 * - `incident_status`：告警**本身没有状态**，状态属于它被聚类成的**事件**。
 *   `null` 表示尚未聚类成事件（前端显示「待聚类」）。
 * - `dedup_count`：同指纹命中次数（含首次入库），持久化在 `alerts.dedup_count`。
 */

import { api } from "./client";

export interface AlertDTO {
  id: number;
  source: string;
  component: string;
  severity: string;
  title: string;
  fingerprint: string;
  incident_id: number | null;
  integration_id: number | null;
  task_names: string[];
  incident_status: string | null;
  dedup_count: number;
  last_seen_at: string | null;
  created_at: string;
}

export interface AlertListOut {
  items: AlertDTO[];
  total: number;
}

export interface AlertQuery {
  minutes?: number;
  component?: string;
  severity?: string;
  /** 任务筛选：任务 ID；"none" = 仅未归属；缺省 = 全部 */
  task_id?: string;
  /** 事件状态；"unclustered" = 仅未聚类的告警 */
  status?: string;
  q?: string;
  page?: number;
  size?: number;
}

export interface AlertFacetItem {
  key: string;
  count: number;
}

/** GET /alerts/summary —— 看板分面计数（不受已选严重度/状态/组件影响） */
export interface AlertSummary {
  window_minutes: number;
  total: number;
  by_severity: AlertFacetItem[];
  by_status: AlertFacetItem[];
  by_component: AlertFacetItem[];
}

function toQueryString(params: AlertQuery): string {
  const search = new URLSearchParams();
  for (const [key, value] of Object.entries(params)) {
    if (value === undefined || value === null || value === "") continue;
    search.set(key, String(value));
  }
  const qs = search.toString();
  return qs ? `?${qs}` : "";
}

export const alertsApi = {
  recent(params: AlertQuery = {}) {
    return api.get<AlertListOut>(`/alerts/recent${toQueryString(params)}`);
  },

  /** 分面计数：只吃时间窗口与任务作用域，其余筛选不影响它 */
  summary(params: { minutes?: number; task_id?: string } = {}) {
    return api.get<AlertSummary>(
      `/alerts/summary${toQueryString(params as unknown as AlertQuery)}`,
    );
  },

  /** 物理删除单条告警（后端 admin-only）。定位是运维清理，事件记录不受影响。 */
  remove(id: number) {
    return api.delete<{ deleted: number; title: string }>(`/alerts/${id}`);
  },
};
