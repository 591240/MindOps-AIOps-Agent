<script setup lang="ts">
import { computed } from "vue";
import { cn } from "@/lib/utils";

const props = defineProps<{
  status: "new" | "diagnosing" | "human_needed" | "resolved" | "closed";
}>();

const CONFIG = {
  new: { bg: "bg-accent/10", text: "text-accent", label: "新建", dot: "bg-accent", hint: "事件刚创建，尚未开始诊断" },
  diagnosing: { bg: "bg-accent/10", text: "text-accent", label: "诊断中", dot: "bg-accent", hint: "AI 智能体正在收集证据并定位根因" },
  human_needed: {
    bg: "bg-danger/10",
    text: "text-danger",
    label: "待人工",
    dot: "bg-danger",
    hint: "AI 诊断完成但置信度不足，已停止自动推理，等待人工确认根因与处置",
  },
  resolved: { bg: "bg-success/10", text: "text-success", label: "已解决", dot: "bg-success", hint: "处置已确认执行，事件已解决" },
  closed: { bg: "bg-muted/40", text: "text-muted-foreground", label: "已关闭", dot: "bg-muted-foreground/40", hint: "事件已关闭，不再跟进" },
} as const;

const config = computed(() => CONFIG[props.status] ?? CONFIG.new);
</script>

<template>
  <span
    :title="config.hint"
    :class="
      cn(
        'inline-flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[12px] font-medium cursor-help',
        config.bg,
        config.text,
      )
    "
  >
    <span :class="cn('w-1.5 h-1.5 rounded-full', config.dot)" />
    {{ config.label }}
  </span>
</template>
