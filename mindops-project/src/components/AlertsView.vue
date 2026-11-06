<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import { alertsApi, type AlertDTO, type AlertSummary } from "@/api/alerts";
import { tasksApi, type TaskDTO } from "@/api/tasks";
import { ApiError } from "@/api/client";
import { openAlertStream, type StreamStatus } from "@/api/stream";
import { readSession } from "@/lib/session";
import StatusBadge from "@/components/business/StatusBadge.vue";
import StatCard from "@/components/business/StatCard.vue";
import {
  Search,
  RefreshCw,
  ChevronRight,
  Inbox,
  WifiOff,
  Link2Off,
  Trash2,
  BellRing,
  ShieldAlert,
  UserRoundCheck,
  Layers,
} from "lucide-vue-next";
import { toast } from "vue-sonner";

/**
 * 告警看板 /alerts —— 信息架构对齐 Keep 的 Feed 页：
 * 顶部 4 张态势卡 → 左侧带计数的分面栏 → 右侧密排告警流。
 *
 * 关键设计立场：
 * - **分面计数不随已选筛选变化**（后端 /alerts/summary 只吃时间窗口 + 任务作用域）。
 *   否则选中 P0 后其余严重度全变 0，筛选项就从"态势概览"退化成"死按钮"。
 * - 状态属于**事件**而非告警：未聚类的告警走 `unclustered` 独立一档。
 * - 严重度用**左缘色带**而不是色点：色带在密集列表里形成连续视觉列，
 *   扫一眼就能看出"这一屏有多少红条"。
 */

/** 任务筛选哨兵：后端约定 `task_id=none` 表示"只看未归属告警" */
const TASK_FILTER_NONE = "none";

type StatusFilter =
  | "all"
  | "unclustered"
  | "new"
  | "diagnosing"
  | "human_needed"
  | "resolved"
  | "closed";

const STATUS_LABEL: Record<string, string> = {
  unclustered: "待聚类",
  new: "新建",
  diagnosing: "诊断中",
  human_needed: "待人工",
  resolved: "已解决",
  closed: "已关闭",
};

const PAGE_SIZE = 20;
/** 时间窗口：后端 /alerts/recent 按 minutes 过滤，取 24h 覆盖看板场景 */
const WINDOW_MINUTES = 1440;

const router = useRouter();
const route = useRoute();

const loading = ref(true);
const alerts = ref<AlertDTO[]>([]);
const total = ref(0);
const statusFilter = ref<StatusFilter>("all");
const severityFilter = ref("");
const componentFilter = ref("");
/** "" = 全部任务；TASK_FILTER_NONE = 未归属；其余为任务 ID */
const taskFilter = ref("");
const tasks = ref<TaskDTO[]>([]);
const keyword = ref("");
/** 已提交的搜索词：与输入框分离，避免每敲一个字就打一次接口 */
const submittedKeyword = ref("");
const page = ref(1);
const error = ref<{ offline: boolean; message: string } | null>(null);
/** 硬删除仅对管理员开放（后端 require_admin 兜底） */
const isAdmin = readSession()?.user.role === "admin";

/** 分面计数：与列表分开取，失败时静默降级（侧栏退化成纯筛选器，不影响看板） */
const summary = ref<AlertSummary | null>(null);

const streamStatus = ref<StreamStatus>("connecting");
const liveCount = ref(0);

async function handleDelete(alert: AlertDTO) {
  if (
    !window.confirm(`物理删除告警「${alert.title}」？\n该操作不可恢复（关联事件不受影响）。`)
  )
    return;
  try {
    await alertsApi.remove(alert.id);
    toast.success("告警已删除", { description: `#${alert.id} ${alert.title}` });
    alerts.value = alerts.value.filter((a) => a.id !== alert.id);
    total.value = Math.max(0, total.value - 1);
  } catch (err) {
    toast.error("删除失败", { description: err instanceof Error ? err.message : String(err) });
  }
}

async function load() {
  loading.value = true;
  error.value = null;
  try {
    const data = await alertsApi.recent({
      minutes: WINDOW_MINUTES,
      component: componentFilter.value || undefined,
      severity: severityFilter.value || undefined,
      status: statusFilter.value === "all" ? undefined : statusFilter.value,
      task_id: taskFilter.value || undefined,
      q: submittedKeyword.value || undefined,
      page: page.value,
      size: PAGE_SIZE,
    });
    alerts.value = data.items;
    total.value = data.total;
  } catch (err) {
    const offline = err instanceof ApiError && err.isNetworkError;
    error.value = {
      offline,
      message: offline
        ? "无法连接后端服务，请确认 mindops-backend 已在 8010 端口启动。"
        : err instanceof Error
          ? err.message
          : "加载告警列表失败",
    };
    alerts.value = [];
    total.value = 0;
  } finally {
    loading.value = false;
  }
}

async function loadSummary() {
  try {
    summary.value = await alertsApi.summary({
      minutes: WINDOW_MINUTES,
      task_id: taskFilter.value || undefined,
    });
  } catch {
    // 分面是增强信息，取不到就不显示计数——但不能因此把看板打成错误态
    summary.value = null;
  }
}

watch([componentFilter, severityFilter, statusFilter, submittedKeyword, page], () => void load(), {
  immediate: true,
});

// 任务作用域同时影响列表与分面计数
watch(taskFilter, () => {
  void loadSummary();
});

// 路由守卫用 ?forbidden=1 把非管理员送到这里：提示一次，并把参数从地址栏抹掉，
// 否则用户刷新页面会反复看到同一条提示。
onMounted(() => {
  if (route.query.forbidden !== "1") return;
  toast.error("无权访问", { description: "该页面仅管理员可访问，已返回告警看板。" });
  const query = { ...route.query };
  delete query.forbidden;
  void router.replace({ path: route.path, query });
});

// 任务下拉的数据源：只取未归档任务；取不到时静默退化，不影响看板本身的错误态
async function loadTasks() {
  try {
    const data = await tasksApi.list({ include_archived: false, size: 100 });
    tasks.value = data.items;
  } catch {
    tasks.value = [];
  }
}

// SSE：静默合并期内新到的告警数
let coalesceTimer: number | undefined;
let closeStream: (() => void) | null = null;

onMounted(() => {
  void loadTasks();
  void loadSummary();
  closeStream = openAlertStream({
    onAlert: () => {
      liveCount.value += 1;
      // 去重窗口内的重复告警与单批最多 200 条的 webhook，会让"逐条刷新"
      // 变成对后端的自我 DDoS。1.5s 静默期内合并成一次列表重载。
      window.clearTimeout(coalesceTimer);
      coalesceTimer = window.setTimeout(() => {
        coalesceTimer = undefined;
        liveCount.value = 0;
        void load();
        void loadSummary();
      }, 1500);
    },
    onStatus: (status) => (streamStatus.value = status),
  });
});

onBeforeUnmount(() => {
  closeStream?.();
  window.clearTimeout(coalesceTimer);
});

/** 改筛选条件必须回到第 1 页，否则会停在越界页码上看到空列表 */
function resetToFirstPage() {
  page.value = 1;
}

function formatTime(iso: string) {
  return new Date(iso).toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
}

/** 相对时间：Keep 式「x 分钟前」，超过 24h 回退绝对时间 */
function formatRelative(iso: string) {
  const diff = Date.now() - new Date(iso).getTime();
  if (diff < 0) return formatTime(iso);
  const minutes = Math.floor(diff / 60000);
  if (minutes < 1) return "刚刚";
  if (minutes < 60) return `${minutes} 分钟前`;
  const hours = Math.floor(minutes / 60);
  if (hours < 24) return `${hours} 小时前`;
  return formatTime(iso);
}

/** 严重度色：与 SeverityTag 的四档语义对齐（P0 红 / P1 橙 / P2 蓝 / P3 灰） */
const SEVERITY_COLOR: Record<string, string> = {
  P0: "#e5484d",
  P1: "#f5a623",
  P2: "#0070f3",
  P3: "#9ca3af",
};

function severityColor(severity: string): string {
  return SEVERITY_COLOR[severity] ?? "#9ca3af";
}

// ---------------------------------------------------------------- 分面数据
const severityFacets = computed(() => {
  const rows = summary.value?.by_severity ?? [];
  return [
    { key: "", label: "全部", count: summary.value?.total ?? 0, color: "" },
    ...rows.map((r) => ({ key: r.key, label: r.key, count: r.count, color: severityColor(r.key) })),
  ];
});

const statusFacets = computed(() => {
  const rows = summary.value?.by_status ?? [];
  return [
    { key: "all", label: "全部", count: summary.value?.total ?? 0 },
    ...rows.map((r) => ({ key: r.key, label: STATUS_LABEL[r.key] ?? r.key, count: r.count })),
  ];
});

const componentFacets = computed(() => {
  const rows = summary.value?.by_component ?? [];
  return [
    { key: "", label: "全部", count: summary.value?.total ?? 0 },
    ...rows.map((r) => ({ key: r.key, label: r.key, count: r.count })),
  ];
});

/** 态势卡：高危口径 = P0 + P1（P2/P3 是提醒级，不进卡片） */
const kpiCards = computed(() => {
  const bySeverity = summary.value?.by_severity ?? [];
  const byStatus = summary.value?.by_status ?? [];
  const sevCount = (keys: string[]) =>
    bySeverity.filter((r) => keys.includes(r.key)).reduce((sum, r) => sum + r.count, 0);
  const statusCount = (key: string) => byStatus.find((r) => r.key === key)?.count ?? 0;
  return [
    {
      label: "近 24h 告警",
      value: summary.value?.total ?? 0,
      icon: BellRing,
      tone: "text-glow-pink",
      hint: "含聚合去重后的入库条数",
    },
    {
      label: "高危告警",
      value: sevCount(["P0", "P1"]),
      icon: ShieldAlert,
      tone: "text-[#e5484d]",
      hint: "P0 + P1，需优先处置",
    },
    {
      label: "待人工",
      value: statusCount("human_needed"),
      icon: UserRoundCheck,
      tone: "text-warning",
      hint: "置信度不足已转人工",
    },
    {
      label: "待聚类",
      value: statusCount("unclustered"),
      icon: Layers,
      tone: "text-accent",
      hint: "尚未聚成事件",
    },
  ];
});

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)));
const hasFilter = computed(
  () =>
    statusFilter.value !== "all" ||
    severityFilter.value !== "" ||
    componentFilter.value !== "" ||
    taskFilter.value !== "" ||
    submittedKeyword.value !== "",
);

function clearFilters() {
  statusFilter.value = "all";
  severityFilter.value = "";
  componentFilter.value = "";
  taskFilter.value = "";
  keyword.value = "";
  submittedKeyword.value = "";
  page.value = 1;
}

function submitKeyword() {
  submittedKeyword.value = keyword.value.trim();
  resetToFirstPage();
}

function openIncident(alert: AlertDTO) {
  if (alert.incident_id === null) return;
  void router.push(`/incidents/${alert.incident_id}`);
}
</script>

<template>
  <div class="space-y-4">
    <!-- 态势卡：先给结论，再给明细 -->
    <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
      <StatCard
        v-for="card in kpiCards"
        :key="card.label"
        :label="card.label"
        :value="card.value"
        :icon="card.icon"
        :tone="card.tone"
        :hint="card.hint"
        :loading="summary === null && loading"
      />
    </div>

    <div class="grid lg:grid-cols-[200px_minmax(0,1fr)] gap-4 items-start">
      <!-- 分面筛选栏（Keep 式）：带计数，点即筛 -->
      <aside class="glass rounded-xl p-2 lg:sticky lg:top-4">
        <div class="pb-1">
          <div class="eyebrow px-2 pt-2 pb-1.5">严重度</div>
          <button
            v-for="item in severityFacets"
            :key="`sev-${item.key}`"
            @click="
              () => {
                severityFilter = item.key;
                resetToFirstPage();
              }
            "
            class="w-full flex items-center gap-2 px-2 py-1.5 rounded-lg text-[12px] transition-colors"
            :class="
              severityFilter === item.key
                ? 'bg-secondary text-foreground font-medium'
                : 'text-muted-foreground hover:bg-secondary/50'
            "
          >
            <span
              v-if="item.color"
              class="w-1.5 h-1.5 rounded-full shrink-0"
              :style="{ background: item.color }"
            />
            <span v-else class="w-1.5 h-1.5 shrink-0" />
            <span class="truncate">{{ item.label }}</span>
            <span class="ml-auto tabular-nums text-[11px]">{{ item.count }}</span>
          </button>
        </div>

        <div class="border-t border-border pt-1">
          <div class="eyebrow px-2 pt-2 pb-1.5">状态</div>
          <button
            v-for="item in statusFacets"
            :key="`st-${item.key}`"
            @click="
              () => {
                statusFilter = item.key as StatusFilter;
                resetToFirstPage();
              }
            "
            class="w-full flex items-center px-2 py-1.5 rounded-lg text-[12px] transition-colors"
            :class="
              statusFilter === item.key
                ? 'bg-secondary text-foreground font-medium'
                : 'text-muted-foreground hover:bg-secondary/50'
            "
          >
            <span class="truncate">{{ item.label }}</span>
            <span class="ml-auto tabular-nums text-[11px]">{{ item.count }}</span>
          </button>
        </div>

        <div class="border-t border-border pt-1">
          <div class="eyebrow px-2 pt-2 pb-1.5">组件</div>
          <button
            v-for="item in componentFacets"
            :key="`cmp-${item.key}`"
            @click="
              () => {
                componentFilter = item.key;
                resetToFirstPage();
              }
            "
            class="w-full flex items-center gap-1.5 px-2 py-1.5 rounded-lg text-[12px] transition-colors"
            :class="
              componentFilter === item.key
                ? 'bg-secondary text-foreground font-medium'
                : 'text-muted-foreground hover:bg-secondary/50'
            "
          >
            <span class="truncate font-mono">{{ item.label }}</span>
            <span class="ml-auto tabular-nums text-[11px]">{{ item.count }}</span>
          </button>
        </div>

        <button
          v-if="hasFilter"
          @click="clearFilters"
          class="w-full mt-1 px-2 py-1.5 rounded-lg text-[12px] text-muted-foreground hover:bg-secondary/50 transition-colors"
        >
          清除全部筛选
        </button>
      </aside>

      <!-- 主区 -->
      <div class="space-y-4 min-w-0">
        <!-- 工具栏 -->
        <div class="glass rounded-xl p-3 flex items-center gap-2 flex-wrap">
          <select
            v-model="taskFilter"
            @change="resetToFirstPage"
            class="px-3 py-1.5 rounded-lg border border-border bg-card text-[12px] focus:outline-none focus:ring-2 focus:ring-accent/30 cursor-pointer max-w-[180px]"
            title="按监控任务筛选告警。同一项目可绑定多个任务，因此各任务告警数之和可能大于全局数。"
          >
            <option value="">全部任务</option>
            <option v-for="t in tasks" :key="t.id" :value="t.id">
              {{ t.name }}{{ t.status === "paused" ? "（已暂停）" : "" }}
            </option>
            <option :value="TASK_FILTER_NONE">未归属告警</option>
          </select>

          <div class="relative flex-1 min-w-[180px] max-w-md">
            <Search :size="15" class="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
            <input
              v-model="keyword"
              type="text"
              placeholder="搜索告警标题后按回车..."
              class="w-full pl-9 pr-3 py-1.5 rounded-lg border border-border bg-card text-[12px] focus:outline-none focus:ring-2 focus:ring-accent/30"
              @keydown.enter="submitKeyword"
            />
          </div>

          <!-- 实时状态：少了这个点，用户无法区分"确实没有新告警"和"推送通道断了" -->
          <div
            class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full border border-border text-[11px] text-muted-foreground btn-pill"
            :title="
              streamStatus === 'open'
                ? '已连接后端实时推送（SSE）'
                : streamStatus === 'connecting'
                  ? '正在连接实时推送通道…'
                  : '实时推送已断开，列表仅在手动刷新时更新'
            "
          >
            <span
              class="w-1.5 h-1.5 rounded-full"
              :class="
                streamStatus === 'open'
                  ? 'bg-success animate-pulse'
                  : streamStatus === 'connecting'
                    ? 'bg-warning'
                    : 'bg-muted-foreground/40'
              "
            />
            {{
              streamStatus === "open"
                ? liveCount > 0
                  ? `${liveCount} 条新告警同步中`
                  : "实时"
                : streamStatus === "connecting"
                  ? "连接中"
                  : "已断开"
            }}
          </div>

          <button
            @click="
              () => {
                void load();
                void loadSummary();
              }
            "
            class="inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full border border-border bg-card text-[12px] font-medium hover:bg-secondary transition-colors btn-pill"
          >
            <RefreshCw :size="13" :class="loading ? 'animate-spin' : ''" />
            刷新
          </button>
        </div>

        <!-- 后端不可达提示 -->
        <div
          v-if="error"
          class="glass rounded-xl p-4 flex items-start gap-3 border border-glow-pink/30"
        >
          <WifiOff :size="18" class="text-glow-pink shrink-0 mt-0.5" />
          <div class="flex-1 min-w-0">
            <div class="text-[13px] font-medium">
              {{ error.offline ? "后端未连接" : "告警列表加载失败" }}
            </div>
            <div class="text-[12px] text-muted-foreground mt-1 break-words">{{ error.message }}</div>
          </div>
          <button
            @click="load"
            class="shrink-0 flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-secondary text-[12px] font-medium hover:bg-secondary/70 transition-colors"
          >
            <RefreshCw :size="13" />
            重试
          </button>
        </div>

        <!-- 列表 -->
        <div class="glass rounded-xl overflow-hidden">
          <div v-if="loading" class="p-4 space-y-3">
            <div v-for="i in 6" :key="i" class="flex items-center gap-4 h-10">
              <div class="h-3.5 bg-secondary rounded animate-pulse w-32" />
              <div class="h-3.5 bg-secondary rounded animate-pulse w-16" />
              <div class="h-3.5 bg-secondary rounded animate-pulse w-10" />
              <div class="h-3.5 bg-secondary rounded animate-pulse flex-1" />
              <div class="h-3.5 bg-secondary rounded animate-pulse w-20" />
            </div>
          </div>

          <!-- 空态：区分"没接入"与"筛没了"两种原因，并给出下一步动作 -->
          <div v-else-if="alerts.length === 0" class="py-14 flex flex-col items-center gap-3">
            <div class="w-12 h-12 rounded-2xl bg-secondary flex items-center justify-center">
              <Inbox :size="22" class="text-muted-foreground" />
            </div>
            <div class="text-[15px] font-medium">
              {{ hasFilter ? "当前筛选下没有告警" : "近 24 小时没有接入告警" }}
            </div>
            <div class="text-[12px] text-muted-foreground text-center max-w-[380px] leading-relaxed">
              {{
                hasFilter
                  ? "换个分面条件，或清除筛选查看全量。"
                  : "在测试项目控制台点一个故障开关，告警会自动推送到这里并触发诊断。"
              }}
            </div>
            <div class="flex gap-2 mt-1">
              <button
                v-if="hasFilter"
                @click="clearFilters"
                class="px-4 py-1.5 rounded-full border border-border bg-card text-[12px] font-medium hover:bg-secondary transition-colors btn-pill"
              >
                清除筛选
              </button>
              <button
                v-else
                @click="void router.push('/tasks')"
                class="px-4 py-1.5 rounded-full bg-primary text-primary-foreground text-[12px] font-medium hover:bg-primary/90 transition-colors btn-pill"
              >
                查看监控任务
              </button>
            </div>
          </div>

          <!-- Keep 式单行密排告警流 -->
          <div v-else class="divide-y divide-border">
            <div
              v-for="alert in alerts"
              :key="alert.id"
              class="relative flex items-center gap-3 pl-4 pr-3 py-2 transition-colors"
              :class="alert.incident_id !== null ? 'hover:bg-secondary/30 cursor-pointer' : 'cursor-default'"
              @click="openIncident(alert)"
            >
              <!-- 左缘严重度色带 -->
              <span
                class="absolute left-0 top-0 bottom-0 w-[3px]"
                :style="{ background: severityColor(alert.severity) }"
                :title="alert.severity"
              />
              <div class="min-w-0 flex-1">
                <div class="flex items-center gap-2 min-w-0">
                  <span class="text-[13px] font-medium truncate">{{ alert.title }}</span>
                  <span
                    class="shrink-0 text-[10px] font-mono px-1.5 py-0.5 rounded"
                    :style="{
                      color: severityColor(alert.severity),
                      background: `${severityColor(alert.severity)}14`,
                    }"
                    >{{ alert.severity }}</span
                  >
                </div>
                <div class="flex items-center gap-1.5 text-[11px] text-muted-foreground mt-0.5 min-w-0">
                  <span class="font-mono uppercase shrink-0">{{ alert.component }}</span>
                  <span class="shrink-0">·</span>
                  <span class="truncate">{{ alert.source }}</span>
                  <template v-if="alert.task_names.length > 0">
                    <span class="shrink-0">·</span>
                    <span class="truncate">{{ alert.task_names.join(" / ") }}</span>
                  </template>
                </div>
              </div>
              <span
                class="shrink-0 text-[11px] font-mono tabular-nums px-2 py-0.5 rounded-full border"
                :class="
                  alert.dedup_count > 1
                    ? 'border-glow-orange/40 bg-glow-orange/10 text-[#8a5306]'
                    : 'border-border text-muted-foreground'
                "
                :title="alert.dedup_count > 1 ? `同指纹告警已聚合 ${alert.dedup_count} 条` : '同指纹首次上报'"
              >
                {{ alert.dedup_count > 1 ? `聚合 ×${alert.dedup_count}` : "首报" }}
              </span>
              <StatusBadge
                v-if="alert.incident_status"
                :status="
                  alert.incident_status as 'new' | 'diagnosing' | 'human_needed' | 'resolved' | 'closed'
                "
              />
              <span
                v-else
                class="shrink-0 inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium bg-secondary/60 text-muted-foreground"
              >
                <Link2Off :size="10" />
                待聚类
              </span>
              <span
                class="shrink-0 w-[70px] text-right text-[11px] text-muted-foreground tabular-nums"
                :title="formatTime(alert.created_at)"
              >
                {{ formatRelative(alert.last_seen_at ?? alert.created_at) }}
              </span>
              <button
                v-if="isAdmin"
                @click.stop="handleDelete(alert)"
                title="物理删除（不可恢复）"
                class="shrink-0 inline-flex items-center justify-center w-6 h-6 rounded-md text-muted-foreground hover:text-red-600 hover:bg-red-50 transition-colors"
              >
                <Trash2 :size="13" />
              </button>
              <ChevronRight
                :size="15"
                :class="
                  alert.incident_id !== null
                    ? 'text-muted-foreground shrink-0'
                    : 'text-muted-foreground/25 shrink-0'
                "
              />
            </div>
          </div>

          <!-- 分页（服务端分页：total 来自后端 count，不是当前页条数） -->
          <div
            v-if="!loading && alerts.length > 0"
            class="px-4 py-2.5 border-t border-border flex items-center justify-between"
          >
            <span class="text-[11px] text-muted-foreground">
              共 {{ total }} 条，第 {{ page }} / {{ totalPages }} 页
            </span>
            <div class="flex gap-1">
              <button
                @click="page = Math.max(1, page - 1)"
                :disabled="page <= 1"
                class="px-3 py-1 rounded-lg border border-border bg-card text-[11px] disabled:opacity-40 hover:bg-secondary transition-colors"
              >
                上一页
              </button>
              <button
                @click="page = page + 1"
                :disabled="page >= totalPages"
                class="px-3 py-1 rounded-lg border border-border bg-card text-[11px] disabled:opacity-40 hover:bg-secondary transition-colors"
              >
                下一页
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
