<script setup lang="ts">
import { computed } from "vue";
import { RouterLink } from "vue-router";
import {
  AlertTriangle,
  BarChart3,
  Settings,
  Cog,
  LogOut,
  Plug,
  ListChecks,
  BookOpen,
  ScrollText,
  type LucideIcon,
} from "lucide-vue-next";

interface NavItem {
  label: string;
  path: string;
  icon: LucideIcon;
  role?: "admin";
  group: "work" | "admin";
}

// 首项「任务」= 登录后落地页。「事件详情」不常驻侧栏：它是二级页面，
// 入口是告警行点击与任务工作台的事件列表。
const NAV_ITEMS: NavItem[] = [
  { label: "任务", path: "/tasks", icon: ListChecks, group: "work" },
  { label: "告警看板", path: "/alerts", icon: AlertTriangle, group: "work" },
  { label: "知识库", path: "/knowledge", icon: BookOpen, group: "work" },
  { label: "统计大盘", path: "/metrics", icon: BarChart3, role: "admin", group: "work" },
  { label: "模型设置", path: "/settings/models", icon: Settings, role: "admin", group: "admin" },
  { label: "规则库", path: "/rules", icon: ScrollText, role: "admin", group: "admin" },
  { label: "集成管理", path: "/settings/integrations", icon: Plug, role: "admin", group: "admin" },
  { label: "系统设置", path: "/settings/system", icon: Cog, role: "admin", group: "admin" },
];

const GROUP_TITLES: Record<NavItem["group"], string> = {
  work: "工作台",
  admin: "管理",
};

const props = defineProps<{
  role: string;
  currentPath: string;
  user: { username: string; role: string };
}>();
defineEmits<{ logout: [] }>();

/** 分组渲染：engineer 只剩「工作台」组时，组标题也要跟着消失 */
const groups = computed(() => {
  const visible = NAV_ITEMS.filter((item) => !item.role || item.role === props.role);
  return (["work", "admin"] as const)
    .map((key) => ({
      key,
      title: GROUP_TITLES[key],
      items: visible.filter((item) => item.group === key),
    }))
    .filter((group) => group.items.length > 0 && !(group.key === "admin" && props.role !== "admin"));
});

function isActive(path: string) {
  return props.currentPath === path || (path !== "/alerts" && props.currentPath.startsWith(path));
}
</script>

<template>
  <aside class="w-[220px] shrink-0 bg-white border-r border-border flex flex-col">
    <!-- Logo：黑底方块 + 白字，与整站的克制风格一致 -->
    <div class="h-14 flex items-center px-4 border-b border-border/60">
      <div class="flex items-center gap-2.5">
        <div class="w-7 h-7 rounded-lg bg-foreground flex items-center justify-center">
          <span class="text-white text-[13px] font-semibold">M</span>
        </div>
        <span class="text-[15px] font-semibold text-foreground tracking-tight">MindOps</span>
      </div>
    </div>

    <!-- 导航：分组 + 紧凑行；激活态用浅灰底，不再用蓝色竖条和阴影胶囊 -->
    <nav class="flex-1 overflow-y-auto px-2.5 py-4 space-y-5">
      <div v-for="group in groups" :key="group.key">
        <p
          class="px-3 mb-1.5 text-[11px] font-medium text-muted-foreground/70"
          style="letter-spacing: 0.12em"
        >
          {{ group.title }}
        </p>
        <div class="space-y-0.5">
          <RouterLink
            v-for="item in group.items"
            :key="item.path"
            :to="item.path"
            class="group flex items-center gap-2.5 px-3 h-9 rounded-lg text-[13.5px] transition-colors"
            :class="
              isActive(item.path)
                ? 'bg-secondary text-foreground font-medium'
                : 'text-muted-foreground hover:bg-secondary/60 hover:text-foreground'
            "
          >
            <component
              :is="item.icon"
              :size="16"
              :stroke-width="1.75"
              :class="isActive(item.path) ? 'text-foreground' : 'text-muted-foreground/70'"
            />
            <span class="flex-1">{{ item.label }}</span>
          </RouterLink>
        </div>
      </div>
    </nav>

    <!-- 底部用户区：去玻璃拟态，发丝线分隔 + 悬停浅灰 -->
    <div class="border-t border-border/60 p-3">
      <div
        class="flex items-center gap-2.5 px-2 py-2 rounded-lg hover:bg-secondary/60 transition-colors"
      >
        <div
          class="w-8 h-8 rounded-full bg-secondary border border-border flex items-center justify-center shrink-0"
        >
          <span class="text-[12px] font-medium text-foreground/80">
            {{ user.username?.[0]?.toUpperCase() || "U" }}
          </span>
        </div>

        <div class="flex-1 min-w-0">
          <div class="text-[13px] font-medium text-foreground truncate">{{ user.username }}</div>
          <div class="text-[11px] text-muted-foreground">
            {{ user.role === "admin" ? "管理员" : "工程师" }}
          </div>
        </div>

        <button
          @click="$emit('logout')"
          class="p-1.5 rounded-md text-muted-foreground/50 hover:text-destructive hover:bg-destructive/5 transition-colors"
          title="退出登录"
        >
          <LogOut :size="14" :stroke-width="1.8" />
        </button>
      </div>
    </div>
  </aside>
</template>
