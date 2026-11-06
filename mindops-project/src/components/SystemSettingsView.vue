<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { RotateCcw, Loader2, CheckCircle2, Minus, Plus } from "lucide-vue-next";
import Button from "@/components/ui/Button.vue";
import Label from "@/components/ui/Label.vue";
import Switch from "@/components/ui/Switch.vue";
import AppDialog from "@/components/ui/AppDialog.vue";
import { configApi } from "@/api/config";
import { ApiError } from "@/api/client";
import { getSessionUser } from "@/lib/session";

/**
 * 运行时可调参数来自后端白名单：**仅这 4 个字段**。
 *
 * 这里刻意把原先的 `auto_diagnosis` / `diagnosis_debounce_seconds` 两项删掉了 ——
 * 它们虽然存在于后端 `Settings`，但**没有任何接口暴露**改写入口。
 * 留着就是两个"假开关"：用户拨动 → 前端 state 变了 → 保存时后端根本不收，
 * 页面提示"已生效"而实际配置纹丝不动。能存下去的字段才配出现在面板上。
 */
interface SystemConfig {
  conf_threshold: number;
  max_retry: number;
  reasoning_escalation: boolean;
  cross_component_fallback: boolean;
}

// 后端 env 基线默认值；后端未落库时 GET 返回的即是这组值
const DEFAULT_CONFIG: SystemConfig = {
  conf_threshold: 0.6,
  max_retry: 2,
  reasoning_escalation: true,
  cross_component_fallback: true,
};

const loading = ref(true);
const saving = ref(false);
const config = ref<SystemConfig>({ ...DEFAULT_CONFIG });
/** 最近一次从后端加载/保存成功的快照，脏检测以此为准 */
const savedConfig = ref<SystemConfig>({ ...DEFAULT_CONFIG });
const confirmDialogOpen = ref(false);
const saveSuccess = ref(false);
let successTimer: number | null = null;
let cancelled = false;

const loadError = ref<string | null>(null);
/** 后端 PUT 是 admin-only；engineer 打开这张面板应当只读，而不是保存时才炸。 */
const canEdit = getSessionUser()?.role === "admin";

const hasChanges = computed(
  () => JSON.stringify(config.value) !== JSON.stringify(savedConfig.value),
);

function fieldDirty(key: keyof SystemConfig): boolean {
  return config.value[key] !== savedConfig.value[key];
}

onMounted(() => {
  loading.value = true;
  loadError.value = null;
  configApi
    .getConfidence()
    .then((data) => {
      if (cancelled) return;
      config.value = {
        conf_threshold: data.conf_threshold,
        max_retry: data.max_retry,
        reasoning_escalation: data.reasoning_escalation,
        cross_component_fallback: data.cross_component_fallback,
      };
      savedConfig.value = { ...config.value };
    })
    .catch((err: unknown) => {
      if (cancelled) return;
      loadError.value =
        err instanceof ApiError && err.isNetworkError
          ? "无法连接后端服务，请确认 mindops-backend 已在 8010 端口启动。"
          : err instanceof Error
            ? err.message
            : "加载配置失败";
    })
    .finally(() => {
      if (!cancelled) loading.value = false;
    });
});

onBeforeUnmount(() => {
  cancelled = true;
  if (successTimer !== null) window.clearTimeout(successTimer);
});

function handleConfigChange(key: keyof SystemConfig, value: number | boolean) {
  config.value = { ...config.value, [key]: value } as SystemConfig;
  saveSuccess.value = false;
}

function handleReset(key: keyof SystemConfig) {
  config.value = { ...config.value, [key]: DEFAULT_CONFIG[key] } as SystemConfig;
  saveSuccess.value = false;
}

const saveError = ref<string | null>(null);

async function handleSave() {
  confirmDialogOpen.value = false;
  saving.value = true;
  saveError.value = null;
  try {
    await configApi.updateConfidence({
      conf_threshold: config.value.conf_threshold,
      max_retry: config.value.max_retry,
      reasoning_escalation: config.value.reasoning_escalation,
      cross_component_fallback: config.value.cross_component_fallback,
    });
    saving.value = false;
    savedConfig.value = { ...config.value };
    saveSuccess.value = true;
    if (successTimer !== null) window.clearTimeout(successTimer);
    successTimer = window.setTimeout(() => {
      saveSuccess.value = false;
    }, 3000);
  } catch (err) {
    saving.value = false;
    saveError.value = err instanceof Error ? err.message : "保存失败，请重试";
  }
}

function reloadPage() {
  window.location.reload();
}

// 数值参数边界：与后端校验保持一致（conf_threshold∈(0,1)，max_retry 0-3）
const THRESHOLD_MIN = 0.1;
const THRESHOLD_MAX = 0.99;
const THRESHOLD_STEP = 0.05;
const RETRY_MIN = 0;
const RETRY_MAX = 3;

/** 按步长对齐并夹紧到 [min,max]；乘 100 取整避免 0.1+0.05 浮点尾巴 */
function clampThreshold(v: number): number {
  const aligned = Math.round(v / THRESHOLD_STEP) * THRESHOLD_STEP;
  const clamped = Math.min(THRESHOLD_MAX, Math.max(THRESHOLD_MIN, aligned));
  return Math.round(clamped * 100) / 100;
}

function stepThreshold(dir: -1 | 1) {
  handleConfigChange(
    "conf_threshold",
    clampThreshold(config.value.conf_threshold + dir * THRESHOLD_STEP),
  );
}

function commitThresholdInput(e: Event) {
  const raw = (e.target as HTMLInputElement).value.trim();
  const parsed = Number.parseFloat(raw);
  if (raw === "" || Number.isNaN(parsed)) {
    // 输空/非法：回退到线上值，并清掉输入框里的脏内容
    (e.target as HTMLInputElement).value = savedConfig.value.conf_threshold.toFixed(2);
    config.value = { ...config.value, conf_threshold: savedConfig.value.conf_threshold };
    return;
  }
  const next = clampThreshold(parsed);
  (e.target as HTMLInputElement).value = next.toFixed(2);
  handleConfigChange("conf_threshold", next);
}

function clampRetry(v: number): number {
  return Math.min(RETRY_MAX, Math.max(RETRY_MIN, Math.round(v)));
}

function stepRetry(dir: -1 | 1) {
  handleConfigChange("max_retry", clampRetry(config.value.max_retry + dir));
}

function commitRetryInput(e: Event) {
  const raw = (e.target as HTMLInputElement).value.trim();
  const parsed = Number.parseInt(raw, 10);
  if (raw === "" || Number.isNaN(parsed)) {
    (e.target as HTMLInputElement).value = String(savedConfig.value.max_retry);
    config.value = { ...config.value, max_retry: savedConfig.value.max_retry };
    return;
  }
  const next = clampRetry(parsed);
  (e.target as HTMLInputElement).value = String(next);
  handleConfigChange("max_retry", next);
}
</script>

<template>
  <div v-if="loading" class="space-y-4">
    <div>
      <h2 class="text-[20px] font-semibold heading-tight">系统设置</h2>
      <p class="text-[13px] text-muted-foreground mt-1">诊断引擎运行时参数</p>
    </div>
    <div class="bg-white rounded-xl border border-border p-6 space-y-6">
      <div v-for="i in 4" :key="i" class="space-y-2">
        <div class="h-4 bg-secondary rounded animate-pulse w-32" />
        <div class="h-8 bg-secondary rounded animate-pulse w-full max-w-md" />
      </div>
    </div>
  </div>

  <div v-else class="space-y-4">
    <!-- 页面标题 -->
    <div class="flex items-center justify-between">
      <div>
        <h2 class="text-[20px] font-semibold heading-tight">系统设置</h2>
        <p class="text-[13px] text-muted-foreground mt-1">诊断引擎运行时参数</p>
      </div>
      <div
        v-if="saveSuccess"
        class="flex items-center gap-1.5 text-[13px] text-success"
      >
        <CheckCircle2 :size="16" />
        配置已生效
      </div>
    </div>

    <!-- 加载失败：此时表单里是默认值，必须先说清楚，否则用户会把默认值当成线上配置 -->
    <div
      v-if="loadError"
      class="rounded-xl border border-glow-pink/30 bg-danger/5 p-4 text-[13px] text-danger flex items-center justify-between gap-3"
    >
      <span class="flex-1">配置加载失败：{{ loadError }}</span>
      <Button variant="outline" size="sm" @click="reloadPage">重试</Button>
    </div>

    <!-- 只读提示：PUT 是 admin-only，engineer 不该到点保存才发现没权限 -->
    <div
      v-if="!loadError && !canEdit"
      class="rounded-xl border border-border bg-secondary/50 p-4 text-[13px] text-muted-foreground"
    >
      当前账号为工程师角色，仅可查看诊断参数。修改请使用管理员账号登录。
    </div>

    <!-- 配置表单：一张卡片、行间发丝线分隔 -->
    <div class="bg-white rounded-xl border border-border">
      <div
        v-if="saveError"
        class="mx-6 mt-6 rounded-lg border border-glow-pink/30 bg-danger/5 p-3 text-[13px] text-danger"
      >
        保存失败：{{ saveError }}
      </div>

      <div class="divide-y divide-border">
        <!-- 置信度阈值 -->
        <div class="p-6">
          <div class="flex items-start justify-between gap-4">
            <div>
              <div class="flex items-center gap-2">
                <Label class="text-[14px] font-medium">置信度阈值</Label>
                <span
                  v-if="fieldDirty('conf_threshold')"
                  class="h-1.5 w-1.5 rounded-full bg-glow-pink"
                  title="未保存"
                />
              </div>
              <p class="text-[12.5px] text-muted-foreground mt-1.5">
                智能体自评置信度低于该值时触发回环补查。调低更宽容、出结果快；调高更严格、更常回环。
              </p>
            </div>
          </div>
          <div class="mt-5 flex items-center gap-3">
            <div
              class="inline-flex h-9 items-center rounded-full border border-border bg-white"
            >
              <button
                type="button"
                class="inline-flex h-9 w-9 items-center justify-center rounded-l-full text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground disabled:pointer-events-none disabled:opacity-40"
                :disabled="!canEdit || config.conf_threshold <= THRESHOLD_MIN"
                aria-label="减小 0.05"
                @click="stepThreshold(-1)"
              >
                <Minus :size="14" />
              </button>
              <input
                type="number"
                :min="THRESHOLD_MIN"
                :max="THRESHOLD_MAX"
                :step="THRESHOLD_STEP"
                :value="config.conf_threshold.toFixed(2)"
                :disabled="!canEdit"
                class="h-9 w-16 border-x border-border bg-transparent text-center font-mono text-[13px] tabular-nums outline-none disabled:cursor-not-allowed disabled:opacity-50 [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
                @change="commitThresholdInput"
              />
              <button
                type="button"
                class="inline-flex h-9 w-9 items-center justify-center rounded-r-full text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground disabled:pointer-events-none disabled:opacity-40"
                :disabled="!canEdit || config.conf_threshold >= THRESHOLD_MAX"
                aria-label="增大 0.05"
                @click="stepThreshold(1)"
              >
                <Plus :size="14" />
              </button>
            </div>
            <span class="text-[12px] text-muted-foreground">
              范围 0.10 – 0.99，步长 0.05
            </span>
            <button
              type="button"
              class="ml-auto inline-flex items-center gap-1 text-[12px] text-muted-foreground transition-colors hover:text-foreground"
              :disabled="!canEdit"
              @click="handleReset('conf_threshold')"
            >
              <RotateCcw :size="12" />
              恢复默认
            </button>
          </div>
        </div>

        <!-- 最大回环次数 -->
        <div class="p-6">
          <div class="flex items-start justify-between gap-4">
            <div>
              <div class="flex items-center gap-2">
                <Label class="text-[14px] font-medium">最大回环次数</Label>
                <span
                  v-if="fieldDirty('max_retry')"
                  class="h-1.5 w-1.5 rounded-full bg-glow-pink"
                  title="未保存"
                />
              </div>
              <p class="text-[12.5px] text-muted-foreground mt-1.5">
                单次事件允许的自动补查上限。0 为只诊断一次；用满次数仍不达标则转人工。
              </p>
            </div>
          </div>
          <div class="mt-5 flex items-center gap-3">
            <div
              class="inline-flex h-9 items-center rounded-full border border-border bg-white"
            >
              <button
                type="button"
                class="inline-flex h-9 w-9 items-center justify-center rounded-l-full text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground disabled:pointer-events-none disabled:opacity-40"
                :disabled="!canEdit || config.max_retry <= RETRY_MIN"
                aria-label="减小 1"
                @click="stepRetry(-1)"
              >
                <Minus :size="14" />
              </button>
              <input
                type="number"
                :min="RETRY_MIN"
                :max="RETRY_MAX"
                :step="1"
                :value="config.max_retry"
                :disabled="!canEdit"
                class="h-9 w-16 border-x border-border bg-transparent text-center font-mono text-[13px] tabular-nums outline-none disabled:cursor-not-allowed disabled:opacity-50 [appearance:textfield] [&::-webkit-inner-spin-button]:appearance-none [&::-webkit-outer-spin-button]:appearance-none"
                @change="commitRetryInput"
              />
              <button
                type="button"
                class="inline-flex h-9 w-9 items-center justify-center rounded-r-full text-muted-foreground transition-colors hover:bg-secondary hover:text-foreground disabled:pointer-events-none disabled:opacity-40"
                :disabled="!canEdit || config.max_retry >= RETRY_MAX"
                aria-label="增大 1"
                @click="stepRetry(1)"
              >
                <Plus :size="14" />
              </button>
            </div>
            <span class="text-[12px] text-muted-foreground">范围 0 – 3 次</span>
            <button
              type="button"
              class="ml-auto inline-flex items-center gap-1 text-[12px] text-muted-foreground transition-colors hover:text-foreground"
              :disabled="!canEdit"
              @click="handleReset('max_retry')"
            >
              <RotateCcw :size="12" />
              恢复默认
            </button>
          </div>
        </div>

        <!-- 推理升级（PT-04 白名单字段） -->
        <div class="p-6 flex items-center justify-between gap-4">
          <div>
            <div class="flex items-center gap-2">
              <Label class="text-[14px] font-medium">推理升级</Label>
              <span
                v-if="fieldDirty('reasoning_escalation')"
                class="h-1.5 w-1.5 rounded-full bg-glow-pink"
                title="未保存"
              />
            </div>
            <p class="text-[12.5px] text-muted-foreground mt-1.5">
              第 1 次回环时切换到推理型模型重诊，用更强算力换更准的结论。
            </p>
          </div>
          <Switch
            :model-value="config.reasoning_escalation"
            :disabled="!canEdit"
            @update:model-value="(v) => handleConfigChange('reasoning_escalation', v)"
          />
        </div>

        <!-- 跨组件兜底检索（PT-04 白名单字段） -->
        <div class="p-6 flex items-center justify-between gap-4">
          <div>
            <div class="flex items-center gap-2">
              <Label class="text-[14px] font-medium">跨组件兜底检索</Label>
              <span
                v-if="fieldDirty('cross_component_fallback')"
                class="h-1.5 w-1.5 rounded-full bg-glow-pink"
                title="未保存"
              />
            </div>
            <p class="text-[12.5px] text-muted-foreground mt-1.5">
              第 2 次回环放宽检索范围到全部组件，用于排查跨服务连锁故障。
            </p>
          </div>
          <Switch
            :model-value="config.cross_component_fallback"
            :disabled="!canEdit"
            @update:model-value="(v) => handleConfigChange('cross_component_fallback', v)"
          />
        </div>
      </div>

      <!-- 底栏：状态说明 + 保存 -->
      <div
        class="flex items-center justify-between gap-4 border-t border-border px-6 py-4"
      >
        <p class="text-[12px] text-muted-foreground">
          <template v-if="saving">正在保存…</template>
          <template v-else-if="hasChanges">有未保存的修改，将影响后续所有诊断</template>
          <template v-else>当前配置与线上一致</template>
        </p>
        <Button
          :disabled="!hasChanges || saving || !canEdit"
          :title="canEdit ? undefined : '仅管理员可修改'"
          class="h-9 px-6 rounded-full bg-primary text-primary-foreground text-[13px] font-medium btn-pill hover:bg-primary/90 disabled:opacity-40"
          @click="confirmDialogOpen = true"
        >
          <template v-if="saving">
            <Loader2 :size="14" class="animate-spin mr-1.5" />
            保存中...
          </template>
          <template v-else>保存修改</template>
        </Button>
      </div>
    </div>

    <!-- 确认保存弹窗 -->
    <AppDialog
      :open="confirmDialogOpen"
      title="确认保存"
      max-width="400px"
      @update:open="confirmDialogOpen = $event"
    >
      <p class="text-[13px] text-muted-foreground py-2">修改将影响后续所有诊断，是否确认保存？</p>
      <template #footer>
        <Button variant="outline" class="rounded-full" @click="confirmDialogOpen = false">
          取消
        </Button>
        <Button
          class="rounded-full bg-primary text-primary-foreground hover:bg-primary/90"
          @click="handleSave"
        >
          确认保存
        </Button>
      </template>
    </AppDialog>
  </div>
</template>
