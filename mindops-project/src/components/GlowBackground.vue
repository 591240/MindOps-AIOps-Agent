<script setup lang="ts">
import { onBeforeUnmount, onMounted } from "vue";

/**
 * 四色径向辉光背景 — Vercel 风核心识别符号
 *
 * 动态三件套：鼠标视差跟随 + 待机漂浮 + 透明度呼吸。
 * 实现铁律：rAF + lerp(0.16) + translate3d，无 CSS transition。
 */

const props = withDefaults(defineProps<{ dimmed?: boolean }>(), { dimmed: false });

const GLOWS = [
  { id: "pink", depth: 180, drift: 60, phase: 0, baseOp: 0.55 },
  { id: "blue", depth: 220, drift: 80, phase: 1.7, baseOp: 0.5 },
  { id: "green", depth: 260, drift: 90, phase: 3.4, baseOp: 0.5 },
  { id: "orange", depth: 90, drift: 70, phase: 5.1, baseOp: 0.4 },
];

let rafId = 0;
const mouse = { x: 0, y: 0 };

function onMouseMove(e: MouseEvent) {
  mouse.x = (e.clientX / window.innerWidth) * 2 - 1;
  mouse.y = (e.clientY / window.innerHeight) * 2 - 1;
}

onMounted(() => {
  const prefersReduced = window.matchMedia("(prefers-reduced-motion: reduce)").matches;

  const glows = GLOWS.map((g) => {
    const el = document.getElementById(`glow-${g.id}`);
    const baseOp = props.dimmed ? g.baseOp * 0.7 : g.baseOp;
    return { el, x: 0, y: 0, depth: g.depth, drift: g.drift, phase: g.phase, baseOp, op: baseOp };
  });

  if (prefersReduced) {
    for (const g of glows) {
      if (!g.el) continue;
      g.el.style.transform = "translate3d(0,0,0)";
      g.el.style.opacity = String(g.baseOp);
    }
    return;
  }

  window.addEventListener("mousemove", onMouseMove, { passive: true });

  const startTime = performance.now();
  // 限帧 ~30fps：缓慢漂浮 + lerp 插值，30fps 已看不出差别；
  // 页面不可见时跳过本帧，避免后台标签页空转。
  const FRAME_INTERVAL = 1000 / 30;
  let lastFrame = 0;

  const tick = (now: number) => {
    rafId = requestAnimationFrame(tick);
    if (document.hidden) return;
    if (now - lastFrame < FRAME_INTERVAL) return;
    lastFrame = now;

    const t = now - startTime;
    for (const g of glows) {
      if (!g.el) continue;
      const tx = -mouse.x * g.depth + Math.cos(t * 0.00045 + g.phase) * g.drift;
      const ty = -mouse.y * g.depth * 0.85 + Math.sin(t * 0.0004 + g.phase) * g.drift;

      g.x += (tx - g.x) * 0.16;
      g.y += (ty - g.y) * 0.16;
      const targetOp = g.baseOp + Math.sin(t * 0.0007 + g.phase) * 0.1;
      g.op += (targetOp - g.op) * 0.16;

      g.el.style.transform = `translate3d(${g.x}px, ${g.y}px, 0)`;
      g.el.style.opacity = String(Math.max(0, g.op));
    }
  };

  rafId = requestAnimationFrame(tick);
});

onBeforeUnmount(() => {
  window.removeEventListener("mousemove", onMouseMove);
  cancelAnimationFrame(rafId);
});
</script>

<template>
  <div class="fixed inset-0 z-0 pointer-events-none overflow-hidden bg-white">
    <div
      id="glow-pink"
      class="absolute"
      style="
        top: -15vw;
        left: -10vw;
        width: 60vw;
        height: 60vw;
        background: radial-gradient(circle, #ff0080 0%, transparent 70%);
        filter: blur(120px);
        opacity: 0.55;
        will-change: transform, opacity;
      "
    />
    <div
      id="glow-blue"
      class="absolute"
      style="
        top: -10vw;
        right: -15vw;
        width: 55vw;
        height: 55vw;
        background: radial-gradient(circle, #0070f3 0%, transparent 70%);
        filter: blur(130px);
        opacity: 0.5;
        will-change: transform, opacity;
      "
    />
    <div
      id="glow-green"
      class="absolute"
      style="
        bottom: -20vw;
        left: 20vw;
        width: 65vw;
        height: 65vw;
        background: radial-gradient(circle, #50e3c2 0%, transparent 70%);
        filter: blur(140px);
        opacity: 0.5;
        will-change: transform, opacity;
      "
    />
    <div
      id="glow-orange"
      class="absolute"
      style="
        bottom: -10vw;
        left: -15vw;
        width: 45vw;
        height: 45vw;
        background: radial-gradient(circle, #f5a623 0%, transparent 70%);
        filter: blur(120px);
        opacity: 0.4;
        will-change: transform, opacity;
      "
    />
  </div>
</template>
