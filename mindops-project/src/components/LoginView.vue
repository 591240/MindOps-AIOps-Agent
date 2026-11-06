<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRoute, useRouter } from "vue-router";
import GlowBackground from "@/components/GlowBackground.vue";
import { Eye, EyeOff, Mail, AlertCircle, X, Sparkles } from "lucide-vue-next";
import { authApi } from "@/api/auth";
import { ApiError } from "@/api/client";

/**
 * 登录页（F-10）：两个 Tab 都走真实接口 —— 密码登录与邮箱验证码登录。
 * 短信登录已整体移除（mock 通道会把 debug_code 回传给任何调用方）。
 */

type LoginTab = "password" | "email";

/** 与后端 is_valid_email 同口径：本地部分允许常见符号，域名须含点且 TLD ≥2 位。 */
const EMAIL_RE = /^[A-Za-z0-9._%+\-]+@[A-Za-z0-9\-]+(\.[A-Za-z0-9\-]+)*\.[A-Za-z]{2,}$/;

function describeLoginError(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    if (err.isNetworkError) {
      return "无法连接后端服务，请确认 mindops-backend 已在 8010 端口启动。";
    }
    switch (err.code) {
      case "INVALID_CREDENTIALS":
        return "用户名或密码错误";
      case "ACCOUNT_LOCKED": {
        const p = (err.payload ?? {}) as { detail?: { retry_after?: number } };
        const seconds = p.detail?.retry_after;
        return seconds
          ? `账号已锁定，请 ${Math.max(1, Math.ceil(seconds / 60))} 分钟后重试`
          : "账号已锁定，请稍后重试";
      }
      case "INVALID_CODE":
        return "验证码错误或已过期";
      case "USER_NOT_FOUND":
        return "该邮箱尚未注册，请先注册或联系管理员开通账号";
      case "ACCOUNT_DISABLED":
        return "账号已停用，请联系管理员";
      case "EMAIL_SEND_INTERVAL":
        return "发送过于频繁，请稍后重试";
      case "EMAIL_SEND_FAILED":
        return "验证码邮件发送失败，请稍后重试或联系管理员";
      case "EMAIL_REQUIRED":
        return "邮箱不能为空";
      case "INVALID_EMAIL":
        return "邮箱格式不正确";
      case "USERNAME_REQUIRED":
        return "用户名不能为空";
      case "PASSWORD_REQUIRED":
        return "密码不能为空";
      default:
        return err.message || fallback;
    }
  }
  return err instanceof Error ? err.message : fallback;
}

const router = useRouter();
const route = useRoute();

// 历史版本存过 "sms"，已下线，回落密码登录
const saved = localStorage.getItem("login_tab");
const tab = ref<LoginTab>(saved === "email" ? "email" : "password");
const loading = ref(false);
const error = ref<string | null>(null);
const notice = ref<string | null>(null);

const username = ref("");
const password = ref("");
const showPassword = ref(false);
const rememberMe = ref(false);

const email = ref("");
const emailCode = ref("");
const countdown = ref(0);
const emailError = ref<string | null>(null);
const sendingCode = ref(false);
const codeRef = ref<HTMLInputElement | null>(null);
/** 防止"输满验证码自动提交"重复触发 */
let autoSubmitted = false;

const registerEnabled = ref(false);
const codeLength = ref(6);

onMounted(() => {
  const remembered = localStorage.getItem("mindops_remember_username");
  if (remembered) {
    username.value = remembered;
    rememberMe.value = true;
  }

  // 失败静默：登录页不该因为注册配置拉取失败而报错
  authApi
    .registerConfig()
    .then((cfg) => {
      registerEnabled.value = cfg.enabled;
      if (cfg.code_length >= 4 && cfg.code_length <= 8) codeLength.value = cfg.code_length;
    })
    .catch(() => (registerEnabled.value = false));
});

watch(tab, (t) => localStorage.setItem("login_tab", t));

let countdownTimer: number | undefined;
watch(countdown, (value) => {
  if (value <= 0) return;
  countdownTimer = window.setTimeout(() => (countdown.value = value - 1), 1000);
});
onBeforeUnmount(() => window.clearTimeout(countdownTimer));

const emailValid = computed(() => EMAIL_RE.test(email.value.trim()));

function validateEmail(val: string) {
  return EMAIL_RE.test(val.trim());
}

function handleEmailBlur() {
  emailError.value = email.value && !validateEmail(email.value) ? "请输入有效的邮箱地址" : null;
}

function goAfterLogin() {
  const redirect = route.query.redirect;
  const target = typeof redirect === "string" && redirect ? redirect : "/tasks";
  void router.push(target);
}

async function handleSendCode() {
  error.value = null;
  notice.value = null;
  const target = email.value.trim();
  if (!validateEmail(target)) {
    emailError.value = "请输入有效的邮箱地址";
    return;
  }
  sendingCode.value = true;
  try {
    const res = await authApi.emailLoginSend(target);
    // 限流间隔以服务端返回为准
    countdown.value = res.interval || 60;
    codeRef.value?.focus();
    if (res.debug_code) {
      emailCode.value = res.debug_code;
      notice.value = `本地调试：验证码 ${res.debug_code} 已自动填入`;
    } else {
      notice.value = "验证码已发送，请查收邮箱";
    }
  } catch (err) {
    error.value = describeLoginError(err, "验证码发送失败");
  } finally {
    sendingCode.value = false;
  }
}

async function handlePasswordLogin() {
  error.value = null;
  notice.value = null;
  if (!username.value || !password.value) {
    error.value = "请输入用户名和密码";
    return;
  }
  loading.value = true;
  try {
    await authApi.login(username.value, password.value);
    if (rememberMe.value) localStorage.setItem("mindops_remember_username", username.value);
    else localStorage.removeItem("mindops_remember_username");
    goAfterLogin();
  } catch (err) {
    error.value = describeLoginError(err, "登录失败");
  } finally {
    loading.value = false;
  }
}

async function handleEmailLogin() {
  error.value = null;
  notice.value = null;
  if (!email.value.trim() || !emailCode.value) {
    error.value = "请输入邮箱和验证码";
    return;
  }
  loading.value = true;
  try {
    await authApi.emailLogin(email.value.trim(), emailCode.value);
    goAfterLogin();
  } catch (err) {
    error.value = describeLoginError(err, "登录失败");
    autoSubmitted = false; // 失败后允许用户改一位再自动提交
  } finally {
    loading.value = false;
  }
}

// 验证码输满自动提交（位数以服务端配置为准）
watch([emailCode, codeLength], () => {
  if (emailCode.value.length < codeLength.value) {
    autoSubmitted = false;
    return;
  }
  if (
    tab.value === "email" &&
    emailCode.value.length === codeLength.value &&
    !loading.value &&
    !autoSubmitted
  ) {
    autoSubmitted = true;
    void handleEmailLogin();
  }
});

function handleSubmit(e: Event) {
  e.preventDefault();
  if (tab.value === "password") void handlePasswordLogin();
  else void handleEmailLogin();
}

function onCodeInput(e: Event) {
  const raw = (e.target as HTMLInputElement).value.replace(/\D/g, "");
  emailCode.value = raw.slice(0, codeLength.value);
}
</script>

<template>
  <div class="min-h-screen relative flex items-center justify-center px-4">
    <GlowBackground />

    <div class="relative z-10 w-full max-w-[620px]">
      <!-- 头部：DeepSeek 式公告行 + 大字距标语 -->
      <div class="text-center mb-10">
        <p
          class="inline-flex items-center justify-center gap-1.5 text-[13px] text-[#41597a] mb-6 flex-wrap"
        >
          <Sparkles :size="14" class="text-accent shrink-0" />
          <span>
            MindOps 现已支持处置剧本与置信度回环，告警降噪 87%，欢迎体验
          </span>
          <span aria-hidden="true">→</span>
        </p>
        <h1
          class="text-[38px] sm:text-[46px] font-medium text-[#16294a] mb-4"
          style="letter-spacing: 0.3em; margin-right: -0.3em"
        >
          让故障无处遁形
        </h1>
        <p class="text-[14px] text-muted-foreground">智能运维诊断工作台</p>
      </div>

      <!-- 卡片 -->
      <div class="glass-strong rounded-xl shadow-lg p-8 max-w-[400px] mx-auto">
        <!-- Error Banner -->
        <div
          v-if="error"
          class="mb-4 px-3 py-2.5 rounded-lg bg-danger/5 border border-danger/20 flex items-start gap-2 fade-in-300"
        >
          <AlertCircle :size="16" class="text-danger shrink-0 mt-0.5" />
          <span class="text-[13px] text-danger flex-1">{{ error }}</span>
          <button @click="error = null" class="text-danger/60 hover:text-danger shrink-0">
            <X :size="14" />
          </button>
        </div>

        <!-- 非错误提示 -->
        <div
          v-if="notice && !error"
          class="mb-4 px-3 py-2.5 rounded-lg bg-success/5 border border-success/20 flex items-start gap-2 fade-in-300"
        >
          <Mail :size="16" class="text-success shrink-0 mt-0.5" />
          <span class="text-[13px] text-success flex-1">{{ notice }}</span>
          <button @click="notice = null" class="text-success/60 hover:text-success shrink-0">
            <X :size="14" />
          </button>
        </div>

        <!-- Tab 切换 -->
        <div class="flex gap-1 mb-6 p-1 rounded-full bg-secondary/60">
          <button
            @click="tab = 'password'"
            class="flex-1 py-2 rounded-full text-[13px] font-medium transition-all"
            :class="tab === 'password' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground'"
          >
            密码登录
          </button>
          <button
            @click="tab = 'email'"
            class="flex-1 py-2 rounded-full text-[13px] font-medium transition-all"
            :class="tab === 'email' ? 'bg-card text-foreground shadow-sm' : 'text-muted-foreground'"
          >
            邮箱登录
          </button>
        </div>

        <form @submit="handleSubmit" class="space-y-4">
          <template v-if="tab === 'password'">
            <!-- 用户名 -->
            <div>
              <label class="block text-[13px] font-medium mb-1.5">用户名</label>
              <input
                v-model="username"
                type="text"
                placeholder="请输入用户名"
                class="w-full px-3.5 py-2.5 rounded-lg border border-border bg-card text-[14px] focus:outline-none focus:ring-2 focus:ring-accent/30 focus:border-accent transition-colors"
                autocomplete="username"
              />
            </div>

            <!-- 密码 -->
            <div>
              <label class="block text-[13px] font-medium mb-1.5">密码</label>
              <div class="relative">
                <input
                  v-model="password"
                  :type="showPassword ? 'text' : 'password'"
                  placeholder="请输入密码"
                  class="w-full px-3.5 py-2.5 pr-10 rounded-lg border border-border bg-card text-[14px] focus:outline-none focus:ring-2 focus:ring-accent/30 focus:border-accent transition-colors"
                  autocomplete="current-password"
                />
                <button
                  type="button"
                  @click="showPassword = !showPassword"
                  class="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                >
                  <EyeOff v-if="showPassword" :size="16" />
                  <Eye v-else :size="16" />
                </button>
              </div>
            </div>

            <!-- 记住我 + 忘记密码 -->
            <div class="flex items-center justify-between">
              <label class="flex items-center gap-2 cursor-pointer">
                <input
                  v-model="rememberMe"
                  type="checkbox"
                  class="w-4 h-4 rounded border-border accent-accent"
                />
                <span class="text-[13px] text-muted-foreground">记住我</span>
              </label>
              <span
                class="text-[13px] text-muted-foreground/40 cursor-not-allowed"
                title="当前版本暂不支持自助找回密码"
              >
                忘记密码？
              </span>
            </div>
          </template>

          <template v-else>
            <!-- 邮箱 -->
            <div>
              <label class="block text-[13px] font-medium mb-1.5">邮箱</label>
              <input
                v-model="email"
                type="email"
                placeholder="请输入注册邮箱"
                autocomplete="email"
                class="w-full px-3.5 py-2.5 rounded-lg border border-border bg-card text-[14px] focus:outline-none focus:ring-2 focus:ring-accent/30 focus:border-accent transition-colors"
                @blur="handleEmailBlur"
              />
              <p v-if="emailError" class="text-[12px] text-danger mt-1">{{ emailError }}</p>
            </div>

            <!-- 验证码 -->
            <div>
              <label class="block text-[13px] font-medium mb-1.5">验证码</label>
              <div class="flex gap-2">
                <input
                  ref="codeRef"
                  :value="emailCode"
                  type="text"
                  inputmode="numeric"
                  :placeholder="`${codeLength} 位验证码`"
                  class="flex-1 px-3.5 py-2.5 rounded-lg border border-border bg-card text-[14px] font-mono tracking-widest focus:outline-none focus:ring-2 focus:ring-accent/30 focus:border-accent transition-colors"
                  @input="onCodeInput"
                />
                <button
                  type="button"
                  @click="handleSendCode"
                  :disabled="countdown > 0 || sendingCode || !emailValid"
                  class="px-4 py-2.5 rounded-lg border border-border bg-card text-[13px] font-medium hover:bg-secondary transition-colors disabled:opacity-40 disabled:cursor-not-allowed shrink-0"
                >
                  {{
                    sendingCode
                      ? "发送中..."
                      : countdown > 0
                        ? `重新获取 (${countdown}s)`
                        : "获取验证码"
                  }}
                </button>
              </div>
            </div>
          </template>

          <!-- 提交按钮 -->
          <button
            type="submit"
            :disabled="loading"
            class="w-full py-2.5 rounded-full bg-primary text-primary-foreground text-[14px] font-medium btn-pill hover:bg-primary/90 transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
          >
            {{ loading ? "登录中..." : tab === "password" ? "登录" : "邮箱验证码登录" }}
          </button>
        </form>

        <!-- 注册入口：仅在后端开启自助注册时显示（否则点了也是 403） -->
        <p
          v-if="registerEnabled"
          class="mt-4 pt-4 border-t border-border text-[13px] text-muted-foreground text-center"
        >
          还没有账号？
          <button type="button" @click="router.push('/register')" class="ml-1 text-accent hover:underline">
            立即注册
          </button>
        </p>
      </div>
    </div>
  </div>
</template>
