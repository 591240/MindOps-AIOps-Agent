/**
 * 登录会话的**唯一**存储点。
 *
 * 为什么要有这个模块：修复前前端把会话拆成了两半，各写各的 ——
 *  - 路由守卫（`routes/_layout.tsx`、`routes/index.tsx`）与 `LayoutShell` 读 `mindops_auth`；
 *  - HTTP 客户端（`api/client.ts`）读 `mindops_access_token` 拼 `Authorization` 头。
 *
 * 后果是"登录成功但请求全 401"：只要有一边没写，页面就进去了、接口却全挂。
 * 现在两者都从这里取，键只剩 `mindops_auth` 一个。
 *
 * `token` 字段名保持与后端 `POST /auth/login` 响应一致（`access_token`），
 * 但为了兼容既有守卫代码，对外暴露的会话对象仍用 `token` 作为访问令牌字段名。
 */

const SESSION_KEY = "mindops_auth";

/** 旧版本遗留的分散键，登录/登出时一并清理，避免残留 token 继续被读到。 */
const LEGACY_KEYS = ["mindops_access_token", "mindops_refresh_token"] as const;

export interface AuthUser {
  id: number;
  username: string;
  role: string;
}

export interface AuthSession {
  /** 访问令牌（后端字段名 `access_token`） */
  token: string;
  refresh_token: string;
  user: AuthUser;
  /** 访问令牌到期时刻（epoch ms）；未知时为 null */
  expires_at: number | null;
}

export function readSession(): AuthSession | null {
  try {
    const raw = localStorage.getItem(SESSION_KEY);
    if (!raw) return null;
    const parsed = JSON.parse(raw) as Partial<AuthSession>;
    if (!parsed || typeof parsed.token !== "string" || !parsed.token) return null;
    return {
      token: parsed.token,
      refresh_token: typeof parsed.refresh_token === "string" ? parsed.refresh_token : "",
      user: (parsed.user as AuthUser) ?? { id: 0, username: "", role: "engineer" },
      expires_at: typeof parsed.expires_at === "number" ? parsed.expires_at : null,
    };
  } catch {
    // 存储损坏（手改 / 旧格式）时视为未登录，而不是让整页崩掉
    return null;
  }
}

export function writeSession(session: AuthSession): void {
  try {
    localStorage.setItem(SESSION_KEY, JSON.stringify(session));
    for (const key of LEGACY_KEYS) localStorage.removeItem(key);
  } catch {
    /* localStorage 不可用（隐私模式）时静默降级 */
  }
}

export function clearSession(): void {
  try {
    localStorage.removeItem(SESSION_KEY);
    for (const key of LEGACY_KEYS) localStorage.removeItem(key);
  } catch {
    /* ignore */
  }
}

/** 审查 #15：token 轮换后就地更新会话（保留 user，仅换 token pair）。 */
export function updateSessionTokens(token: string, refreshToken: string, expiresIn: number | null): void {
  const current = readSession();
  if (!current) return;
  writeSession({
    token,
    refresh_token: refreshToken,
    user: current.user,
    expires_at: expiresIn ? Date.now() + expiresIn * 1000 : null,
  });
}

/** 供 HTTP 客户端取 `Authorization` 头用。 */
export function getAccessToken(): string | null {
  return readSession()?.token ?? null;
}

/** 审查 #15：供 401 单飞轮换取 refresh token 用。 */
export function getRefreshToken(): string | null {
  return readSession()?.refresh_token || null;
}

export function getSessionUser(): AuthUser | null {
  return readSession()?.user ?? null;
}

/** 当前会话是否为 admin（前端路由级权限预判，后端仍会独立鉴权）。 */
export function isAdmin(): boolean {
  return readSession()?.user.role === "admin";
}
