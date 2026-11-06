/**
 * 内网地址校验（F-11 SSRF 防护的前端第一道闸）
 *
 * 后端有五道闸（`docs/27` §5.1），前端这道只求「用户填公网地址时立刻拦住、不给提交」，
 * 真正的安全判据永远在后端，前端校验不构成任何安全保证。
 *
 * 规则与 `IntegrationView.validateAddress()` 一致（localhost / 10. / 192.168. / 172.16-31.）。
 * TODO(联调)：IntegrationView 内的同名函数改为引用本模块，避免两份实现漂移。
 */

export const INTERNAL_HINT = "仅允许内网或受信地址段（10.x / 172.16-31.x / 192.168.x / localhost）";

export function validateInternalAddress(url: string): { ok: boolean; reason?: string } {
  const raw = url.trim();
  if (!raw) return { ok: false, reason: "请输入地址" };

  let parsed: URL;
  try {
    parsed = new URL(raw);
  } catch {
    return { ok: false, reason: "地址格式不正确，需形如 http://10.0.0.5:9090" };
  }

  if (parsed.protocol !== "http:" && parsed.protocol !== "https:") {
    return { ok: false, reason: "只支持 http / https 协议" };
  }
  if (parsed.username || parsed.password) {
    return { ok: false, reason: "地址中不允许携带用户名密码" };
  }

  const hostname = parsed.hostname;
  const isInternal =
    hostname === "localhost" ||
    hostname === "127.0.0.1" ||
    hostname.startsWith("10.") ||
    hostname.startsWith("192.168.") ||
    (() => {
      const m = hostname.match(/^172\.(\d+)\./);
      if (!m) return false;
      const second = parseInt(m[1], 10);
      return second >= 16 && second <= 31;
    })();

  if (!isInternal) return { ok: false, reason: INTERNAL_HINT };

  const port = parsed.port ? parseInt(parsed.port, 10) : parsed.protocol === "https:" ? 443 : 80;
  if (port < 1 || port > 65535) return { ok: false, reason: "端口需在 1-65535 之间" };

  return { ok: true };
}
