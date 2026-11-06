<script setup lang="ts">
import { computed } from "vue";
import { FileText, BookOpen, Brain, ExternalLink } from "lucide-vue-next";

const props = defineProps<{
  quote: string;
  source_doc: string;
  score: number;
  source_type: "rag" | "incident" | "llm-reasoning";
}>();

const SOURCE_ICONS = {
  rag: { Icon: BookOpen, label: "知识库" },
  incident: { Icon: FileText, label: "相似案例" },
  "llm-reasoning": { Icon: Brain, label: "LLM推理" },
} as const;

const config = computed(() => SOURCE_ICONS[props.source_type] ?? SOURCE_ICONS.rag);
</script>

<template>
  <div class="rounded-lg border border-border bg-card p-3 hover:shadow-md transition-shadow">
    <div class="flex items-start gap-2.5">
      <div class="w-7 h-7 rounded-lg bg-secondary flex items-center justify-center shrink-0">
        <component :is="config.Icon" :size="14" class="text-muted-foreground" />
      </div>
      <div class="flex-1 min-w-0">
        <div class="flex items-center justify-between gap-2 mb-1">
          <span class="text-[11px] text-muted-foreground eyebrow">{{ config.label }}</span>
          <span class="text-[11px] font-mono text-muted-foreground">
            score: {{ score.toFixed(4) }}
          </span>
        </div>
        <p class="text-[13px] text-foreground leading-relaxed line-clamp-3 mb-1.5">{{ quote }}</p>
        <button class="inline-flex items-center gap-1 text-[12px] text-accent hover:underline">
          <span class="font-mono truncate max-w-[200px]">{{ source_doc }}</span>
          <ExternalLink :size="11" class="shrink-0" />
        </button>
      </div>
    </div>
  </div>
</template>
