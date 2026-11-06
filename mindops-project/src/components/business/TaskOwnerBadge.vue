<script setup lang="ts">
/**
 * 告警行上的「归属任务」pill 组。
 * 告警本身不持有 task_id，归属关系由 alerts.integration_id 经 task_targets
 * 反查得出，因此同一告警可以归属多个任务；空列表是正常状态（未绑定）。
 */
defineProps<{ names: string[] }>();
</script>

<template>
  <span
    v-if="names.length === 0"
    class="text-[12px] text-muted-foreground/60"
    title="该告警来自未被任何任务绑定的项目"
  >
    未归属
  </span>
  <div
    v-else
    class="flex flex-wrap gap-1"
    :title="names.length > 1 ? `该告警同时归属：${names.join('、')}` : `归属任务：${names[0]}`"
  >
    <span
      v-for="name in names"
      :key="name"
      class="px-1.5 py-0.5 rounded border border-border text-[11px] text-muted-foreground whitespace-nowrap"
    >
      {{ name }}
    </span>
  </div>
</template>
