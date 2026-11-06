<script setup lang="ts">
import { computed, reactive, ref, watch, type Component } from "vue";
import { useRouter } from "vue-router";
import { toast } from "vue-sonner";
import {
  knowledgeApi,
  SOURCE_TYPE_OPTIONS,
  type KnowledgeDocDTO,
  type KnowledgeHitDTO,
  type KnowledgeSourceType,
  type KnowledgeSearchOut,
} from "@/api/knowledge";
import { ApiError } from "@/api/client";
import Button from "@/components/ui/Button.vue";
import {
  BookOpen,
  ChevronRight,
  Database,
  FileText,
  FileUp,
  Loader2,
  RefreshCw,
  Search,
  Upload,
  WifiOff,
} from "lucide-vue-next";
import AppDialog from "@/components/ui/AppDialog.vue";

const SOURCE_STYLE: Record<string, { Icon: Component; label: string; bg: string; text: string }> = {
  runbook: { Icon: BookOpen, label: "运维手册", bg: "bg-accent/10", text: "text-accent" },
  incident: { Icon: FileText, label: "事件回灌", bg: "bg-glow-pink/10", text: "text-glow-pink" },
  upload: { Icon: Database, label: "上传文档", bg: "bg-success/10", text: "text-success" },
  api_doc: { Icon: FileText, label: "接口文档", bg: "bg-secondary", text: "text-muted-foreground" },
  best_practice: { Icon: BookOpen, label: "最佳实践", bg: "bg-glow-orange/10", text: "text-glow-orange" },
};

function sourceStyle(sourceType: string) {
  return SOURCE_STYLE[sourceType] ?? SOURCE_STYLE.upload;
}

/**
 * 上传体积上限，与后端 `knowledge/ingest.py:MAX_CONTENT_BYTES` 保持一致。
 *
 * 修复前前端写的是 10MB，但后端上限是 **1MB**：超过 1MB 的文件前端放行、
 * 后端 422，用户看到的是"上传失败"而不是"文件太大"。
 */
const MAX_UPLOAD_BYTES = 1024 * 1024;

/** 后端按 utf-8 解码正文，二进制（PDF 等）会变成乱码入库，因此只放行纯文本格式。 */
const ACCEPTED_EXTENSIONS = [".md", ".txt"];

const PAGE_SIZE = 20;

const router = useRouter();

const loading = ref(true);
const searchMode = ref(false);
const searchQuery = ref("");
const componentFilter = ref("");
/** 类型筛选：对应后端 knowledge_docs.source_type（runbook=运维手册 / incident=事件回灌 等） */
const typeFilter = ref("");
const typeCounts = ref<Record<string, number>>({});

/**
 * 类型标签页：只渲染有文档的类型（数量来自列表接口的 type_counts），
 * 空类型不出现——五个类型全摆出来而库里只有两种，是纯噪音。
 * 未知类型（词表外但库里存在）兜底用原始 key 展示。
 */
const typeTabs = computed(() => {
  const labelOf = new Map<string, string>(
    SOURCE_TYPE_OPTIONS.map((t) => [t.value as string, t.label]),
  );
  const tabs: { value: string; label: string; count: number }[] = [];
  let sum = 0;
  for (const [value, count] of Object.entries(typeCounts.value)) {
    if (count <= 0) continue;
    sum += count;
    tabs.push({ value, label: labelOf.get(value) ?? value, count });
  }
  return { all: sum, tabs };
});
const docs = ref<KnowledgeDocDTO[]>([]);
const total = ref(0);
const page = ref(1);
const hits = ref<KnowledgeHitDTO[]>([]);
const searchMeta = ref<KnowledgeSearchOut | null>(null);
const error = ref<{ offline: boolean; message: string } | null>(null);

const uploadOpen = ref(false);
const uploading = ref(false);
const dragOver = ref(false);
const uploadFile = ref<File | null>(null);
const uploadMeta = reactive<{
  title: string;
  source_type: KnowledgeSourceType;
  component: string;
}>({
  title: "",
  source_type: "runbook",
  component: "redis",
});
const fileInputRef = ref<HTMLInputElement | null>(null);

// 组件选项统一维护（后端 knowledge_docs.component 为 VARCHAR(64) 无枚举约束，
// 只要求小写字母/数字/下划线/短横线；此处覆盖规则库与两个被监控项目的实际取值）
const COMPONENT_OPTIONS = [
  { value: "redis", label: "Redis" },
  { value: "mysql", label: "MySQL" },
  { value: "nginx", label: "Nginx" },
  { value: "docker", label: "Docker" },
  { value: "k8s", label: "Kubernetes" },
  { value: "host", label: "宿主机" },
  { value: "order-service", label: "订单服务" },
  { value: "order-api", label: "订单 API" },
  { value: "common", label: "通用" },
] as const;

// 类型选项直接复用 api 层导出的 SOURCE_TYPE_OPTIONS（与后端词表一致）

async function loadDocs() {
  loading.value = true;
  error.value = null;
  try {
    const data = await knowledgeApi.list({
      component: componentFilter.value || undefined,
      source_type: typeFilter.value || undefined,
      page: page.value,
      page_size: PAGE_SIZE,
    });
    docs.value = data.items;
    total.value = data.total;
    typeCounts.value = data.type_counts ?? {};
  } catch (err) {
    const offline = err instanceof ApiError && err.isNetworkError;
    error.value = {
      offline,
      message: offline
        ? "无法连接后端服务，请确认 mindops-backend 已在 8010 端口启动。"
        : err instanceof Error
          ? err.message
          : "加载知识库失败",
    };
    docs.value = [];
    total.value = 0;
  } finally {
    loading.value = false;
  }
}

// 检索模式下不走列表接口；退出检索（searchMode 变 false）时重新拉列表
watch(
  [searchMode, componentFilter, typeFilter, page],
  () => {
    if (!searchMode.value) void loadDocs();
  },
  { immediate: true },
);

async function handleSearch() {
  const query = searchQuery.value.trim();
  if (!query) {
    searchMode.value = false;
    hits.value = [];
    searchMeta.value = null;
    return;
  }
  searchMode.value = true;
  loading.value = true;
  error.value = null;
  try {
    const data = await knowledgeApi.search({
      query,
      component: componentFilter.value || undefined,
      top_k: 10,
    });
    hits.value = data.results;
    searchMeta.value = data;
  } catch (err) {
    const offline = err instanceof ApiError && err.isNetworkError;
    error.value = {
      offline,
      message: offline
        ? "无法连接后端服务，请确认 mindops-backend 已在 8010 端口启动。"
        : err instanceof Error
          ? err.message
          : "检索失败",
    };
    hits.value = [];
    searchMeta.value = null;
  } finally {
    loading.value = false;
  }
}

function exitSearch() {
  searchMode.value = false;
  searchQuery.value = "";
  hits.value = [];
  searchMeta.value = null;
}

function handleFileSelect(file: File) {
  const ext = file.name.substring(file.name.lastIndexOf(".")).toLowerCase();
  if (!ACCEPTED_EXTENSIONS.includes(ext)) {
    // 用 toast 而非原生 alert：原生弹窗阻塞渲染线程、样式无法统一，
    // 且与项目其他反馈（sonner）不一致。
    toast.error("仅支持纯文本格式（.md / .txt）", {
      description: `当前文件类型：${ext || "未知"}。PDF 等二进制需先在服务端做文本抽取。`,
    });
    return;
  }
  if (file.size > MAX_UPLOAD_BYTES) {
    toast.error("文件超过 1MB 限制", {
      description: `当前大小：${(file.size / 1024 / 1024).toFixed(2)}MB（后端上限 1MB）`,
    });
    return;
  }
  uploadFile.value = file;
  uploadMeta.title = file.name.slice(0, file.name.length - ext.length);
}

function onFileChange(e: Event) {
  const input = e.target as HTMLInputElement;
  const file = input.files?.[0];
  if (file) handleFileSelect(file);
}

function onDragOver(e: DragEvent) {
  e.preventDefault();
  dragOver.value = true;
}

function onDrop(e: DragEvent) {
  e.preventDefault();
  dragOver.value = false;
  const file = e.dataTransfer?.files[0];
  if (file) handleFileSelect(file);
}

function openFilePicker() {
  fileInputRef.value?.click();
}

function onSourceTypeChange(e: Event) {
  uploadMeta.source_type = (e.target as HTMLSelectElement).value as KnowledgeSourceType;
}

async function handleUpload() {
  const file = uploadFile.value;
  if (!file || !uploadMeta.title.trim()) return;
  uploading.value = true;
  try {
    const result = await knowledgeApi.upload({
      file,
      title: uploadMeta.title.trim(),
      source_type: uploadMeta.source_type,
      component: uploadMeta.component,
    });
    toast.success("上传成功", {
      description: `《${uploadMeta.title}》v${result.version}，${result.chunk_count} 个切片`,
    });
    uploadOpen.value = false;
    uploadFile.value = null;
    uploadMeta.title = "";
    uploadMeta.source_type = "runbook";
    uploadMeta.component = "redis";
    // 回到第 1 页重新拉，保证新文档立刻可见
    if (searchMode.value) exitSearch();
    page.value = 1;
    await loadDocs();
  } catch (err) {
    toast.error("上传失败", {
      description: err instanceof Error ? err.message : "未知错误",
    });
  } finally {
    uploading.value = false;
  }
}

/** 上传中禁止关闭弹窗（与 React 版遮罩 / 关闭按钮的 !uploading 判断一致）。 */
function handleUploadOpenChange(open: boolean) {
  if (!open && uploading.value) return;
  uploadOpen.value = open;
}

function retry() {
  if (searchMode.value) void handleSearch();
  else void loadDocs();
}

function formatTime(iso: string) {
  return new Date(iso).toLocaleDateString("zh-CN");
}

function openDoc(docId: number | null) {
  if (docId === null) return;
  void router.push({ name: "knowledge-detail", params: { id: String(docId) } });
}

const docRows = computed(() =>
  docs.value.map((doc) => ({ doc, config: sourceStyle(doc.source_type) })),
);

const hitRows = computed(() =>
  hits.value.map((hit) => ({
    hit,
    // 命中的 doc_id 指向文档最新版本；同理相似事件路（legacy）可能不带 doc_id，
    // 此时退化为不可点击，避免跳到一个不存在的 id。
    docId: hit.doc_id ?? null,
    config: sourceStyle(hit.doc_type ?? "upload"),
  })),
);

const totalPages = computed(() => Math.max(1, Math.ceil(total.value / PAGE_SIZE)));
const listEmpty = computed(() => !loading.value && docs.value.length === 0);
const hitEmpty = computed(() => !loading.value && hits.value.length === 0);
</script>

<template>
  <div class="space-y-4">
    <!-- 筛选 + 搜索 -->
    <div class="glass rounded-xl p-4 flex items-center gap-3 flex-wrap">
      <select
        v-model="componentFilter"
        class="px-3 py-2 rounded-lg border border-border bg-card text-[13px] focus:outline-none focus:ring-2 focus:ring-accent/30 cursor-pointer"
        @change="page = 1"
      >
        <option value="">全部组件</option>
        <option v-for="c in COMPONENT_OPTIONS" :key="c.value" :value="c.value">{{ c.label }}</option>
      </select>

      <!-- 类型标签页：带数量、只显示非空类型；检索模式下置灰（语义检索不支持类型参数） -->
      <div
        class="inline-flex items-center gap-1 p-1 rounded-full border border-border bg-card"
        :class="searchMode ? 'opacity-50 pointer-events-none' : ''"
        :title="searchMode ? '检索模式下按相关性返回，类型筛选不生效' : undefined"
      >
        <button
          type="button"
          class="px-3 py-1.5 rounded-full text-[12.5px] transition-colors"
          :class="
            typeFilter === ''
              ? 'bg-primary text-primary-foreground font-medium'
              : 'text-muted-foreground hover:text-foreground'
          "
          @click="
            typeFilter = '';
            page = 1;
          "
        >
          全部 <span class="font-mono tabular-nums">{{ typeTabs.all }}</span>
        </button>
        <button
          v-for="tab in typeTabs.tabs"
          :key="tab.value"
          type="button"
          class="px-3 py-1.5 rounded-full text-[12.5px] transition-colors"
          :class="
            typeFilter === tab.value
              ? 'bg-primary text-primary-foreground font-medium'
              : 'text-muted-foreground hover:text-foreground'
          "
          @click="
            typeFilter = tab.value;
            page = 1;
          "
        >
          {{ tab.label }}
          <span class="font-mono tabular-nums">{{ tab.count }}</span>
        </button>
      </div>

      <div class="relative flex-1 min-w-[200px] max-w-md">
        <Search :size="16" class="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
        <input
          v-model="searchQuery"
          type="text"
          placeholder="检索知识库（BM25 + 向量融合）..."
          class="w-full pl-9 pr-3 py-2 rounded-lg border border-border bg-card text-[13px] focus:outline-none focus:ring-2 focus:ring-accent/30"
          @keydown.enter="handleSearch"
        />
      </div>

      <button
        class="px-3 py-2 rounded-full border border-border bg-card text-[13px] font-medium hover:bg-secondary transition-colors btn-pill"
        @click="handleSearch"
      >
        搜索
      </button>

      <button
        v-if="searchMode"
        class="px-3 py-2 rounded-full border border-border bg-card text-[13px] font-medium hover:bg-secondary transition-colors btn-pill"
        @click="exitSearch"
      >
        返回列表
      </button>

      <button
        class="inline-flex items-center gap-1.5 px-3 py-2 rounded-full bg-primary text-primary-foreground text-[13px] font-medium btn-pill hover:bg-primary/90 transition-colors"
        @click="uploadOpen = true"
      >
        <Upload :size="14" />
        上传文档
      </button>
    </div>

    <!-- 离线 / 错误提示 -->
    <div
      v-if="error"
      class="glass rounded-xl p-4 flex items-start gap-3 border border-glow-pink/30"
    >
      <WifiOff :size="18" class="text-glow-pink shrink-0 mt-0.5" />
      <div class="flex-1 min-w-0">
        <div class="text-[13px] font-medium">
          {{ error.offline ? "后端未连接" : "请求失败" }}
        </div>
        <div class="text-[12px] text-muted-foreground mt-1 break-words">{{ error.message }}</div>
      </div>
      <button
        class="shrink-0 flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-secondary text-[12px] font-medium hover:bg-secondary/70 transition-colors"
        @click="retry"
      >
        <RefreshCw :size="13" />
        重试
      </button>
    </div>

    <!-- 检索元信息：融合方式 / 是否降级 / 耗时，全部来自后端响应 -->
    <div
      v-if="searchMode && searchMeta && !loading"
      class="flex items-center gap-2 text-[11px] text-muted-foreground flex-wrap"
    >
      <span class="px-1.5 py-0.5 rounded bg-secondary/60">融合：{{ searchMeta.fusion_method }}</span>
      <span v-if="searchMeta.degraded" class="px-1.5 py-0.5 rounded bg-warning/15 text-warning">
        已降级：稠密向量路不可用，仅 BM25
      </span>
      <span>命中 {{ hits.length }} 条 · 耗时 {{ searchMeta.latency_ms }}ms</span>
    </div>

    <!-- 结果列表 -->
    <div class="space-y-2">
      <template v-if="loading">
        <div v-for="i in 4" :key="i" class="glass rounded-xl p-4 h-20 animate-pulse"></div>
      </template>

      <template v-else-if="searchMode">
        <div v-if="hitEmpty" class="glass rounded-xl py-16 flex flex-col items-center gap-3">
          <div class="w-12 h-12 rounded-full bg-secondary flex items-center justify-center">
            <Search :size="24" class="text-muted-foreground" />
          </div>
          <div class="text-[15px] font-medium">未检索到相关文档</div>
          <div class="text-[13px] text-muted-foreground text-center max-w-[420px] px-4">
            试试更短的关键词，或切换组件筛选（检索会按切片级 component 过滤）
          </div>
        </div>

        <template v-else>
          <component
            :is="row.docId !== null ? 'button' : 'div'"
            v-for="(row, idx) in hitRows"
            :key="row.hit.chunk_id ?? row.hit.source_doc + '-' + idx"
            :class="
              row.docId !== null
                ? 'w-full text-left glass rounded-xl p-4 hover:shadow-md transition-shadow cursor-pointer focus:outline-none focus:ring-2 focus:ring-accent/30'
                : 'glass rounded-xl p-4'
            "
            @click="openDoc(row.docId)"
          >
            <div class="flex items-start gap-3">
              <div class="w-9 h-9 rounded-lg bg-secondary flex items-center justify-center shrink-0">
                <component :is="row.config.Icon" :size="16" class="text-muted-foreground" />
              </div>
              <div class="flex-1 min-w-0">
                <div class="flex items-center gap-2 mb-1 flex-wrap">
                  <span class="text-[11px] font-mono text-muted-foreground">
                    #{{ row.hit.fusion_rank }}
                  </span>
                  <h3 class="text-[14px] font-medium truncate">{{ row.hit.source_doc }}</h3>
                  <span
                    :class="`inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[11px] font-medium ${row.config.bg} ${row.config.text}`"
                  >
                    {{ row.config.label }}
                  </span>
                </div>
                <p class="text-[13px] text-muted-foreground line-clamp-3 leading-relaxed mb-2">
                  {{ row.hit.text }}
                </p>
                <div class="flex items-center gap-3 text-[11px] text-muted-foreground flex-wrap">
                  <span class="font-mono uppercase">{{ row.hit.component }}</span>
                  <span>·</span>
                  <span class="font-mono">
                    score:
                    <span
                      v-if="row.hit.score === null"
                      title="相似事件路（Jaccard）分值与 RRF 不同量纲，后端置空"
                    >—</span>
                    <span v-else>{{ row.hit.score.toFixed(4) }}</span>
                  </span>
                  <template v-if="row.hit.global_scope">
                    <span>·</span>
                    <span>全局范围</span>
                  </template>
                </div>
              </div>
              <ChevronRight
                v-if="row.docId !== null"
                :size="16"
                class="text-muted-foreground shrink-0 mt-0.5"
              />
            </div>
          </component>
        </template>
      </template>

      <div v-else-if="listEmpty" class="glass rounded-xl py-16 flex flex-col items-center gap-3">
        <div class="w-12 h-12 rounded-full bg-secondary flex items-center justify-center">
          <Search :size="24" class="text-muted-foreground" />
        </div>
        <div class="text-[15px] font-medium">知识库为空</div>
        <div class="text-[13px] text-muted-foreground text-center max-w-[420px] px-4">
          上传运维手册或事件回灌后，文档会出现在这里
        </div>
      </div>

      <template v-else>
        <button
          v-for="row in docRows"
          :key="row.doc.doc_id"
          class="w-full text-left glass rounded-xl p-4 hover:shadow-md transition-shadow cursor-pointer focus:outline-none focus:ring-2 focus:ring-accent/30"
          @click="openDoc(row.doc.doc_id)"
        >
          <div class="flex items-start gap-3">
            <div class="w-9 h-9 rounded-lg bg-secondary flex items-center justify-center shrink-0">
              <component :is="row.config.Icon" :size="16" class="text-muted-foreground" />
            </div>
            <div class="flex-1 min-w-0">
              <div class="flex items-center gap-2 mb-1 flex-wrap">
                <h3 class="text-[14px] font-medium truncate">{{ row.doc.title }}</h3>
                <span
                  :class="`inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full text-[11px] font-medium ${row.config.bg} ${row.config.text}`"
                >
                  {{ row.config.label }}
                </span>
                <span class="text-[11px] font-mono text-muted-foreground">v{{ row.doc.version }}</span>
                <span
                  v-if="!row.doc.is_latest"
                  class="text-[11px] px-1.5 py-0.5 rounded bg-secondary text-muted-foreground"
                >
                  历史版本
                </span>
              </div>
              <!-- 列表接口不返回正文，这里只呈现真实存在的元数据，不伪造摘要 -->
              <div class="flex items-center gap-3 text-[11px] text-muted-foreground flex-wrap">
                <span class="font-mono uppercase">{{ row.doc.component }}</span>
                <span>·</span>
                <span>{{ row.doc.chunk_count }} 个切片</span>
                <span>·</span>
                <span>{{ row.doc.created_by }}</span>
                <span>·</span>
                <span>{{ formatTime(row.doc.created_at) }}</span>
              </div>
            </div>
            <ChevronRight :size="16" class="text-muted-foreground shrink-0 mt-0.5" />
          </div>
        </button>
      </template>
    </div>

    <!-- 列表分页（服务端分页；检索模式不分页） -->
    <div
      v-if="!loading && !searchMode && docs.length > 0"
      class="glass rounded-xl px-4 py-3 flex items-center justify-between"
    >
      <span class="text-[12px] text-muted-foreground">
        共 {{ total }} 份文档，第 {{ page }} / {{ totalPages }} 页
      </span>
      <div class="flex gap-1">
        <button
          :disabled="page <= 1"
          class="px-3 py-1.5 rounded-lg border border-border bg-card text-[12px] disabled:opacity-40 hover:bg-secondary transition-colors"
          @click="page = Math.max(1, page - 1)"
        >
          上一页
        </button>
        <button
          :disabled="page >= totalPages"
          class="px-3 py-1.5 rounded-lg border border-border bg-card text-[12px] disabled:opacity-40 hover:bg-secondary transition-colors"
          @click="page = page + 1"
        >
          下一页
        </button>
      </div>
    </div>

    <!-- 上传弹窗：走 AppDialog 默认白底样式，不叠玻璃拟态 -->
    <AppDialog
      :open="uploadOpen"
      title="上传文档"
      description="上传运维手册或文档，入库后参与 BM25 + 向量混合检索"
      max-width="480px"
      @update:open="handleUploadOpenChange"
    >
      <div class="space-y-4">
        <!-- 拖拽区：静默浅灰底，悬停/拖入时点亮 -->
        <div
          class="rounded-xl border border-dashed p-6 text-center cursor-pointer transition-colors"
          :class="
            dragOver
              ? 'border-accent bg-accent/5'
              : 'border-border bg-secondary/40 hover:border-accent/50 hover:bg-accent/5'
          "
          @dragover="onDragOver"
          @dragleave="dragOver = false"
          @drop="onDrop"
          @click="openFilePicker"
        >
          <div v-if="uploadFile" class="flex items-center justify-center gap-2">
            <span class="inline-flex w-8 h-8 rounded-full bg-success/10 items-center justify-center">
              <FileUp :size="15" class="text-success" />
            </span>
            <span class="text-[13px] font-medium truncate max-w-[220px]">{{ uploadFile.name }}</span>
            <span class="text-[12px] text-muted-foreground font-mono tabular-nums"
              >{{ (uploadFile.size / 1024).toFixed(0) }}KB</span
            >
            <span class="text-[11px] text-muted-foreground/70">点击可重选</span>
          </div>
          <div v-else class="space-y-2">
            <span
              class="mx-auto inline-flex w-10 h-10 rounded-full bg-background border border-border items-center justify-center"
            >
              <FileUp :size="18" class="text-muted-foreground" />
            </span>
            <p class="text-[13px] font-medium">拖拽文件到此处，或点击选择</p>
            <p class="text-[11px] text-muted-foreground">
              支持 .md / .txt，单文件 1MB 以内
            </p>
          </div>
          <input
            ref="fileInputRef"
            type="file"
            :accept="ACCEPTED_EXTENSIONS.join(',')"
            class="hidden"
            @change="onFileChange"
          />
        </div>

        <!-- 表单 -->
        <div class="space-y-3">
          <div>
            <label class="block text-[12.5px] font-medium mb-1.5">标题</label>
            <input
              v-model="uploadMeta.title"
              type="text"
              placeholder="请输入文档标题"
              class="w-full px-3 py-2 rounded-lg border border-border bg-card text-[13px] focus:outline-none focus:ring-2 focus:ring-accent/30"
            />
          </div>
          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="block text-[12.5px] font-medium mb-1.5">来源类型</label>
              <select
                :value="uploadMeta.source_type"
                class="w-full px-3 py-2 rounded-lg border border-border bg-card text-[13px] focus:outline-none focus:ring-2 focus:ring-accent/30 cursor-pointer"
                @change="onSourceTypeChange"
              >
                <option v-for="opt in SOURCE_TYPE_OPTIONS" :key="opt.value" :value="opt.value">
                  {{ opt.label }}
                </option>
              </select>
            </div>
            <div>
              <label class="block text-[12.5px] font-medium mb-1.5">所属组件</label>
              <select
                v-model="uploadMeta.component"
                class="w-full px-3 py-2 rounded-lg border border-border bg-card text-[13px] focus:outline-none focus:ring-2 focus:ring-accent/30 cursor-pointer"
              >
                <option v-for="c in COMPONENT_OPTIONS" :key="c.value" :value="c.value">{{ c.label }}</option>
              </select>
            </div>
          </div>
        </div>
      </div>

      <template #footer>
        <Button variant="outline" class="rounded-full" :disabled="uploading" @click="uploadOpen = false">
          取消
        </Button>
        <Button
          class="rounded-full bg-primary text-primary-foreground hover:bg-primary/90"
          :disabled="!uploadFile || !uploadMeta.title.trim() || uploading"
          @click="handleUpload"
        >
          <Loader2 v-if="uploading" :size="14" class="animate-spin mr-1.5" />
          <Upload v-else :size="14" class="mr-1.5" />
          {{ uploading ? "上传中..." : "确认上传" }}
        </Button>
      </template>
    </AppDialog>
  </div>
</template>
