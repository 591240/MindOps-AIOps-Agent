<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from "vue";
import { cn } from "@/lib/utils";
import { AlertTriangle, Copy, Check } from "lucide-vue-next";

const props = defineProps<{
  command: string;
  description: string;
  risk: "low" | "medium" | "high";
  disabled?: boolean;
}>();
defineEmits<{ execute: []; reject: [] }>();

const RISK_CONFIG = {
  low: { label: "低风险", bg: "bg-success/10", text: "text-success" },
  medium: { label: "中风险", bg: "bg-warning/10", text: "text-warning" },
  high: { label: "高风险", bg: "bg-danger/10", text: "text-danger" },
} as const;

const riskConfig = computed(() => RISK_CONFIG[props.risk]);

const copied = ref(false);
let copyTimer: number | undefined;
onBeforeUnmount(() => window.clearTimeout(copyTimer));

function handleCopy() {
  void navigator.clipboard.writeText(props.command);
  copied.value = true;
  window.clearTimeout(copyTimer);
  copyTimer = window.setTimeout(() => (copied.value = false), 2000);
}
</script>

<template>
  <div class="rounded-lg border border-border bg-card overflow-hidden">
    <!-- 命令行 -->
    <div class="relative bg-[#1a1a1a] px-4 py-3">
      <div class="flex items-center justify-between mb-1">
        <span class="text-[11px] text-white/40 font-mono">SHELL</span>
        <div class="flex items-center gap-2">
          <span
            :class="
              cn(
                'inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[11px] font-medium',
                riskConfig.bg,
                riskConfig.text,
              )
            "
          >
            <AlertTriangle :size="10" />
            {{ riskConfig.label }}
          </span>
          <button
            @click="handleCopy"
            class="text-white/40 hover:text-white/80 transition-colors"
            title="复制命令"
          >
            <Check v-if="copied" :size="14" />
            <Copy v-else :size="14" />
          </button>
        </div>
      </div>
      <pre class="text-[13px] text-green-400 font-mono whitespace-pre-wrap break-all"><span class="text-white/30">$ </span>{{ command }}</pre>
    </div>

    <!-- 说明 + 执行 -->
    <div class="px-4 py-3 flex items-center justify-between gap-3">
      <p class="text-[13px] text-muted-foreground flex-1">{{ description }}</p>
      <div class="flex items-center gap-2 shrink-0">
        <button
          @click="$emit('reject')"
          :disabled="disabled"
          :class="
            cn(
              'px-3 py-1.5 rounded-full text-[13px] font-medium btn-pill border transition-colors',
              disabled
                ? 'border-border text-muted-foreground cursor-not-allowed'
                : 'border-border text-muted-foreground hover:bg-secondary',
            )
          "
        >
          驳回
        </button>
        <button
          @click="$emit('execute')"
          :disabled="disabled"
          :class="
            cn(
              'px-4 py-1.5 rounded-full text-[13px] font-medium btn-pill transition-colors',
              disabled
                ? 'bg-muted text-muted-foreground cursor-not-allowed'
                : risk === 'high'
                  ? 'bg-danger text-danger-foreground hover:bg-danger/90'
                  : 'bg-primary text-primary-foreground hover:bg-primary/90',
            )
          "
        >
          {{ disabled ? "已处理" : "执行" }}
        </button>
      </div>
    </div>
  </div>
</template>
