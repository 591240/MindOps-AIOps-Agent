/** F-12 监控任务接口（对齐 app/routers/tasks.py，docs/30 §3）。
 *
 * 契约要点（与 mock 阶段的行为差异，别再想当然）：
 * 1. `task.id` 是**字符串** `task_001`；而绑定目标的 `integration_id` 是**整数**
 *    （F-11 的 `integrations.id`），不是 `"int_001"` 这种字符串。
 * 2. `PUT /tasks/{id}` **不接受 `target_ids`**：改绑定必须走
 *    `POST /{id}/targets` 与 `DELETE /{id}/targets/{integration_id}`，
 *    由调用方自己做集合 diff。
 * 3. `archived` 是**终态**：归档后任何状态变更返回 409 `ALREADY_ARCHIVED`。
 * 4. 写操作全部 admin-only，且有按用户限流（更新 20/min，绑定 30/min）。
 */

import { api } from "./client";

export type TaskStatus = "active" | "paused" | "archived";

export interface TaskDTO {
  id: string;
  name: string;
  description: string;
  status: TaskStatus;
  target_count: number;
  alerts_24h: number;
  created_by: string | null;
  created_at: string | null;
  updated_at: string | null;
  archived_at: string | null;
}

export interface TaskTargetDTO {
  integration_id: number;
  name: string;
  type: string | null;
  base_url: string | null;
  scrape_config: string | null;
  status: string | null;
  targets_total: number | null;
  targets_up: number | null;
  last_heartbeat: string | null;
  added_at: string | null;
}

export interface TaskDetailDTO extends TaskDTO {
  targets: TaskTargetDTO[];
}

export interface TaskListOut {
  total: number;
  page: number;
  size: number;
  items: TaskDTO[];
}

export interface TaskListQuery {
  status?: TaskStatus;
  q?: string;
  include_archived?: boolean;
  page?: number;
  size?: number;
}

export const tasksApi = {
  list(params: TaskListQuery = {}) {
    const qs = new URLSearchParams();
    if (params.status) qs.set("status", params.status);
    if (params.q) qs.set("q", params.q);
    if (params.include_archived) qs.set("include_archived", "true");
    if (params.page) qs.set("page", String(params.page));
    if (params.size) qs.set("size", String(params.size));
    const suffix = qs.toString() ? `?${qs.toString()}` : "";
    return api.get<TaskListOut>(`/tasks${suffix}`);
  },

  detail(id: string) {
    return api.get<TaskDetailDTO>(`/tasks/${id}`);
  },

  create(body: { name: string; description?: string; target_ids?: number[] }) {
    return api.post<TaskDTO>("/tasks", body);
  },

  /** 只改 name / description / status；改绑定请用 bindTarget / unbindTarget */
  update(id: string, body: { name?: string; description?: string; status?: TaskStatus }) {
    return api.put<TaskDTO>(`/tasks/${id}`, body);
  },

  /** 软删除 = 归档（终态） */
  archive(id: string) {
    return api.delete<{ deleted: boolean; soft: boolean; status: string; reason: string }>(
      `/tasks/${id}`
    );
  },

  bindTarget(id: string, integrationId: number) {
    return api.post<{ task_id: string; integration_id: number; target_count: number }>(
      `/tasks/${id}/targets`,
      { integration_id: integrationId }
    );
  },

  unbindTarget(id: string, integrationId: number) {
    return api.delete<{ task_id: string; integration_id: number; target_count: number }>(
      `/tasks/${id}/targets/${integrationId}`
    );
  },
};

/**
 * 把目标集合从 `current` 同步到 `next`（`PUT /tasks` 不管绑定，必须自行 diff）。
 *
 * 先解绑后绑定：避免中间态超出目标数量上限；单条失败不中断其余，
 * 返回成功/失败明细供调用方提示用户。
 */
export async function syncTaskTargets(
  taskId: string,
  current: number[],
  next: number[]
): Promise<{ bound: number[]; unbound: number[]; failed: { id: number; message: string }[] }> {
  const currentSet = new Set(current);
  const nextSet = new Set(next);
  const toUnbind = current.filter((id) => !nextSet.has(id));
  const toBind = next.filter((id) => !currentSet.has(id));

  const bound: number[] = [];
  const unbound: number[] = [];
  const failed: { id: number; message: string }[] = [];

  for (const id of toUnbind) {
    try {
      await tasksApi.unbindTarget(taskId, id);
      unbound.push(id);
    } catch (err) {
      failed.push({ id, message: err instanceof Error ? err.message : "解绑失败" });
    }
  }
  for (const id of toBind) {
    try {
      await tasksApi.bindTarget(taskId, id);
      bound.push(id);
    } catch (err) {
      failed.push({ id, message: err instanceof Error ? err.message : "绑定失败" });
    }
  }

  return { bound, unbound, failed };
}
