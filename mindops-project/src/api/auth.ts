/** F-10 认证接口（对齐 app/routers/auth.py）。
 *
 * 契约要点：
 * - `POST /auth/login` 未知账号返回 **401 `INVALID_CREDENTIALS`**（统一措辞，防用户名枚举），
 *   并且**不会自动注册**（`auth_allow_auto_register` 默认 False）。
 * - 连续密码错误会返回 **423 `ACCOUNT_LOCKED`**（带 `retry_after` 秒数）。
 * - 登录方式只有两种：密码、邮箱验证码。短信登录已整体移除（mock 通道的
 *   `debug_code` 等于公开后门）。
 * - `POST /auth/email/login/send` 有 60s 冷却，超频返回 **429 `EMAIL_SEND_INTERVAL`**；
 *   仅当 `email_provider == "mock"` 时响应里才有 `debug_code`（生产不返回）。
 * - `POST /auth/email/login` 验证码错误 **401 `INVALID_CODE`**；验证码正确但邮箱
 *   未注册 **403 `USER_NOT_FOUND`**（不自动建号）。
 * - 邮箱注册：`POST /auth/email/send` 发码（60s 冷却 → 429 `EMAIL_SEND_INTERVAL`）；
 *   `POST /auth/register` 注册成功**直接返回 token pair**（无需再登录一次）。
 *   注册接口的验证码错误是 **400 `INVALID_CODE`**（区别于登录的 401：
 *   注册表单的字段校验失败语义上不属"认证失败"），前端按 code 分支即可。
 */

import { api } from "./client";
import { writeSession, type AuthUser } from "@/lib/session";

export type { AuthUser };

export interface LoginOut {
  access_token: string;
  refresh_token: string;
  token_type: string;
  expires_in: number;
  user: AuthUser;
}

export interface CodeSendOut {
  message: string;
  ttl: number;
  interval: number;
  /** 仅 mock 邮件通道返回，方便本地调试 */
  debug_code?: string;
}

/** 邮箱验证码发送响应（注册与登录结构一致）。 */
export type EmailSendOut = CodeSendOut;

/** 自助注册开关（`GET /auth/register/config`，无需鉴权）。 */
export interface RegisterConfigOut {
  enabled: boolean;
  /** mock = 验证码直接回传，smtp = 真实发信 */
  channel: string;
  roles: string[];
  code_length: number;
  send_interval: number;
}

export interface RegisterPayload {
  email: string;
  code: string;
  username: string;
  password: string;
}

/** 把登录响应落成会话（`client.ts` 的 `Authorization` 头即从这里取）。 */
function persist(data: LoginOut): LoginOut {
  writeSession({
    token: data.access_token,
    refresh_token: data.refresh_token,
    user: data.user,
    expires_at: data.expires_in ? Date.now() + data.expires_in * 1000 : null,
  });
  return data;
}

export const authApi = {
  /** 密码登录；成功后写入会话，供后续请求自动携带。 */
  async login(username: string, password: string): Promise<LoginOut> {
    const data = await api.post<LoginOut>(
      "/auth/login",
      { username, password },
      { anonymous: true },
    );
    return persist(data);
  },

  /** 审查 #15：refresh token 轮换（后端旧 jti 拉黑 + 新 pair）。失败抛 ApiError。 */
  async refresh(refreshToken: string): Promise<LoginOut> {
    const data = await api.post<LoginOut>(
      "/auth/refresh",
      { refresh_token: refreshToken },
      { anonymous: true },
    );
    return persist(data);
  },

  /** 发送邮箱登录验证码（60s 冷却；mock 通道会回传 debug_code）。 */
  emailLoginSend(email: string) {
    return api.post<CodeSendOut>("/auth/email/login/send", { email }, { anonymous: true });
  },

  /** 邮箱验证码登录；成功后写入会话。未注册邮箱返回 403 USER_NOT_FOUND。 */
  async emailLogin(email: string, code: string): Promise<LoginOut> {
    const data = await api.post<LoginOut>(
      "/auth/email/login",
      { email, code },
      { anonymous: true },
    );
    return persist(data);
  },

  /** 自助注册是否开启 + 验证码通道（登录页据此决定要不要显示"注册"入口）。 */
  registerConfig() {
    return api.get<RegisterConfigOut>("/auth/register/config", { anonymous: true });
  },

  /** 发送邮箱注册验证码（60s 冷却；mock 通道会回传 debug_code）。 */
  emailSend(email: string) {
    return api.post<EmailSendOut>("/auth/email/send", { email }, { anonymous: true });
  },

  /** 邮箱验证码注册；成功后后端**直接签发** token pair，这里一并写入会话。 */
  async register(payload: RegisterPayload): Promise<LoginOut> {
    const data = await api.post<LoginOut>("/auth/register", payload, { anonymous: true });
    return persist(data);
  },

  me() {
    return api.get<AuthUser & { last_login: string | null; last_login_method: string | null }>(
      "/auth/me",
    );
  },

  logout() {
    return api.post<{ message: string; revoked?: string[] }>("/auth/logout", {});
  },
};
