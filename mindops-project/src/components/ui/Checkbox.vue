<script setup lang="ts">
import { CheckboxRoot, CheckboxIndicator } from "reka-ui";
import { Check } from "lucide-vue-next";
import { cn } from "@/lib/utils";

defineProps<{ modelValue: boolean; class?: string }>();
const emit = defineEmits<{ "update:modelValue": [boolean] }>();
</script>

<template>
  <CheckboxRoot
    :model-value="modelValue"
    @update:model-value="emit('update:modelValue', $event === true)"
    :class="
      cn(
        'grid place-content-center peer h-4 w-4 shrink-0 rounded-sm border border-primary shadow cursor-pointer focus-visible:outline-none focus-visible:ring-1 focus-visible:ring-ring disabled:cursor-not-allowed disabled:opacity-50 data-[state=checked]:bg-primary data-[state=checked]:text-primary-foreground',
        $props.class,
      )
    "
  >
    <CheckboxIndicator class="grid place-content-center text-current">
      <Check class="h-4 w-4" />
    </CheckboxIndicator>
  </CheckboxRoot>
</template>
