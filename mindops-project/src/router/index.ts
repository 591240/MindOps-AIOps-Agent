import { createRouter, createWebHistory } from "vue-router";
import { clearSession, readSession } from "@/lib/session";

import LayoutShell from "@/components/layout/LayoutShell.vue";
import LandingPage from "@/components/LandingPage.vue";
import LoginView from "@/components/LoginView.vue";
import RegisterView from "@/components/RegisterView.vue";
import AlertsView from "@/components/AlertsView.vue";
import MetricsView from "@/components/MetricsView.vue";
import KnowledgeView from "@/components/KnowledgeView.vue";
import KnowledgeDetailView from "@/components/KnowledgeDetailView.vue";
import IncidentDetailView from "@/components/IncidentDetailView.vue";
import IntegrationView from "@/components/IntegrationView.vue";
import RulesView from "@/components/RulesView.vue";
import ModelSettingsView from "@/components/ModelSettingsView.vue";
import SystemSettingsView from "@/components/SystemSettingsView.vue";
import TaskListView from "@/components/tasks/TaskListView.vue";
import TaskWorkspaceView from "@/components/tasks/TaskWorkspaceView.vue";

const ADMIN_PREFIXES = ["/metrics", "/settings", "/rules"];

const router = createRouter({
  history: createWebHistory(),
  // 浏览器前进/后退时恢复滚动位置，新导航回到顶部（对齐 React 版的 scrollRestoration）
  scrollBehavior: (_to, _from, savedPosition) => savedPosition ?? { top: 0 },
  routes: [
    { path: "/", name: "landing", component: LandingPage },
    { path: "/login", name: "login", component: LoginView },
    { path: "/register", name: "register", component: RegisterView },
    {
      // 布局壳用「无 URL 前缀的父级」承载：子路由全部是绝对路径，
      // 因此 /shell 本身不参与任何真实页面匹配，URL 与 React 版完全一致。
      path: "/shell",
      component: LayoutShell,
      meta: { requiresAuth: true },
      children: [
        { path: "/tasks", name: "tasks", component: TaskListView },
        { path: "/tasks/:id", name: "task-workspace", component: TaskWorkspaceView, props: true },
        { path: "/alerts", name: "alerts", component: AlertsView },
        { path: "/incidents/:id", name: "incident-detail", component: IncidentDetailView, props: true },
        { path: "/knowledge", name: "knowledge", component: KnowledgeView },
        { path: "/knowledge/:id", name: "knowledge-detail", component: KnowledgeDetailView, props: true },
        { path: "/metrics", name: "metrics", component: MetricsView, meta: { adminOnly: true } },
        { path: "/rules", name: "rules", component: RulesView, meta: { adminOnly: true } },
        { path: "/settings/models", name: "settings-models", component: ModelSettingsView, meta: { adminOnly: true } },
        { path: "/settings/integrations", name: "settings-integrations", component: IntegrationView, meta: { adminOnly: true } },
        { path: "/settings/system", name: "settings-system", component: SystemSettingsView, meta: { adminOnly: true } },
      ],
    },
    { path: "/:pathMatch(.*)*", redirect: "/" },
  ],
});

router.beforeEach((to) => {
  const auth = readSession();

  // 坏会话（手改 / 旧格式 / 缺 token）清掉，否则每次进受保护页都会带着一条
  // 永远解析不出记录的记录跳回登录页空转。
  if (!auth && localStorage.getItem("mindops_auth")) clearSession();

  // 已登录用户访问首页 → 跳任务列表（登录后第一屏是任务）
  if (to.path === "/" && auth) return { path: "/tasks" };

  if (to.matched.some((r) => r.meta.requiresAuth)) {
    if (!auth) {
      return { path: "/login", query: { redirect: to.fullPath } };
    }
    // meta.adminOnly 由子路由声明；前缀判断兜底，防止新增页面忘标 meta
    const adminOnly =
      to.matched.some((r) => r.meta.adminOnly) ||
      ADMIN_PREFIXES.some((p) => to.path === p || to.path.startsWith(p + "/"));
    if (adminOnly && auth.user.role !== "admin") {
      return { path: "/alerts", query: { forbidden: "1" } };
    }
  }

  return true;
});

export { router };
