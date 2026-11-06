<script setup lang="ts">
import { computed } from "vue";
import { cn } from "@/lib/utils";

const props = defineProps<{ value: number }>();
const isHigh = computed(() => props.value >= 0.6);
</script>

<template>
  <span
    :class="
      cn(
        'inline-flex items-center px-2 py-0.5 rounded-full text-[12px] font-medium font-mono',
        isHigh ? 'bg-success/10 text-success' : 'bg-warning/10 text-warning',
      )
    "
  >
    {{ (value * 100).toFixed(0) }}%
  </span>
</template>
