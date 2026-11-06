/**
 * SSE 实时流客户端（对齐 app/routers/stream.py）。
 *
 * 为什么单独成文件：EventSource 是**浏览器原生**的长连接对象，
 * 和 `client.ts` 里那套基于 fetch 的请求/响应模型不是一回事
 * ——没有 method、没有 body、断线靠浏览器自动重连，硬塞进 api 客户端
 * 只会让两边都为对方让步。
 *
 * 三个必须知道的实现细节：
 *
 * 1. **鉴权走短票**。EventSource 不允许自定义请求头，凭证只能放 query。
 *    先用 Bearer 调 `POST /auth/stream-ticket` 换一张 5 分钟有效的 stream
 *    短票，再拼到 `?token=`；长效 access token 不再进入 URL 与访问日志。
 *    短票过期后重连必须重新换票，因此这里不用浏览器自动重连，改为自行
 *    关闭旧连接、换票、带 `last_event_id` 续传。
 *
 * 2. **断线重连带 Last-Event-ID**。重连时把最后收到的事件 id 作为
 *    `last_event_id` 传回，配合服务端 `_REPLAY_SIZE` 回放窗口补齐断连期间
 *    的事件。不要依赖 EventSource 自己的重连——它只会复用旧 URL 里的过期票据。
 *
 * 3. **错误分级**。换票返回 401 说明会话已失效，属于不可恢复错误；网络抖动、
 *    后端重启、票据过期都按指数退避重连，不能把页面永久卡死。
 */

import { ApiError, API_BASE_URL, api } from "./client";

/** `event: alert` 的载荷（app/alerts/service.py `_push_sse`）。 */
export interface AlertStreamEvent {
  id: number;
  source: string;
  component: string;
  severity: string;
  title: string;
  fingerprint: string;
  incident_id: number | null;
  deduped: boolean;
  created_at: string;
  last_seen_at: string | null;
}

/** `event: incident.*` 的公共载荷（app/incidents/cluster.py / app/agents/orchestrator.py）。 */
export interface IncidentStreamEvent {
  incident_id?: number;
  [key: string]: unknown;
}

export interface KnowledgeIngestedEvent {
  doc_id: number;
  title: string;
  source_type: string;
  version: number;
  chunk_count: number;
  is_reingest: boolean;
}

export type StreamStatus = "connecting" | "open" | "closed";

export interface StreamHandlers {
  /** 新告警入库（含去重命中后刷新计数的情况）。 */
  onAlert?: (payload: AlertStreamEvent) => void;
  /** 事件相关事件：新建 / 合并 / 编排进度 / 终态。事件名原样透传。 */
  onIncident?: (event: string, payload: IncidentStreamEvent) => void;
  /** F-07 知识回灌完成。 */
  onKnowledge?: (payload: KnowledgeIngestedEvent) => void;
  /** 连接状态变化，用于渲染"实时 / 重连中"指示。 */
  onStatus?: (status: StreamStatus) => void;
  /** 鉴权失败等**不可恢复**错误（区别于网络抖动，需要重新登录）。 */
  onFatal?: (reason: string) => void;
}

interface StreamTicket {
  ticket: string;
  expires_in: number;
}

const INCIDENT_EVENTS = [
  "incident.created",
  "incident.updated",
  "incident.queued",
  "incident.step",
  "incident.loop",
  "incident.human_needed",
  "incident.action_required",
  "incident.evidence_added",
  "incident.diagnosed",
  "incident.recap_failed",
] as const;

function streamUrl(path: string, ticket: string, lastEventId: number | null): string {
  const base = API_BASE_URL.replace(/\/+$/, "");
  const params = new URLSearchParams({ token: ticket });
  if (lastEventId !== null) params.set("last_event_id", String(lastEventId));
  return `${base}${path}?${params.toString()}`;
}

function openStream(
  path: string,
  handlers: StreamHandlers,
  includeAlerts: boolean,
): () => void {
  let source: EventSource | null = null;
  let stopped = false;
  let retryTimer: number | null = null;
  let retryDelay = 1000;
  let lastEventId: number | null = null;

  const closeSource = () => {
    if (!source) return;
    source.onerror = null;
    source.close();
    source = null;
  };

  const remember = (ev: MessageEvent<string>) => {
    if (!ev.lastEventId) return;
    const id = Number(ev.lastEventId);
    if (Number.isFinite(id)) lastEventId = id;
  };

  const scheduleReconnect = () => {
    if (stopped || retryTimer !== null) return;
    handlers.onStatus?.("connecting");
    retryTimer = window.setTimeout(() => {
      retryTimer = null;
      void connect();
    }, retryDelay);
    retryDelay = Math.min(retryDelay * 2, 30_000);
  };

  const connect = async () => {
    if (stopped) return;
    closeSource();

    let ticket: string;
    try {
      const resp = await api.post<StreamTicket>("/auth/stream-ticket");
      ticket = resp.ticket;
    } catch (err) {
      if (stopped) return;
      if (err instanceof ApiError && err.isUnauthorized) {
        stopped = true;
        handlers.onStatus?.("closed");
        handlers.onFatal?.("登录状态已失效，请重新登录");
        return;
      }
      scheduleReconnect();
      return;
    }
    if (stopped) return;

    const es = new EventSource(streamUrl(path, ticket, lastEventId));
    source = es;

    es.onopen = () => {
      retryDelay = 1000;
      handlers.onStatus?.("open");
    };

    const onJsonEvent =
      (fn: (data: unknown) => void) =>
      (ev: Event) => {
        const me = ev as MessageEvent<string>;
        remember(me);
        try {
          fn(JSON.parse(me.data));
        } catch {
          // 坏帧丢弃：一条脏数据不该让整条流崩掉
        }
      };

    if (includeAlerts) {
      es.addEventListener(
        "alert",
        onJsonEvent((data) => handlers.onAlert?.(data as AlertStreamEvent)),
      );
      es.addEventListener(
        "knowledge.ingested",
        onJsonEvent((data) => handlers.onKnowledge?.(data as KnowledgeIngestedEvent)),
      );
    }
    for (const name of INCIDENT_EVENTS) {
      es.addEventListener(
        name,
        onJsonEvent((data) => handlers.onIncident?.(name, data as IncidentStreamEvent)),
      );
    }

    es.onerror = () => {
      if (stopped) return;
      // 票据可能已过期或后端重启：主动关掉，换新票后按 last_event_id 续传。
      closeSource();
      scheduleReconnect();
    };
  };

  handlers.onStatus?.("connecting");
  void connect();

  return () => {
    stopped = true;
    if (retryTimer !== null) {
      window.clearTimeout(retryTimer);
      retryTimer = null;
    }
    closeSource();
    handlers.onStatus?.("closed");
  };
}

/**
 * 订阅全局告警流（GET /stream/alerts）。
 *
 * @returns 取消订阅函数；务必在组件卸载时调用，否则连接会泄漏。
 */
export function openAlertStream(handlers: StreamHandlers): () => void {
  return openStream("/stream/alerts", handlers, true);
}

/**
 * 订阅单个事件的编排进度（GET /stream/incidents/{id}）。
 *
 * 详情页用它实现"诊断步骤逐条推进"；告警看板不需要。
 */
export function openIncidentStream(
  incidentId: number | string,
  handlers: Omit<StreamHandlers, "onAlert" | "onKnowledge">,
): () => void {
  return openStream(`/stream/incidents/${incidentId}`, handlers, false);
}
