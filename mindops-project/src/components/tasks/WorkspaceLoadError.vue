<script setup lang="ts">
import { WifiOff, RefreshCw } from "lucide-vue-next";

defineProps<{ message: string; offline: boolean }>();
defineEmits<{ retry: [] }>();
</script>

<template>
  <div class="glass rounded-xl p-4 flex items-start gap-3 border border-glow-pink/30">
    <WifiOff :size="18" class="text-glow-pink shrink-0 mt-0.5" />
    <div class="flex-1 min-w-0">
      <div class="text-[13px] font-medium">{{ offline ? "后端未连接" : "加载失败" }}</div>
      <div class="text-[12px] text-muted-foreground mt-1 break-words">{{ message }}</div>
    </div>
    <button
      @click="$emit('retry')"
      class="shrink-0 flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-secondary text-[12px] font-medium hover:bg-secondary/70 transition-colors"
    >
      <RefreshCw :size="13" />
      重试
    </button>
  </div>
</template>
