<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { tasksApi, syncTaskTargets, type TaskDTO, type TaskStatus } from "@/api/tasks";
import { useTaskList, toggleTaskStatus } from "@/lib/task-store";
import TaskStatusPill from "./TaskStatusPill.vue";
import TaskWizard from "./TaskWizard.vue";
import AppTooltip from "@/components/ui/AppTooltip.vue";
import AppDialog from "@/components/ui/AppDialog.vue";
import Button from "@/components/ui/Button.vue";
import StatCard from "@/components/business/StatCard.vue";
import { toast } from "vue-sonner";
import {
  Plus,
  Search,
  RefreshCw,
  ChevronRight,
  Inbox,
  ListChecks,
  Pause,
  Play,
  Archive,
  Pencil,
  Info,
  BellRing,
} from "lucide-vue-next";

/**
 * 任务列表 /tasks —— 登录后第一屏（F-12 §8.2，docs/26 §2.9 页面一）
 *
 * 信息架构：任务 > 被监控项目 > 告警 > 事件 > 诊断 > 知识。
 * 「任务在先、项目在后」：先建任务，再从资产池勾选要监控的项目。
 *
 * 第二轮修复：
 * - 数据由 localStorage mock 改为 `GET /tasks`（服务端筛选 + 分页）；
 * - 筛选与分页改为服务端参数，`total` 用后端 count（此前是"过滤当前页"的假分页）；
 * - 写操作（新建/编辑/暂停/归档）走真实接口，失败用 toast 明示并刷新回真实状态。
 */

type StatusFilter = "all" | TaskStatus;

const PAGE_SIZE = 10;

const router = useRouter();
const statusFilter = ref<StatusFilter>("all");
/** 已提交的搜索词：与输入框分离，避免每敲一个字就打一次接口 */
const submittedKeyword = ref("");
const keyword = ref("");
const page = ref(1);

const wizardOpen = ref(false);
const editTask = ref<TaskDTO | null>(null);
/** 编辑时已绑定的项目 id（TaskDTO 不带，需从详情接口取） */
const editTargetIds = ref<number[]>([]);
const archiving = ref<TaskDTO | null>(null);
const busy = ref(false);

const query = reactive<{ status?: TaskStatus; q?: string; page: number; size: number }>({
  status: undefined,
  q: undefined,
  page: 1,
  size: PAGE_SIZE,
});

watch(
  [statusFilter, submittedKeyword, page],
  () => {
    query.status = statusFilter.value === "all" ? undefined : statusFilter.value;
    query.q = submittedKeyword.value || undefined;
    query.page = page.value;
  },
  { immediate: true },
);

const { items: tasks, total, loading, refresh } = useTaskList(query);

// 同名冲突校验要用"全部任务名"，而不是当前这一页（翻页后撞名会被后端 409
// 打回，但那是一次白跑的往返）。后端单页上限 100，仍以 409 兜底。
const { items: allTasksForNames } = useTaskList({ size: 100 });
const existingNames = computed(() => allTasksForNames.value.map((t) => t.name));

const isEmptyPool = computed(
  () => !loading.value && tasks.value.length === 0 && statusFilter.value === "all" && !submittedKeyword.value,
);
const totalPages = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)));

/**
 * 顶部 KPI：用"全部任务"而不是当前页/当前筛选来算。
 * 筛选是"看某一片"，KPI 是"看整体态势"——若跟着筛选变，卡片就失去了参照系。
 * 后端单页上限 100，超出部分不计入（本项目任务量级远低于此）。
 */
const taskStats = computed(() => {
  const all = allTasksForNames.value;
  return {
    total: all.length,
    active: all.filter((t) => t.status === "active").length,
    paused: all.filter((t) => t.status === "paused").length,
    alerts24h: all.reduce((sum, t) => sum + (t.alerts_24h ?? 0), 0),
  };
});

/** 编辑任务的绑定需要 diff：PUT /tasks 不接收 target_ids，得逐条 bind/unbind */
async function loadCurrentTargetIds(taskId: string): Promise<number[]> {
  const detail = await tasksApi.detail(taskId);
  return detail.targets.map((t) => t.integration_id);
}

/**
 * 打开编辑向导。
 *
 * 先把已绑定项目读回来再开窗 —— 反过来（先开窗后异步回填）会让
 * `editTargetIds` 变化触发向导的初始化 effect，把用户刚开始输入的内容冲掉。
 */
async function openEdit(task: TaskDTO) {
  try {
    const ids = await loadCurrentTargetIds(task.id);
    editTask.value = task;
    editTargetIds.value = ids;
    wizardOpen.value = true;
  } catch (err) {
    toast.error("读取已绑定项目失败", {
      description: err instanceof Error ? err.message : "未知错误",
    });
  }
}

function openCreate() {
  editTask.value = null;
  editTargetIds.value = [];
  wizardOpen.value = true;
}

async function handleSubmit(payload: {
  name: string;
  description: string;
  target_ids: number[];
  id?: string;
}) {
  busy.value = true;
  try {
    if (payload.id) {
      await tasksApi.update(payload.id, {
        name: payload.name,
        description: payload.description,
      });
      const current = await loadCurrentTargetIds(payload.id);
      const { failed } = await syncTaskTargets(payload.id, current, payload.target_ids);
      if (failed.length) {
        toast.warning("任务已保存，但部分项目绑定失败", {
          description: failed.map((f) => `#${f.id}：${f.message}`).join("；"),
        });
      } else {
        toast.success("任务已更新");
      }
      wizardOpen.value = false;
      editTask.value = null;
      await refresh();
      return;
    }

    const created = await tasksApi.create({
      name: payload.name,
      description: payload.description,
      target_ids: payload.target_ids,
    });
    toast.success("任务已创建", {
      description: payload.target_ids.length
        ? `已绑定 ${payload.target_ids.length} 个监控项目`
        : "尚未绑定监控项目，暂时收不到告警",
    });
    wizardOpen.value = false;
    await router.push(`/tasks/${created.id}`);
  } catch (err) {
    toast.error(payload.id ? "更新任务失败" : "创建任务失败", {
      description: err instanceof Error ? err.message : "未知错误",
    });
  } finally {
    busy.value = false;
  }
}

async function togglePause(task: TaskDTO) {
  try {
    await tasksApi.update(task.id, { status: toggleTaskStatus(task.status) });
    await refresh();
  } catch (err) {
    toast.error("状态变更失败", {
      description: err instanceof Error ? err.message : "未知错误",
    });
  }
}

async function confirmArchive() {
  if (!archiving.value) return;
  busy.value = true;
  try {
    await tasksApi.archive(archiving.value.id);
    toast.success("任务已归档", {
      description: "归档是终态，不可恢复；历史告警与诊断记录保留用于审计。",
    });
    archiving.value = null;
    await refresh();
  } catch (err) {
    toast.error("归档失败", {
      description: err instanceof Error ? err.message : "未知错误",
    });
  } finally {
    busy.value = false;
  }
}

const formatTime = (iso: string) =>
  new Date(iso).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
</script>

<template>
  <div class="space-y-4">
    <!-- 态势卡：先给结论，再看明细 -->
    <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
      <StatCard
        label="监控任务"
        :value="taskStats.total"
        :icon="ListChecks"
        tone="text-foreground"
        hint="含已归档"
        :loading="loading && allTasksForNames.length === 0"
      />
      <StatCard
        label="进行中"
        :value="taskStats.active"
        :icon="Play"
        tone="text-success"
        hint="正在接收告警"
        :loading="loading && allTasksForNames.length === 0"
      />
      <StatCard
        label="已暂停"
        :value="taskStats.paused"
        :icon="Pause"
        tone="text-warning"
        hint="暂停期间不触发诊断"
        :loading="loading && allTasksForNames.length === 0"
      />
      <StatCard
        label="近 24h 告警"
        :value="taskStats.alerts24h"
        :icon="BellRing"
        tone="text-glow-pink"
        hint="各任务计数之和"
        :loading="loading && allTasksForNames.length === 0"
      />
    </div>

    <!-- 筛选栏 + 新建 -->
    <div class="glass rounded-xl p-4 flex items-center gap-3 flex-wrap">
      <select
        :value="statusFilter"
        @change="
          (e: Event) => {
            statusFilter = (e.target as HTMLSelectElement).value as StatusFilter;
            page = 1;
          }
        "
        class="px-3 py-2 rounded-lg border border-border bg-card text-[13px] focus:outline-none focus:ring-2 focus:ring-accent/30 cursor-pointer"
      >
        <option value="all">全部状态</option>
        <option value="active">进行中</option>
        <option value="paused">已暂停</option>
        <option value="archived">已归档</option>
      </select>

      <div class="relative flex-1 min-w-[200px] max-w-md">
        <Search
          :size="16"
          class="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground"
        />
        <input
          type="text"
          v-model="keyword"
          @input="
            () => {
              // 清空输入立即回到全量列表，否则用户会盯着 '0 条结果' 找不到重置入口
              if (keyword === '' && submittedKeyword) {
                submittedKeyword = '';
                page = 1;
              }
            }
          "
          @keydown.enter="
            () => {
              submittedKeyword = keyword.trim();
              page = 1;
            }
          "
          placeholder="搜索任务名或描述（回车）..."
          class="w-full pl-9 pr-3 py-2 rounded-lg border border-border bg-card text-[13px] focus:outline-none focus:ring-2 focus:ring-accent/30"
        />
      </div>

      <button
        @click="void refresh()"
        class="inline-flex items-center gap-1.5 px-3 py-2 rounded-full border border-border bg-card text-[13px] font-medium hover:bg-secondary transition-colors btn-pill"
      >
        <RefreshCw :size="14" :class="loading ? 'animate-spin' : ''" />
        刷新
      </button>

      <Button
        @click="openCreate"
        class="h-9 px-4 rounded-full bg-primary text-primary-foreground text-[13px] font-medium btn-pill hover:bg-primary/90"
      >
        <Plus :size="16" class="mr-1.5" />
        新建任务
      </Button>
    </div>

    <!-- 表格 -->
    <div class="glass rounded-xl overflow-hidden">
      <div v-if="loading" class="p-4 space-y-3">
        <div v-for="i in 5" :key="i" class="flex items-center gap-4 h-12">
          <div class="h-4 bg-secondary rounded animate-pulse w-40" />
          <div class="h-4 bg-secondary rounded animate-pulse w-16" />
          <div class="h-4 bg-secondary rounded animate-pulse w-20" />
          <div class="h-4 bg-secondary rounded animate-pulse flex-1" />
          <div class="h-4 bg-secondary rounded animate-pulse w-24" />
        </div>
      </div>

      <!-- 首屏第一印象：任务池为空 → 引导卡（不是空白表格） -->
      <div v-else-if="isEmptyPool" class="py-20 flex flex-col items-center gap-3">
        <div class="w-14 h-14 rounded-2xl bg-secondary flex items-center justify-center">
          <ListChecks :size="26" class="text-muted-foreground" />
        </div>
        <div class="text-[16px] font-medium">还没有监控任务</div>
        <div class="text-[13px] text-muted-foreground text-center max-w-[420px]">
          建一个任务，选上要监控的项目，系统就会自动收告警并做诊断。
        </div>
        <Button
          @click="openCreate"
          class="mt-2 h-9 px-4 rounded-full bg-primary text-primary-foreground text-[13px] font-medium btn-pill hover:bg-primary/90"
        >
          创建第一个监控任务
        </Button>
      </div>

      <div v-else-if="tasks.length === 0" class="py-16 flex flex-col items-center gap-3">
        <div class="w-12 h-12 rounded-full bg-secondary flex items-center justify-center">
          <Inbox :size="24" class="text-muted-foreground" />
        </div>
        <div class="text-[15px] font-medium">没有匹配的任务</div>
        <div class="text-[13px] text-muted-foreground">当前筛选条件下没有任务</div>
        <button
          @click="
            () => {
              statusFilter = 'all';
              keyword = '';
            }
          "
          class="mt-2 px-4 py-2 rounded-full border border-border bg-card text-[13px] font-medium hover:bg-secondary transition-colors btn-pill"
        >
          清除筛选
        </button>
      </div>

      <table v-else class="w-full">
        <thead>
          <tr class="border-b border-border bg-secondary/40">
            <th class="text-left px-4 py-3 text-[12px] font-semibold text-muted-foreground">任务名</th>
            <th class="text-left px-4 py-3 text-[12px] font-semibold text-muted-foreground">状态</th>
            <th class="text-left px-4 py-3 text-[12px] font-semibold text-muted-foreground">
              <AppTooltip text="本任务引用的被监控项目。同一个项目可以被多个任务同时引用，解绑不会删除资产。">
                <span class="flex items-center gap-1">
                  绑定项目
                  <Info :size="12" class="text-muted-foreground/70" />
                </span>
              </AppTooltip>
            </th>
            <th class="text-left px-4 py-3 text-[12px] font-semibold text-muted-foreground">
              <AppTooltip
                text="同一条告警可同时归属多个任务（一个监控项目可能被多个任务引用），因此各任务计数之和可能大于全局告警总量。"
              >
                <span class="flex items-center gap-1">
                  近 24h 告警
                  <Info :size="12" class="text-muted-foreground/70" />
                </span>
              </AppTooltip>
            </th>
            <th class="text-left px-4 py-3 text-[12px] font-semibold text-muted-foreground">创建人</th>
            <th class="text-left px-4 py-3 text-[12px] font-semibold text-muted-foreground">创建时间</th>
            <th class="text-right px-4 py-3 text-[12px] font-semibold text-muted-foreground">操作</th>
          </tr>
        </thead>
        <tbody>
          <tr
            v-for="task in tasks"
            :key="task.id"
            @click="void router.push(`/tasks/${task.id}`)"
            class="border-b border-border last:border-0 hover:bg-secondary/30 cursor-pointer transition-colors h-12"
          >
            <td class="px-4 py-3">
              <div class="text-[13px] font-medium">{{ task.name }}</div>
              <div
                v-if="task.description"
                class="text-[12px] text-muted-foreground truncate max-w-[280px]"
              >
                {{ task.description }}
              </div>
            </td>
            <td class="px-4 py-3">
              <TaskStatusPill :status="task.status" />
            </td>
            <td class="px-4 py-3">
              <span v-if="task.target_count === 0" class="text-[13px] text-muted-foreground">—</span>
              <AppTooltip
                v-else
                :text="`本任务绑定了 ${task.target_count} 个被监控项目，点开任务可查看具体是哪些。`"
              >
                <span class="text-[13px] border-b border-dashed border-border">
                  {{ task.target_count }} 个
                </span>
              </AppTooltip>
            </td>
            <td class="px-4 py-3 text-[13px] font-mono tabular-nums">{{ task.alerts_24h }}</td>
            <td class="px-4 py-3 text-[13px] text-muted-foreground">{{ task.created_by }}</td>
            <td
              class="px-4 py-3 text-[13px] text-muted-foreground font-mono tabular-nums whitespace-nowrap"
            >
              {{ task.created_at ? formatTime(task.created_at) : "—" }}
            </td>
            <td class="px-4 py-3">
              <div class="flex items-center justify-end gap-1" @click.stop>
                <button
                  v-if="task.status !== 'archived'"
                  title="编辑"
                  @click="void openEdit(task)"
                  class="p-1.5 rounded-lg text-muted-foreground hover:bg-secondary hover:text-foreground transition-colors"
                >
                  <Pencil :size="14" />
                </button>
                <button
                  v-if="task.status !== 'archived'"
                  :title="task.status === 'active' ? '暂停' : '恢复'"
                  @click="void togglePause(task)"
                  class="p-1.5 rounded-lg text-muted-foreground hover:bg-secondary hover:text-foreground transition-colors"
                >
                  <Pause v-if="task.status === 'active'" :size="14" />
                  <Play v-else :size="14" />
                </button>
                <button
                  v-if="task.status !== 'archived'"
                  title="归档"
                  @click="archiving = task"
                  class="p-1.5 rounded-lg text-muted-foreground hover:bg-secondary hover:text-foreground transition-colors"
                >
                  <Archive :size="14" />
                </button>
                <ChevronRight :size="16" class="text-muted-foreground" />
              </div>
            </td>
          </tr>
        </tbody>
      </table>

      <!-- 分页 -->
      <div
        v-if="!loading && !isEmptyPool && tasks.length > 0"
        class="px-4 py-3 border-t border-border flex items-center justify-between"
      >
        <span class="text-[12px] text-muted-foreground">
          共 {{ total }} 条，第 {{ page }} / {{ totalPages }} 页
        </span>
        <div class="flex gap-1">
          <button
            @click="page = Math.max(1, page - 1)"
            :disabled="page === 1"
            class="px-3 py-1.5 rounded-lg border border-border bg-card text-[12px] disabled:opacity-40 hover:bg-secondary transition-colors"
          >
            上一页
          </button>
          <button
            @click="page = page + 1"
            :disabled="page >= totalPages"
            class="px-3 py-1.5 rounded-lg border border-border bg-card text-[12px] disabled:opacity-40 hover:bg-secondary transition-colors"
          >
            下一页
          </button>
        </div>
      </div>
    </div>

    <!-- 创建 / 编辑向导 -->
    <TaskWizard
      :open="wizardOpen"
      @update:open="wizardOpen = $event"
      :edit-task="editTask"
      :edit-target-ids="editTargetIds"
      :existing-names="existingNames"
      :on-submit="handleSubmit"
    />

    <!-- 归档二次确认（任务移出列表，历史保留） -->
    <AppDialog
      :open="Boolean(archiving)"
      @update:open="(v: boolean) => !v && (archiving = null)"
      max-width="400px"
    >
      <template #title>
        <span class="text-[15px] font-semibold">归档任务</span>
      </template>
      <p class="text-[13px] text-muted-foreground">
        任务「{{ archiving?.name }}」将移出列表。归档是<b>终态、不可恢复</b>，其告警与诊断结果仍保留用于审计。
      </p>
      <template #footer>
        <Button
          @click="archiving = null"
          class="h-9 px-4 rounded-full border border-border bg-card text-foreground text-[13px] font-medium btn-pill hover:bg-secondary"
        >
          取消
        </Button>
        <Button
          @click="void confirmArchive()"
          class="h-9 px-4 rounded-full bg-primary text-primary-foreground text-[13px] font-medium btn-pill hover:bg-primary/90"
        >
          确认归档
        </Button>
      </template>
    </AppDialog>
  </div>
</template>
