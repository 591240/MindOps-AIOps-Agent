<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { integrationsApi, type IntegrationDTO, type IntegrationStatus } from "@/api/integrations";
import { ApiError } from "@/api/client";
import Checkbox from "@/components/ui/Checkbox.vue";
import Input from "@/components/ui/Input.vue";
import Label from "@/components/ui/Label.vue";
import Button from "@/components/ui/Button.vue";
import AppDialog from "@/components/ui/AppDialog.vue";
import { toast } from "vue-sonner";
import { Plus, Server, AlertCircle, Info, RefreshCw, WifiOff } from "lucide-vue-next";
import { validateInternalAddress, INTERNAL_HINT } from "@/lib/network-guard";

/**
 * 选择要监控的项目（F-12 §5.4 / docs/26 §2.9 第 2 步）
 *
 * 两处复用：创建向导第 2 步、任务工作台「管理项目」弹窗。
 *
 * 语义提醒（务必保留）：
 * - 资产池只列 status != disabled 的目标（停用目标不可绑定，与后端 422 TARGET_DISABLED 对齐）；
 * - 「就地新增」**不做连通测试**——F-11 是"先落库后探测"，新目标状态为 pending，
 *   连不通的目标有排查价值、必须留在列表里显示 error。别照搬模型设置页的"保存即测试"。
 */

const props = defineProps<{
  /** 已选目标 id（后端 integrations.id，整数） */
  selectedIds: number[];
  /** 就地新增成功后的回调（向导用它把新目标自动勾选） */
  onCreate?: (created: IntegrationDTO) => void;
}>();
const emit = defineEmits<{ change: [ids: number[]] }>();

const INTEGRATION_STATUS: Record<IntegrationStatus, { label: string; dot: string; pill: string }> = {
  pending: { label: "待探测", dot: "bg-warning", pill: "bg-warning/10 text-warning" },
  active: { label: "已连通", dot: "bg-success", pill: "bg-success/10 text-success" },
  error: { label: "异常", dot: "bg-danger", pill: "bg-danger/10 text-danger" },
  disabled: { label: "已停用", dot: "bg-muted-foreground/50", pill: "bg-muted text-muted-foreground" },
};

const pool = ref<IntegrationDTO[]>([]);
const loading = ref(true);
const offline = ref(false);
const loadError = ref("");
const addOpen = ref(false);
const creating = ref(false);
const form = ref({ name: "", base_url: "", scrape_config: "" });
const urlError = ref("");

async function loadPool() {
  loading.value = true;
  loadError.value = "";
  offline.value = false;
  try {
    // include_disabled 拉全量后在前端剔除，这样"已停用"的目标也能被识别出
    // 「你绑的它已停用」而不是静默消失（与后端 422 TARGET_DISABLED 的语义对齐）
    const data = await integrationsApi.list({ include_disabled: true, size: 100 });
    pool.value = data.items;
  } catch (err) {
    const isOffline = err instanceof ApiError && err.isNetworkError;
    offline.value = isOffline;
    loadError.value = isOffline
      ? "无法连接后端服务，请确认 mindops-backend 已在 8010 端口启动。"
      : err instanceof Error
        ? err.message
        : "加载监控目标失败";
    pool.value = [];
  } finally {
    loading.value = false;
  }
}

onMounted(() => void loadPool());

const selectable = computed(() => pool.value.filter((i) => i.status !== "disabled"));

const statusConfig = (status: IntegrationStatus) =>
  INTEGRATION_STATUS[status] ?? INTEGRATION_STATUS.pending;

const toggle = (id: number) => {
  emit(
    "change",
    props.selectedIds.includes(id)
      ? props.selectedIds.filter((x) => x !== id)
      : [...props.selectedIds, id],
  );
};

async function handleCreate() {
  if (!form.value.name.trim()) return;
  const check = validateInternalAddress(form.value.base_url);
  if (!check.ok) {
    urlError.value = check.reason || INTERNAL_HINT;
    return;
  }
  creating.value = true;
  try {
    const created = await integrationsApi.create({
      name: form.value.name.trim(),
      type: "prometheus",
      base_url: form.value.base_url.trim(),
      scrape_config: form.value.scrape_config.trim() || undefined,
    });
    pool.value = [...pool.value, created];
    emit("change", [...props.selectedIds, created.id]);
    props.onCreate?.(created);
    form.value = { name: "", base_url: "", scrape_config: "" };
    urlError.value = "";
    addOpen.value = false;
    toast.success("监控项目已新增", {
      description: `「${created.name}」状态为待探测，连通性需在集成管理页手动触发。`,
    });
  } catch (err) {
    toast.error("新增失败", {
      description: err instanceof Error ? err.message : "未知错误",
    });
  } finally {
    creating.value = false;
  }
}
</script>

<template>
  <div class="space-y-3">
    <!-- 顶部：已选计数 + 就地新增 -->
    <div class="flex items-center justify-between">
      <span class="text-[12px] text-muted-foreground">
        已选 <span class="text-foreground font-medium">{{ selectedIds.length }}</span> 个监控项目
      </span>
      <Button
        type="button"
        @click="addOpen = true"
        class="h-8 px-3 rounded-full border border-border bg-card text-foreground text-[12px] font-medium btn-pill hover:bg-secondary"
      >
        <Plus :size="14" class="mr-1" />
        新增监控项目
      </Button>
    </div>

    <!-- 加载失败提示 -->
    <div
      v-if="loadError"
      class="rounded-xl border border-glow-pink/30 p-3 flex items-start gap-2.5"
    >
      <WifiOff :size="15" class="text-glow-pink shrink-0 mt-0.5" />
      <div class="flex-1 min-w-0">
        <div class="text-[12px] font-medium">{{ offline ? "后端未连接" : "加载失败" }}</div>
        <div class="text-[11px] text-muted-foreground mt-0.5 break-words">{{ loadError }}</div>
      </div>
      <button
        type="button"
        @click="void loadPool()"
        class="shrink-0 flex items-center gap-1 px-2.5 py-1 rounded-lg bg-secondary text-[11px] font-medium hover:bg-secondary/70 transition-colors"
      >
        <RefreshCw :size="12" />
        重试
      </button>
    </div>

    <!-- 资产池列表 -->
    <div v-if="loading" class="space-y-2">
      <div v-for="i in 3" :key="i" class="h-16 rounded-xl bg-secondary/40 animate-pulse" />
    </div>
    <div
      v-else-if="selectable.length === 0"
      class="py-10 flex flex-col items-center gap-2 border border-dashed border-border rounded-xl"
    >
      <Server :size="22" class="text-muted-foreground" />
      <div class="text-[13px] font-medium">资产池里还没有监控目标</div>
      <div class="text-[12px] text-muted-foreground">先新增一个，再把它绑到本任务</div>
    </div>
    <div v-else class="border border-border rounded-xl overflow-hidden max-h-[320px] overflow-y-auto">
      <label
        v-for="it in selectable"
        :key="it.id"
        :class="[
          'flex items-center gap-3 px-3.5 py-3 border-b border-border last:border-0 cursor-pointer transition-colors',
          selectedIds.includes(it.id) ? 'bg-secondary/40' : 'hover:bg-secondary/20',
        ]"
      >
        <Checkbox
          :model-value="selectedIds.includes(it.id)"
          @update:model-value="toggle(it.id)"
        />
        <div class="flex-1 min-w-0">
          <div class="flex items-center gap-2">
            <span class="text-[13px] font-medium truncate">{{ it.name }}</span>
            <span
              class="text-[11px] font-mono text-muted-foreground border border-border rounded px-1.5 py-0.5"
            >
              {{ it.type }}
            </span>
            <span
              :class="[
                'inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[11px] font-medium whitespace-nowrap',
                statusConfig(it.status).pill,
              ]"
            >
              <span :class="['w-1.5 h-1.5 rounded-full', statusConfig(it.status).dot]" />
              {{ statusConfig(it.status).label }}
            </span>
          </div>
          <div class="mt-1 flex items-center gap-3 text-[12px] text-muted-foreground">
            <span class="font-mono truncate">{{ it.base_url }}</span>
            <span v-if="it.scrape_config" class="font-mono truncate hidden sm:inline">
              {{ it.scrape_config }}
            </span>
          </div>
        </div>
        <div class="shrink-0 text-right">
          <div class="text-[12px] text-muted-foreground">目标数</div>
          <div class="text-[13px] font-mono tabular-nums">
            {{ it.targets_up ?? "—" }} / {{ it.targets_total ?? "—" }}
          </div>
        </div>
      </label>
    </div>

    <!-- 0 选提示（允许空任务，但要提示后果） -->
    <div
      v-if="selectedIds.length === 0 && selectable.length > 0"
      class="flex items-start gap-2 px-3 py-2 rounded-lg bg-warning/5 border border-warning/20"
    >
      <AlertCircle :size="14" class="text-warning shrink-0 mt-0.5" />
      <span class="text-[12px] text-muted-foreground">
        可以先不选，任务会先建出来；但未绑定项目时它收不到任何告警。
      </span>
    </div>

    <!-- 就地新增（内嵌 F-11 新建表单，字段与校验完全复用） -->
    <AppDialog
      :open="addOpen"
      @update:open="(v: boolean) => !creating && (addOpen = v)"
      title="新增监控项目"
      max-width="520px"
    >
      <template #title>
        <span class="text-[16px] font-semibold">新增监控项目</span>
      </template>

      <div class="space-y-4 py-1">
        <div class="space-y-1.5">
          <Label class="text-[13px]">项目名称</Label>
          <Input
            :model-value="form.name"
            @update:model-value="form.name = $event"
            placeholder="如：订单服务集群"
            class="h-9 text-[13px]"
          />
        </div>

        <div class="space-y-1.5">
          <Label class="text-[13px]">类型</Label>
          <Input model-value="prometheus" readonly class="h-9 text-[13px] bg-secondary/50" />
          <p class="text-[11px] text-muted-foreground">本版仅支持 Prometheus 类型</p>
        </div>

        <div class="space-y-1.5">
          <Label class="text-[13px]">监控地址</Label>
          <Input
            :model-value="form.base_url"
            @update:model-value="(v: string) => { form.base_url = v; urlError = ''; }"
            placeholder="http://10.0.0.5:9090"
            :class="urlError ? 'h-9 text-[13px] font-mono border-danger' : 'h-9 text-[13px] font-mono'"
          />
          <p v-if="urlError" class="text-[11px] text-danger flex items-center gap-1">
            <AlertCircle :size="12" />
            {{ urlError }}
          </p>
          <p v-else class="text-[11px] text-muted-foreground">{{ INTERNAL_HINT }}</p>
        </div>

        <div class="space-y-1.5">
          <Label class="text-[13px]">服务选择器（可选）</Label>
          <Input
            :model-value="form.scrape_config"
            @update:model-value="form.scrape_config = $event"
            placeholder='job=~&quot;order-.*&quot;'
            class="h-9 text-[13px] font-mono"
          />
        </div>

        <div class="flex items-start gap-2 px-3 py-2 rounded-lg bg-accent/5 border border-accent/20">
          <Info :size="14" class="text-accent shrink-0 mt-0.5" />
          <span class="text-[12px] text-muted-foreground">
            新增后状态为「待探测」。连通性探测需在集成管理页手动触发 —— 新增不做保存即测试。
          </span>
        </div>
      </div>

      <template #footer>
        <Button
          type="button"
          @click="addOpen = false"
          :disabled="creating"
          class="h-9 px-4 rounded-full border border-border bg-card text-foreground text-[13px] font-medium btn-pill hover:bg-secondary disabled:opacity-40"
        >
          取消
        </Button>
        <Button
          type="button"
          @click="void handleCreate()"
          :disabled="!form.name.trim() || !form.base_url.trim() || creating"
          class="h-9 px-4 rounded-full bg-primary text-primary-foreground text-[13px] font-medium btn-pill hover:bg-primary/90 disabled:opacity-40"
        >
          {{ creating ? "新增中..." : "新增并勾选" }}
        </Button>
      </template>
    </AppDialog>
  </div>
</template>
