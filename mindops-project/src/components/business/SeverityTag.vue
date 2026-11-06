<script setup lang="ts">
import { computed } from "vue";
import { cn } from "@/lib/utils";

const props = defineProps<{ severity: "P0" | "P1" | "P2" | "P3" }>();

const CONFIG = {
  P0: { bg: "bg-danger/10", text: "text-danger", label: "P0", hint: "严重度 P0：最高级，核心服务不可用/全面故障，需立即处理" },
  P1: { bg: "bg-danger/10", text: "text-danger", label: "P1", hint: "严重度 P1：重要功能受损，需尽快处理" },
  P2: { bg: "bg-warning/10", text: "text-warning", label: "P2", hint: "严重度 P2：部分功能受影响，可择期处理" },
  P3: { bg: "bg-muted/40", text: "text-muted-foreground", label: "P3", hint: "严重度 P3：轻微问题/提示信息" },
} as const;

const config = computed(() => CONFIG[props.severity] ?? CONFIG.P3);
</script>

<template>
  <span
    :title="config.hint"
    :class="
      cn(
        'inline-flex items-center px-2 py-0.5 rounded-full text-[12px] font-medium cursor-help',
        config.bg,
        config.text,
      )
    "
  >
    {{ config.label }}
  </span>
</template>
