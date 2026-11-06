<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import {
  Plus,
  RefreshCw,
  Search,
  Loader2,
  CheckCircle2,
  XCircle,
  Activity,
  WifiOff,
} from "lucide-vue-next";
import Button from "@/components/ui/Button.vue";
import Input from "@/components/ui/Input.vue";
import Label from "@/components/ui/Label.vue";
import Badge from "@/components/ui/Badge.vue";
import Switch from "@/components/ui/Switch.vue";
import Select from "@/components/ui/Select.vue";
import AppDialog from "@/components/ui/AppDialog.vue";
import AppTooltip from "@/components/ui/AppTooltip.vue";
import { validateInternalAddress, INTERNAL_HINT } from "@/lib/network-guard";
import { integrationsApi, type IntegrationDTO } from "@/api/integrations";
import { ApiError, API_BASE_URL } from "@/api/client";
import { toast } from "vue-sonner";

/** 集成目标状态四态（与后端 `integrations.status` 一致） */
type IntegrationStatus = "pending" | "active" | "error" | "disabled";

/**
 * 集成目标视图模型（后端 `IntegrationDTO` 的展示形态）。
 *
 * 注意 `id` 在这里是**字符串**（DOM key 与 URL 参数用），来自后端的数值 id；
 * `fromDTO()` 负责转换，`integrationsApi` 内部会再转回数值。
 */
interface Integration {
  id: string;
  name: string;
  type: "prometheus";
  base_url: string;
  scrape_config?: string;
  status: IntegrationStatus;
  last_heartbeat?: string;
  last_error?: string;
  targets_total?: number;
  targets_up?: number;
  created_by: string;
  created_at: string;
}

const STATUS_CONFIG: Record<IntegrationStatus, { label: string; color: string; bgColor: string }> = {
  pending: { label: "待探测", color: "text-warning", bgColor: "bg-warning/10" },
  active: { label: "已连通", color: "text-success", bgColor: "bg-success/10" },
  error: { label: "异常", color: "text-danger", bgColor: "bg-danger/10" },
  disabled: { label: "已停用", color: "text-muted-foreground", bgColor: "bg-muted" },
};

const STATUS_FILTER_OPTIONS = [
  { value: "all", label: "全部状态" },
  { value: "pending", label: "待探测" },
  { value: "active", label: "已连通" },
  { value: "error", label: "异常" },
  { value: "disabled", label: "已停用" },
];

// 相对时间格式化
function formatRelativeTime(isoString?: string): string {
  if (!isoString) return "—";
  const date = new Date(isoString);
  const now = new Date();
  const diffMs = now.getTime() - date.getTime();
  const diffMins = Math.floor(diffMs / 60000);
  const diffHours = Math.floor(diffMs / 3600000);
  const diffDays = Math.floor(diffMs / 86400000);

  if (diffMins < 1) return "刚刚";
  if (diffMins < 60) return `${diffMins} 分钟前`;
  if (diffHours < 24) return `${diffHours} 小时前`;
  if (diffDays < 30) return `${diffDays} 天前`;
  return date.toLocaleDateString("zh-CN");
}

/** 后端 DTO → 视图模型（后端 id 为数值，前端统一用字符串）。 */
function fromDTO(row: IntegrationDTO): Integration {
  return {
    id: String(row.id),
    name: row.name,
    type: (row.type as Integration["type"]) ?? "prometheus",
    base_url: row.base_url,
    scrape_config: row.scrape_config ?? undefined,
    status: row.status,
    last_heartbeat: row.last_heartbeat ?? undefined,
    last_error: row.last_error ?? undefined,
    targets_total: row.targets_total ?? undefined,
    targets_up: row.targets_up ?? undefined,
    created_by: row.created_by ?? "—",
    created_at: row.created_at ?? "",
  };
}

/** 视图层筛选（后端分页/筛选尚未覆盖的展示维度，保持原有交互）。 */
function applyLocalFilters(
  rows: Integration[],
  opts: {
    showDisabled: boolean;
    statusFilter: "all" | IntegrationStatus;
    keyword: string;
  },
): Integration[] {
  let data = [...rows];
  if (!opts.showDisabled) data = data.filter((i) => i.status !== "disabled");
  if (opts.statusFilter !== "all") data = data.filter((i) => i.status === opts.statusFilter);
  if (opts.keyword) {
    const lower = opts.keyword.toLowerCase();
    data = data.filter(
      (i) => i.name.toLowerCase().includes(lower) || i.base_url.toLowerCase().includes(lower),
    );
  }
  return data;
}

const loading = ref(true);
const integrations = ref<Integration[]>([]);
/** 列表加载失败（含后端未启动）；非空时页面顶部展示错误条 + 重试 */
const loadError = ref<{ offline: boolean; message: string } | null>(null);
const statusFilter = ref<"all" | IntegrationStatus>("all");
const keyword = ref("");
const showDisabled = ref(false);
const page = ref(1);
const pageSize = 10;

// 弹窗状态
const dialogOpen = ref(false);
const editingIntegration = ref<Integration | null>(null);
const formData = ref({
  name: "",
  type: "prometheus" as const,
  base_url: "",
  scrape_config: "",
});
const testing = ref(false);
const saving = ref(false);
const testResult = ref<{
  ok: boolean;
  message: string;
  targets_total?: number;
  targets_up?: number;
  latency_ms?: number;
} | null>(null);
const urlError = ref("");

// 删除确认
const deleteDialogOpen = ref(false);
const deletingId = ref<string | null>(null);

// 真实拉取 GET /api/v1/integrations。
// 后端不可达时**不再注入本地示例数据** —— 那会让人误以为"已经有 4 个集成"，
// 且离线保存只写进内存、刷新即丢。改为显式错误态 + 重试。
async function loadIntegrations() {
  loading.value = true;
  try {
    const res = await integrationsApi.list({ include_disabled: true, size: 200 });
    loadError.value = null;
    integrations.value = res.items.map(fromDTO);
  } catch (err) {
    integrations.value = [];
    loadError.value = {
      offline: err instanceof ApiError && err.isNetworkError,
      message: err instanceof ApiError ? err.message : "加载集成列表失败",
    };
  } finally {
    loading.value = false;
  }
}

onMounted(() => {
  void loadIntegrations();
});

function handleRefresh() {
  void loadIntegrations();
}

function openAddDialog() {
  editingIntegration.value = null;
  formData.value = { name: "", type: "prometheus", base_url: "", scrape_config: "" };
  testResult.value = null;
  urlError.value = "";
  dialogOpen.value = true;
}

function openEditDialog(integration: Integration) {
  editingIntegration.value = integration;
  formData.value = {
    name: integration.name,
    type: integration.type,
    base_url: integration.base_url,
    scrape_config: integration.scrape_config || "",
  };
  testResult.value = null;
  urlError.value = "";
  dialogOpen.value = true;
}

function handleUrlChange(url: string) {
  formData.value.base_url = url;
  // P2-2：统一走 network-guard，并把后端给的拒绝原因透出给用户
  const result: { ok: boolean; reason?: string } = url
    ? validateInternalAddress(url)
    : { ok: true };
  urlError.value = result.ok ? "" : (result.reason ?? INTERNAL_HINT);
}

// 探测表单里当前的地址：POST /integrations/test 会做 SSRF 校验并真实请求，
// 但不落库（新增/编辑都适用）；行级"测试"按钮才走会写库的 /{id}/test。
async function handleTest() {
  if (!formData.value.base_url || urlError.value) return;
  testing.value = true;
  testResult.value = null;
  try {
    const res = await integrationsApi.testDraft({
      type: formData.value.type,
      base_url: formData.value.base_url,
      scrape_config: formData.value.scrape_config,
    });
    testResult.value = {
      ok: res.ok,
      message: res.ok ? res.reason || "探测成功" : res.last_error || res.reason || "探测失败",
      targets_total: res.targets_total ?? undefined,
      targets_up: res.targets_up ?? undefined,
      latency_ms: res.latency_ms,
    };
  } catch (err) {
    testResult.value = {
      ok: false,
      message: err instanceof ApiError ? err.message : "探测失败",
    };
  } finally {
    testing.value = false;
  }
}

// P1-4：原实现是 no-op（只关弹窗 + 假 loading），新增/编辑的结果从未写入 state，
// 用户填完表单数据直接丢失。现在真正落库并回写 state。
async function handleSave() {
  if (!formData.value.name.trim()) {
    toast.error("请填写名称");
    return;
  }
  if (urlError.value) {
    toast.error(urlError.value);
    return;
  }

  saving.value = true;
  try {
    if (editingIntegration.value) {
      const updated = await integrationsApi.update(editingIntegration.value.id, {
        name: formData.value.name,
        base_url: formData.value.base_url,
        scrape_config: formData.value.scrape_config,
      });
      const row = fromDTO(updated);
      integrations.value = integrations.value.map((i) => (i.id === row.id ? row : i));
      toast.success("已保存");
    } else {
      const created = await integrationsApi.create({
        name: formData.value.name,
        type: formData.value.type,
        base_url: formData.value.base_url,
        scrape_config: formData.value.scrape_config,
      });
      const row = fromDTO(created);
      integrations.value = [row, ...integrations.value];
      toast.success("已新增集成");
    }
    dialogOpen.value = false;
    loadError.value = null;
  } catch (err) {
    // 保存失败一律保留弹窗与已填内容，让用户改完重试。
    // 不再"离线时写进本地数组假装成功"—— 那种成功刷新即蒸发，比报错更糟。
    toast.error(err instanceof ApiError ? err.message : "保存失败");
  } finally {
    saving.value = false;
  }
}

// P1-5：真实探测 POST /api/v1/integrations/{id}/test
async function handleTestRow(id: string) {
  integrations.value = integrations.value.map((i) =>
    i.id === id ? { ...i, status: "pending" as IntegrationStatus } : i,
  );
  try {
    const res = await integrationsApi.probeById(id);
    const failure = res.last_error || res.reason || "探测失败";
    integrations.value = integrations.value.map((i) =>
      i.id === id
        ? {
            ...i,
            status: (res.ok ? "active" : "error") as IntegrationStatus,
            last_error: res.ok ? undefined : failure,
            // 以服务端落定的心跳为准（失败时是上次成功的时间）
            last_heartbeat: res.last_heartbeat ?? i.last_heartbeat,
            targets_total: res.targets_total,
            targets_up: res.targets_up,
          }
        : i,
    );
    if (res.ok) toast.success("探测成功");
    else toast.error(failure);
  } catch (err) {
    integrations.value = integrations.value.map((i) =>
      i.id === id ? { ...i, status: "error" as IntegrationStatus } : i,
    );
    toast.error(err instanceof ApiError ? err.message : "探测失败");
  }
}

// P1-5：启停真实落库 PUT /api/v1/integrations/{id}
async function handleToggleStatus(id: string, currentStatus: IntegrationStatus) {
  const newStatus: IntegrationStatus = currentStatus === "disabled" ? "active" : "disabled";
  const prevList = integrations.value;
  integrations.value = prevList.map((i) => (i.id === id ? { ...i, status: newStatus } : i));
  try {
    const updated = await integrationsApi.update(id, { status: newStatus });
    integrations.value = integrations.value.map((i) => (i.id === id ? fromDTO(updated) : i));
  } catch (err) {
    integrations.value = prevList; // 回滚
    toast.error(err instanceof ApiError ? err.message : "状态更新失败");
  }
}

function openDeleteDialog(id: string) {
  deletingId.value = id;
  deleteDialogOpen.value = true;
}

// P1-5：真实软删除 DELETE /api/v1/integrations/{id}
async function handleDelete() {
  if (!deletingId.value) return;
  const id = deletingId.value;
  const prevList = integrations.value;
  integrations.value = integrations.value.filter((i) => i.id !== id);
  deleteDialogOpen.value = false;
  deletingId.value = null;
  try {
    await integrationsApi.remove(id);
    toast.success("已删除");
  } catch (err) {
    integrations.value = prevList; // 回滚
    toast.error(err instanceof ApiError ? err.message : "删除失败");
  }
}

const filteredIntegrations = computed(() =>
  applyLocalFilters(integrations.value, {
    showDisabled: showDisabled.value,
    statusFilter: statusFilter.value,
    keyword: keyword.value,
  }),
);
const totalPages = computed(() => Math.ceil(filteredIntegrations.value.length / pageSize));
const paginatedData = computed(() =>
  filteredIntegrations.value.slice((page.value - 1) * pageSize, page.value * pageSize),
);

function setStatusFilter(value: string) {
  statusFilter.value = value as "all" | IntegrationStatus;
}

function statusBadgeClass(status: IntegrationStatus) {
  return `text-[11px] ${STATUS_CONFIG[status].bgColor} ${STATUS_CONFIG[status].color} border-0`;
}
</script>

<template>
  <div class="space-y-4">
    <!-- 页面标题 -->
    <div class="flex items-center justify-between">
      <div>
        <h2 class="text-[20px] font-semibold heading-tight">集成管理</h2>
        <p class="text-[13px] text-muted-foreground mt-1">管理 Prometheus 等外部监控系统的接入配置</p>
      </div>
    </div>

    <div
      v-if="loadError"
      class="rounded-xl border border-glow-pink/30 bg-danger/5 px-4 py-3 flex items-start gap-3"
    >
      <WifiOff :size="18" class="text-glow-pink shrink-0 mt-0.5" />
      <div class="flex-1 min-w-0">
        <div class="text-[13px] font-medium">
          {{ loadError.offline ? "后端未连接" : "加载集成列表失败" }}
        </div>
        <div class="text-[12px] text-muted-foreground mt-1 break-words">
          {{
            loadError.offline
              ? `无法连接 ${API_BASE_URL}，请确认 mindops-backend 已在 8010 端口启动。`
              : loadError.message
          }}
        </div>
      </div>
      <Button
        variant="outline"
        class="h-8 px-3 rounded-full text-[12px] btn-pill shrink-0"
        @click="handleRefresh"
      >
        <RefreshCw :size="13" class="mr-1.5" />
        重试
      </Button>
    </div>

    <!-- 筛选栏 - 毛玻璃圆角条 -->
    <div class="glass rounded-xl p-4 flex items-center gap-3 flex-wrap">
      <Select
        :model-value="statusFilter"
        :options="STATUS_FILTER_OPTIONS"
        placeholder="全部状态"
        class="w-[140px] h-9 text-[13px]"
        @update:model-value="setStatusFilter"
      />

      <div class="relative flex-1 min-w-[200px] max-w-md">
        <Search :size="16" class="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
        <Input
          v-model="keyword"
          placeholder="搜索目标名称或地址..."
          class="pl-9 h-9 text-[13px]"
        />
      </div>

      <div class="flex items-center gap-2">
        <Switch id="show-disabled" v-model="showDisabled" />
        <Label for="show-disabled" class="text-[13px] cursor-pointer">显示已停用</Label>
      </div>

      <Button
        class="h-9 px-4 rounded-full bg-primary text-primary-foreground text-[13px] font-medium btn-pill hover:bg-primary/90"
        @click="openAddDialog"
      >
        <Plus :size="16" class="mr-1.5" />
        新增监控目标
      </Button>

      <Button
        variant="outline"
        class="h-9 px-3 rounded-full text-[13px] btn-pill"
        @click="handleRefresh"
      >
        <RefreshCw :size="14" :class="loading ? 'animate-spin mr-1.5' : 'mr-1.5'" />
        刷新
      </Button>
    </div>

    <!-- 表格 -->
    <div class="bg-white rounded-xl border border-border overflow-hidden shadow-sm">
      <div v-if="loading" class="p-4 space-y-3">
        <div v-for="i in 4" :key="i" class="flex items-center gap-4 h-12">
          <div class="h-4 bg-secondary rounded animate-pulse w-32" />
          <div class="h-4 bg-secondary rounded animate-pulse w-20" />
          <div class="h-4 bg-secondary rounded animate-pulse w-40" />
          <div class="h-4 bg-secondary rounded animate-pulse w-24" />
          <div class="h-4 bg-secondary rounded animate-pulse w-16" />
          <div class="h-4 bg-secondary rounded animate-pulse flex-1" />
        </div>
      </div>
      <div
        v-else-if="!loadError && paginatedData.length === 0"
        class="py-16 flex flex-col items-center gap-3"
      >
        <div class="w-12 h-12 rounded-full bg-secondary flex items-center justify-center">
          <Activity :size="24" class="text-muted-foreground" />
        </div>
        <div class="text-[15px] font-medium">还没有接入任何被监控项目</div>
        <div class="text-[13px] text-muted-foreground">接入 Prometheus 后，它的告警会自动流入告警看板</div>
        <Button
          class="mt-2 h-9 px-4 rounded-full bg-primary text-primary-foreground text-[13px] font-medium btn-pill hover:bg-primary/90"
          @click="openAddDialog"
        >
          新增监控目标
        </Button>
      </div>
      <template v-else>
        <table class="w-full">
          <thead>
            <tr class="border-b border-border bg-secondary/40">
              <th class="text-left px-4 py-3 text-[12px] font-semibold text-muted-foreground">名称</th>
              <th class="text-left px-4 py-3 text-[12px] font-semibold text-muted-foreground">类型</th>
              <th class="text-left px-4 py-3 text-[12px] font-semibold text-muted-foreground">地址</th>
              <th class="text-left px-4 py-3 text-[12px] font-semibold text-muted-foreground">
                服务选择器
              </th>
              <th class="text-left px-4 py-3 text-[12px] font-semibold text-muted-foreground">状态</th>
              <th class="text-left px-4 py-3 text-[12px] font-semibold text-muted-foreground">
                <AppTooltip>
                  <button type="button" class="flex items-center gap-1">最近心跳</button>
                  <template #content>
                    <p>上次成功探测的时间</p>
                  </template>
                </AppTooltip>
              </th>
              <th class="text-left px-4 py-3 text-[12px] font-semibold text-muted-foreground">目标数</th>
              <th class="text-right px-4 py-3 text-[12px] font-semibold text-muted-foreground">操作</th>
            </tr>
          </thead>
          <tbody>
            <tr
              v-for="item in paginatedData"
              :key="item.id"
              class="border-b border-border last:border-0 hover:bg-secondary/20 transition-colors h-12"
            >
              <td class="px-4 py-3">
                <div class="flex flex-col">
                  <span class="text-[13px] font-medium">{{ item.name }}</span>
                  <span
                    v-if="item.status === 'error' && item.last_error"
                    class="text-[11px] text-danger truncate max-w-[200px]"
                    :title="item.last_error"
                  >
                    {{ item.last_error }}
                  </span>
                </div>
              </td>
              <td class="px-4 py-3">
                <Badge variant="secondary" class="text-[11px] font-mono bg-secondary/60">
                  {{ item.type }}
                </Badge>
              </td>
              <td class="px-4 py-3">
                <AppTooltip>
                  <button
                    type="button"
                    class="text-[13px] font-mono text-muted-foreground truncate max-w-[180px] block"
                  >
                    {{ item.base_url }}
                  </button>
                  <template #content>
                    <p>{{ item.base_url }}</p>
                  </template>
                </AppTooltip>
              </td>
              <td class="px-4 py-3">
                <AppTooltip v-if="item.scrape_config">
                  <button
                    type="button"
                    class="text-[13px] font-mono text-muted-foreground truncate max-w-[120px] block"
                  >
                    {{ item.scrape_config }}
                  </button>
                  <template #content>
                    <p>{{ item.scrape_config }}</p>
                  </template>
                </AppTooltip>
                <span v-else class="text-[13px] text-muted-foreground">—</span>
              </td>
              <td class="px-4 py-3">
                <Badge :class="statusBadgeClass(item.status)">
                  {{ STATUS_CONFIG[item.status].label }}
                </Badge>
              </td>
              <td class="px-4 py-3">
                <AppTooltip>
                  <button
                    type="button"
                    :class="
                      item.status === 'error'
                        ? 'text-[13px] text-muted-foreground'
                        : 'text-[13px]'
                    "
                  >
                    {{ formatRelativeTime(item.last_heartbeat) }}
                  </button>
                  <template #content>
                    <p>
                      {{
                        item.last_heartbeat
                          ? new Date(item.last_heartbeat).toLocaleString("zh-CN")
                          : "无记录"
                      }}
                    </p>
                  </template>
                </AppTooltip>
              </td>
              <td class="px-4 py-3">
                <span
                  v-if="item.status === 'active' && item.targets_total !== undefined"
                  class="text-[13px] font-mono"
                >
                  {{ item.targets_up }} / {{ item.targets_total }}
                </span>
                <span v-else class="text-[13px] text-muted-foreground">—</span>
              </td>
              <td class="px-4 py-3 text-right">
                <div class="flex items-center justify-end gap-2">
                  <button
                    type="button"
                    class="text-[12px] text-accent hover:text-accent/80 font-medium"
                    @click="handleTestRow(item.id)"
                  >
                    测试
                  </button>
                  <span class="text-muted-foreground">·</span>
                  <button
                    type="button"
                    class="text-[12px] text-accent hover:text-accent/80 font-medium"
                    @click="openEditDialog(item)"
                  >
                    编辑
                  </button>
                  <span class="text-muted-foreground">·</span>
                  <button
                    type="button"
                    class="text-[12px] text-accent hover:text-accent/80 font-medium"
                    @click="handleToggleStatus(item.id, item.status)"
                  >
                    {{ item.status === "disabled" ? "启用" : "暂停" }}
                  </button>
                  <span class="text-muted-foreground">·</span>
                  <button
                    type="button"
                    class="text-[12px] text-danger hover:text-danger/80 font-medium"
                    @click="openDeleteDialog(item.id)"
                  >
                    删除
                  </button>
                </div>
              </td>
            </tr>
          </tbody>
        </table>
      </template>

      <!-- 分页 -->
      <div
        v-if="!loading && filteredIntegrations.length > 0"
        class="px-4 py-3 border-t border-border flex items-center justify-between"
      >
        <span class="text-[12px] text-muted-foreground">
          共 {{ filteredIntegrations.length }} 条，第 {{ page }} 页
        </span>
        <div class="flex gap-1">
          <Button
            variant="outline"
            size="sm"
            :disabled="page === 1"
            class="text-[12px]"
            @click="page = Math.max(1, page - 1)"
          >
            上一页
          </Button>
          <Button
            variant="outline"
            size="sm"
            :disabled="page >= totalPages"
            class="text-[12px]"
            @click="page = Math.min(totalPages, page + 1)"
          >
            下一页
          </Button>
        </div>
      </div>
    </div>

    <!-- 新增/编辑弹窗 -->
    <AppDialog
      :open="dialogOpen"
      :title="editingIntegration ? '编辑监控目标' : '新增监控目标'"
      max-width="560px"
      @update:open="dialogOpen = $event"
    >
      <div class="space-y-4 py-2">
        <div>
          <Label class="text-[13px] font-medium">名称</Label>
          <Input
            v-model="formData.name"
            placeholder="给监控目标起个名字"
            class="mt-1.5 text-[13px]"
          />
        </div>

        <div>
          <Label class="text-[13px] font-medium">类型</Label>
          <!-- 后端 VALID_TYPES 只有 prometheus；做成单选项下拉会暗示还有别的可选 -->
          <div
            class="mt-1.5 h-9 px-3 rounded-lg border border-border bg-secondary/40 flex items-center text-[13px] font-mono"
          >
            prometheus
            <span class="ml-2 text-[11px] text-muted-foreground font-sans">本版仅支持 Prometheus</span>
          </div>
        </div>

        <div>
          <Label class="text-[13px] font-medium">地址</Label>
          <Input
            :model-value="formData.base_url"
            placeholder="http://10.0.0.5:9090"
            class="mt-1.5 text-[13px] font-mono"
            @update:model-value="handleUrlChange"
          />
          <p v-if="urlError" class="text-[12px] text-danger mt-1">{{ urlError }}</p>
        </div>

        <div>
          <Label class="text-[13px] font-medium">服务选择器（选填）</Label>
          <Input
            v-model="formData.scrape_config"
            placeholder='job=~"order-.*"'
            class="mt-1.5 text-[13px] font-mono"
          />
        </div>

        <!-- 测试连通性 -->
        <div class="pt-2 border-t border-border">
          <Button
            variant="outline"
            :disabled="testing || !formData.base_url || !!urlError"
            class="text-[13px]"
            @click="handleTest"
          >
            <Loader2 v-if="testing" :size="14" class="animate-spin mr-1.5" />
            <Activity v-else :size="14" class="mr-1.5" />
            测试连通性
          </Button>

          <div
            v-if="testResult"
            class="mt-3 p-3 rounded-lg text-[13px]"
            :class="
              testResult.ok ? 'bg-success/10 text-success' : 'bg-danger/10 text-danger'
            "
          >
            <div class="flex items-center gap-2 font-medium">
              <CheckCircle2 v-if="testResult.ok" :size="16" />
              <XCircle v-else :size="16" />
              {{ testResult.ok ? "探测成功" : "探测失败" }}
            </div>
            <div
              v-if="testResult.ok && testResult.targets_total !== undefined"
              class="mt-1 text-[12px] opacity-80"
            >
              目标总数 {{ testResult.targets_total }} · 存活 {{ testResult.targets_up }} · 延迟
              {{ testResult.latency_ms }}ms
            </div>
            <div v-if="!testResult.ok" class="mt-1 text-[12px]">{{ testResult.message }}</div>
          </div>
        </div>
      </div>

      <template #footer>
        <Button variant="outline" class="rounded-full" @click="dialogOpen = false">取消</Button>
        <Button
          :disabled="!!urlError || saving"
          class="rounded-full bg-primary text-primary-foreground hover:bg-primary/90"
          @click="handleSave"
        >
          <Loader2 v-if="saving" class="mr-2 h-4 w-4 animate-spin" />
          {{ saving ? "保存中…" : "保存" }}
        </Button>
      </template>
    </AppDialog>

    <!-- 删除确认弹窗 -->
    <AppDialog
      :open="deleteDialogOpen"
      title="确认删除"
      max-width="400px"
      @update:open="deleteDialogOpen = $event"
    >
      <p class="text-[13px] text-muted-foreground py-2">删除后保留历史接入记录用于审计，不会物理删除数据。</p>
      <template #footer>
        <Button variant="outline" class="rounded-full" @click="deleteDialogOpen = false">取消</Button>
        <Button variant="destructive" class="rounded-full" @click="handleDelete">确认删除</Button>
      </template>
    </AppDialog>
  </div>
</template>
