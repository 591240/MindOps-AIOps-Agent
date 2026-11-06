/**
 * 统一 API 客户端（P1-5：补上前端缺失的后端接缝）。
 *
 * 修复前：`react-query` 虽已注入（router.tsx），但全仓零 `useQuery`、
 * 无 `src/api`、无 HTTP 客户端，所有页面直接消费 `src/mock`，
 * 导致"零后端接缝"——前端与后端完全脱节。
 *
 * 本模块提供：
 *  - baseURL 集中配置（VITE_API_BASE_URL，默认 http://localhost:8010/api/v1）
 *  - Bearer token：**委托给 `lib/session.ts`**（单一存储点，见该模块注释）
 *  - 错误归一化：后端 `{"detail": {code, message}}` → ApiError{status, code, message}
 *  - 网络不可达单独标记为 kind="network"，供页面显示"后端未连接"错误态（不回退假数据）
 */

import { clearSession, getAccessToken } from "@/lib/session";

const RAW_BASE: string =
  (import.meta.env.VITE_API_BASE_URL as string | undefined) ??
  "http://localhost:8010/api/v1";

export const API_BASE_URL = RAW_BASE.replace(/\/+$/, "");

/** 读取当前访问令牌（会话唯一存储点在 `lib/session.ts`）。 */
export function getToken(): string | null {
  return getAccessToken();
}

/** 清空整个会话（token + refresh_token + user），用于登出与 401 兜底。 */
export function clearToken(): void {
  clearSession();
}

export type ApiErrorKind = "http" | "network" | "parse";

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly kind: ApiErrorKind;
  readonly payload: unknown;

  constructor(args: {
    message: string;
    status?: number;
    code?: string;
    kind?: ApiErrorKind;
    payload?: unknown;
  }) {
    super(args.message);
    this.name = "ApiError";
    this.status = args.status ?? 0;
    this.code = args.code ?? "UNKNOWN";
    this.kind = args.kind ?? "http";
    this.payload = args.payload;
  }

  /** 网络不可达 / 后端未启动（页面据此提示"请确认后端已启动"）。 */
  get isNetworkError(): boolean {
    return this.kind === "network";
  }

  /** 未认证或令牌失效。 */
  get isUnauthorized(): boolean {
    return this.status === 401;
  }
}

/** 后端统一错误信封：{ detail: { code, message, ... } } */
interface DetailEnvelope {
  code?: string;
  message?: string;
  errors?: unknown;
}

function extractDetail(body: unknown): DetailEnvelope | null {
  if (body && typeof body === "object" && "detail" in body) {
    const detail = (body as { detail: unknown }).detail;
    if (typeof detail === "string") {
      return { message: detail };
    }
    if (detail && typeof detail === "object") {
      return detail as DetailEnvelope;
    }
  }
  return null;
}

export interface RequestOptions {
  /** 不携带 Authorization 头（登录等场景）。 */
  anonymous?: boolean;
  signal?: AbortSignal;
}

async function request<T>(
  method: string,
  path: string,
  body?: unknown,
  options: RequestOptions = {},
): Promise<T> {
  const url = `${API_BASE_URL}${path.startsWith("/") ? path : `/${path}`}`;
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };

  if (!options.anonymous) {
    const token = getToken();
    if (token) headers.Authorization = `Bearer ${token}`;
  }

  let resp: Response;
  try {
    resp = await fetch(url, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      signal: options.signal,
    });
  } catch (err) {
    throw new ApiError({
      message: "无法连接后端服务",
      kind: "network",
      code: "NETWORK_ERROR",
      payload: err,
    });
  }

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
    // 401 说明令牌已失效/被吊销。审查 #15：后端提供 /auth/refresh 轮换，
    // 先尝试用 refresh_token 换新（单飞防止并发请求重复轮换），成功后重放原
    // 请求一次；失败才清会话交给路由守卫送回登录页。
    // 匿名请求（登录本身）与轮换请求自身的 401 不适用——后者意味着 refresh_token
    // 也已失效，清会话走登录。
    if (resp.status === 401 && !options.anonymous) {
      if (path.startsWith("/auth/refresh")) {
        clearSession();
      } else {
        const refreshed = await tryRefreshSession();
        if (refreshed) {
          return request<T>(method, path, body, options);
        }
        clearSession();
      }
    }
    const detail = extractDetail(parsed);
    throw new ApiError({
      message: detail?.message ?? `请求失败（HTTP ${resp.status}）`,
      status: resp.status,
      code: detail?.code ?? "HTTP_ERROR",
      kind: "http",
      payload: parsed,
    });
  }

  if (parsed === null && text) {
    throw new ApiError({
      message: "响应不是合法 JSON",
      kind: "parse",
      code: "PARSE_ERROR",
      payload: text,
    });
  }

  return parsed as T;
}

export const api = {
  get: <T>(path: string, options?: RequestOptions) =>
    request<T>("GET", path, undefined, options),
  post: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>("POST", path, body, options),
  put: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>("PUT", path, body, options),
  patch: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>("PATCH", path, body, options),
  delete: <T>(path: string, body?: unknown, options?: RequestOptions) =>
    request<T>("DELETE", path, body, options),
};

// ---- 审查 #15：单飞 token 轮换 ----
// 并发的多个 401 只触发一次 /auth/refresh；轮换期间其余请求挂起等待，
// 完成后各自用新 token 重放。动态 import 避免 client ↔ auth 的循环依赖。
let refreshInFlight: Promise<boolean> | null = null;

async function tryRefreshSession(): Promise<boolean> {
  const { getRefreshToken } = await import("@/lib/session");
  const refreshToken = getRefreshToken();
  if (!refreshToken) return false;
  if (!refreshInFlight) {
    refreshInFlight = (async () => {
      try {
        const { authApi } = await import("./auth");
        await authApi.refresh(refreshToken);
        return true;
      } catch {
        return false;
      } finally {
        refreshInFlight = null;
      }
    })();
  }
  return refreshInFlight;
}
