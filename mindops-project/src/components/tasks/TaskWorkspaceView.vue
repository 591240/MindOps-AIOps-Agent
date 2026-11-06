<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { useRouter } from "vue-router";
import { tasksApi, syncTaskTargets } from "@/api/tasks";
import { alertsApi, type AlertDTO } from "@/api/alerts";
import { incidentsApi } from "@/api/incidents";
import { knowledgeApi, type KnowledgeDocDTO } from "@/api/knowledge";
import { useTaskDetail, useTaskList, toggleTaskStatus } from "@/lib/task-store";
import { ApiError } from "@/api/client";
import { asIncidentStatus, asSeverity } from "@/types/incident";
import TaskStatusPill from "./TaskStatusPill.vue";
import TaskTargetPicker from "./TaskTargetPicker.vue";
import TaskWizard from "./TaskWizard.vue";
import WorkspaceEmptyPane from "./WorkspaceEmptyPane.vue";
import WorkspaceLoadError from "./WorkspaceLoadError.vue";
import SeverityTag from "@/components/business/SeverityTag.vue";
import StatusBadge from "@/components/business/StatusBadge.vue";
import DedupBadge from "@/components/business/DedupBadge.vue";
import AppDialog from "@/components/ui/AppDialog.vue";
import Button from "@/components/ui/Button.vue";
import { TabsRoot, TabsList, TabsTrigger, TabsContent } from "reka-ui";
import { toast } from "vue-sonner";
import {
  ArrowLeft,
  Pencil,
  Pause,
  Play,
  Archive,
  Plus,
  Server,
  ChevronRight,
  FileText,
  BookOpen,
} from "lucide-vue-next";

/**
 * 任务工作台 /tasks/{id}（F-12 §8.4，docs/26 §2.9 页面三）
 *
 * 只呈现「归属于本任务的项目」所产生的告警 / 事件 / 知识。
 * 归属推导见 docs/30 §5.3：告警 → 集成目标 → 任务（N:N），不做 alerts.task_id 强绑定。
 *
 * 第二轮修复：数据全部改为真实接口。
 * - 任务与绑定目标：`GET /tasks/{id}`（`targets` 明细，不再依赖 mockIntegrations）
 * - 告警：`GET /alerts/recent?task_id=` —— 后端唯一原生支持任务过滤的端点
 * - 事件：后端 `/incidents` **没有** task_id 参数，改为"本任务告警的 incident_id 集合"
 *   再反查，这是 docs/30 §5.3 那条链路的直接推论，不是猜测
 * - 知识：知识文档是**组件级**资产（`knowledge_docs.component`），既无 integration_id
 *   也无 task_id，只能用"本任务告警覆盖了哪些组件"作桥，逐个组件查
 */

const props = defineProps<{ id: string }>();
const router = useRouter();
const { task, loading, error, refresh } = useTaskDetail(() => props.id);

const manageOpen = ref(false);
const draftIds = ref<number[]>([]);
const wizardOpen = ref(false);
const archiving = ref(false);
const busy = ref(false);

// 归属数据：告警 → 事件 → 知识，三级依次推导
interface WorkspaceIncident {
  id: number;
  title: string;
  severity: string;
  status: string;
  confidence: number | null;
}

const alerts = ref<AlertDTO[]>([]);
const alertTotal = ref(0);
const incidents = ref<WorkspaceIncident[]>([]);
const knowledge = ref<KnowledgeDocDTO[]>([]);
const tabError = ref<{ offline: boolean; message: string } | null>(null);
const tabLoading = ref(false);

const SOURCE_LABEL: Record<string, string> = {
  runbook: "运维手册",
  incident: "事件回灌",
  upload: "上传文档",
  api_doc: "接口文档",
  best_practice: "最佳实践",
};

const targets = computed(() => task.value?.targets ?? []);
/** 已绑定项目 id（`TaskDTO` 不带 target_ids，编辑回填要用） */
const targetIds = computed(() => targets.value.map((t) => t.integration_id));

/**
 * 已有任务名：仅用于编辑向导的同名冲突前置校验。
 * 后端仍会以 409 DUPLICATE_TASK_NAME 兜底，这里只是让用户少跑一次往返。
 */
const { items: allTasks } = useTaskList({ size: 100 });
const existingNames = computed(() => allTasks.value.map((t) => t.name));

const activeIncidentCount = computed(
  () =>
    incidents.value.filter((inc) => inc.status !== "resolved" && inc.status !== "closed").length,
);

async function loadAttributed() {
  if (!props.id) return;
  tabLoading.value = true;
  tabError.value = null;
  try {
    // 1) 本任务归属的告警（后端原生支持 task_id 过滤）
    const alertPage = await alertsApi.recent({ task_id: props.id, minutes: 1440, size: 100 });
    alerts.value = alertPage.items;
    alertTotal.value = alertPage.total;

    // 2) 事件：用归属告警的 incident_id 集合反查（/incidents 无 task_id 参数）
    const incidentIds = [
      ...new Set(
        alertPage.items.map((a) => a.incident_id).filter((x): x is number => x !== null),
      ),
    ];
    if (incidentIds.length) {
      const page = await incidentsApi.list({ size: 100 });
      const wanted = new Set(incidentIds);
      incidents.value = page.items
        .filter((inc) => wanted.has(inc.id))
        .map((inc) => ({
          id: inc.id,
          title: inc.title,
          severity: inc.severity,
          status: inc.status,
          confidence: inc.root_cause_confidence,
        }));
    } else {
      incidents.value = [];
    }

    // 3) 知识：知识文档是组件级资产，只能用"归属告警覆盖的组件"作桥，逐组件查
    const components = [...new Set(alertPage.items.map((a) => a.component))];
    if (components.length === 0) {
      knowledge.value = [];
    } else {
      const pages = await Promise.all(
        components.map((c) =>
          knowledgeApi.list({ component: c, page_size: 50 }).catch(() => ({ total: 0, items: [] })),
        ),
      );
      const seen = new Set<number>();
      const merged: KnowledgeDocDTO[] = [];
      for (const page of pages) {
        for (const doc of page.items) {
          if (seen.has(doc.doc_id)) continue;
          seen.add(doc.doc_id);
          merged.push(doc);
        }
      }
      knowledge.value = merged;
    }
  } catch (err) {
    const offline = err instanceof ApiError && err.isNetworkError;
    tabError.value = {
      offline,
      message: offline
        ? "无法连接后端服务，请确认 mindops-backend 已在 8010 端口启动。"
        : err instanceof Error
          ? err.message
          : "加载归属数据失败",
    };
    alerts.value = [];
    incidents.value = [];
    knowledge.value = [];
  } finally {
    tabLoading.value = false;
  }
}

watch(() => props.id, () => void loadAttributed(), { immediate: true });

function openManage() {
  draftIds.value = targets.value.map((t) => t.integration_id);
  manageOpen.value = true;
}

async function saveTargets() {
  if (!task.value) return;
  busy.value = true;
  try {
    const { failed } = await syncTaskTargets(
      task.value.id,
      targets.value.map((t) => t.integration_id),
      draftIds.value,
    );
    if (failed.length) {
      toast.warning("部分项目未同步成功", {
        description: failed.map((f) => `#${f.id}：${f.message}`).join("；"),
      });
    } else {
      toast.success("绑定已更新");
    }
    manageOpen.value = false;
    await Promise.all([refresh(), loadAttributed()]);
  } catch (err) {
    toast.error("绑定更新失败", {
      description: err instanceof Error ? err.message : "未知错误",
    });
  } finally {
    busy.value = false;
  }
}

async function changeStatus() {
  if (!task.value) return;
  try {
    await tasksApi.update(task.value.id, { status: toggleTaskStatus(task.value.status) });
    await refresh();
  } catch (err) {
    toast.error("状态变更失败", {
      description: err instanceof Error ? err.message : "未知错误",
    });
  }
}

async function confirmArchive() {
  if (!task.value) return;
  busy.value = true;
  try {
    await tasksApi.archive(task.value.id);
    toast.success("任务已归档", {
      description: "归档是终态，不可恢复；历史告警与诊断记录保留用于审计。",
    });
    archiving.value = false;
    await refresh();
  } catch (err) {
    toast.error("归档失败", {
      description: err instanceof Error ? err.message : "未知错误",
    });
  } finally {
    busy.value = false;
  }
}

async function handleEditSubmit(payload: {
  name: string;
  description: string;
  target_ids: number[];
  id?: string;
}) {
  if (!task.value) return;
  busy.value = true;
  try {
    await tasksApi.update(task.value.id, { name: payload.name, description: payload.description });
    const { failed } = await syncTaskTargets(
      task.value.id,
      targets.value.map((t) => t.integration_id),
      payload.target_ids,
    );
    if (failed.length) {
      toast.warning("任务已保存，但部分项目绑定失败", {
        description: failed.map((f) => `#${f.id}：${f.message}`).join("；"),
      });
    } else {
      toast.success("任务已更新");
    }
    wizardOpen.value = false;
    await Promise.all([refresh(), loadAttributed()]);
  } catch (err) {
    toast.error("更新任务失败", {
      description: err instanceof Error ? err.message : "未知错误",
    });
  } finally {
    busy.value = false;
  }
}

/** 告警表格一次最多渲染这么多行；真实总数用后端 total 呈现，不假装是全部。 */
const ALERT_ROWS = 10;
const shownAlerts = computed(() => alerts.value.slice(0, ALERT_ROWS));
</script>

<template>
  <div v-if="loading" class="space-y-4">
    <div class="glass rounded-xl p-5 h-40 animate-pulse" />
    <div class="glass rounded-xl p-5 h-32 animate-pulse" />
  </div>

  <div
    v-else-if="error?.notFound || (!task && !error)"
    class="glass rounded-xl py-20 flex flex-col items-center gap-3"
  >
    <div class="w-12 h-12 rounded-full bg-secondary flex items-center justify-center">
      <FileText :size="22" class="text-muted-foreground" />
    </div>
    <div class="text-[15px] font-medium">任务不存在或已归档</div>
    <div class="text-[13px] text-muted-foreground text-center max-w-[380px]">
      没有找到 id 为 {{ id }} 的任务，它可能已被删除，或链接里的 id 不正确。
    </div>
    <button
      @click="void router.push('/tasks')"
      class="mt-2 px-4 py-2 rounded-full border border-border bg-card text-[13px] font-medium hover:bg-secondary transition-colors btn-pill"
    >
      返回任务列表
    </button>
  </div>

  <WorkspaceLoadError
    v-else-if="!task"
    :message="error?.message ?? '加载任务详情失败'"
    :offline="error?.offline ?? false"
    @retry="void refresh()"
  />

  <div v-else class="space-y-4">
    <!-- 顶部：任务名 + 状态 + 操作 -->
    <div class="glass rounded-xl p-5">
      <button
        @click="void router.push('/tasks')"
        class="inline-flex items-center gap-1 text-[12px] text-muted-foreground hover:text-foreground transition-colors mb-2"
      >
        <ArrowLeft :size="13" />
        返回任务列表
      </button>

      <div class="flex items-start justify-between gap-4 flex-wrap">
        <div class="min-w-0">
          <div class="flex items-center gap-3">
            <h2 class="text-[22px] font-semibold heading-tight truncate">{{ task.name }}</h2>
            <TaskStatusPill :status="task.status" />
          </div>
          <p class="mt-1 text-[13px] text-muted-foreground">{{ task.description || "（无描述）" }}</p>
        </div>

        <div class="flex items-center gap-2 shrink-0">
          <button
            v-if="task.status !== 'archived'"
            @click="wizardOpen = true"
            class="inline-flex items-center gap-1.5 h-9 px-3.5 rounded-full border border-border bg-card text-[13px] font-medium btn-pill hover:bg-secondary transition-colors"
          >
            <Pencil :size="14" />
            编辑
          </button>
          <button
            v-if="task.status !== 'archived'"
            @click="void changeStatus()"
            class="inline-flex items-center gap-1.5 h-9 px-3.5 rounded-full border border-border bg-card text-[13px] font-medium btn-pill hover:bg-secondary transition-colors"
          >
            <Pause v-if="task.status === 'active'" :size="14" />
            <Play v-else :size="14" />
            {{ task.status === "active" ? "暂停" : "恢复" }}
          </button>
          <button
            v-if="task.status !== 'archived'"
            @click="archiving = true"
            class="inline-flex items-center gap-1.5 h-9 px-3.5 rounded-full border border-border bg-card text-[13px] font-medium btn-pill hover:bg-secondary transition-colors"
          >
            <Archive :size="14" />
            归档
          </button>
        </div>
      </div>

      <!-- 概览条 -->
      <div class="mt-4 pt-4 border-t border-border grid grid-cols-3 gap-4">
        <div>
          <div class="text-[12px] text-muted-foreground">绑定项目</div>
          <div class="text-[18px] font-medium font-mono tabular-nums">{{ targets.length }}</div>
        </div>
        <div>
          <div class="text-[12px] text-muted-foreground">近 24h 告警</div>
          <div class="text-[18px] font-medium font-mono tabular-nums">{{ task.alerts_24h }}</div>
        </div>
        <div>
          <div class="text-[12px] text-muted-foreground">进行中事件</div>
          <div class="text-[18px] font-medium font-mono tabular-nums">{{ activeIncidentCount }}</div>
        </div>
      </div>
    </div>

    <!-- 绑定项目 -->
    <div class="glass rounded-xl p-5">
      <div class="flex items-center justify-between mb-3">
        <h3 class="text-[14px] font-medium">监控的项目</h3>
        <span v-if="task.status === 'archived'" class="text-[12px] text-muted-foreground">
          已归档，绑定不可变更
        </span>
        <button
          v-else
          @click="openManage"
          class="inline-flex items-center gap-1.5 h-8 px-3 rounded-full border border-border bg-card text-[12px] font-medium btn-pill hover:bg-secondary transition-colors"
        >
          <Plus :size="13" />
          管理项目
        </button>
      </div>

      <div
        v-if="targets.length === 0"
        class="py-8 flex flex-col items-center gap-2 border border-dashed border-border rounded-xl"
      >
        <Server :size="22" class="text-muted-foreground" />
        <div class="text-[13px] font-medium">本任务还没有绑定项目，先添加一个</div>
        <div class="text-[12px] text-muted-foreground">未绑定项目时，这个任务收不到任何告警。</div>
        <button
          v-if="task.status !== 'archived'"
          @click="openManage"
          class="mt-1 px-4 py-2 rounded-full bg-primary text-primary-foreground text-[13px] font-medium btn-pill hover:bg-primary/90"
        >
          选择要监控的项目
        </button>
      </div>

      <div v-else class="grid grid-cols-1 md:grid-cols-2 gap-3">
        <div
          v-for="t in targets"
          :key="t.integration_id"
          class="rounded-xl border border-border p-3.5 flex items-center justify-between gap-3"
        >
          <div class="min-w-0">
            <div class="text-[13px] font-medium truncate">{{ t.name }}</div>
            <div class="text-[12px] text-muted-foreground font-mono truncate">
              {{ t.base_url ?? "—" }}
            </div>
            <div
              v-if="t.scrape_config"
              class="text-[11px] text-muted-foreground font-mono truncate"
            >
              {{ t.scrape_config }}
            </div>
          </div>
          <div class="text-right shrink-0">
            <div class="text-[11px] text-muted-foreground">目标数</div>
            <div class="text-[13px] font-mono tabular-nums">
              {{ t.targets_up ?? "—" }} / {{ t.targets_total ?? "—" }}
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 三 Tab：告警 / 事件 / 知识 -->
    <div v-if="targets.length === 0" class="glass rounded-xl py-14 flex flex-col items-center gap-2">
      <div class="text-[13px] font-medium">先绑定监控项目</div>
      <div class="text-[12px] text-muted-foreground">
        绑定后，这些项目产生的告警与诊断结果会汇总到这里。
      </div>
    </div>

    <TabsRoot v-else default-value="alerts">
      <TabsList
        class="inline-flex h-9 items-center justify-center rounded-lg bg-muted p-1 text-muted-foreground"
      >
        <TabsTrigger
          v-for="tab in [
            { value: 'alerts', label: '告警' },
            { value: 'incidents', label: '事件' },
            { value: 'knowledge', label: '知识' },
          ]"
          :key="tab.value"
          :value="tab.value"
          class="inline-flex items-center justify-center whitespace-nowrap rounded-md px-3 py-1 text-sm font-medium ring-offset-background cursor-pointer transition-all focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2 disabled:pointer-events-none disabled:opacity-50 disabled:cursor-not-allowed data-[state=active]:bg-background data-[state=active]:text-foreground data-[state=active]:shadow"
        >
          {{ tab.label }}
        </TabsTrigger>
      </TabsList>

      <TabsContent
        value="alerts"
        class="mt-4 ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
      >
        <WorkspaceLoadError
          v-if="tabError"
          :message="tabError.message"
          :offline="tabError.offline"
          @retry="void loadAttributed()"
        />
        <div v-else-if="tabLoading" class="glass rounded-xl h-40 animate-pulse" />
        <WorkspaceEmptyPane
          v-else-if="alerts.length === 0"
          title="暂无归属告警"
          hint="绑定的监控项目还没有产生告警。未归属的告警仍可在全局告警看板查看。"
        />
        <div v-else class="glass rounded-xl overflow-hidden">
          <table class="w-full">
            <thead>
              <tr class="border-b border-border bg-secondary/40">
                <th class="text-left px-4 py-3 text-[12px] font-semibold text-muted-foreground">时间</th>
                <th class="text-left px-4 py-3 text-[12px] font-semibold text-muted-foreground">组件</th>
                <th class="text-left px-4 py-3 text-[12px] font-semibold text-muted-foreground">严重度</th>
                <th class="text-left px-4 py-3 text-[12px] font-semibold text-muted-foreground">标题</th>
                <th class="text-left px-4 py-3 text-[12px] font-semibold text-muted-foreground">事件状态</th>
                <th class="text-left px-4 py-3 text-[12px] font-semibold text-muted-foreground">去重</th>
                <th class="text-right px-4 py-3 text-[12px] font-semibold text-muted-foreground">操作</th>
              </tr>
            </thead>
            <tbody>
              <tr
                v-for="a in shownAlerts"
                :key="a.id"
                @click="
                  a.incident_id !== null ? void router.push(`/incidents/${a.incident_id}`) : undefined
                "
                :class="[
                  'border-b border-border last:border-0 transition-colors h-12',
                  a.incident_id !== null ? 'hover:bg-secondary/30 cursor-pointer' : 'cursor-default',
                ]"
              >
                <td
                  class="px-4 py-3 text-[13px] text-muted-foreground font-mono tabular-nums whitespace-nowrap"
                >
                  {{
                    new Date(a.created_at).toLocaleString("zh-CN", {
                      month: "2-digit",
                      day: "2-digit",
                      hour: "2-digit",
                      minute: "2-digit",
                      hour12: false,
                    })
                  }}
                </td>
                <td class="px-4 py-3 text-[13px] font-mono uppercase">{{ a.component }}</td>
                <td class="px-4 py-3"><SeverityTag :severity="asSeverity(a.severity)" /></td>
                <td class="px-4 py-3 text-[13px]">{{ a.title }}</td>
                <td class="px-4 py-3">
                  <StatusBadge
                    v-if="a.incident_status"
                    :status="asIncidentStatus(a.incident_status)"
                  />
                  <span v-else class="text-[12px] text-muted-foreground">待聚类</span>
                </td>
                <td class="px-4 py-3"><DedupBadge :count="a.dedup_count" /></td>
                <td class="px-4 py-3 text-right">
                  <ChevronRight
                    :size="16"
                    :class="[
                      'inline',
                      a.incident_id !== null ? 'text-muted-foreground' : 'text-muted-foreground/30',
                    ]"
                  />
                </td>
              </tr>
            </tbody>
          </table>
          <div
            v-if="alertTotal > shownAlerts.length"
            class="px-4 py-2.5 text-[12px] text-muted-foreground bg-secondary/20"
          >
            仅显示最近 {{ shownAlerts.length }} 条，本任务近 24h 共 {{ alertTotal }} 条归属告警
          </div>
        </div>
      </TabsContent>

      <TabsContent
        value="incidents"
        class="mt-4 ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
      >
        <WorkspaceLoadError
          v-if="tabError"
          :message="tabError.message"
          :offline="tabError.offline"
          @retry="void loadAttributed()"
        />
        <div v-else-if="tabLoading" class="glass rounded-xl h-40 animate-pulse" />
        <WorkspaceEmptyPane
          v-else-if="incidents.length === 0"
          title="暂无归属事件"
          hint="本任务绑定的目标产生的告警尚未聚合成诊断事件。"
        />
        <div v-else class="glass rounded-xl overflow-hidden">
          <div
            v-for="inc in incidents"
            :key="inc.id"
            @click="void router.push(`/incidents/${inc.id}`)"
            class="px-4 py-4 flex items-center gap-3 hover:bg-secondary/30 cursor-pointer transition-colors border-b border-border last:border-0"
          >
            <SeverityTag :severity="asSeverity(inc.severity)" />
            <div class="flex-1 min-w-0">
              <div class="text-[13px] font-medium truncate">{{ inc.title }}</div>
              <div class="text-[12px] text-muted-foreground font-mono">
                #{{ inc.id }} · 置信度
                {{ inc.confidence === null ? "—" : `${(inc.confidence * 100).toFixed(0)}%` }}
              </div>
            </div>
            <StatusBadge :status="asIncidentStatus(inc.status)" />
            <ChevronRight :size="16" class="text-muted-foreground" />
          </div>
        </div>
      </TabsContent>

      <TabsContent
        value="knowledge"
        class="mt-4 ring-offset-background focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-ring focus-visible:ring-offset-2"
      >
        <WorkspaceLoadError
          v-if="tabError"
          :message="tabError.message"
          :offline="tabError.offline"
          @retry="void loadAttributed()"
        />
        <div v-else-if="tabLoading" class="glass rounded-xl h-40 animate-pulse" />
        <WorkspaceEmptyPane
          v-else-if="knowledge.length === 0"
          title="暂无关联知识"
          hint="归属告警涉及的组件还没有对应的知识文档。"
        />
        <div v-else class="glass rounded-xl overflow-hidden">
          <div
            v-for="k in knowledge"
            :key="k.doc_id"
            class="px-4 py-4 flex items-start gap-3 border-b border-border last:border-0"
          >
            <BookOpen :size="16" class="text-muted-foreground mt-0.5 shrink-0" />
            <div class="flex-1 min-w-0">
              <div class="text-[13px] font-medium">{{ k.title }}</div>
              <!-- 列表接口不返回正文，因此这里不摆"摘要"占位，只给真实元数据 -->
              <div class="text-[12px] text-muted-foreground">
                {{ k.chunk_count }} 个切片 · {{ k.created_by || "未知来源" }}
              </div>
              <div class="mt-1 flex items-center gap-2 text-[11px] text-muted-foreground">
                <span class="px-1.5 py-0.5 rounded border border-border">
                  {{ SOURCE_LABEL[k.source_type] ?? k.source_type }}
                </span>
                <span class="font-mono">{{ k.component }}</span>
                <span>v{{ k.version }}</span>
              </div>
            </div>
          </div>
        </div>
      </TabsContent>
    </TabsRoot>

    <!-- 管理项目（勾选式增删） -->
    <AppDialog v-model:open="manageOpen" max-width="560px">
      <template #title>
        <span class="text-[16px] font-semibold">管理监控项目</span>
      </template>
      <TaskTargetPicker :selected-ids="draftIds" @change="draftIds = $event" />
      <template #footer>
        <Button
          @click="manageOpen = false"
          class="h-9 px-4 rounded-full border border-border bg-card text-foreground text-[13px] font-medium btn-pill hover:bg-secondary"
        >
          取消
        </Button>
        <Button
          @click="void saveTargets()"
          :disabled="busy"
          class="h-9 px-4 rounded-full bg-primary text-primary-foreground text-[13px] font-medium btn-pill hover:bg-primary/90 disabled:opacity-60"
        >
          保存绑定
        </Button>
      </template>
    </AppDialog>

    <!-- 编辑任务 -->
    <TaskWizard
      :open="wizardOpen"
      @update:open="wizardOpen = $event"
      :edit-task="task"
      :edit-target-ids="targetIds"
      :existing-names="existingNames"
      :on-submit="handleEditSubmit"
    />

    <!-- 归档二次确认 -->
    <AppDialog v-model:open="archiving" max-width="400px">
      <template #title>
        <span class="text-[15px] font-semibold">归档任务</span>
      </template>
      <p class="text-[13px] text-muted-foreground">
        任务「{{ task.name }}」将移出列表。归档是<b>终态、不可恢复</b>，其告警与诊断结果仍保留用于审计。
      </p>
      <template #footer>
        <Button
          @click="archiving = false"
          class="h-9 px-4 rounded-full border border-border bg-card text-foreground text-[13px] font-medium btn-pill hover:bg-secondary"
        >
          取消
        </Button>
        <Button
          @click="void confirmArchive()"
          :disabled="busy"
          class="h-9 px-4 rounded-full bg-primary text-primary-foreground text-[13px] font-medium btn-pill hover:bg-primary/90 disabled:opacity-60"
        >
          确认归档
        </Button>
      </template>
    </AppDialog>
  </div>
</template>
