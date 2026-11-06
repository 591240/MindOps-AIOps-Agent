<script setup lang="ts">
import { computed } from "vue";
import { cn } from "@/lib/utils";

/**
 * 去重状态徽标：`count` = 该指纹的总命中次数（含首次入库），
 * 来自 `alerts.dedup_count`。每个指纹在 alerts 只有一行，因此
 * 有信息量的是"被去重了多少次"，而不是"是否首次"。
 */
const props = defineProps<{ count: number }>();

const total = computed(() =>
  Number.isFinite(props.count) && props.count > 0 ? props.count : 1,
);
</script>

<template>
  <span
    v-if="total <= 1"
    class="inline-flex items-center px-2 py-0.5 rounded-full text-[12px] font-medium bg-muted/60 text-muted-foreground"
  >
    无重复
  </span>
  <span
    v-else
    :class="
      cn(
        'inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[12px] font-medium',
        'bg-accent/10 text-accent',
      )
    "
    :title="`该指纹累计命中 ${total} 次，已去重 ${total - 1} 次`"
  >
    去重
    <span class="font-mono">×{{ total - 1 }}</span>
  </span>
</template>
