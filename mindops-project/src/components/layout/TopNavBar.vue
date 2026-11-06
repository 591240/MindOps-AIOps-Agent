<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref } from "vue";
import { useRouter } from "vue-router";
import { Bell, Wifi, WifiOff, RefreshCw, Menu, ArrowLeft } from "lucide-vue-next";

const props = defineProps<{
  pathname: string;
  /** 后端连通状态。由 REST 轮询（30s）得出，不是 SSE 推送通道的状态。 */
  connectionStatus: "connected" | "polling" | "disconnected";
  unreadCount: number;
  /** 移动端传 true：显示汉堡按钮以开合侧栏抽屉。 */
  showNavToggle?: boolean;
}>();
defineEmits<{ "toggle-nav": [] }>();

const router = useRouter();

const PAGE_TITLES: Record<string, string> = {
  "/tasks": "监控任务",
  "/alerts": "告警看板",
  "/incidents": "事件详情",
  "/knowledge": "知识库",
  "/metrics": "统计大盘",
  "/settings": "系统设置",
  "/settings/models": "模型设置",
  "/settings/integrations": "集成管理",
};

function getPageTitle(pathname: string): string {
  if (pathname === "/") return "首页";
  // 最长前缀优先：/settings/models 必须先于 /settings 命中
  const keys = Object.keys(PAGE_TITLES).sort((a, b) => b.length - a.length);
  for (const key of keys) {
    if (pathname === key || pathname.startsWith(key + "/")) return PAGE_TITLES[key];
  }
  return "MindOps";
}

const isDetailPage = computed(() => /^\/(incidents|tasks)\/[^/]+$/.test(props.pathname));

const time = ref(new Date());
let timer: number | undefined;
onMounted(() => {
  timer = window.setInterval(() => (time.value = new Date()), 1000);
});
onBeforeUnmount(() => window.clearInterval(timer));

const CONNECTION = {
  connected: { color: "bg-success", text: "已连接", Icon: Wifi },
  polling: { color: "bg-warning", text: "降级轮询", Icon: RefreshCw },
  disconnected: { color: "bg-muted-foreground/40", text: "未连接", Icon: WifiOff },
} as const;

const conn = computed(() => CONNECTION[props.connectionStatus]);

const showTip = ref(false);

function goBack() {
  // 用父级列表路由而不是 history.back()：详情页可能是直接打开的
  // （分享链接/刷新后），此时 back 会退到站外。
  if (props.pathname.startsWith("/tasks/")) void router.push("/tasks");
  else void router.push("/alerts");
}
</script>

<template>
  <header
    class="h-10 glass-strong border-b border-border flex items-center justify-between px-4 sm:px-6 shrink-0"
  >
    <!-- 面包屑 -->
    <div class="flex items-center gap-2 text-[14px] min-w-0">
      <button
        v-if="showNavToggle"
        type="button"
        @click="$emit('toggle-nav')"
        aria-label="打开导航"
        class="p-1 -ml-1 rounded-md hover:bg-white/50 transition-colors shrink-0"
      >
        <Menu :size="16" class="text-muted-foreground" />
      </button>
      <button
        v-if="isDetailPage"
        type="button"
        @click="goBack"
        aria-label="返回列表"
        title="返回列表"
        class="p-1 rounded-md hover:bg-white/50 transition-colors shrink-0"
      >
        <ArrowLeft :size="16" class="text-muted-foreground" />
      </button>
      <span class="text-muted-foreground shrink-0">MindOps</span>
      <span class="text-muted-foreground/40 shrink-0">/</span>
      <span class="font-medium truncate">{{ getPageTitle(pathname) }}</span>
    </div>

    <!-- 右侧 -->
    <div class="flex items-center gap-4">
      <!-- 后端连通指示灯（REST 轮询，非 SSE 推送通道） -->
      <div
        class="relative flex items-center gap-1.5 cursor-default"
        @mouseenter="showTip = true"
        @mouseleave="showTip = false"
      >
        <span
          class="w-2 h-2 rounded-full"
          :class="[conn.color, connectionStatus === 'connected' ? 'pulse-dot' : '']"
        />
        <component :is="conn.Icon" :size="14" class="text-muted-foreground" />
        <div
          v-if="showTip"
          class="absolute top-full right-0 mt-1 px-2.5 py-1.5 rounded-lg glass-strong text-[12px] whitespace-nowrap shadow-md z-50"
        >
          后端连接：{{ conn.text }}
        </div>
      </div>

      <!-- 通知铃铛 -->
      <div class="relative cursor-pointer">
        <Bell :size="16" class="text-muted-foreground" />
        <span
          v-if="unreadCount > 0"
          class="absolute -top-1.5 -right-1.5 min-w-[16px] h-[16px] px-1 rounded-full bg-danger text-white text-[10px] flex items-center justify-center font-medium"
        >
          {{ unreadCount }}
        </span>
      </div>

      <!-- 时间 -->
      <span class="text-[13px] text-muted-foreground tabular-nums">
        {{ time.toLocaleString("zh-CN", { hour12: false }) }}
      </span>
    </div>
  </header>
</template>
