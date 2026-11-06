import { ref, watch } from "vue";
import {
  tasksApi,
  type TaskDetailDTO,
  type TaskDTO,
  type TaskListQuery,
  type TaskStatus,
} from "@/api/tasks";
import { ApiError } from "@/api/client";

/**
 * 任务数据层（对齐 React 版的 task-store）：
 * 数据一律来自 `GET /tasks` / `GET /tasks/{id}`，不保留本地副本 ——
 * 本地副本会和真实状态分叉（别人改了状态你看不到，刷新一次就"回退"）。
 *
 * 列表页与工作台需要的字段不同（工作台要 `targets` 明细），因此拆成两个
 * composable，各自只拉自己需要的。
 */

export interface TaskLoadError {
  offline: boolean;
  message: string;
  notFound?: boolean;
}

function toError(err: unknown, fallback: string): TaskLoadError {
  const offline = err instanceof ApiError && err.isNetworkError;
  return {
    offline,
    message: offline
      ? "无法连接后端服务，请确认 mindops-backend 已在 8010 端口启动。"
      : err instanceof Error
        ? err.message
        : fallback,
  };
}

/** 任务列表（服务端筛选 + 分页） */
export function useTaskList(query: TaskListQuery) {
  const items = ref<TaskDTO[]>([]);
  const total = ref(0);
  const loading = ref(true);
  const error = ref<TaskLoadError | null>(null);

  async function refresh() {
    loading.value = true;
    error.value = null;
    try {
      const data = await tasksApi.list(JSON.parse(JSON.stringify(query)) as TaskListQuery);
      items.value = data.items;
      total.value = data.total;
    } catch (err) {
      error.value = toError(err, "加载任务列表失败");
      items.value = [];
      total.value = 0;
    } finally {
      loading.value = false;
    }
  }

  // query 是响应式对象，序列化后作依赖，避免字段未变时重复请求
  watch(
    () => JSON.stringify(query),
    () => void refresh(),
    { immediate: true },
  );

  return { items, total, loading, error, refresh };
}

/** 任务详情（含绑定目标明细，工作台用） */
export function useTaskDetail(id: () => string | undefined) {
  const task = ref<TaskDetailDTO | null>(null);
  const loading = ref(true);
  const error = ref<TaskLoadError | null>(null);

  async function refresh() {
    const taskId = id();
    if (!taskId) {
      task.value = null;
      error.value = { offline: false, message: "缺少任务 id", notFound: true };
      loading.value = false;
      return;
    }
    loading.value = true;
    error.value = null;
    try {
      task.value = await tasksApi.detail(taskId);
    } catch (err) {
      const notFound = err instanceof ApiError && err.status === 404;
      task.value = null;
      error.value = { ...toError(err, "加载任务详情失败"), notFound };
    } finally {
      loading.value = false;
    }
  }

  watch(id, () => void refresh(), { immediate: true });

  return { task, loading, error, refresh };
}

/** 状态流转：active ↔ paused（archived 是终态，只能走 archive） */
export function toggleTaskStatus(current: TaskStatus): TaskStatus {
  return current === "active" ? "paused" : "active";
}
