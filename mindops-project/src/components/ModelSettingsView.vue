<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import {
  Plus,
  Edit,
  Trash2,
  Star,
  Eye,
  EyeOff,
  Loader2,
  CheckCircle2,
  XCircle,
  Zap,
} from "lucide-vue-next";
import { llmApi, type LlmProviderDTO, type LlmPreset } from "@/api/llm";
import { ApiError } from "@/api/client";
import { toast } from "vue-sonner";

// 厂商类型是后端词表（deepseek/qwen/openai/zhipu/moonshot/ollama/custom），
// 前端不再维护一份会漂移的联合类型。
type ProviderRole = "chat" | "reasoning" | "embedding";
type ProviderStatus = "connected" | "error" | "untested";

interface Provider {
  id: string;
  name: string;
  provider_type: string;
  api_key_masked: string;
  base_url: string;
  model_name: string;
  role: ProviderRole;
  is_default: boolean;
  status: ProviderStatus;
  latency_ms: number;
}

/**
 * 厂商预设以**后端契约**为准（`LlmPreset`）。
 *
 * 以前是 `typeof mockPresets[0]`，那是一份自己写的形状：带 `model_name`
 * 和 `description`。而后端 `/llm/presets` 返回的是 `models` 数组 + `key_hint`。
 * 用 mock 的联合类型去接真实响应，会得到"类型是 mock、运行时是后端"的错位
 * ——TS 不报错，页面上却是 undefined。
 */
type Preset = LlmPreset;

// 角色词表与后端一致：chat / reasoning / embedding。
// 旧版把 4 个智能体角色（triage/diagnose/remediate/recap）映射到后端 2 个
// role，等于让用户以为能给每个智能体单独绑模型，实际只有 chat/reasoning 生效。
const ROLE_LABELS: Record<ProviderRole, string> = {
  chat: "通用对话",
  reasoning: "推理",
  embedding: "向量",
};

function fromDTO(row: LlmProviderDTO): Provider {
  return {
    id: String(row.id),
    name: row.name,
    provider_type: row.provider_type,
    api_key_masked: row.api_key_masked ?? "—",
    base_url: row.base_url,
    model_name: row.model_name,
    role: row.role,
    is_default: row.is_default,
    status: (row.last_test_status === null
      ? "untested"
      : row.last_test_status === "success"
        ? "connected"
        : "error") as Provider["status"],
    latency_ms: row.last_test_latency_ms ?? 0,
  };
}

interface ModelForm {
  name: string;
  api_key: string;
  base_url: string;
  model_name: string;
  role: "chat" | "reasoning";
}

const loading = ref(true);
const providers = ref<Provider[]>([]);
const dialogOpen = ref(false);
const editProvider = ref<Provider | null>(null);
const step = ref<1 | 2>(1);
const selectedPreset = ref<Preset | null>(null);
/** 厂商预设；为空表示接口不可用（非 admin 或后端未启动），届时第一步给出空态。 */
const presets = ref<Preset[]>([]);
const formData = ref<ModelForm>({
  name: "",
  api_key: "",
  base_url: "",
  model_name: "",
  role: "chat",
});
const showKey = ref(false);
const testing = ref(false);
const saving = ref(false);
const testResult = ref<{
  ok: boolean;
  latency?: number;
  error?: string;
  note?: string;
} | null>(null);

/** 改任何参与连通测试的字段都要清掉旧结果，避免"改完地址仍显示上次成功"。 */
function patchForm(patch: Partial<ModelForm>) {
  formData.value = { ...formData.value, ...patch };
  testResult.value = null;
}

function onTextInput(key: "name" | "api_key" | "base_url" | "model_name", event: Event) {
  patchForm({ [key]: (event.target as HTMLInputElement).value } as Partial<ModelForm>);
}

function onRoleChange(event: Event) {
  patchForm({ role: (event.target as HTMLSelectElement).value as "chat" | "reasoning" });
}

function presetModelSummary(preset: Preset) {
  return preset.models.map((m) => preset.model_labels?.[m] ?? m).join(" / ");
}

function selectPreset(preset: Preset) {
  selectedPreset.value = preset;
  formData.value.base_url = preset.base_url;
  // 后端给的是可选模型列表，取首个作为默认值；
  // 用户仍可在第二步表单里改。
  formData.value.model_name = preset.models[0] ?? "";
  step.value = 2;
}

const testMessage = computed(() => {
  const result = testResult.value;
  if (!result) return "";
  if (result.ok) return result.note ?? `连通成功（${result.latency}ms）`;
  return result.error ?? "";
});

// P1-5：真实拉取 GET /api/v1/llm/providers。
// 后端不可达时**不回退本地示例数据**（那会让 4 条假配置看起来像真实接入的
// 模型，与 IntegrationView 的同款修复口径一致）——显示空态 + 错误提示。
async function loadProviders() {
  loading.value = true;
  try {
    const res = await llmApi.list();
    providers.value = res.providers.map(fromDTO);
  } catch (err) {
    if (err instanceof ApiError && err.isNetworkError) {
      toast.error("无法连接后端服务，模型列表不可用。请确认 mindops-backend 已在 8010 端口启动。");
    } else {
      toast.error(err instanceof ApiError ? err.message : "加载模型列表失败");
    }
    providers.value = [];
  } finally {
    loading.value = false;
  }
}

onMounted(() => {
  void loadProviders();
  // GET /llm/presets（admin-only）。失败时保留空列表而不是回退 mock：
  // 预设里的 base_url 决定 API 调哪个厂商，错误的地址会让"连通测试"永远失败，
  // 宁可不给选项，也不能给错的。
  llmApi
    .presets()
    .then((res) => (presets.value = res.presets))
    .catch(() => (presets.value = []));
});

function openAddDialog() {
  editProvider.value = null;
  selectedPreset.value = null;
  formData.value = { name: "", api_key: "", base_url: "", model_name: "", role: "chat" };
  step.value = 1;
  testResult.value = null;
  dialogOpen.value = true;
}

function openEditDialog(p: Provider) {
  editProvider.value = p;
  selectedPreset.value = presets.value.find((ps) => ps.provider_type === p.provider_type) || null;
  formData.value = {
    name: p.name,
    api_key: "",
    base_url: p.base_url,
    model_name: p.model_name,
    // embedding 类模型不在这张表单里改绑；打开编辑时先落到 chat，
    // 只有用户主动改角色才会把 embedding 覆盖掉。
    role: p.role === "reasoning" ? "reasoning" : "chat",
  };
  step.value = 2;
  testResult.value = null;
  dialogOpen.value = true;
}

// 保存前测试走 POST /llm/test（不落库）；编辑已有模型且没填新 Key 时，
// 走已保存凭据的 /providers/{id}/test。
async function handleTest() {
  const providerType =
    selectedPreset.value?.provider_type ?? editProvider.value?.provider_type ?? "custom";
  const baseUrl = (formData.value.base_url || selectedPreset.value?.base_url || "").trim();
  const modelName = formData.value.model_name.trim();
  if (!baseUrl) {
    toast.error("请填写 Base URL");
    return;
  }
  if (!modelName) {
    toast.error("请填写 Model Name");
    return;
  }
  if (
    !editProvider.value &&
    !formData.value.api_key &&
    providerType !== "ollama" &&
    providerType !== "custom"
  ) {
    toast.error("请填写 API Key");
    return;
  }

  testing.value = true;
  testResult.value = null;
  try {
    if (editProvider.value && !formData.value.api_key) {
      const res = await llmApi.test(editProvider.value.id);
      const stub = res.actual_model === "system-stub";
      testResult.value = {
        ok: res.status === "success",
        latency: res.latency_ms,
        note: stub ? "系统内置模型（桩），无需外部连通" : undefined,
      };
    } else {
      const res = await llmApi.testDraft({
        provider_type: providerType,
        base_url: baseUrl,
        model_name: modelName,
        api_key: formData.value.api_key,
        role: formData.value.role,
      });
      testResult.value = { ok: res.status === "success", latency: res.latency_ms };
    }
  } catch (err) {
    testResult.value = {
      ok: false,
      error: err instanceof ApiError ? err.message : "连通测试失败",
    };
  } finally {
    testing.value = false;
  }
}

// P1-4：原实现是 no-op（只关弹窗 + 假 loading 后把 mock 原样塞回），
// 用户填的模型名/Key/地址从未真正保存。现在真实落库并回写 state。
async function handleSave() {
  if (!formData.value.name.trim()) {
    toast.error("请填写模型名称");
    return;
  }
  if (!editProvider.value && !formData.value.api_key) {
    toast.error("请填写 API Key");
    return;
  }

  saving.value = true;
  try {
    if (editProvider.value) {
      const patch: {
        name: string;
        model_name: string;
        base_url: string;
        role: "chat" | "reasoning";
        api_key?: string;
      } = {
        name: formData.value.name,
        model_name: formData.value.model_name,
        base_url: formData.value.base_url,
        role: formData.value.role,
      };
      if (formData.value.api_key) patch.api_key = formData.value.api_key; // 留空 = 不更换
      const updated = await llmApi.update(editProvider.value.id, patch);
      const row = fromDTO(updated);
      providers.value = providers.value.map((p) => (p.id === row.id ? row : p));
      toast.success("已保存");
    } else {
      const created = await llmApi.create({
        name: formData.value.name,
        provider_type: selectedPreset.value?.provider_type ?? "custom",
        base_url: formData.value.base_url || selectedPreset.value?.base_url || "",
        model_name: formData.value.model_name,
        api_key: formData.value.api_key,
        role: formData.value.role,
      });
      providers.value = [...providers.value, fromDTO(created)];
      toast.success("已接入模型");
    }
    dialogOpen.value = false;
  } catch (err) {
    if (err instanceof ApiError && err.isNetworkError) {
      // 后端不可达时不往列表里塞假数据：那会让用户以为模型已接入。
      // 弹窗保持打开，表单内容不丢，等后端恢复后可直接重试。
      toast.error("无法连接后端服务，配置未保存。请确认 mindops-backend 已在 8010 端口启动。");
    } else {
      toast.error(err instanceof ApiError ? err.message : "保存失败");
    }
  } finally {
    saving.value = false;
  }
}

// P1-5：真实设默认 PUT /llm/providers/{id}（后端无独立 /default 端点）
async function handleSetDefault(p: Provider) {
  const prev = providers.value;
  providers.value = providers.value.map((pp) => ({
    ...pp,
    is_default: pp.role === p.role ? pp.id === p.id : pp.is_default,
  }));
  try {
    const updated = await llmApi.update(p.id, { is_default: true });
    providers.value = providers.value.map((pp) =>
      pp.id === p.id
        ? fromDTO(updated)
        : { ...pp, is_default: pp.role === p.role ? false : pp.is_default },
    );
    toast.success("已设为默认模型");
  } catch (err) {
    providers.value = prev; // 回滚
    toast.error(err instanceof ApiError ? err.message : "设置默认失败");
  }
}

// P1-5：真实删除 DELETE /llm/providers/{id}
async function handleDelete(p: Provider) {
  if (p.is_default) return;
  const prev = providers.value;
  providers.value = providers.value.filter((pp) => pp.id !== p.id);
  try {
    await llmApi.remove(p.id);
    toast.success("已删除");
  } catch (err) {
    providers.value = prev; // 回滚
    toast.error(err instanceof ApiError ? err.message : "删除失败");
  }
}
</script>

<template>
  <div class="space-y-4">
    <div class="flex items-center justify-between">
      <div>
        <h2 class="text-[20px] font-semibold heading-tight">模型设置</h2>
        <p class="text-[13px] text-muted-foreground mt-1">管理接入的大语言模型，绑定角色与默认模型</p>
      </div>
      <button
        type="button"
        class="inline-flex items-center gap-1.5 px-4 py-2 rounded-full bg-primary text-primary-foreground text-[13px] font-medium btn-pill hover:bg-primary/90 transition-colors"
        @click="openAddDialog"
      >
        <Plus :size="16" />
        接入新模型
      </button>
    </div>

    <!-- 模型列表 -->
    <div v-if="loading" class="grid md:grid-cols-2 gap-4">
      <div v-for="i in 4" :key="i" class="glass rounded-xl p-5 h-40 animate-pulse" />
    </div>
    <div v-else-if="providers.length === 0" class="glass rounded-xl py-16 flex flex-col items-center gap-3">
      <div class="w-12 h-12 rounded-full bg-secondary flex items-center justify-center">
        <Zap :size="24" class="text-muted-foreground" />
      </div>
      <div class="text-[15px] font-medium">还没有接入任何模型</div>
      <button
        type="button"
        class="mt-2 px-4 py-2 rounded-full bg-primary text-primary-foreground text-[13px] font-medium btn-pill hover:bg-primary/90 transition-colors"
        @click="openAddDialog"
      >
        接入第一个模型
      </button>
    </div>
    <div v-else class="grid md:grid-cols-2 gap-4">
      <div v-for="p in providers" :key="p.id" class="glass rounded-xl p-5">
        <div class="flex items-start justify-between mb-3">
          <div>
            <div class="flex items-center gap-2">
              <h3 class="text-[15px] font-semibold">{{ p.name }}</h3>
              <span
                v-if="p.is_default"
                class="inline-flex items-center gap-1 px-1.5 py-0.5 rounded-full bg-accent/10 text-accent text-[11px] font-medium"
              >
                <Star :size="10" />
                默认
              </span>
            </div>
            <div class="flex items-center gap-2 mt-1">
              <span class="text-[11px] font-mono px-1.5 py-0.5 rounded bg-secondary uppercase">
                {{ p.provider_type }}
              </span>
              <span class="text-[11px] text-muted-foreground">{{ ROLE_LABELS[p.role] }}</span>
            </div>
          </div>
          <!-- 状态灯 -->
          <div class="flex items-center gap-1.5">
            <span
              class="w-2 h-2 rounded-full"
              :class="
                p.status === 'connected'
                  ? 'bg-success'
                  : p.status === 'untested'
                    ? 'bg-muted-foreground/40'
                    : 'bg-danger'
              "
            />
            <span class="text-[11px] text-muted-foreground">
              {{ p.status === "connected" ? "连通" : p.status === "untested" ? "未测试" : "异常" }}
            </span>
            <span v-if="p.latency_ms > 0" class="text-[11px] font-mono text-muted-foreground">
              {{ p.latency_ms }}ms
            </span>
          </div>
        </div>

        <!-- 脱敏 Key -->
        <div class="text-[12px] font-mono text-muted-foreground mb-3 px-2 py-1 rounded bg-secondary/60">
          {{ p.api_key_masked }}
        </div>

        <!-- 操作按钮 -->
        <div class="flex items-center gap-2">
          <button
            type="button"
            :disabled="p.is_default"
            class="px-3 py-1.5 rounded-full border border-border bg-card text-[12px] font-medium hover:bg-secondary transition-colors btn-pill disabled:opacity-40 disabled:cursor-not-allowed"
            @click="handleSetDefault(p)"
          >
            设为默认
          </button>
          <button
            type="button"
            class="inline-flex items-center gap-1 px-3 py-1.5 rounded-full border border-border bg-card text-[12px] font-medium hover:bg-secondary transition-colors btn-pill"
            @click="openEditDialog(p)"
          >
            <Edit :size="12" />
            编辑
          </button>
          <button
            type="button"
            :disabled="p.is_default"
            :title="p.is_default ? '默认模型不可删除' : ''"
            class="inline-flex items-center gap-1 px-3 py-1.5 rounded-full border border-danger/30 text-danger text-[12px] font-medium hover:bg-danger/5 transition-colors btn-pill disabled:opacity-40 disabled:cursor-not-allowed"
            @click="handleDelete(p)"
          >
            <Trash2 :size="12" />
            删除
          </button>
        </div>
      </div>
    </div>

    <!-- 接入/编辑弹窗 -->
    <div
      v-if="dialogOpen"
      class="fixed inset-0 z-50 flex items-center justify-center bg-black/20 backdrop-blur-sm"
      @click.self="dialogOpen = false"
    >
      <div class="glass-strong rounded-xl shadow-lg p-6 w-full max-w-[560px] m-4 max-h-[90vh] overflow-y-auto">
        <div class="flex items-center justify-between mb-4">
          <h3 class="text-[16px] font-semibold">
            {{ editProvider ? "编辑模型" : "接入新模型" }}
          </h3>
          <span class="text-[12px] text-muted-foreground">步骤 {{ step }}/2</span>
        </div>

        <!-- 第一步：选厂商预设 -->
        <div v-if="step === 1 && !editProvider" class="space-y-3">
          <p class="text-[13px] text-muted-foreground mb-3">选择厂商预设模板</p>
          <div
            v-if="presets.length === 0"
            class="rounded-lg border border-border bg-secondary/40 p-4 text-[13px] text-muted-foreground"
          >
            暂无可用预设。该接口仅管理员可见，若已用管理员登录仍为空， 请检查后端服务是否正常。
          </div>
          <div v-else class="grid grid-cols-1 gap-2">
            <button
              v-for="preset in presets"
              :key="preset.provider_type"
              type="button"
              class="flex items-center gap-3 p-3 rounded-lg border text-left transition-colors"
              :class="
                selectedPreset?.provider_type === preset.provider_type
                  ? 'border-accent bg-accent/5'
                  : 'border-border bg-card hover:bg-secondary/40'
              "
              @click="selectPreset(preset)"
            >
              <div class="flex-1">
                <div class="text-[14px] font-medium">{{ preset.label }}</div>
                <div class="text-[12px] text-muted-foreground break-all">
                  {{ presetModelSummary(preset) }}
                </div>
                <div class="text-[11px] font-mono text-muted-foreground mt-1 break-all">
                  {{ preset.base_url }}
                </div>
              </div>
            </button>
          </div>
        </div>

        <!-- 第二步：填写表单 -->
        <div v-if="step === 2" class="space-y-3">
          <div>
            <label class="block text-[13px] font-medium mb-1.5">名称</label>
            <input
              type="text"
              :value="formData.name"
              placeholder="给模型起个名字"
              class="w-full px-3 py-2 rounded-lg border border-border bg-card text-[13px] focus:outline-none focus:ring-2 focus:ring-accent/30"
              @input="onTextInput('name', $event)"
            />
          </div>

          <div>
            <label class="block text-[13px] font-medium mb-1.5">
              API Key
              <span v-if="editProvider" class="text-muted-foreground text-[11px]">（留空表示不更换）</span>
            </label>
            <div class="relative">
              <input
                :type="showKey ? 'text' : 'password'"
                :value="formData.api_key"
                :placeholder="editProvider ? '留空表示不更换' : '请输入 API Key'"
                class="w-full px-3 py-2 pr-10 rounded-lg border border-border bg-card text-[13px] font-mono focus:outline-none focus:ring-2 focus:ring-accent/30"
                @input="onTextInput('api_key', $event)"
              />
              <button
                type="button"
                class="absolute right-3 top-1/2 -translate-y-1/2 text-muted-foreground hover:text-foreground"
                @click="showKey = !showKey"
              >
                <EyeOff v-if="showKey" :size="14" />
                <Eye v-else :size="14" />
              </button>
            </div>
          </div>

          <div>
            <label class="block text-[13px] font-medium mb-1.5">Base URL</label>
            <input
              type="text"
              :value="formData.base_url"
              class="w-full px-3 py-2 rounded-lg border border-border bg-card text-[13px] font-mono focus:outline-none focus:ring-2 focus:ring-accent/30"
              @input="onTextInput('base_url', $event)"
            />
          </div>

          <div class="grid grid-cols-2 gap-3">
            <div>
              <label class="block text-[13px] font-medium mb-1.5">Model Name</label>
              <input
                type="text"
                :value="formData.model_name"
                class="w-full px-3 py-2 rounded-lg border border-border bg-card text-[13px] font-mono focus:outline-none focus:ring-2 focus:ring-accent/30"
                @input="onTextInput('model_name', $event)"
              />
              <p
                v-if="selectedPreset?.model_labels?.[formData.model_name]"
                class="mt-1 text-[11px] text-muted-foreground"
              >
                对应版本：{{ selectedPreset?.model_labels?.[formData.model_name] }}
              </p>
            </div>
            <div>
              <label class="block text-[13px] font-medium mb-1.5">绑定角色</label>
              <select
                :value="formData.role"
                class="w-full px-3 py-2 rounded-lg border border-border bg-card text-[13px] focus:outline-none focus:ring-2 focus:ring-accent/30 cursor-pointer"
                @change="onRoleChange"
              >
                <option value="chat">通用对话（chat）</option>
                <option value="reasoning">推理增强（reasoning）</option>
              </select>
            </div>
          </div>

          <!-- 测试连通 -->
          <div class="pt-2 border-t border-border">
            <button
              type="button"
              :disabled="testing || (!editProvider && !formData.api_key)"
              :title="editProvider ? '编辑时留空 Key 将用已保存凭据测试' : ''"
              class="inline-flex items-center gap-1.5 px-4 py-2 rounded-full border border-border bg-card text-[13px] font-medium hover:bg-secondary transition-colors btn-pill disabled:opacity-40 disabled:cursor-not-allowed"
              @click="handleTest"
            >
              <Loader2 v-if="testing" :size="14" class="animate-spin" />
              <Zap v-else :size="14" />
              {{ testing ? "正在测试连通性..." : "测试连通" }}
            </button>

            <div
              v-if="testResult"
              class="mt-2 inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[12px] fade-in-300"
              :class="testResult.ok ? 'bg-success/10 text-success' : 'bg-danger/10 text-danger'"
            >
              <CheckCircle2 v-if="testResult.ok" :size="12" />
              <XCircle v-else :size="12" />
              {{ testMessage }}
            </div>
          </div>

          <!-- 底部按钮 -->
          <div class="flex justify-between gap-2 pt-3 border-t border-border">
            <button
              v-if="!editProvider"
              type="button"
              class="px-4 py-2 rounded-full border border-border bg-card text-[13px] font-medium hover:bg-secondary transition-colors btn-pill"
              @click="step = 1"
            >
              上一步
            </button>
            <div class="flex gap-2 ml-auto">
              <button
                type="button"
                class="px-4 py-2 rounded-full border border-border bg-card text-[13px] font-medium hover:bg-secondary transition-colors btn-pill"
                @click="dialogOpen = false"
              >
                取消
              </button>
              <button
                type="button"
                :disabled="!testResult?.ok && !editProvider"
                :title="!testResult?.ok && !editProvider ? '请先测试连通' : ''"
                class="px-4 py-2 rounded-full bg-primary text-primary-foreground text-[13px] font-medium btn-pill hover:bg-primary/90 transition-colors disabled:opacity-40 disabled:cursor-not-allowed"
                @click="handleSave"
              >
                保存
              </button>
            </div>
          </div>
        </div>
      </div>
    </div>
  </div>
</template>
