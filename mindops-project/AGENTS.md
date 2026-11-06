# MindOps 前端工程约定（Vue 3）

## 技术栈（不要替换）

- **Vue 3.5** `<script setup lang="ts">` + **Vite 7** + **TypeScript 5.8**
- **Vue Router 5**（history 模式，路由集中在 `src/router/index.ts`）
- **Tailwind CSS v4**（`@tailwindcss/vite`，设计令牌在 `src/styles.css`）
- **reka-ui** — 无头交互基元（Dialog / Select / Switch / Checkbox / Tabs / Tooltip / Slider）
- **lucide-vue-next** — 全部功能图标使用此库的线性 SVG，不用 emoji 充当图标
- **ECharts 6** — 图表统一走 `src/components/ui/EChart.vue`（按需引入，不要在页面里直接 import 全量 echarts）
- **vue-sonner** — toast

> 2026-10-26 前本项目曾是 React 19（TanStack Router + shadcn/ui + recharts）。React 版已归档在 `../_backup/mindops-project-react-20261026-src/`，不要混用两套写法。

## SFC 导入约定（踩过的坑）

- 导入 SFC 必须带 `.vue` 后缀并使用**默认导入**：
  `import Button from "@/components/ui/Button.vue"`
- 不要写 `import { Button } from "@/components/ui/Button.vue"`，`vue-tsc` 在 `moduleResolution: bundler` 下不会解析命名导出。
- 不要省略 `.vue` 后缀，否则 TS2307。

## 设计系统

- Vercel 白纸风格：白色画布 + 四色径向辉光（#FF0080 / #0070F3 / #50E3C2 / #F5A623），辉光由 `GlowBackground.vue` 提供。
- 玻璃拟态用 `styles.css` 里的 `glass` 工具类；按钮多为 `rounded-full` 的 pill。
- Tailwind 类名必须写成**完整字面量**（JIT 扫描源码），不要拼接字符串。
- 颜色只表达语义时必须同时给文字标签（如 `TaskStatusPill`），不许只用颜色区分状态。

## 数据与状态

- 无 Pinia。会话唯一存储点是 `src/lib/session.ts`（localStorage key：`mindops_auth`）。
- 任务域状态在 `src/lib/task-store.ts`（组合式函数 + 请求态）。
- 所有 HTTP 走 `src/api/client.ts`：baseURL 默认 `http://localhost:8010/api/v1`，可用 `VITE_API_BASE_URL` 覆盖；Bearer token 自动注入，错误归一化为 `ApiError`（含 `isNetworkError`）。
- SSE 在 `src/api/stream.ts`（`EventSource`），订阅必须在 `onCleanup` 中关闭，避免路由切换后泄漏。

## 路由与权限

- `/` 为落地页；已登录访问 `/` 跳 `/tasks`。
- `meta.requiresAuth` 的页面未登录时跳 `/login?redirect=...`。
- `meta.adminOnly`（`/metrics`、`/settings/*`）非 admin 跳 `/alerts?forbidden=1`（`AlertsView` 会提示一次并抹掉参数）。
- 布局壳 `LayoutShell.vue` 用 `<RouterView />` 承载子路由，不要改回 `<slot />`。

## 命令

```bash
# dev（端口 3015 为硬约束）
node node_modules/vite/bin/vite.js --port 3015 --host 0.0.0.0 --strictPort

# 类型检查 / 生产构建（构建前会先跑 vue-tsc）
pnpm typecheck
pnpm build
```

> 目录改名后 pnpm 的 node_modules 绝对软链会失效；此时用 `CI=true pnpm install --no-frozen-lockfile` 重建。

## 代码风格

- 注释用中文，只解释「为什么」和非显然的约束，不复述代码。
- 不引入 Options API；不使用 `v-html`。
- 组件 props / emits 显式声明类型；模板内联回调需标注参数类型（如 `(v: boolean) =>`）。
- 改动页面后至少跑 `pnpm typecheck`；涉及构建配置时再跑 `pnpm build`。
