<script setup lang="ts">
defineProps<{ title: string; hint: string }>();
</script>

<template>
  <div class="glass rounded-xl py-14 flex flex-col items-center gap-2">
    <div class="text-[13px] font-medium">{{ title }}</div>
    <div class="text-[12px] text-muted-foreground text-center max-w-[420px] px-4">{{ hint }}</div>
  </div>
</template>
