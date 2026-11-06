<script setup lang="ts">
import { ref, watch } from "vue";
import { useRouter } from "vue-router";
import { incidentsApi, toIncidentVM } from "@/api/incidents";
import { ApiError } from "@/api/client";
import { openIncidentStream } from "@/api/stream";
import type { IncidentVM } from "@/types/incident";
import IncidentOverview from "@/components/incident/IncidentOverview.vue";
import AgentTimeline from "@/components/incident/AgentTimeline.vue";
import DiagnosisPanel from "@/components/incident/DiagnosisPanel.vue";
import { FileQuestion, WifiOff, RefreshCw } from "lucide-vue-next";

/**
 * 事件详情 /incidents/{id}（F-03 / F-04 / F-06 / F-07 消费端）。
 *
 * 数据口径：**只呈现后端真实数据，不存在任何演示数据回退**。
 * - 命中后端 → 用真实详情（`toIncidentVM` 适配）；
 * - 后端 404 → 明确"事件不存在"（id 本身就是错的，编数据只会骗人）；
 * - 后端不可达 / 其它错误 → 显式错误态 + 重试按钮，不拿假事件顶替。
 *
 * 静默刷新（SSE 触发的重拉）失败时保留当前已在屏的真实数据，不把页面打成错误态。
 */
const props = defineProps<{ id: string }>();
const router = useRouter();

const loading = ref(true);
const incident = ref<IncidentVM | null>(null);
const state = ref<"ok" | "not_found" | "error">("ok");
const errorMsg = ref("");
const isNetworkFailure = ref(false);

async function load(rawId: string | undefined, silent = false) {
  if (!silent) loading.value = true;
  errorMsg.value = "";
  if (!rawId) {
    state.value = "not_found";
    incident.value = null;
    loading.value = false;
    return;
  }
  try {
    const dto = await incidentsApi.detail(rawId);
    incident.value = toIncidentVM(dto);
    state.value = "ok";
    isNetworkFailure.value = false;
  } catch (err) {
    if (err instanceof ApiError && err.isNetworkError) {
      // 静默刷新失败：保留当前数据不动（重诊断轮询期间网络抖动不应清空页面）
      if (!silent) {
        state.value = "error";
        incident.value = null;
        isNetworkFailure.value = true;
        errorMsg.value = "无法连接后端服务，请确认 mindops-backend 已启动后重试。";
      }
    } else if (err instanceof ApiError && err.status === 404) {
      state.value = "not_found";
      incident.value = null;
      isNetworkFailure.value = false;
    } else {
      state.value = "error";
      incident.value = null;
      isNetworkFailure.value = false;
      errorMsg.value = err instanceof Error ? err.message : "加载事件详情失败";
    }
  } finally {
    if (!silent) loading.value = false;
  }
}

watch(
  () => props.id,
  (id) => void load(id),
  { immediate: true },
);

// 审查 #14：订阅该事件的编排进度流（此前 openIncidentStream 全仓零调用，
// 诊断期间页面不自动更新，用户只能手动刷新）。SSE 事件到达 → 静默重拉详情；
// 1s 防抖合并批量 step/loop 事件，避免一秒内打七八次接口。
watch(
  [() => props.id, state],
  ([id, s], _prev, onCleanup) => {
    if (!id || s === "not_found") return;
    let timer: number | null = null;
    const close = openIncidentStream(id, {
      onIncident: () => {
        if (timer !== null) return;
        timer = window.setTimeout(() => {
          timer = null;
          void load(id, true);
        }, 1000);
      },
    });
    onCleanup(() => {
      close();
      if (timer !== null) window.clearTimeout(timer);
    });
  },
  { immediate: true },
);
</script>

<template>
  <div v-if="loading" class="grid grid-cols-12 gap-4 h-[calc(100vh-88px)]">
    <div class="col-span-3 space-y-3">
      <div v-for="i in 4" :key="i" class="h-16 bg-secondary rounded-lg animate-pulse" />
    </div>
    <div class="col-span-3 space-y-3">
      <div v-for="i in 3" :key="i" class="h-24 bg-secondary rounded-lg animate-pulse" />
    </div>
    <div class="col-span-6 space-y-3">
      <div v-for="i in 3" :key="i" class="h-20 bg-secondary rounded-lg animate-pulse" />
    </div>
  </div>

  <!-- 空态：区分「事件不存在」与「加载失败」。两者都不编数据，但可行动项不同。 -->
  <div
    v-else-if="state === 'not_found' || state === 'error' || !incident"
    class="glass rounded-xl py-20 flex flex-col items-center gap-3"
  >
    <div class="w-12 h-12 rounded-full bg-secondary flex items-center justify-center">
      <FileQuestion v-if="state === 'not_found'" :size="22" class="text-muted-foreground" />
      <WifiOff v-else :size="22" class="text-warning" />
    </div>
    <div class="text-[15px] font-medium">
      {{ state === "not_found" ? "事件不存在" : "加载失败" }}
    </div>
    <div class="text-[13px] text-muted-foreground text-center max-w-[380px]">
      {{
        state === "not_found"
          ? `没有找到 id 为 ${id ?? "—"} 的诊断事件，它可能已被清理或链接不正确。`
          : errorMsg || "加载事件详情失败"
      }}
    </div>
    <div class="flex items-center gap-2 mt-2">
      <button
        v-if="state !== 'not_found'"
        @click="void load(id)"
        class="px-4 py-2 rounded-full bg-primary text-primary-foreground text-[13px] font-medium hover:opacity-90 transition-opacity btn-pill flex items-center gap-1.5"
      >
        <RefreshCw :size="13" />
        重试
      </button>
      <button
        @click="void router.push('/alerts')"
        class="px-4 py-2 rounded-full border border-border bg-card text-[13px] font-medium hover:bg-secondary transition-colors btn-pill"
      >
        返回告警看板
      </button>
    </div>
    <div v-if="isNetworkFailure" class="text-[12px] text-muted-foreground/70 mt-1">
      提示：后端默认监听 127.0.0.1:8010，请确认服务已启动。
    </div>
  </div>

  <div v-else class="space-y-3">
    <div class="grid grid-cols-12 gap-4 h-[calc(100vh-88px)]">
      <!-- 左栏 · 事件概览 -->
      <div class="col-span-3 overflow-y-auto">
        <IncidentOverview :incident="incident" />
      </div>

      <!-- 中栏 · 诊断结论（结论先行：先给答案，过程放最后） -->
      <div class="col-span-3 overflow-y-auto">
        <DiagnosisPanel :incident="incident" @refresh="void load(id, true)" />
      </div>

      <!-- 右栏 · Agent 时间线 -->
      <div class="col-span-6 overflow-y-auto">
        <AgentTimeline :steps="incident.steps" />
      </div>
    </div>
  </div>
</template>
