<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { useRouter } from "vue-router";
import GlowBackground from "@/components/GlowBackground.vue";
import {
  AlertCircle,
  ArrowLeft,
  Eye,
  EyeOff,
  KeyRound,
  Mail,
  ShieldAlert,
  ShieldCheck,
  User,
  X,
} from "lucide-vue-next";
import { authApi, type RegisterConfigOut } from "@/api/auth";
import { ApiError } from "@/api/client";

/** 把注册接口的错误码翻译成用户能看懂的一句话。 */
function describeRegisterError(err: unknown, fallback: string): string {
  if (err instanceof ApiError) {
    if (err.isNetworkError) {
      return "无法连接后端服务，请确认 mindops-backend 已在 8010 端口启动。";
    }
    switch (err.code) {
      case "REGISTER_DISABLED":
        return "自助注册已关闭，请联系管理员开通账号";
      case "INVALID_EMAIL":
        return "邮箱格式不正确，请检查后重试";
      case "EMAIL_REQUIRED":
        return "请先填写邮箱";
      case "CODE_REQUIRED":
        return "请先填写验证码";
      case "INVALID_CODE":
        return "验证码错误或已过期，请重新获取";
      case "EMAIL_SEND_INTERVAL":
        return "验证码发送过于频繁，请稍后重试";
      case "EMAIL_SEND_FAILED":
        return "验证码邮件发送失败，请稍后重试或联系管理员";
      case "USERNAME_REQUIRED":
        return "用户名不能为空";
      case "INVALID_USERNAME":
        return "用户名需为 3-32 位字母、数字、下划线或短横线";
      case "USERNAME_TAKEN":
        return "该用户名已被占用，请换一个";
      case "EMAIL_TAKEN":
        return "该邮箱已注册，请直接登录或改用其它邮箱";
      case "PASSWORD_REQUIRED":
        return "密码不能为空";
      case "WEAK_PASSWORD":
        return "密码至少 8 位";
      default:
        return err.message || fallback;
    }
  }
  return err instanceof Error ? err.message : fallback;
}

/** 邮箱格式：宽松校验，只挡明显的错，真正的有效性由"能否收到码"证明。 */
const EMAIL_RE = /^[A-Za-z0-9._%+-]+@[A-Za-z0-9-]+(\.[A-Za-z0-9-]+)*\.[A-Za-z]{2,}$/;

const router = useRouter();
const config = ref<RegisterConfigOut | null>(null);

const email = ref("");
const code = ref("");
const username = ref("");
const password = ref("");
const showPassword = ref(false);

const loading = ref(false);
const sendingCode = ref(false);
const countdown = ref(0);
const error = ref<string | null>(null);
const notice = ref<string | null>(null);

onMounted(() => {
  // 读注册开关：关掉时不该让用户白填一整张表单才收到 403
  authApi
    .registerConfig()
    .then((cfg) => (config.value = cfg))
    .catch((err) => (error.value = describeRegisterError(err, "无法获取注册配置")));
});

let countdownTimer: number | undefined;
watch(countdown, (value) => {
  if (value <= 0) return;
  countdownTimer = window.setTimeout(() => (countdown.value = value - 1), 1000);
});
onBeforeUnmount(() => window.clearTimeout(countdownTimer));

const emailOk = computed(() => EMAIL_RE.test(email.value.trim()));
const disabled = computed(() => config.value !== null && !config.value.enabled);

async function handleSendCode() {
  if (!emailOk.value) {
    error.value = "请先填写正确的邮箱地址";
    return;
  }
  sendingCode.value = true;
  error.value = null;
  try {
    const res = await authApi.emailSend(email.value.trim());
    notice.value = `验证码已发送，${Math.round(res.ttl / 60)} 分钟内有效`;
    countdown.value = res.interval || 60;
    if (res.debug_code) {
      code.value = res.debug_code;
      notice.value = `本地调试：验证码 ${res.debug_code} 已自动填入`;
    }
  } catch (err) {
    error.value = describeRegisterError(err, "验证码发送失败");
  } finally {
    sendingCode.value = false;
  }
}

async function handleSubmit(e: Event) {
  e.preventDefault();
  if (!emailOk.value) {
    error.value = "邮箱格式不正确";
    return;
  }
  loading.value = true;
  error.value = null;
  try {
    await authApi.register({
      email: email.value.trim(),
      code: code.value.trim(),
      username: username.value.trim(),
      password: password.value,
    });
    // 后端注册成功即签发 token，直接进主界面，不要求再登录一次
    void router.push("/tasks");
  } catch (err) {
    error.value = describeRegisterError(err, "注册失败");
  } finally {
    loading.value = false;
  }
}

function onCodeInput(e: Event) {
  const raw = (e.target as HTMLInputElement).value.replace(/\D/g, "");
  code.value = raw.slice(0, config.value?.code_length ?? 6);
}
</script>

<template>
  <div class="min-h-screen relative flex items-center justify-center px-4 py-10">
    <GlowBackground />

    <div class="relative z-10 w-full max-w-[400px]">
      <div class="text-center mb-8">
        <h1 class="text-[28px] font-semibold heading-tight mb-2">MindOps</h1>
        <p class="text-[14px] text-muted-foreground">智能运维诊断工作台</p>
      </div>

      <div class="glass-strong rounded-xl shadow-lg p-8">
        <div class="flex items-center gap-2 mb-6">
          <button
            type="button"
            @click="router.push('/login')"
            class="p-1 -ml-1 rounded-full text-muted-foreground hover:text-foreground hover:bg-secondary transition-colors"
            title="返回登录"
          >
            <ArrowLeft :size="18" />
          </button>
          <h2 class="text-[16px] font-medium">注册账号</h2>
        </div>

        <!-- 错误提示 -->
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

        <!-- 成功/提示 -->
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

        <!-- 注册被关闭：不展示表单，避免用户白填 -->
        <div v-if="disabled" class="py-6 text-center">
          <ShieldAlert :size="32" class="mx-auto mb-3 text-muted-foreground" />
          <p class="text-[14px] font-medium mb-1.5">自助注册已关闭</p>
          <p class="text-[13px] text-muted-foreground leading-relaxed">
            请联系管理员为你开通账号，
            <br />
            或使用已有的账号登录。
          </p>
          <button
            type="button"
            @click="router.push('/login')"
            class="mt-5 w-full py-2.5 rounded-full bg-primary text-primary-foreground text-[14px] font-medium btn-pill hover:bg-primary/90 transition-colors"
          >
            返回登录
          </button>
        </div>

        <form v-else @submit="handleSubmit" class="space-y-4">
          <!-- 邮箱 -->
          <div>
            <label class="block text-[13px] font-medium mb-1.5">邮箱</label>
            <div class="relative">
              <Mail :size="15" class="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
              <input
                v-model="email"
                type="email"
                placeholder="请输入邮箱，如 you@qq.com"
                class="w-full pl-9 pr-3.5 py-2.5 rounded-lg border border-border bg-card text-[14px] focus:outline-none focus:ring-2 focus:ring-accent/30 focus:border-accent transition-colors"
                autocomplete="email"
              />
            </div>
            <p v-if="config?.channel === 'mock'" class="text-[12px] text-muted-foreground/70 mt-1">
              当前为本地调试通道，验证码会直接显示在页面上，不会真的发信
            </p>
          </div>

          <!-- 验证码 -->
          <div>
            <label class="block text-[13px] font-medium mb-1.5">邮箱验证码</label>
            <div class="flex gap-2">
              <input
                :value="code"
                type="text"
                :placeholder="`${config?.code_length ?? 6} 位验证码`"
                class="flex-1 px-3.5 py-2.5 rounded-lg border border-border bg-card text-[14px] font-mono tracking-widest focus:outline-none focus:ring-2 focus:ring-accent/30 focus:border-accent transition-colors"
                autocomplete="one-time-code"
                @input="onCodeInput"
              />
              <button
                type="button"
                @click="handleSendCode"
                :disabled="countdown > 0 || sendingCode || !emailOk"
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

          <!-- 用户名 -->
          <div>
            <label class="block text-[13px] font-medium mb-1.5">用户名</label>
            <div class="relative">
              <User :size="15" class="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground" />
              <input
                v-model="username"
                type="text"
                placeholder="3-32 位字母、数字、下划线或短横线"
                class="w-full pl-9 pr-3.5 py-2.5 rounded-lg border border-border bg-card text-[14px] focus:outline-none focus:ring-2 focus:ring-accent/30 focus:border-accent transition-colors"
                autocomplete="username"
              />
            </div>
          </div>

          <!-- 密码 -->
          <div>
            <label class="block text-[13px] font-medium mb-1.5">密码</label>
            <div class="relative">
              <KeyRound
                :size="15"
                class="absolute left-3 top-1/2 -translate-y-1/2 text-muted-foreground"
              />
              <input
                v-model="password"
                :type="showPassword ? 'text' : 'password'"
                placeholder="至少 8 位"
                class="w-full pl-9 pr-10 py-2.5 rounded-lg border border-border bg-card text-[14px] focus:outline-none focus:ring-2 focus:ring-accent/30 focus:border-accent transition-colors"
                autocomplete="new-password"
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

          <!-- 提交 -->
          <button
            type="submit"
            :disabled="loading"
            class="w-full py-2.5 rounded-full bg-primary text-primary-foreground text-[14px] font-medium btn-pill hover:bg-primary/90 transition-colors disabled:opacity-60 disabled:cursor-not-allowed"
          >
            {{ loading ? "注册中..." : "注册并进入" }}
          </button>

          <p class="text-[13px] text-muted-foreground text-center">
            已有账号？
            <button type="button" @click="router.push('/login')" class="ml-1 text-accent hover:underline">
              返回登录
            </button>
          </p>
        </form>
      </div>

      <p
        class="mt-6 text-[12px] text-muted-foreground/70 text-center flex items-center justify-center gap-1.5"
      >
        <ShieldCheck :size="13" />
        注册信息仅存储在本系统数据库，不会用于其它用途
      </p>
    </div>
  </div>
</template>
