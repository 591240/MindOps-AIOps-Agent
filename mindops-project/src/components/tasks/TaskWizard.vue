<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type { TaskDTO } from "@/api/tasks";
import TaskTargetPicker from "./TaskTargetPicker.vue";
import Input from "@/components/ui/Input.vue";
import Label from "@/components/ui/Label.vue";
import Button from "@/components/ui/Button.vue";
import AppDialog from "@/components/ui/AppDialog.vue";
import { AlertCircle, Check, Loader2 } from "lucide-vue-next";

/**
 * 创建 / 编辑监控任务向导（F-12 §8.3，docs/26 §2.9 页面二）
 *
 * 三步：① 基本信息 → ② 选择要监控的项目 → ③ 确认
 * - 已完成的步可点击回退；关闭时若已填内容二次确认
 * - 同名冲突在本地先拦（对应后端 409 DUPLICATE_TASK_NAME，后端仍会兜底）
 * - 提交是真实异步请求：不关弹窗、不假装延迟，成功由调用方关窗，失败保持打开
 */

interface WizardPayload {
  name: string;
  description: string;
  /** 后端 integrations.id（整数） */
  target_ids: number[];
  id?: string;
}

const props = defineProps<{
  open: boolean;
  /** 编辑模式：传入待编辑任务 */
  editTask?: TaskDTO | null;
  /**
   * 编辑模式：当前已绑定的集成目标 id。
   *
   * 必须单独传 —— `TaskDTO` 里**没有** `target_ids` 字段（绑定关系存在
   * `task_targets` 关联表，只在 `GET /tasks/{id}` 的 `targets` 明细里给出），
   * 早先直接读 `editTask?.target_ids` 恒为 undefined，导致编辑时勾选被清空。
   */
  editTargetIds?: number[];
  /** 已有任务名（用于同名冲突提示，对应后端 409 DUPLICATE_TASK_NAME） */
  existingNames: string[];
  /**
   * 提交回调。必须是**异步函数**：向导要 await 它才能把 `submitting` 撑到
   * 请求真正结束（Vue 的事件是一次性的、拿不到父组件的 Promise，改成 emit
   * 会让按钮立刻恢复可点，连点就是重复建任务）。
   */
  onSubmit: (payload: WizardPayload) => Promise<void> | void;
}>();

const emit = defineEmits<{
  "update:open": [boolean];
}>();

const STEPS = ["基本信息", "选择监控项目", "确认"];

const step = ref<1 | 2 | 3>(1);
const name = ref("");
const description = ref("");
const selectedIds = ref<number[]>([]);
const nameError = ref("");
const submitting = ref(false);
const confirmDiscard = ref(false);

// 打开时初始化（编辑模式回填）
watch(
  [() => props.open, () => props.editTask, () => props.editTargetIds],
  ([open]) => {
    if (!open) return;
    step.value = 1;
    submitting.value = false;
    nameError.value = "";
    name.value = props.editTask?.name ?? "";
    description.value = props.editTask?.description ?? "";
    selectedIds.value = [...(props.editTargetIds ?? [])];
  },
  { immediate: true },
);

const dirty = computed(() => Boolean(name.value || description.value || selectedIds.value.length));

function validateName(): boolean {
  const v = name.value.trim();
  if (!v) {
    nameError.value = "任务名不能为空";
    return false;
  }
  if (v.length > 64) {
    nameError.value = "任务名不能超过 64 个字符";
    return false;
  }
  const clash = props.existingNames.some(
    (n) => n.trim().toLowerCase() === v.toLowerCase() && n !== props.editTask?.name,
  );
  if (clash) {
    nameError.value = "已存在同名任务";
    return false;
  }
  nameError.value = "";
  return true;
}

const handleNext = () => {
  if (step.value === 1 && !validateName()) return;
  step.value = (step.value + 1) as 1 | 2 | 3;
};

const handleClose = (v: boolean) => {
  if (!v && dirty.value && !submitting.value) {
    confirmDiscard.value = true;
    return;
  }
  emit("update:open", v);
};

async function handleSubmit() {
  if (!validateName()) {
    step.value = 1;
    return;
  }
  submitting.value = true;
  try {
    // 真实提交：不关弹窗、不假装延迟 —— 关不关由调用方按请求结果决定，
    // 失败时保持打开，用户改完可以直接重试。
    await props.onSubmit({
      id: props.editTask?.id,
      name: name.value.trim(),
      description: description.value.trim(),
      target_ids: selectedIds.value,
    });
  } finally {
    submitting.value = false;
  }
}
</script>

<template>
  <AppDialog :open="open" @update:open="handleClose" max-width="640px">
    <template #title>
      <span class="text-[16px] font-semibold">{{ editTask ? "编辑监控任务" : "创建监控任务" }}</span>
    </template>

    <!-- 步骤条 -->
    <div class="flex items-center gap-2 py-1">
      <div v-for="(label, i) in STEPS" :key="label" class="flex items-center gap-2 flex-1">
        <button
          type="button"
          :disabled="!(step > i + 1)"
          @click="step > i + 1 && (step = (i + 1) as 1 | 2 | 3)"
          :class="['flex items-center gap-2', step > i + 1 ? 'cursor-pointer' : 'cursor-default']"
        >
          <span
            :class="[
              'w-6 h-6 rounded-full flex items-center justify-center text-[12px] font-medium',
              step === i + 1
                ? 'bg-primary text-primary-foreground'
                : step > i + 1
                  ? 'bg-success/15 text-success'
                  : 'bg-secondary text-muted-foreground',
            ]"
          >
            <Check v-if="step > i + 1" :size="13" />
            <template v-else>{{ i + 1 }}</template>
          </span>
          <span
            :class="[
              'text-[12.5px] whitespace-nowrap',
              step === i + 1 ? 'text-foreground font-medium' : 'text-muted-foreground',
            ]"
          >
            {{ label }}
          </span>
        </button>
        <span v-if="i < STEPS.length - 1" class="flex-1 h-px bg-border min-w-[12px]" />
      </div>
    </div>

    <!-- 步骤一 -->
    <div v-if="step === 1" class="space-y-4 py-2">
      <div class="space-y-1.5">
        <Label class="text-[13px]">任务名 <span class="text-danger">*</span></Label>
        <Input
          :model-value="name"
          @update:model-value="
            (v: string) => {
              name = v;
              nameError = '';
            }
          "
          placeholder="如：大促保障"
          :class="nameError ? 'h-9 text-[13px] border-danger' : 'h-9 text-[13px]'"
        />
        <p v-if="nameError" class="text-[11px] text-danger flex items-center gap-1">
          <AlertCircle :size="12" />
          {{ nameError }}
        </p>
      </div>
      <div class="space-y-1.5">
        <Label class="text-[13px]">描述（可选）</Label>
        <textarea
          :value="description"
          @input="description = ($event.target as HTMLTextAreaElement).value.slice(0, 255)"
          rows="3"
          placeholder="这次监控是为了什么，交给谁看"
          class="w-full px-3 py-2 rounded-lg border border-border bg-card text-[13px] focus:outline-none focus:ring-2 focus:ring-accent/30 resize-none"
        />
        <p class="text-[11px] text-muted-foreground text-right">{{ description.length }} / 255</p>
      </div>
    </div>

    <!-- 步骤二 -->
    <div v-else-if="step === 2" class="py-2">
      <TaskTargetPicker :selected-ids="selectedIds" @change="selectedIds = $event" />
    </div>

    <!-- 步骤三 -->
    <div v-else class="space-y-3 py-2">
      <div class="rounded-xl border border-border overflow-hidden">
        <div class="px-4 py-3 border-b border-border bg-secondary/30">
          <div class="text-[11px] text-muted-foreground">任务名</div>
          <div class="text-[14px] font-medium">{{ name.trim() }}</div>
        </div>
        <div class="px-4 py-3 border-b border-border">
          <div class="text-[11px] text-muted-foreground">描述</div>
          <div class="text-[13px] text-muted-foreground">
            {{ description.trim() || "（未填写）" }}
          </div>
        </div>
        <div class="px-4 py-3">
          <div class="text-[11px] text-muted-foreground">
            要监控的项目（{{ selectedIds.length }} 个）
          </div>
          <div v-if="selectedIds.length === 0" class="text-[13px] text-warning">
            未选择 —— 该任务不会收到任何告警
          </div>
          <div v-else class="mt-1 flex flex-wrap gap-1.5">
            <span
              v-for="id in selectedIds"
              :key="id"
              class="px-2 py-0.5 rounded-full bg-secondary text-[12px] font-mono"
            >
              {{ id }}
            </span>
          </div>
        </div>
      </div>
      <p class="text-[11px] text-muted-foreground">提交后可在任务工作台继续增删项目。</p>
    </div>

    <template #footer>
      <Button
        v-if="step > 1"
        type="button"
        @click="step = (step - 1) as 1 | 2 | 3"
        class="h-9 px-4 rounded-full border border-border bg-card text-foreground text-[13px] font-medium btn-pill hover:bg-secondary"
      >
        上一步
      </Button>
      <Button
        v-else
        type="button"
        @click="handleClose(false)"
        class="h-9 px-4 rounded-full border border-border bg-card text-foreground text-[13px] font-medium btn-pill hover:bg-secondary"
      >
        取消
      </Button>
      <Button
        v-if="step < 3"
        type="button"
        @click="handleNext"
        class="h-9 px-4 rounded-full bg-primary text-primary-foreground text-[13px] font-medium btn-pill hover:bg-primary/90"
      >
        下一步
      </Button>
      <Button
        v-else
        type="button"
        @click="void handleSubmit()"
        :disabled="submitting"
        class="h-9 px-4 rounded-full bg-primary text-primary-foreground text-[13px] font-medium btn-pill hover:bg-primary/90 disabled:opacity-60"
      >
        <Loader2 v-if="submitting" :size="14" class="mr-1.5 animate-spin" />
        {{ editTask ? "保存修改" : "创建任务" }}
      </Button>
    </template>
  </AppDialog>

  <!-- 放弃填写确认 -->
  <AppDialog v-model:open="confirmDiscard" max-width="380px">
    <template #title>
      <span class="text-[15px] font-semibold">放弃已填写的内容？</span>
    </template>
    <p class="text-[13px] text-muted-foreground">
      关闭后本次填写的任务名、描述与已勾选的项目都不会保留。
    </p>
    <template #footer>
      <Button
        type="button"
        @click="confirmDiscard = false"
        class="h-9 px-4 rounded-full border border-border bg-card text-foreground text-[13px] font-medium btn-pill hover:bg-secondary"
      >
        继续填写
      </Button>
      <Button
        type="button"
        @click="
          () => {
            confirmDiscard = false;
            emit('update:open', false);
          }
        "
        class="h-9 px-4 rounded-full bg-primary text-primary-foreground text-[13px] font-medium btn-pill hover:bg-primary/90"
      >
        放弃
      </Button>
    </template>
  </AppDialog>
</template>
