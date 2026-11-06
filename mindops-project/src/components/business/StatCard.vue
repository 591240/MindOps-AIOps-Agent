<script setup lang="ts">
import type { Component } from "vue";

/**
 * 统计卡（KPI Card）—— 全站唯一实现。
 *
 * 为什么要抽出来：任务页、告警看板、统计大盘三处都要「一眼看清态势」的卡片，
 * 各写一份必然漂移（字号、内距、图标底色各不相同）。这里固定一套度量：
 * 图标 32px 方块 / 数值 26px 半粗 / 标签 12px 灰 / 可选副文案。
 *
 * 注意：卡片本身**不负责取数**，loading 与空值由调用方决定传什么。
 */
withDefaults(
  defineProps<{
    label: string;
    value: string | number;
    icon?: Component;
    /** 图标与数值的语义色（tailwind 文本色类，如 text-glow-pink） */
    tone?: string;
    /** 副文案，例如「较昨日 +3」或口径说明 */
    hint?: string;
    /** 加载中：数值位显示破折号，避免先渲染 0 再跳成真实值 */
    loading?: boolean;
    /** 点击跳转（可选），用于「点卡片看明细」 */
    to?: string;
  }>(),
  { tone: "text-foreground", loading: false },
);
</script>

<template>
  <component
    :is="to ? 'router-link' : 'div'"
    :to="to"
    class="glass rounded-xl p-4 flex items-start gap-3 transition-all"
    :class="to ? 'hover:-translate-y-0.5 hover:shadow-sm cursor-pointer' : ''"
  >
    <div v-if="icon" class="w-8 h-8 rounded-lg bg-secondary flex items-center justify-center shrink-0">
      <component :is="icon" :size="16" :class="tone" />
    </div>
    <div class="min-w-0">
      <div class="text-[24px] font-semibold heading-tight tabular-nums leading-none">
        {{ loading ? "—" : value }}
      </div>
      <div class="text-[12px] text-muted-foreground mt-1.5 truncate">{{ label }}</div>
      <div v-if="hint" class="text-[11px] text-muted-foreground/70 mt-0.5 truncate">{{ hint }}</div>
    </div>
  </component>
</template>
