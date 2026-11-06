<script setup lang="ts" generic="T extends DockItem">
import { computed, onBeforeUnmount, onMounted, reactive, ref } from "vue";
import "@/components/ui/dock.css";

export interface DockItem {
  id: string;
  label: string;
  className?: string;
}

const props = withDefaults(
  defineProps<{
    items: T[];
    magnification?: number;
    distance?: number;
    panelHeight?: number;
    dockHeight?: number;
    baseItemSize?: number;
  }>(),
  {
    magnification: 76,
    distance: 200,
    panelHeight: 68,
    dockHeight: 256,
    baseItemSize: 50,
  },
);

const emit = defineEmits<{ select: [id: string] }>();

const itemRefs = ref<(HTMLDivElement | null)[]>([]);
const sizes = reactive<number[]>(props.items.map(() => props.baseItemSize));
const rowHeight = ref(props.panelHeight);
const hovered = ref(false);
const hoveredItem = ref<string | null>(null);

// 原版用 framer-motion 的 spring；这里用逐帧临界阻尼趋近，
// 视觉上等价（缓慢放大 + 回弹感），且不引入额外依赖。
const EASE = 0.22;

const maxHeight = computed(() =>
  Math.max(props.dockHeight, props.magnification + props.magnification / 2 + 4),
);

let mouseX = Number.POSITIVE_INFINITY;
let rafId = 0;

function targetSize(distanceFromCenter: number): number {
  const d = Math.abs(distanceFromCenter);
  if (!Number.isFinite(d) || d >= props.distance) return props.baseItemSize;
  const ratio = 1 - d / props.distance;
  return props.baseItemSize + (props.magnification - props.baseItemSize) * ratio;
}

function tick() {
  rafId = requestAnimationFrame(tick);
  props.items.forEach((_, i) => {
    const el = itemRefs.value[i];
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const target = targetSize(mouseX - (rect.x + props.baseItemSize / 2));
    sizes[i] += (target - sizes[i]) * EASE;
  });
  const targetH = hovered.value ? maxHeight.value : props.panelHeight;
  rowHeight.value += (targetH - rowHeight.value) * EASE;
}

onMounted(() => {
  rafId = requestAnimationFrame(tick);
});
onBeforeUnmount(() => cancelAnimationFrame(rafId));

function onMove(e: MouseEvent) {
  hovered.value = true;
  mouseX = e.pageX;
}

function onLeave() {
  hovered.value = false;
  mouseX = Number.POSITIVE_INFINITY;
  hoveredItem.value = null;
}

function onKeydown(e: KeyboardEvent, id: string) {
  if (e.key === "Enter" || e.key === " ") {
    e.preventDefault();
    emit("select", id);
  }
}
</script>

<template>
  <div class="dock-outer" :style="{ height: `${rowHeight}px` }">
    <div
      class="dock-panel"
      :style="{ height: `${panelHeight}px` }"
      role="toolbar"
      aria-label="功能导航 Dock"
      @mousemove="onMove"
      @mouseleave="onLeave"
    >
      <div
        v-for="(item, i) in items"
        :key="item.id"
        :ref="(el) => (itemRefs[i] = el as HTMLDivElement | null)"
        class="dock-item"
        :class="item.className"
        :style="{ width: `${sizes[i]}px`, height: `${sizes[i]}px` }"
        tabindex="0"
        role="button"
        aria-haspopup="true"
        :aria-label="item.label"
        @click="emit('select', item.id)"
        @mouseenter="hoveredItem = item.id"
        @keydown="onKeydown($event, item.id)"
      >
        <div v-if="hoveredItem === item.id" class="dock-label">{{ item.label }}</div>
        <div class="dock-card-inner">
          <slot name="item" :item="item" />
        </div>
      </div>
    </div>
  </div>
</template>
