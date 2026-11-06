/** 规则库管理接口（对齐 app/routers/rules.py）。
 *
 * 规则以 config/pattern_rules.yaml 为唯一事实源：这里的增删改
 * 都是「校验 → 原子写回 YAML」，Triage 按 mtime+digest 热加载，
 * 写回即生效。命中逻辑一律走后端 /rules/test，前端不做匹配模拟。
 */

import { api } from "./client";

export interface RulePlaybookStep {
  command: string;
  description: string;
  /** 手动指定的风险等级；缺省 = 由风险库自动判定 */
  risk_level?: "low" | "medium" | "high";
}

export interface PatternRule {
  name: string;
  component: string;
  keywords: string[];
  aliases: string[];
  root_cause: string;
  playbook: RulePlaybookStep[];
}

export interface RuleTestOut {
  matched: boolean;
  rule: {
    name: string;
    component: string;
    root_cause: string;
    playbook: RulePlaybookStep[];
  } | null;
}

export interface RulePayload {
  name?: string;
  component: string;
  keywords: string[];
  aliases?: string[];
  root_cause: string;
  playbook?: RulePlaybookStep[];
}

export const rulesApi = {
  list() {
    return api.get<{ rules: PatternRule[]; file: string }>("/rules");
  },
  create(payload: RulePayload) {
    return api.post<PatternRule>("/rules", payload);
  },
  update(name: string, payload: RulePayload) {
    return api.put<PatternRule>(`/rules/${name}`, payload);
  },
  remove(name: string) {
    return api.delete<{ deleted: string }>(`/rules/${name}`);
  },
  test(text: string, component?: string) {
    return api.post<RuleTestOut>("/rules/test", { text, component: component || undefined });
  },
};
