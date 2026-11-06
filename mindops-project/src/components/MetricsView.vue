<script setup lang="ts">
import { computed, ref, watch } from "vue";
import { metricsApi, type MetricsDashboard, type MetricsRange } from "@/api/metrics";
import { ApiError } from "@/api/client";
import EChart from "@/components/ui/EChart.vue";
import StatCard from "@/components/business/StatCard.vue";
import type { EChartsOption } from "echarts";
import { AlertTriangle, CheckCircle2, Clock, Database, RefreshCw, WifiOff, Inbox } from "lucide-vue-next";

const RANGE_LABELS: Record<MetricsRange, string> = {
  today: "今日",
  "7d": "近 7 天",
  "30d": "近 30 天",
};

const RANGES = Object.keys(RANGE_LABELS) as MetricsRange[];

const range = ref<MetricsRange>("today");
const loading = ref(true);
const error = ref<{ offline: boolean; message: string } | null>(null);
const metrics = ref<MetricsDashboard | null>(null);

async function load(target: MetricsRange) {
  loading.value = true;
  error.value = null;
  try {
    metrics.value = await metricsApi.dashboard(target);
  } catch (err) {
    // 保留上一次成功的数据，避免切换区间失败后图表整块闪空
    const offline = err instanceof ApiError && err.isNetworkError;
    error.value = {
      offline,
      message: offline
        ? "无法连接后端服务，请确认 mindops-backend 已在 8010 端口启动。"
        : err instanceof Error
          ? err.message
          : "加载统计数据失败",
    };
  } finally {
    loading.value = false;
  }
}

watch(range, (r) => void load(r), { immediate: true });

const hasData = computed(() => Boolean(metrics.value?.has_data));
const kpi = computed(() => metrics.value?.kpi);
const bucketUnit = computed(() => (metrics.value?.bucket === "hour" ? "按小时" : "按天"));

const kpiCards = computed(() => [
  {
    label: `${RANGE_LABELS[range.value]}告警量`,
    value: (kpi.value?.today_alerts ?? 0).toLocaleString(),
    icon: AlertTriangle,
    color: "text-glow-pink",
  },
  {
    label: "自动闭环率",
    value: `${kpi.value?.auto_close_rate ?? 0}%`,
    icon: CheckCircle2,
    color: "text-success",
  },
  {
    label: "平均确诊时延",
    value: `${kpi.value?.mttr_min ?? 0}min`,
    icon: Clock,
    color: "text-accent",
  },
  {
    label: "知识命中率",
    value: `${kpi.value?.knowledge_hit_rate ?? 0}%`,
    icon: Database,
    color: "text-glow-orange",
  },
]);

const TOOLTIP_STYLE = {
  borderRadius: 12,
  border: "1px solid #EAEAEA",
  fontSize: 13,
  backgroundColor: "#fff",
  textStyle: { color: "#000", fontSize: 13 },
} as const;

const mttrOption = computed<EChartsOption>(() => ({
  grid: { left: 48, right: 16, top: 16, bottom: 32 },
  tooltip: {
    trigger: "axis",
    ...TOOLTIP_STYLE,
    valueFormatter: (v: unknown) => `${v} min`,
  },
  xAxis: {
    type: "category",
    data: (metrics.value?.mttr_trend ?? []).map((d) => d.date),
    axisLine: { lineStyle: { color: "#EAEAEA" } },
    axisTick: { show: false },
    axisLabel: { fontSize: 12, color: "#888" },
  },
  yAxis: {
    type: "value",
    axisLine: { lineStyle: { color: "#EAEAEA" } },
    splitLine: { lineStyle: { color: "#EAEAEA", type: "dashed" } },
    axisLabel: { fontSize: 12, color: "#888", formatter: "{value} min" },
  },
  series: [
    {
      name: "MTTR",
      type: "line",
      smooth: true,
      data: (metrics.value?.mttr_trend ?? []).map((d) => d.mttr),
      symbolSize: 8,
      lineStyle: { width: 2, color: "#0070F3" },
      itemStyle: { color: "#0070F3" },
    },
  ],
}));

const hitRateOption = computed<EChartsOption>(() => ({
  grid: { left: 48, right: 16, top: 16, bottom: 32 },
  tooltip: {
    trigger: "axis",
    ...TOOLTIP_STYLE,
    valueFormatter: (v: unknown) => `${v}%`,
  },
  xAxis: {
    type: "category",
    data: (metrics.value?.hit_rate_by_component ?? []).map((d) => d.component),
    axisLine: { lineStyle: { color: "#EAEAEA" } },
    axisTick: { show: false },
    axisLabel: { fontSize: 12, color: "#888" },
  },
  yAxis: {
    type: "value",
    min: 0,
    max: 100,
    axisLine: { lineStyle: { color: "#EAEAEA" } },
    splitLine: { lineStyle: { color: "#EAEAEA", type: "dashed" } },
    axisLabel: { fontSize: 12, color: "#888", formatter: "{value}%" },
  },
  series: [
    {
      name: "命中率",
      type: "bar",
      data: (metrics.value?.hit_rate_by_component ?? []).map((d) => d.rate),
      itemStyle: { color: "#50E3C2", borderRadius: [6, 6, 0, 0] },
    },
  ],
}));

const radarOption = computed<EChartsOption>(() => {
  const rows = metrics.value?.agent_performance ?? [];
  const max = Math.max(1, ...rows.map((r) => r.avg_ms));
  return {
    tooltip: {
      ...TOOLTIP_STYLE,
      valueFormatter: (v: unknown) => `${v}ms`,
    },
    radar: {
      indicator: rows.map((r) => ({ name: r.agent, max: Math.ceil(max * 1.1) })),
      axisName: { fontSize: 12, color: "#666" },
      splitLine: { lineStyle: { color: "#EAEAEA" } },
      splitArea: { areaStyle: { color: ["#fff"] } },
      axisLine: { lineStyle: { color: "#EAEAEA" } },
    },
    series: [
      {
        type: "radar",
        data: [
          {
            name: "平均耗时",
            value: rows.map((r) => r.avg_ms),
            lineStyle: { width: 2, color: "#0070F3" },
            itemStyle: { color: "#0070F3" },
            areaStyle: { color: "#0070F3", opacity: 0.15 },
          },
        ],
      },
    ],
  };
});

/** 严重度配色：红/橙/蓝/灰，与告警看板左缘色带完全一致 */
const SEVERITY_SERIES = [
  { key: "P0" as const, label: "P0 致命", color: "#e5484d" },
  { key: "P1" as const, label: "P1 严重", color: "#f5a623" },
  { key: "P2" as const, label: "P2 警告", color: "#0070f3" },
  { key: "P3" as const, label: "P3 提示", color: "#b8bcc4" },
];

const alertTrendOption = computed<EChartsOption>(() => {
  const rows = metrics.value?.alert_trend ?? [];
  return {
    grid: { left: 44, right: 16, top: 36, bottom: 32 },
    tooltip: { trigger: "axis", ...TOOLTIP_STYLE },
    legend: {
      top: 0,
      right: 0,
      itemWidth: 10,
      itemHeight: 10,
      icon: "circle",
      textStyle: { fontSize: 11, color: "#666" },
    },
    xAxis: {
      type: "category",
      data: rows.map((d) => d.date),
      axisLine: { lineStyle: { color: "#EAEAEA" } },
      axisTick: { show: false },
      axisLabel: { fontSize: 11, color: "#888" },
    },
    yAxis: {
      type: "value",
      minInterval: 1,
      axisLine: { show: false },
      splitLine: { lineStyle: { color: "#EAEAEA", type: "dashed" } },
      axisLabel: { fontSize: 11, color: "#888" },
    },
    series: SEVERITY_SERIES.map((s, i) => ({
      name: s.label,
      type: "bar" as const,
      stack: "alerts",
      data: rows.map((d) => d[s.key]),
      itemStyle: {
        color: s.color,
        borderRadius: i === SEVERITY_SERIES.length - 1 ? [4, 4, 0, 0] : 0,
      },
      barMaxWidth: 22,
    })),
  };
});

const confidenceOption = computed<EChartsOption>(() => {
  const rows = metrics.value?.confidence_distribution ?? [];
  return {
    grid: { left: 44, right: 16, top: 24, bottom: 32 },
    tooltip: { trigger: "axis", ...TOOLTIP_STYLE },
    xAxis: {
      type: "category",
      data: rows.map((d) => d.bucket),
      axisLine: { lineStyle: { color: "#EAEAEA" } },
      axisTick: { show: false },
      axisLabel: { fontSize: 11, color: "#888" },
    },
    yAxis: {
      type: "value",
      minInterval: 1,
      axisLine: { show: false },
      splitLine: { lineStyle: { color: "#EAEAEA", type: "dashed" } },
      axisLabel: { fontSize: 11, color: "#888" },
    },
    series: [
      {
        name: "事件数",
        type: "bar",
        data: rows.map((d, i) => ({
          value: d.count,
          // 0.6 是回环分水岭：低于它的事件会被打回重诊
          itemStyle: { color: i < 3 ? "#f5a623" : "#0070f3", borderRadius: [6, 6, 0, 0] },
        })),
        barMaxWidth: 40,
        markLine: {
          silent: true,
          symbol: "none",
          lineStyle: { color: "#e5484d", type: "dashed", width: 1 },
          label: { formatter: "阈值 0.6", fontSize: 10, color: "#e5484d" },
          data: [{ xAxis: 2.5 }],
        },
      },
    ],
  };
});

const componentTopOption = computed<EChartsOption>(() => {
  // ECharts 横向条形图：y 轴类目默认自下而上，倒序后最吵的组件排在最上面
  const rows = [...(metrics.value?.component_top ?? [])].reverse();
  return {
    grid: { left: 88, right: 32, top: 12, bottom: 24 },
    tooltip: { trigger: "axis", ...TOOLTIP_STYLE },
    xAxis: {
      type: "value",
      minInterval: 1,
      axisLine: { show: false },
      splitLine: { lineStyle: { color: "#EAEAEA", type: "dashed" } },
      axisLabel: { fontSize: 11, color: "#888" },
    },
    yAxis: {
      type: "category",
      data: rows.map((d) => d.component),
      axisLine: { show: false },
      axisTick: { show: false },
      axisLabel: { fontSize: 11, color: "#666" },
    },
    series: [
      {
        name: "告警数",
        type: "bar",
        data: rows.map((d) => d.count),
        itemStyle: { color: "#0070f3", borderRadius: [0, 4, 4, 0] },
        barMaxWidth: 16,
        label: { show: true, position: "right", fontSize: 11, color: "#666" },
      },
    ],
  };
});
</script>

<template>
  <div class="space-y-4">
    <!-- 时间范围切换 -->
    <div class="flex items-center justify-between flex-wrap gap-3">
      <h2 class="text-[20px] font-semibold heading-tight">统计大盘</h2>
      <div class="flex gap-1 p-1 rounded-full bg-secondary/60">
        <button
          v-for="r in RANGES"
          :key="r"
          @click="range = r"
          class="px-3 py-1.5 rounded-full text-[12px] font-medium transition-all"
          :class="range === r ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground'"
        >
          {{ RANGE_LABELS[r] }}
        </button>
      </div>
    </div>

    <!-- 数据来源 / 离线提示 -->
    <div
      v-if="error"
      class="glass rounded-xl p-4 flex items-start gap-3 border border-glow-pink/30"
    >
      <WifiOff :size="18" class="text-glow-pink shrink-0 mt-0.5" />
      <div class="flex-1 min-w-0">
        <div class="text-[13px] font-medium">
          {{ error.offline ? "后端未连接" : "统计数据加载失败" }}
        </div>
        <div class="text-[12px] text-muted-foreground mt-1 break-words">{{ error.message }}</div>
      </div>
      <button
        @click="load(range)"
        class="shrink-0 flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-secondary text-[12px] font-medium hover:bg-secondary/70 transition-colors"
      >
        <RefreshCw :size="13" />
        重试
      </button>
    </div>
    <div v-else class="flex items-center gap-2 text-[11px] text-muted-foreground flex-wrap">
      <span
        class="px-1.5 py-0.5 rounded"
        :class="hasData ? 'bg-success/15 text-success' : 'bg-secondary/60'"
      >
        {{ hasData ? "真实数据" : "暂无数据" }}
      </span>
      <span v-if="metrics">
        区间自 {{ new Date(metrics.since).toLocaleString() }} 起 · 更新于
        {{ new Date(metrics.generated_at).toLocaleTimeString() }}
      </span>
    </div>

    <!-- KPI 卡片 -->
    <div class="grid grid-cols-2 lg:grid-cols-4 gap-3">
      <StatCard
        v-for="item in kpiCards"
        :key="item.label"
        :label="item.label"
        :value="loading && !metrics ? '—' : item.value"
        :icon="item.icon"
        :tone="item.color"
        :loading="loading && !metrics"
      />
    </div>

    <!-- 图表区 -->
    <div class="grid lg:grid-cols-2 gap-4">
      <!-- 告警趋势（按严重度堆叠） -->
      <div class="glass rounded-xl p-5 lg:col-span-2">
        <div class="flex items-center justify-between mb-3">
          <h3 class="text-[15px] font-semibold">告警趋势</h3>
          <span class="text-[12px] text-muted-foreground">{{ bucketUnit }} · 按严重度堆叠</span>
        </div>
        <div v-if="loading && !metrics" class="h-[220px] bg-secondary/40 rounded-lg animate-pulse" />
        <div
          v-else-if="!metrics?.alert_trend.some((d) => d.P0 + d.P1 + d.P2 + d.P3 > 0)"
          class="flex flex-col items-center justify-center gap-2 rounded-lg bg-secondary/30"
          style="height: 220px"
        >
          <Inbox :size="22" class="text-muted-foreground/50" />
          <div class="text-[12px] text-muted-foreground">当前区间没有告警</div>
        </div>
        <EChart v-else :option="alertTrendOption" height="220px" />
      </div>

      <!-- 置信度分布 -->
      <div class="glass rounded-xl p-5">
        <div class="flex items-center justify-between mb-3">
          <h3 class="text-[15px] font-semibold">根因置信度分布</h3>
          <span class="text-[12px] text-muted-foreground">橙色 = 触发回环</span>
        </div>
        <div v-if="loading && !metrics" class="h-[220px] bg-secondary/40 rounded-lg animate-pulse" />
        <div
          v-else-if="!metrics?.confidence_distribution.some((d) => d.count > 0)"
          class="flex flex-col items-center justify-center gap-2 rounded-lg bg-secondary/30"
          style="height: 220px"
        >
          <Inbox :size="22" class="text-muted-foreground/50" />
          <div class="text-[12px] text-muted-foreground">暂无带置信度的事件</div>
        </div>
        <EChart v-else :option="confidenceOption" height="220px" />
      </div>

      <!-- 组件告警 Top -->
      <div class="glass rounded-xl p-5">
        <div class="flex items-center justify-between mb-3">
          <h3 class="text-[15px] font-semibold">组件告警 Top</h3>
          <span class="text-[12px] text-muted-foreground">哪个组件最吵</span>
        </div>
        <div v-if="loading && !metrics" class="h-[220px] bg-secondary/40 rounded-lg animate-pulse" />
        <div
          v-else-if="!metrics?.component_top.length"
          class="flex flex-col items-center justify-center gap-2 rounded-lg bg-secondary/30"
          style="height: 220px"
        >
          <Inbox :size="22" class="text-muted-foreground/50" />
          <div class="text-[12px] text-muted-foreground">当前区间没有告警</div>
        </div>
        <EChart v-else :option="componentTopOption" height="220px" />
      </div>

      <!-- MTTR 趋势 -->
      <div class="glass rounded-xl p-5">
        <div class="flex items-center justify-between mb-4">
          <h3 class="text-[15px] font-semibold">MTTR 趋势</h3>
          <span class="text-[12px] text-muted-foreground">{{ bucketUnit }}</span>
        </div>
        <div v-if="loading && !metrics" class="h-[240px] bg-secondary/40 rounded-lg animate-pulse" />
        <div
          v-else-if="!hasData"
          class="flex flex-col items-center justify-center gap-2 rounded-lg bg-secondary/30"
          style="height: 240px"
        >
          <Inbox :size="22" class="text-muted-foreground/50" />
          <div class="text-[12px] text-muted-foreground">当前区间暂无数据</div>
        </div>
        <EChart v-else :option="mttrOption" height="240px" />
      </div>

      <!-- 知识命中率 -->
      <div class="glass rounded-xl p-5">
        <div class="flex items-center justify-between mb-4">
          <h3 class="text-[15px] font-semibold">知识命中率</h3>
          <span class="text-[12px] text-muted-foreground">按组件</span>
        </div>
        <div v-if="loading && !metrics" class="h-[240px] bg-secondary/40 rounded-lg animate-pulse" />
        <div
          v-else-if="!metrics?.hit_rate_by_component.length"
          class="flex flex-col items-center justify-center gap-2 rounded-lg bg-secondary/30"
          style="height: 240px"
        >
          <Inbox :size="22" class="text-muted-foreground/50" />
          <div class="text-[12px] text-muted-foreground">当前区间暂无数据</div>
        </div>
        <EChart v-else :option="hitRateOption" height="240px" />
      </div>

      <!-- Agent 性能雷达图 -->
      <div class="glass rounded-xl p-5 lg:col-span-2">
        <div class="flex items-center justify-between mb-4">
          <h3 class="text-[15px] font-semibold">Agent 性能</h3>
          <span class="text-[12px] text-muted-foreground">平均耗时（ms）</span>
        </div>
        <div v-if="loading && !metrics" class="h-[280px] bg-secondary/40 rounded-lg animate-pulse" />
        <div
          v-else-if="!metrics?.agent_performance.length"
          class="flex flex-col items-center justify-center gap-2 rounded-lg bg-secondary/30"
          style="height: 280px"
        >
          <Inbox :size="22" class="text-muted-foreground/50" />
          <div class="text-[12px] text-muted-foreground">当前区间暂无数据</div>
        </div>
        <EChart v-else :option="radarOption" height="280px" />
      </div>
    </div>
  </div>
</template>
