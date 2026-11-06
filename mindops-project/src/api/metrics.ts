/** 统计大盘接口（对齐 app/routers/metrics.py）。
 *
 * 与 Prometheus 文本端点 `/metrics` 区分：这里是给「统计大盘」页用的结构化 JSON。
 */

import { api } from "./client";

export type MetricsRange = "today" | "7d" | "30d";

export interface MetricsKpi {
  today_alerts: number;
  auto_close_rate: number;
  mttr_min: number;
  knowledge_hit_rate: number;
}

export interface MetricsDashboard {
  range: MetricsRange;
  /** 分桶粒度：today → hour，7d/30d → day */
  bucket: "hour" | "day";
  since: string;
  generated_at: string;
  /** 该区间是否存在真实样本；false 时前端应展示空态而非伪造数据 */
  has_data: boolean;
  kpi: MetricsKpi;
  mttr_trend: { date: string; mttr: number }[];
  hit_rate_by_component: { component: string; rate: number }[];
  agent_performance: { agent: string; avg_ms: number }[];
  /** 告警趋势：按分桶 + 严重度堆叠 */
  alert_trend: { date: string; P0: number; P1: number; P2: number; P3: number }[];
  /** 根因置信度五档分布（0.6 是回环分水岭） */
  confidence_distribution: { bucket: string; count: number }[];
  /** 告警量最高的组件（最多 8 个） */
  component_top: { component: string; count: number }[];
}

export const metricsApi = {
  dashboard(range: MetricsRange) {
    return api.get<MetricsDashboard>(`/metrics/dashboard?range=${range}`);
  },
};
