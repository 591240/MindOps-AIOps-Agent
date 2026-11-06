<script setup lang="ts">
import StatusBadge from "@/components/business/StatusBadge.vue";
import SeverityTag from "@/components/business/SeverityTag.vue";
import { ArrowUp, Clock, Tag } from "lucide-vue-next";
import type { IncidentVM } from "@/types/incident";

defineProps<{ incident: IncidentVM }>();

const formatTime = (iso: string) => {
  const d = new Date(iso);
  return d.toLocaleString("zh-CN", {
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
    hour12: false,
  });
};
</script>

<template>
  <div class="glass rounded-xl p-5 space-y-4">
    <!-- 标题 -->
    <div>
      <h2
        class="text-[16px] font-semibold leading-snug mb-2 cursor-help"
        title="告警规则名：触发本事件的告警规则标识，同一规则反复触发会归并到同一事件"
      >
        {{ incident.title }}
      </h2>
      <div class="flex items-center gap-2 flex-wrap">
        <span
          class="inline-flex items-center gap-1 px-2 py-0.5 rounded-full bg-secondary text-[12px] font-mono cursor-help"
          title="组件：出问题的服务/模块，AI 检索知识库与历史案例时按它定位范围"
        >
          <Tag :size="10" />
          {{ incident.component }}
        </span>
        <SeverityTag :severity="incident.severity" />
        <StatusBadge :status="incident.status" />
      </div>
    </div>

    <!-- 时间信息 -->
    <div class="space-y-2 pt-2 border-t border-border">
      <div
        class="flex items-center gap-2 text-[13px]"
        title="本事件首次创建的时间：第一笔告警聚成事件的时刻"
      >
        <Clock :size="14" class="text-muted-foreground shrink-0" />
        <span class="text-muted-foreground">创建时间</span>
        <span class="font-mono ml-auto tabular-nums">{{ formatTime(incident.created_at) }}</span>
      </div>
      <div
        class="flex items-center gap-2 text-[13px]"
        title="同一告警最近一次触发的时刻：告警重复触发会按指纹去重并归并到本事件"
      >
        <Clock :size="14" class="text-muted-foreground shrink-0" />
        <span class="text-muted-foreground">最近告警</span>
        <span class="font-mono ml-auto tabular-nums">{{ formatTime(incident.last_alert_at) }}</span>
      </div>
    </div>

    <!-- 回灌标记 -->
    <div v-if="incident.knowledge_injected" class="pt-2 border-t border-border">
      <div
        class="inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg bg-success/5 border border-success/20 text-[12px] text-success"
      >
        <ArrowUp :size="12" />
        <span
          >已回灌知识库（v{{ incident.knowledge_injected.version }}，{{
            incident.knowledge_injected.chunk_count
          }}
          切片）</span
        >
      </div>
    </div>
  </div>
</template>
