<script setup lang="ts">
import { computed } from "vue";
import type { TaskStatus } from "@/api/tasks";

/**
 * 任务状态三态 pill：active 进行中（绿）/ paused 已暂停（黄）/ archived 已归档（灰）。
 * 状态色只表达语义，且必须配文字标签，不允许只靠颜色区分。
 */
const props = defineProps<{ status: TaskStatus }>();

const CONFIG: Record<TaskStatus, { label: string; dot: string; pill: string }> = {
  active: { label: "进行中", dot: "bg-success", pill: "bg-success/10 text-success" },
  paused: { label: "已暂停", dot: "bg-warning", pill: "bg-warning/10 text-warning" },
  archived: { label: "已归档", dot: "bg-muted-foreground/50", pill: "bg-muted text-muted-foreground" },
};

const cfg = computed(() => CONFIG[props.status] ?? CONFIG.active);
</script>

<template>
  <span
    class="inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[12px] font-medium whitespace-nowrap"
    :class="cfg.pill"
  >
    <span class="w-1.5 h-1.5 rounded-full" :class="cfg.dot" />
    {{ cfg.label }}
  </span>
</template>
