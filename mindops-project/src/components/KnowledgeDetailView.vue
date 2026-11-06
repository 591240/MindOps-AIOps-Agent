<script setup lang="ts">
import { computed, onBeforeUnmount, ref, watch, type Component } from "vue";
import { RouterLink, useRouter } from "vue-router";
import { toast } from "vue-sonner";
import {
  knowledgeApi,
  type KnowledgeChunkDTO,
  type KnowledgeDetailOut,
} from "@/api/knowledge";
import { ApiError } from "@/api/client";
import {
  ArrowLeft,
  BookOpen,
  Boxes,
  Check,
  ChevronRight,
  Clock,
  Copy,
  Database,
  FileQuestion,
  FileText,
  Hash,
  History,
  Layers,
  RefreshCw,
  User,
  WifiOff,
} from "lucide-vue-next";

/** 与 KnowledgeView 的 sourceStyle 保持一致（列表与详情页标签不能对不上）。 */
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

function formatTime(iso: string) {
  return new Date(iso).toLocaleString("zh-CN", {
    year: "numeric",
    month: "2-digit",
    day: "2-digit",
    hour: "2-digit",
    minute: "2-digit",
  });
}

/**
 * 知识库文档详情 /knowledge/{id}（F-05 / F-07 消费端）。
 *
 * 设计口径（与 IncidentDetailView 一致）：
 * - 命中后端 → 展示真实详情；
 * - 后端 404 → 明确"文档不存在"，**不回退到任何假数据**（id 本身就错了，编内容只会骗人）；
 * - 后端不可达 → 显示离线态并给重试按钮，**不伪造正文**。
 *
 * 版本的显示规则：详情页按传入 id 取该版本自己的切片，因此翻到旧版本时
 * 正文与切片一起回到旧版，不会出现"版本号 v1、正文是 v2"的错位。
 */
const props = defineProps<{ id: string }>();

const router = useRouter();

const loading = ref(true);
const doc = ref<KnowledgeDetailOut | null>(null);
const state = ref<"ok" | "not_found" | "offline" | "error">("ok");
const errorMsg = ref("");
const copied = ref(false);
/** 长切片折叠状态：按 chunk_id 记录，加载新版本时整体重置（对应 React 子组件重新挂载）。 */
const openChunks = ref<Record<number, boolean>>({});
let copyTimer: number | null = null;

async function load(rawId: string | undefined) {
  loading.value = true;
  errorMsg.value = "";
  openChunks.value = {};
  if (!rawId) {
    state.value = "not_found";
    doc.value = null;
    loading.value = false;
    return;
  }
  try {
    const data = await knowledgeApi.detail(rawId);
    doc.value = data;
    state.value = "ok";
  } catch (err) {
    if (err instanceof ApiError && err.isNetworkError) {
      state.value = "offline";
      doc.value = null;
      errorMsg.value = "无法连接后端服务，请确认 mindops-backend 已在 8010 端口启动。";
    } else if (err instanceof ApiError && err.status === 404) {
      state.value = "not_found";
      doc.value = null;
    } else {
      state.value = "error";
      doc.value = null;
      errorMsg.value = err instanceof Error ? err.message : "加载文档详情失败";
    }
  } finally {
    loading.value = false;
  }
}

watch(() => props.id, (id) => void load(id), { immediate: true });

onBeforeUnmount(() => {
  if (copyTimer !== null) window.clearTimeout(copyTimer);
});

async function copyContent() {
  if (!doc.value) return;
  try {
    await navigator.clipboard.writeText(doc.value.content);
    copied.value = true;
    toast.success("已复制正文到剪贴板");
    if (copyTimer !== null) window.clearTimeout(copyTimer);
    copyTimer = window.setTimeout(() => (copied.value = false), 2000);
  } catch {
    toast.error("复制失败", { description: "当前浏览器可能未授予剪贴板权限" });
  }
}

function goBack() {
  void router.push("/knowledge");
}

function openVersion(docId: number) {
  void router.push({ name: "knowledge-detail", params: { id: String(docId) } });
}

function retryLoad() {
  void load(props.id);
}

function formatEmbeddingId(embeddingId: string | null) {
  return embeddingId ? `向量 id：${embeddingId}` : "";
}

// 长切片默认折叠：详情页首屏要能一眼看到整体结构，而不是被单个长表格淹没
const LONG_THRESHOLD = 240;

function isCollapsible(chunk: KnowledgeChunkDTO) {
  return chunk.text.length > LONG_THRESHOLD;
}

function isChunkOpen(chunkId: number) {
  return openChunks.value[chunkId] === true;
}

function toggleChunk(chunkId: number) {
  openChunks.value = { ...openChunks.value, [chunkId]: !openChunks.value[chunkId] };
}

const config = computed(() => sourceStyle(doc.value?.source_type ?? "upload"));

/** 元信息网格：全部来自后端真实字段 */
const metaItems = computed(() => {
  const d = doc.value;
  if (!d) return [];
  return [
    { icon: Boxes, label: "所属组件", value: d.component.toUpperCase(), mono: true },
    { icon: Layers, label: "切片数", value: `${d.chunk_count} 个`, mono: false },
    { icon: User, label: "创建人", value: d.created_by, mono: false },
    { icon: Clock, label: "创建时间", value: formatTime(d.created_at), mono: false },
  ];
});

const fallback = computed<{ icon: Component; title: string; hint: string; tone: string }>(() => {
  if (state.value === "not_found") {
    return {
      icon: FileQuestion,
      title: "文档不存在",
      hint: "该文档可能已被删除，或链接中的 id 有误。",
      tone: "text-muted-foreground",
    };
  }
  if (state.value === "offline") {
    return { icon: WifiOff, title: "后端未连接", hint: errorMsg.value, tone: "text-glow-pink" };
  }
  return { icon: FileQuestion, title: "请求失败", hint: errorMsg.value, tone: "text-glow-pink" };
});
</script>

<template>
  <!-- ---- 加载骨架：避免白屏闪烁 ---- -->
  <div v-if="loading" class="space-y-4">
    <div class="glass rounded-xl p-4 h-16 animate-pulse"></div>
    <div class="glass rounded-xl p-6 h-64 animate-pulse"></div>
    <div class="glass rounded-xl p-6 h-40 animate-pulse"></div>
  </div>

  <!-- ---- 非正常态：统一渲染一个明确的空态，并提供返回/重试 ---- -->
  <div v-else-if="!(doc && state === 'ok')" class="space-y-4">
    <button
      class="inline-flex items-center gap-1.5 text-[13px] text-muted-foreground hover:text-foreground transition-colors"
      @click="goBack"
    >
      <ArrowLeft :size="14" />
      返回知识库
    </button>
    <div class="glass rounded-xl py-16 flex flex-col items-center gap-3">
      <div class="w-12 h-12 rounded-full bg-secondary flex items-center justify-center">
        <component :is="fallback.icon" :size="24" :class="fallback.tone" />
      </div>
      <div class="text-[15px] font-medium">{{ fallback.title }}</div>
      <div class="text-[13px] text-muted-foreground text-center max-w-[420px] px-4 break-words">
        {{ fallback.hint }}
      </div>
      <button
        v-if="state === 'offline' || state === 'error'"
        class="mt-1 inline-flex items-center gap-1.5 px-3 py-1.5 rounded-lg bg-secondary text-[12px] font-medium hover:bg-secondary/70 transition-colors"
        @click="retryLoad"
      >
        <RefreshCw :size="13" />
        重试
      </button>
    </div>
  </div>

  <div v-else class="space-y-4">
    <button
      class="inline-flex items-center gap-1.5 text-[13px] text-muted-foreground hover:text-foreground transition-colors"
      @click="goBack"
    >
      <ArrowLeft :size="14" />
      返回知识库
    </button>

    <!-- ---- 头部：标题与元信息 ---- -->
    <div class="glass rounded-xl p-5">
      <div class="flex items-start gap-3">
        <div :class="`w-10 h-10 rounded-lg ${config.bg} flex items-center justify-center shrink-0`">
          <component :is="config.Icon" :size="18" :class="config.text" />
        </div>
        <div class="flex-1 min-w-0">
          <div class="flex items-center gap-2 flex-wrap">
            <h1 class="text-[18px] font-semibold break-words">{{ doc.title }}</h1>
            <span
              :class="`inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[11px] font-medium ${config.bg} ${config.text}`"
            >
              {{ config.label }}
            </span>
            <span class="text-[11px] font-mono text-muted-foreground">v{{ doc.version }}</span>
            <span
              v-if="doc.is_latest"
              class="text-[11px] px-1.5 py-0.5 rounded-full bg-success/10 text-success"
            >
              最新版
            </span>
            <span
              v-else
              class="text-[11px] px-1.5 py-0.5 rounded-full bg-secondary text-muted-foreground"
            >
              历史版本
            </span>
          </div>

          <div class="grid grid-cols-2 sm:grid-cols-4 gap-x-4 gap-y-2 mt-3">
            <div v-for="item in metaItems" :key="item.label" class="min-w-0">
              <div class="flex items-center gap-1 text-[11px] text-muted-foreground mb-0.5">
                <component :is="item.icon" :size="11" />
                {{ item.label }}
              </div>
              <div
                :class="['text-[13px] truncate', item.mono ? 'font-mono uppercase' : '']"
                :title="item.value"
              >
                {{ item.value }}
              </div>
            </div>
          </div>

          <div
            v-if="doc.content_hash"
            class="flex items-center gap-1.5 mt-3 text-[11px] text-muted-foreground"
          >
            <Hash :size="11" />
            <span class="font-mono break-all">{{ doc.content_hash }}</span>
            <span class="shrink-0">· 内容指纹（同标题同内容不会重复入库）</span>
          </div>
        </div>

        <button
          title="复制完整正文"
          class="shrink-0 inline-flex items-center gap-1.5 px-3 py-1.5 rounded-full border border-border bg-card text-[12px] font-medium hover:bg-secondary transition-colors btn-pill"
          @click="copyContent"
        >
          <Check v-if="copied" :size="13" class="text-success" />
          <Copy v-else :size="13" />
          {{ copied ? "已复制" : "复制正文" }}
        </button>
      </div>
    </div>

    <!-- ---- 历史版本切换：仅多版本时出现 ---- -->
    <div v-if="doc.versions.length > 1" class="glass rounded-xl p-4">
      <div class="flex items-center gap-1.5 mb-3 text-[13px] font-medium">
        <History :size="14" class="text-muted-foreground" />
        版本历史
        <span class="text-[11px] font-normal text-muted-foreground">
          （共 {{ doc.versions.length }} 版，点击切换查看各版正文）
        </span>
      </div>
      <div class="flex items-center gap-2 flex-wrap">
        <button
          v-for="ver in doc.versions"
          :key="ver.doc_id"
          :class="[
            'inline-flex items-center gap-1.5 px-2.5 py-1.5 rounded-lg border text-[12px] transition-colors',
            ver.doc_id === doc.doc_id
              ? 'border-foreground bg-secondary font-medium'
              : 'border-border bg-card hover:bg-secondary',
          ]"
          @click="openVersion(ver.doc_id)"
        >
          <span class="font-mono">v{{ ver.version }}</span>
          <span v-if="ver.is_latest" class="text-[10px] text-success">最新</span>
          <span class="text-[11px] text-muted-foreground">{{ ver.chunk_count }} 切片</span>
        </button>
      </div>
    </div>

    <!-- ---- 完整正文 ---- -->
    <div class="glass rounded-xl p-5">
      <div class="flex items-center justify-between mb-3">
        <h2 class="text-[14px] font-semibold">完整正文</h2>
        <span class="text-[11px] text-muted-foreground">
          {{ doc.content.length }} 字 · UTF-8 按原文存储
        </span>
      </div>
      <!-- whitespace-pre-wrap 保留原文换行；break-words 防止长 URL/哈希撑破容器 -->
      <div class="rounded-lg border border-border bg-muted/40 p-4 max-h-[420px] overflow-auto">
        <pre class="text-[13px] leading-relaxed whitespace-pre-wrap break-words font-sans text-foreground">{{ doc.content }}</pre>
      </div>
    </div>

    <!-- ---- 切片列表：展示 RAG 检索真正命中的最小单元 ---- -->
    <div class="glass rounded-xl p-5">
      <div class="flex items-center gap-1.5 mb-3">
        <h2 class="text-[14px] font-semibold">切片明细</h2>
        <span class="text-[11px] text-muted-foreground">
          （共 {{ doc.chunks.length }} 个切片，检索即按切片粒度匹配与过滤）
        </span>
      </div>
      <div v-if="doc.chunks.length === 0" class="text-[13px] text-muted-foreground py-6 text-center">
        该版本没有切片记录
      </div>
      <div v-else class="space-y-2">
        <div
          v-for="chunk in doc.chunks"
          :key="chunk.chunk_id"
          class="rounded-lg border border-border bg-card overflow-hidden"
        >
          <div class="flex items-center gap-2 px-3 py-2 bg-muted/40">
            <span class="text-[11px] font-mono text-muted-foreground">#{{ chunk.chunk_index }}</span>
            <span class="text-[11px] px-1.5 py-0.5 rounded bg-secondary text-muted-foreground">
              {{ chunk.chunk_kind === "table" ? "表格" : chunk.chunk_kind }}
            </span>
            <span class="text-[11px] font-mono uppercase text-muted-foreground">
              {{ chunk.component }}
            </span>
            <span class="text-[11px] text-muted-foreground">{{ chunk.char_count }} 字</span>
            <span
              v-if="chunk.embedding_id"
              class="text-[10px] font-mono text-muted-foreground/60 truncate max-w-[160px] ml-auto"
              :title="formatEmbeddingId(chunk.embedding_id)"
            >
              vec:{{ chunk.embedding_id.slice(0, 12) }}
            </span>
          </div>
          <div class="px-3 py-2.5">
            <pre
              :class="[
                'text-[13px] leading-relaxed whitespace-pre-wrap break-words font-sans',
                isCollapsible(chunk) && !isChunkOpen(chunk.chunk_id) ? 'line-clamp-3' : '',
              ]"
            >{{ chunk.text }}</pre>
            <button
              v-if="isCollapsible(chunk)"
              class="mt-1.5 text-[11px] text-accent hover:underline"
              @click="toggleChunk(chunk.chunk_id)"
            >
              {{ isChunkOpen(chunk.chunk_id) ? "收起" : "展开全文" }}
            </button>
          </div>
        </div>
      </div>
    </div>

    <!-- ---- 历史版本提示（当前非最新时给回最新入口） ---- -->
    <div
      v-if="!doc.is_latest && doc.latest_doc_id !== doc.doc_id"
      class="glass rounded-xl p-4 flex items-center justify-between gap-3 border border-glow-orange/30"
    >
      <div class="text-[13px]">
        你正在查看 <span class="font-mono">v{{ doc.version }}</span> 历史版本，最新版为另一条记录。
      </div>
      <RouterLink
        :to="{ name: 'knowledge-detail', params: { id: String(doc.latest_doc_id) } }"
        class="shrink-0 inline-flex items-center gap-1 px-3 py-1.5 rounded-full bg-primary text-primary-foreground text-[12px] font-medium btn-pill hover:bg-primary/90 transition-colors"
      >
        跳到最新版
        <ChevronRight :size="13" />
      </RouterLink>
    </div>
  </div>
</template>
