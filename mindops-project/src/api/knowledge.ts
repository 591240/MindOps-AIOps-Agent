/** F-05 / F-07 知识库接口（对齐 app/routers/knowledge.py）。
 *
 * 两个容易踩的契约点（已在类型注释里写死，别再想当然）：
 * 1. **列表不返回正文**：`GET /knowledge` 只有元数据（title / version / chunk_count …），
 *    没有 `preview`。想展示正文必须走检索接口。
 * 2. **检索命中的 `score` 可能为 null**：legacy（相似事件）路的 Jaccard 分量纲与
 *    RRF 不同，后端显式置 null 并跳过 sim_floor 过滤。前端必须容忍 null。
 */

import { api, API_BASE_URL, getToken } from "./client";

/** 后端 VALID_SOURCE_TYPES：比前端原先的三值多了 api_doc / best_practice */
export type KnowledgeSourceType = "runbook" | "incident" | "upload" | "api_doc" | "best_practice";

export const SOURCE_TYPE_OPTIONS: { value: KnowledgeSourceType; label: string }[] = [
  { value: "runbook", label: "运维手册" },
  { value: "incident", label: "事件回灌" },
  { value: "upload", label: "上传文档" },
  { value: "api_doc", label: "接口文档" },
  { value: "best_practice", label: "最佳实践" },
];

export interface KnowledgeDocDTO {
  doc_id: number;
  title: string;
  source_type: string;
  component: string;
  version: number;
  chunk_count: number;
  is_latest: boolean;
  created_by: string;
  created_at: string;
}

export interface KnowledgeListOut {
  total: number;
  items: KnowledgeDocDTO[];
  /** 各 source_type 的文档数（与列表同条件但不吃 source_type 筛选影响），驱动类型标签页 */
  type_counts?: Record<string, number>;
}

export interface KnowledgeHitDTO {
  chunk_id: number | null;
  /** 命中切片所属文档的 id（最新版本）。相似事件路（legacy）可能为 null，此时不可跳详情 */
  doc_id?: number | null;
  text: string;
  source_doc: string;
  component: string;
  doc_type?: string;
  /** RRF 归一化分；legacy 路为 null */
  score: number | null;
  global_scope?: boolean;
  fusion_rank: number;
}

export interface KnowledgeSearchOut {
  results: KnowledgeHitDTO[];
  fusion_method: string;
  /** true = 稠密路缺失（未配 embedding / Milvus 不可用），当前只有 BM25 */
  degraded: boolean;
  latency_ms: number;
}

export interface KnowledgeUploadOut {
  doc_id: number;
  chunk_count: number;
  version: number;
  ingested: boolean;
}

/** 详情页的单个切片（GET /knowledge/{doc_id}）。 */
export interface KnowledgeChunkDTO {
  chunk_id: number;
  chunk_index: number;
  /** text = 普通正文段；table/code 等由切分器标注，前端按 kind 决定渲染容器 */
  chunk_kind: string;
  component: string;
  text: string;
  embedding_id: string | null;
  char_count: number;
}

/** 同标题的版本链条目；`doc_id` 指向该版本自身，可据此切换查看旧版正文。 */
export interface KnowledgeVersionDTO {
  doc_id: number;
  version: number;
  is_latest: boolean;
  chunk_count: number;
  created_at: string;
}

export interface KnowledgeDetailOut {
  doc_id: number;
  title: string;
  content: string;
  source_type: string;
  component: string;
  version: number;
  chunk_count: number;
  is_latest: boolean;
  created_by: string;
  created_at: string;
  updated_at: string;
  content_hash: string | null;
  /** 同标题下的最新版 id；历史版本详情页据此提供"跳到最新"入口 */
  latest_doc_id: number;
  chunks: KnowledgeChunkDTO[];
  versions: KnowledgeVersionDTO[];
}

export interface KnowledgeListQuery {
  q?: string;
  component?: string;
  source_type?: string;
  page?: number;
  page_size?: number;
}

export const knowledgeApi = {
  list(params: KnowledgeListQuery = {}) {
    const search = new URLSearchParams();
    for (const [key, value] of Object.entries(params)) {
      if (value === undefined || value === null || value === "") continue;
      search.set(key, String(value));
    }
    const qs = search.toString();
    return api.get<KnowledgeListOut>(`/knowledge${qs ? `?${qs}` : ""}`);
  },

  search(body: { query: string; component?: string; top_k?: number }) {
    return api.post<KnowledgeSearchOut>("/knowledge/search", body);
  },

  /**
   * 单文档详情（含完整正文、切片、版本链）。
   *
   * `docId` 允许传**历史版本**的 id：详情页要能翻旧版本，后端也是按传入 id
   * 取该版本自己的切片，不会错位成最新版正文。
   */
  detail(docId: number | string) {
    return api.get<KnowledgeDetailOut>(`/knowledge/${docId}`);
  },

  /**
   * 上传走 multipart（后端优先解析 form；JSON 分支要求显式 title+content）。
   * 注意后端 `MAX_CONTENT_BYTES = 1MB` 且按 utf-8 解码 —— **不支持 PDF 等二进制**，
   * 传二进制只会被 `decode("utf-8", "replace")` 变成乱码再入库。
   */
  async upload(args: {
    file: File;
    title: string;
    source_type: KnowledgeSourceType;
    component: string;
  }): Promise<KnowledgeUploadOut> {
    const form = new FormData();
    form.append("file", args.file, args.file.name);
    form.append("source_type", args.source_type);
    form.append("component", args.component);
    if (args.title) form.append("title", args.title);

    const headers: Record<string, string> = {};
    const token = getToken();
    if (token) headers.Authorization = `Bearer ${token}`;

    // 不能用 api.post：它固定 Content-Type: application/json，会破坏 multipart 边界
    const resp = await fetch(`${API_BASE_URL}/knowledge/upload`, {
      method: "POST",
      headers,
      body: form,
    });

    const text = await resp.text();
    let parsed: unknown = null;
    if (text) {
      try {
        parsed = JSON.parse(text);
      } catch {
        parsed = null;
      }
    }
    if (!resp.ok) {
      const detail = (parsed as { detail?: unknown } | null)?.detail;
      const message =
        typeof detail === "string"
          ? detail
          : detail && typeof detail === "object" && "message" in detail
            ? String((detail as { message: unknown }).message)
            : `上传失败（HTTP ${resp.status}）`;
      throw new Error(message);
    }
    return parsed as KnowledgeUploadOut;
  },
};
