<script setup lang="ts">
import { computed, ref } from "vue";
import ConfidenceBadge from "@/components/business/ConfidenceBadge.vue";
import EvidenceCard from "@/components/business/EvidenceCard.vue";
import { ChevronDown, ChevronRight, Loader2, Circle } from "lucide-vue-next";
import type { IncidentStepVM } from "@/types/incident";

const props = defineProps<{ steps: IncidentStepVM[] }>();

const STEP_ICONS: Record<string, string> = {
  Triage: "🔍",
  Context: "📋",
  Diagnose: "🧠",
  Remediate: "🔧",
  Ticket: "🎫",
  Recap: "📝",
};

const expanded = ref<Set<string>>(new Set([props.steps[2]?.id || ""]));

const toggle = (id: string) => {
  const next = new Set(expanded.value);
  if (next.has(id)) next.delete(id);
  else next.add(id);
  expanded.value = next;
};

const formatLatency = (ms: number) => {
  if (ms === 0) return "—";
  if (ms < 1000) return `${ms}ms`;
  return `${(ms / 1000).toFixed(1)}s`;
};

/**
 * 概览条：把"这次诊断到底花了多久、绕了几圈、最后多确定"提到最上面。
 * 对标 Langfuse 的 trace 头部——先给结论，再让人往下钻每一步。
 */
const totalMs = computed(() => props.steps.reduce((sum, s) => sum + (s.latency_ms || 0), 0));
const maxMs = computed(() => Math.max(1, ...props.steps.map((s) => s.latency_ms || 0)));
const totalRetry = computed(() => Math.max(0, ...props.steps.map((s) => s.retry || 0)));
const completedCount = computed(
  () => props.steps.filter((s) => s.status === "completed").length,
);
/** 最终置信度：最后一个已完成且带置信度的步骤（Diagnose 重新判定后会覆盖前值） */
const finalConfidence = computed(() => {
  const done = props.steps.filter((s) => s.status === "completed" && s.confidence > 0);
  return done.length ? done[done.length - 1].confidence : 0;
});

/** 单步耗时占最慢一步的比例，用于瀑布条（低于 2% 给个最小可见宽度） */
const barWidth = (ms: number) => `${Math.max(ms > 0 ? 2 : 0, Math.round((ms / maxMs.value) * 100))}%`;
const barColor = (step: IncidentStepVM) => {
  if (step.retry > 0) return "#f5a623";
  if (step.status === "completed") return "#0070f3";
  if (step.status === "in_progress") return "#50e3c2";
  return "#EAEAEA";
};
</script>

<template>
  <div class="glass rounded-xl p-5">
    <div class="flex items-center justify-between mb-3">
      <h3 class="text-[16px] font-semibold">执行轨迹</h3>
      <span class="text-[12px] text-muted-foreground"
        >{{ completedCount }}/{{ steps.length }} 完成</span
      >
    </div>

    <!-- 概览条：总耗时 / 回环 / 最终置信度（Langfuse trace 头部的等价物） -->
    <div class="grid grid-cols-3 gap-2 mb-4">
      <div class="rounded-lg bg-secondary/40 px-2.5 py-2">
        <div class="text-[10px] text-muted-foreground">总耗时</div>
        <div class="text-[15px] font-semibold tabular-nums leading-tight">
          {{ formatLatency(totalMs) }}
        </div>
      </div>
      <div class="rounded-lg bg-secondary/40 px-2.5 py-2">
        <div class="text-[10px] text-muted-foreground">回环次数</div>
        <div
          class="text-[15px] font-semibold tabular-nums leading-tight"
          :class="totalRetry > 0 ? 'text-warning' : ''"
        >
          {{ totalRetry }}
        </div>
      </div>
      <div class="rounded-lg bg-secondary/40 px-2.5 py-2">
        <div class="text-[10px] text-muted-foreground">最终置信度</div>
        <div class="text-[15px] font-semibold tabular-nums leading-tight">
          {{ finalConfidence > 0 ? finalConfidence.toFixed(2) : "—" }}
        </div>
      </div>
    </div>

    <!-- 时间线 -->
    <div class="relative">
      <!-- 竖直连线 -->
      <div class="absolute left-[11px] top-2 bottom-2 w-0.5 bg-border" />

      <div class="space-y-2">
        <div v-for="step in steps" :key="step.id" class="relative pl-8">
          <!-- 节点 -->
          <div class="absolute left-0 top-3 w-6 h-6 flex items-center justify-center">
            <div v-if="step.status === 'completed'" class="w-3 h-3 rounded-full bg-success z-10" />
            <div
              v-else-if="step.status === 'in_progress'"
              class="w-3 h-3 rounded-full bg-accent z-10 pulse-dot"
            />
            <div
              v-else
              class="w-3 h-3 rounded-full border-2 border-border bg-card z-10"
            />
          </div>

          <!-- 步骤卡片 -->
          <div
            class="rounded-lg border border-border bg-card overflow-hidden cursor-pointer hover:border-border/80 transition-colors"
            @click="step.status !== 'pending' && toggle(step.id)"
          >
            <!-- 折叠态 -->
            <div class="px-3 py-2.5 flex items-center gap-2">
              <ChevronDown
                v-if="expanded.has(step.id)"
                :size="14"
                class="text-muted-foreground"
              />
              <ChevronRight v-else :size="14" class="text-muted-foreground" />
              <span class="text-[11px] font-mono text-muted-foreground">{{
                STEP_ICONS[step.agent] || "•"
              }}</span>
              <span class="text-[13px] font-medium">{{ step.agent }}</span>
              <span class="text-[12px] text-muted-foreground">— {{ step.agent_name }}</span>
              <div class="ml-auto flex items-center gap-2">
                <span
                  v-if="step.retry > 0"
                  class="text-[11px] px-1.5 py-0.5 rounded-full bg-warning/10 text-warning font-mono"
                >
                  回环 ×{{ step.retry }}
                </span>
                <ConfidenceBadge v-if="step.confidence > 0" :value="step.confidence" />
                <span class="text-[11px] font-mono text-muted-foreground tabular-nums">
                  {{ formatLatency(step.latency_ms) }}
                </span>
              </div>
            </div>

            <!-- 耗时瀑布条：宽度 = 该步耗时占最慢一步的比例，橙色 = 发生过回环 -->
            <div v-if="step.latency_ms > 0" class="px-3 pb-2">
              <div class="h-[3px] rounded-full bg-secondary/70 overflow-hidden">
                <div
                  class="h-full rounded-full transition-all duration-500"
                  :style="{ width: barWidth(step.latency_ms), background: barColor(step) }"
                />
              </div>
            </div>

            <!-- 折叠态摘要 -->
            <div v-if="!expanded.has(step.id) && step.output_summary" class="px-3 pb-2.5 -mt-1">
              <p class="text-[12px] text-muted-foreground truncate">{{ step.output_summary }}</p>
            </div>

            <!-- 展开态 -->
            <div
              v-if="expanded.has(step.id) && step.status !== 'pending'"
              class="px-3 pb-3 space-y-3 fade-in-300"
            >
              <!-- 输入摘要 -->
              <div v-if="step.input_summary">
                <div class="eyebrow mb-1">输入</div>
                <p class="text-[12px] text-muted-foreground leading-relaxed">
                  {{ step.input_summary }}
                </p>
              </div>

              <!-- 输出摘要 -->
              <div v-if="step.output_summary">
                <div class="eyebrow mb-1">输出</div>
                <p class="text-[12px] text-foreground leading-relaxed">{{ step.output_summary }}</p>
              </div>

              <!-- 工具调用 -->
              <div v-if="step.tool_calls.length > 0">
                <div class="eyebrow mb-1.5">工具调用</div>
                <div class="space-y-1">
                  <div
                    v-for="(tc, i) in step.tool_calls"
                    :key="i"
                    class="flex items-center gap-2 text-[12px] font-mono px-2 py-1 rounded bg-secondary/60"
                  >
                    <span class="text-accent">{{ tc.name }}</span>
                    <span class="text-muted-foreground">({{ JSON.stringify(tc.args) }})</span>
                    <span class="text-muted-foreground/60 ml-auto">→ {{ tc.result }}</span>
                  </div>
                </div>
              </div>

              <!-- 证据卡片 -->
              <div v-if="step.evidence.length > 0">
                <div class="eyebrow mb-1.5">证据</div>
                <div class="space-y-2">
                  <EvidenceCard v-for="(ev, i) in step.evidence" :key="i" v-bind="ev" />
                </div>
              </div>

              <!-- 空态 -->
              <p
                v-if="step.evidence.length === 0 && step.tool_calls.length === 0"
                class="text-[12px] text-muted-foreground italic"
              >
                本次诊断未引用外部证据
              </p>
            </div>

            <!-- 进行中 -->
            <div
              v-if="expanded.has(step.id) && step.status === 'in_progress'"
              class="px-3 pb-3 flex items-center gap-2 text-[12px] text-accent fade-in-300"
            >
              <Loader2 :size="14" class="animate-spin" />
              推理中...
            </div>

            <!-- 未开始 -->
            <div
              v-if="step.status === 'pending'"
              class="px-3 pb-2.5 flex items-center gap-2 text-[12px] text-muted-foreground"
            >
              <Circle :size="12" />
              等待执行
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
