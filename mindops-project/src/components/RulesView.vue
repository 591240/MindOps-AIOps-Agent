<script setup lang="ts">
import { computed, onMounted, reactive, ref } from "vue";
import {
  Plus,
  Pencil,
  Trash2,
  Play,
  ScrollText,
  FileCog,
  Loader2,
  Workflow,
  ListChecks,
} from "lucide-vue-next";
import { rulesApi, type PatternRule } from "@/api/rules";
import { ApiError } from "@/api/client";
import { readSession } from "@/lib/session";
import Button from "@/components/ui/Button.vue";
import AppDialog from "@/components/ui/AppDialog.vue";

/**
 * 规则库管理页（admin-only）：pattern_rules.yaml 的可视化读写。
 *
 * 事实源仍是 YAML 文件——本页的每次保存都是「后端校验 → 原子写回」，
 * Triage 按 mtime+digest 热加载，保存即生效，无需重启。
 * 命中匹配不在前端模拟：一律调 /rules/test（与诊断链路同一份 match_pattern），
 * 避免前端复刻归一化逻辑造成「页面说命中、线上不命中」的分裂。
 */

const isAdmin = readSession()?.user.role === "admin";

const loading = ref(true);
const rules = ref<PatternRule[]>([]);
const loadError = ref<string | null>(null);

// ---------- 命中测试器 ----------
const testText = ref("");
const testComponent = ref("");
const testing = ref(false);
const testResult = ref<{ matched: boolean; rule: { name: string; root_cause: string } | null } | null>(
  null,
);

const COMPONENT_OPTIONS = [
  { value: "*", label: "通配（所有组件）" },
  { value: "redis", label: "Redis" },
  { value: "mysql", label: "MySQL" },
  { value: "nginx", label: "Nginx" },
  { value: "docker", label: "Docker" },
  { value: "k8s", label: "Kubernetes" },
  { value: "host", label: "宿主机" },
  { value: "order-service", label: "订单服务" },
  { value: "order-api", label: "订单 API" },
] as const;

async function loadRules() {
  loading.value = true;
  loadError.value = null;
  try {
    const data = await rulesApi.list();
    rules.value = data.rules;
  } catch (err) {
    loadError.value =
      err instanceof ApiError && err.isNetworkError
        ? "无法连接后端服务，请确认 mindops-backend 已在 8010 端口启动。"
        : err instanceof Error
          ? err.message
          : "加载规则失败";
  } finally {
    loading.value = false;
  }
}

onMounted(() => void loadRules());

async function handleTest() {
  const text = testText.value.trim();
  if (!text) return;
  testing.value = true;
  testResult.value = null;
  try {
    const data = await rulesApi.test(text, testComponent.value || undefined);
    testResult.value = { matched: data.matched, rule: data.rule };
  } catch (err) {
    testResult.value = {
      matched: false,
      rule: { name: "测试失败", root_cause: err instanceof Error ? err.message : String(err) },
    };
  } finally {
    testing.value = false;
  }
}

// ---------- 编辑器 ----------
const editorOpen = ref(false);
const saving = ref(false);
const editingName = ref<string | null>(null); // null = 新建
const saveError = ref<string | null>(null);

const form = reactive({
  name: "",
  component: "*",
  keywords: "",
  aliases: "",
  root_cause: "",
  // 编辑态的步骤：risk_level 为空串表示「自动判定」（提交时省略该字段）
  playbook: [] as { command: string; description: string; risk_level: string }[],
});

const isEditing = computed(() => editingName.value !== null);

function splitCsv(raw: string): string[] {
  return raw
    .split(/[,，]/)
    .map((s) => s.trim())
    .filter(Boolean);
}

function openCreate() {
  editingName.value = null;
  saveError.value = null;
  Object.assign(form, {
    name: "",
    component: "*",
    keywords: "",
    aliases: "",
    root_cause: "",
    // 默认给一行空步骤：处置剧本的输入位直接可见，不必先点「加一步」；
    // buildPayload 会过滤掉 command 为空的行，空行不会被保存
    playbook: [{ command: "", description: "", risk_level: "" }],
  });
  editorOpen.value = true;
}

function openEdit(rule: PatternRule) {
  editingName.value = rule.name;
  saveError.value = null;
  Object.assign(form, {
    name: rule.name,
    component: rule.component,
    keywords: rule.keywords.join("，"),
    aliases: (rule.aliases ?? []).join("，"),
    root_cause: rule.root_cause,
    playbook:
      rule.playbook && rule.playbook.length > 0
        ? rule.playbook.map((s) => ({ ...s, risk_level: s.risk_level ?? "" }))
        : [{ command: "", description: "", risk_level: "" }],
  });
  editorOpen.value = true;
}

function addStep() {
  if (form.playbook.length < 8)
    form.playbook.push({ command: "", description: "", risk_level: "" });
}

function removeStep(idx: number) {
  form.playbook.splice(idx, 1);
}

const RISK_LEVELS = ["low", "medium", "high"] as const;

function buildPayload() {
  return {
    name: form.name.trim(),
    component: form.component,
    keywords: splitCsv(form.keywords),
    aliases: splitCsv(form.aliases),
    root_cause: form.root_cause.trim(),
    playbook: form.playbook
      .map((s) => ({
        command: s.command.trim(),
        description: s.description.trim(),
        risk_level: (RISK_LEVELS as readonly string[]).includes(s.risk_level)
          ? (s.risk_level as (typeof RISK_LEVELS)[number])
          : undefined,
      }))
      .filter((s) => s.command),
  };
}

async function handleSave() {
  saveError.value = null;
  // 前端先挡一道最常见的错，真正的完整校验在后端（422 会带中文说明）
  if (splitCsv(form.keywords).length < 2) {
    saveError.value = "关键词至少 2 个";
    return;
  }
  if (!form.root_cause.trim()) {
    saveError.value = "根因结论不能为空";
    return;
  }
  saving.value = true;
  try {
    if (isEditing.value) {
      await rulesApi.update(editingName.value as string, buildPayload());
    } else {
      await rulesApi.create(buildPayload());
    }
    editorOpen.value = false;
    await loadRules();
  } catch (err) {
    saveError.value = err instanceof Error ? err.message : "保存失败";
  } finally {
    saving.value = false;
  }
}

async function handleDelete(rule: PatternRule) {
  if (!window.confirm(`删除规则「${rule.name}」？删除后同类告警将不再短路，改走 LLM 诊断。`)) return;
  try {
    await rulesApi.remove(rule.name);
    await loadRules();
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : String(err);
  }
}

// ---------- 分组折叠 ----------
const expandedGroups = ref<Set<string>>(new Set());
const expandedRules = ref<Set<string>>(new Set());

const COMPONENT_LABELS: Record<string, string> = Object.fromEntries(
  COMPONENT_OPTIONS.map((c) => [c.value, c.label]),
);

/** 按组件分组；通配规则单独成组排在最后 */
const ruleGroups = computed(() => {
  const wildcard = rules.value.filter((r) => r.component === "*");
  const byComponent = new Map<string, PatternRule[]>();
  for (const rule of rules.value) {
    if (rule.component === "*") continue;
    const list = byComponent.get(rule.component) ?? [];
    list.push(rule);
    byComponent.set(rule.component, list);
  }
  const groups = [...byComponent.entries()].map(([component, items]) => ({
    key: component,
    label: COMPONENT_LABELS[component] ?? component,
    items,
  }));
  if (wildcard.length > 0) {
    groups.push({ key: "*", label: "通配（所有组件）", items: wildcard });
  }
  return groups;
});

function toggleGroup(key: string) {
  const next = new Set(expandedGroups.value);
  if (next.has(key)) next.delete(key);
  else next.add(key);
  expandedGroups.value = next;
}

function toggleRule(name: string) {
  const next = new Set(expandedRules.value);
  if (next.has(name)) next.delete(name);
  else next.add(name);
  expandedRules.value = next;
}
</script>

<template>
  <div class="space-y-4">
    <!-- 页头 -->
    <div class="flex items-start justify-between gap-4">
      <div>
        <h2 class="text-[20px] font-semibold heading-tight">规则库</h2>
        <p class="text-[13px] text-muted-foreground mt-1">
          规则命中 → 直接出结论（置信度 0.95）并短路 LLM 诊断；保存后写回 YAML，即时生效
        </p>
      </div>
      <Button
        v-if="isAdmin"
        class="h-9 px-4 rounded-full bg-primary text-primary-foreground text-[13px] font-medium btn-pill hover:bg-primary/90"
        @click="openCreate"
      >
        <Plus :size="14" class="mr-1" />
        新建规则
      </Button>
    </div>

    <!-- 命中测试器 -->
    <div class="bg-white rounded-xl border border-border p-5">
      <div class="flex items-center gap-2 mb-3">
        <FlaskConical :size="15" class="text-muted-foreground" />
        <p class="text-[13.5px] font-medium">命中测试</p>
        <span class="text-[12px] text-muted-foreground">粘一段告警标题，看会被哪条规则短路</span>
      </div>
      <div class="flex items-center gap-3 flex-wrap">
        <input
          v-model="testText"
          type="text"
          placeholder="例如：Redis 内存溢出，OOM command not allowed"
          class="flex-1 min-w-[240px] px-3 py-2 rounded-lg border border-border bg-card text-[13px] focus:outline-none focus:ring-2 focus:ring-accent/30"
          @keydown.enter="handleTest"
        />
        <select
          v-model="testComponent"
          class="px-3 py-2 rounded-lg border border-border bg-card text-[13px] focus:outline-none focus:ring-2 focus:ring-accent/30 cursor-pointer"
        >
          <option value="">不限组件</option>
          <option v-for="c in COMPONENT_OPTIONS.filter((c) => c.value !== '*')" :key="c.value" :value="c.value">
            {{ c.label }}
          </option>
        </select>
        <Button
          variant="outline"
          class="rounded-full h-9 px-4 text-[13px]"
          :disabled="!testText.trim() || testing"
          @click="handleTest"
        >
          <Play :size="13" class="mr-1" />
          测试
        </Button>
      </div>
      <div
        v-if="testResult"
        class="mt-3 rounded-lg p-3 text-[13px]"
        :class="
          testResult.matched
            ? 'bg-success/5 border border-success/20 text-foreground'
            : 'bg-secondary/50 border border-border text-muted-foreground'
        "
      >
        <template v-if="testResult.matched && testResult.rule">
          <span class="font-medium text-success">命中</span>
          <span class="font-mono ml-2">{{ testResult.rule.name }}</span>
          <span class="mx-2 text-muted-foreground">→</span>
          <span>{{ testResult.rule.root_cause }}</span>
        </template>
        <template v-else>未命中任何规则，该告警将进入 LLM 诊断链路</template>
      </div>
    </div>

    <!-- 加载失败 -->
    <div
      v-if="loadError"
      class="rounded-xl border border-glow-pink/30 bg-danger/5 p-4 text-[13px] text-danger flex items-center justify-between gap-3"
    >
      <span class="flex-1">{{ loadError }}</span>
      <Button variant="outline" size="sm" @click="loadRules">重试</Button>
    </div>

    <!-- 规则列表：按技术栈分组、组与条目均可折叠 -->
    <div v-if="loading" class="bg-white rounded-xl border border-border p-5 space-y-4">
      <div v-for="i in 4" :key="i" class="space-y-2">
        <div class="h-4 bg-secondary rounded animate-pulse w-48" />
        <div class="h-3 bg-secondary rounded animate-pulse w-full max-w-lg" />
      </div>
    </div>

    <div v-else-if="rules.length === 0" class="bg-white rounded-xl border border-border py-16 flex flex-col items-center gap-3">
      <div class="w-12 h-12 rounded-full bg-secondary flex items-center justify-center">
        <ScrollText :size="24" class="text-muted-foreground" />
      </div>
      <div class="text-[15px] font-medium">规则库为空</div>
      <div class="text-[13px] text-muted-foreground">命中类告警将全部走 LLM 诊断</div>
    </div>

    <div v-else class="space-y-3">
      <div
        v-for="group in ruleGroups"
        :key="group.key"
        class="bg-white rounded-xl border border-border overflow-hidden"
      >
        <!-- 组头：技术栈 + 数量，点击展开/收起 -->
        <button
          type="button"
          class="w-full flex items-center gap-2.5 px-5 py-3.5 text-left transition-colors hover:bg-secondary/40"
          @click="toggleGroup(group.key)"
        >
          <ChevronRight
            :size="15"
            class="text-muted-foreground transition-transform"
            :class="expandedGroups.has(group.key) ? 'rotate-90' : ''"
          />
          <span class="text-[14px] font-medium">{{ group.label }}</span>
          <span
            class="text-[11px] font-mono tabular-nums px-2 py-0.5 rounded-full border border-border bg-secondary/60 text-muted-foreground"
          >
            {{ group.items.length }} 条
          </span>
          <span
            v-if="expandedGroups.has(group.key)"
            class="text-[11px] text-muted-foreground ml-auto"
          >
            点击条目查看关键词与剧本
          </span>
          <span v-else class="text-[11px] text-muted-foreground/60 ml-auto">
            {{ group.items.map((r) => r.name).join(" · ") }}
          </span>
        </button>

        <!-- 组内条目：折叠行，点开看关键词/剧本 -->
        <div v-if="expandedGroups.has(group.key)" class="divide-y divide-border border-t border-border">
          <div v-for="rule in group.items" :key="rule.name">
            <button
              type="button"
              class="w-full flex items-center gap-2.5 px-5 py-3 text-left transition-colors hover:bg-secondary/30"
              @click="toggleRule(rule.name)"
            >
              <ChevronRight
                :size="14"
                class="text-muted-foreground/70 transition-transform shrink-0"
                :class="expandedRules.has(rule.name) ? 'rotate-90' : ''"
              />
              <span class="font-mono text-[13px] font-medium shrink-0">{{ rule.name }}</span>
              <span class="text-[12.5px] text-muted-foreground truncate flex-1 text-left">{{
                rule.root_cause
              }}</span>
              <span
                class="shrink-0 text-[11px] font-mono tabular-nums text-muted-foreground px-2 py-0.5 rounded-full border border-border"
              >
                关键词 {{ rule.keywords.length }}
              </span>
              <span
                v-if="rule.playbook && rule.playbook.length > 0"
                class="shrink-0 inline-flex items-center gap-1 text-[11px] font-mono tabular-nums px-2 py-0.5 rounded-full border border-glow-teal/40 bg-glow-teal/10 text-[#0f6e56]"
              >
                <Workflow :size="11" />
                剧本 {{ rule.playbook.length }} 步
              </span>
            </button>

            <!-- 展开区：关键词 + 剧本 + 编辑入口 -->
            <div v-if="expandedRules.has(rule.name)" class="px-5 pb-4 pl-12">
              <div class="flex items-center gap-1.5 flex-wrap">
                <span class="text-[11px] text-muted-foreground">关键词</span>
                <span
                  v-for="k in rule.keywords"
                  :key="k"
                  class="text-[11px] font-mono px-1.5 py-0.5 rounded bg-secondary/70 text-muted-foreground"
                >
                  {{ k }}
                </span>
                <template v-if="(rule.aliases ?? []).length > 0">
                  <span class="text-[11px] text-muted-foreground ml-1">别名</span>
                  <span
                    v-for="a in rule.aliases"
                    :key="`a-${a}`"
                    class="text-[11px] px-1.5 py-0.5 rounded bg-secondary/40 text-muted-foreground/80"
                  >
                    {{ a }}
                  </span>
                </template>
              </div>
              <div
                v-if="rule.playbook && rule.playbook.length > 0"
                class="mt-3 rounded-lg bg-secondary/40 p-3 space-y-1.5"
              >
                <div class="flex items-center gap-1.5 text-[11px] text-muted-foreground mb-1">
                  <ListChecks :size="12" />
                  处置剧本（未标等级的按风险库判定；标「low」的总闸开启时自动执行；黑名单始终兜底）
                </div>
              <div
                v-for="(step, idx) in rule.playbook"
                :key="idx"
                class="flex items-start gap-2 text-[12px]"
              >
                <span class="font-mono text-muted-foreground shrink-0">{{ idx + 1 }}.</span>
                <code
                  class="font-mono text-[11.5px] bg-white border border-border rounded px-1.5 py-0.5"
                >
                  {{ step.command }}
                </code>
                <span class="text-muted-foreground truncate">{{ step.description }}</span>
                <span
                  v-if="step.risk_level"
                  class="shrink-0 text-[10.5px] px-1.5 py-0.5 rounded-full border font-mono"
                  :class="
                    step.risk_level === 'low'
                      ? 'border-success/40 bg-success/10 text-[#0f6e56]'
                      : step.risk_level === 'high'
                        ? 'border-danger/40 bg-danger/10 text-danger'
                        : 'border-border bg-secondary/60 text-muted-foreground'
                  "
                  title="手动指定的风险等级"
                >
                  {{ step.risk_level }}
                </span>
              </div>
              </div>
              <div v-if="isAdmin" class="mt-3 flex items-center gap-2">
                <Button variant="outline" size="sm" class="rounded-full h-8 text-[12px]" @click="openEdit(rule)">
                  <Pencil :size="12" class="mr-1" />
                  编辑
                </Button>
                <Button
                  variant="outline"
                  size="sm"
                  class="rounded-full h-8 text-[12px] text-danger hover:bg-red-50"
                  @click="handleDelete(rule)"
                >
                  <Trash2 :size="12" class="mr-1" />
                  删除
                </Button>
              </div>
            </div>
          </div>
        </div>
      </div>
    </div>

    <!-- 新建/编辑弹窗 -->
    <AppDialog
      :open="editorOpen"
      :title="isEditing ? `编辑规则 · ${editingName}` : '新建规则'"
      description="保存后写回规则文件，Triage 立即热加载，下一次诊断即生效"
      max-width="560px"
      @update:open="editorOpen = $event"
    >
      <div class="space-y-4">
        <div
          v-if="saveError"
          class="rounded-lg border border-glow-pink/30 bg-danger/5 p-3 text-[13px] text-danger"
        >
          {{ saveError }}
        </div>

        <div class="grid grid-cols-2 gap-3">
          <div>
            <label class="block text-[12.5px] font-medium mb-1.5">规则名</label>
            <input
              v-model="form.name"
              type="text"
              :disabled="isEditing"
              placeholder="如 redis_oom"
              class="w-full px-3 py-2 rounded-lg border border-border bg-card text-[13px] font-mono focus:outline-none focus:ring-2 focus:ring-accent/30 disabled:opacity-60 disabled:cursor-not-allowed"
            />
            <p class="text-[11px] text-muted-foreground mt-1">小写字母开头，仅小写字母/数字/下划线</p>
          </div>
          <div>
            <label class="block text-[12.5px] font-medium mb-1.5">所属组件</label>
            <select
              v-model="form.component"
              class="w-full px-3 py-2 rounded-lg border border-border bg-card text-[13px] focus:outline-none focus:ring-2 focus:ring-accent/30 cursor-pointer"
            >
              <option v-for="c in COMPONENT_OPTIONS" :key="c.value" :value="c.value">{{ c.label }}</option>
            </select>
          </div>
        </div>

        <div>
          <label class="block text-[12.5px] font-medium mb-1.5">
            关键词 <span class="text-muted-foreground font-normal">（逗号分隔，≥2 个，命中任一即触发）</span>
          </label>
          <input
            v-model="form.keywords"
            type="text"
            placeholder="如：OOM，内存溢出，maxmemory"
            class="w-full px-3 py-2 rounded-lg border border-border bg-card text-[13px] focus:outline-none focus:ring-2 focus:ring-accent/30"
          />
        </div>

        <div>
          <label class="block text-[12.5px] font-medium mb-1.5">
            别名 <span class="text-muted-foreground font-normal">（可选，同一现象的不同叫法）</span>
          </label>
          <input
            v-model="form.aliases"
            type="text"
            placeholder="如：redis_pool，连接池耗尽"
            class="w-full px-3 py-2 rounded-lg border border-border bg-card text-[13px] focus:outline-none focus:ring-2 focus:ring-accent/30"
          />
        </div>

        <div>
          <label class="block text-[12.5px] font-medium mb-1.5">
            根因结论 <span class="text-muted-foreground font-normal">（命中后作为诊断结论输出）</span>
          </label>
          <textarea
            v-model="form.root_cause"
            rows="3"
            placeholder="如：Redis 达到 maxmemory 上限且淘汰策略未能释放足够键空间，需排查大键或扩容。"
            class="w-full px-3 py-2 rounded-lg border border-border bg-card text-[13px] focus:outline-none focus:ring-2 focus:ring-accent/30 resize-none"
          />
        </div>

        <div>
          <div class="flex items-center justify-between mb-1.5">
            <label class="block text-[12.5px] font-medium">
              处置剧本
              <span class="text-muted-foreground font-normal">（可选，命中后逐步执行；命令仍受风险表管控）</span>
            </label>
            <button
              type="button"
              class="inline-flex items-center gap-1 text-[12px] text-accent hover:underline disabled:opacity-40"
              :disabled="form.playbook.length >= 8"
              @click="addStep"
            >
              <FileCog :size="13" />
              加一步（{{ form.playbook.length }}/8）
            </button>
          </div>
          <div v-if="form.playbook.length === 0" class="text-[12px] text-muted-foreground px-1">
            不配剧本时，命中规则只出结论，处置建议回退到内置模板。
          </div>
          <p v-else-if="form.playbook.length === 1 && !form.playbook[0].command" class="text-[11.5px] text-muted-foreground px-1 mb-1">
            填写命令即成为该规则的处置剧本；全部留空则只出结论，处置建议回退到内置模板。
          </p>
          <div v-else class="space-y-2">
            <div
              v-for="(step, idx) in form.playbook"
              :key="idx"
              class="flex items-center gap-2"
            >
              <span class="font-mono text-[12px] text-muted-foreground w-4 shrink-0">{{ idx + 1 }}.</span>
              <input
                v-model="step.command"
                type="text"
                placeholder="命令，如 redis-cli --bigkeys"
                class="flex-1 min-w-0 px-2.5 py-1.5 rounded-lg border border-border bg-card text-[12px] font-mono focus:outline-none focus:ring-2 focus:ring-accent/30"
              />
              <input
                v-model="step.description"
                type="text"
                placeholder="说明"
                class="w-[110px] shrink-0 px-2.5 py-1.5 rounded-lg border border-border bg-card text-[12px] focus:outline-none focus:ring-2 focus:ring-accent/30"
              />
              <select
                v-model="step.risk_level"
                title="手动指定风险等级；自动判定 = 按风险库规则识别"
                class="w-[86px] shrink-0 px-1.5 py-1.5 rounded-lg border border-border bg-card text-[12px] focus:outline-none focus:ring-2 focus:ring-accent/30 cursor-pointer"
              >
                <option value="">自动判定</option>
                <option value="low">低（自动执行）</option>
                <option value="medium">中</option>
                <option value="high">高（需确认）</option>
              </select>
              <button
                type="button"
                class="p-1.5 rounded-md text-muted-foreground hover:text-red-600 hover:bg-red-50 transition-colors shrink-0"
                title="删除此步"
                @click="removeStep(idx)"
              >
                <Trash2 :size="13" />
              </button>
            </div>
            <p class="text-[11px] text-muted-foreground px-1">
              标为「低」的命令在总闸开启时会被自动执行；无论怎么标，高危命令黑名单始终拦截。
            </p>
          </div>
        </div>
      </div>

      <template #footer>
        <Button variant="outline" class="rounded-full" :disabled="saving" @click="editorOpen = false">
          取消
        </Button>
        <Button
          class="rounded-full bg-primary text-primary-foreground hover:bg-primary/90"
          :disabled="saving"
          @click="handleSave"
        >
          <Loader2 v-if="saving" :size="14" class="animate-spin mr-1.5" />
          {{ isEditing ? "保存修改" : "创建规则" }}
        </Button>
      </template>
    </AppDialog>
  </div>
</template>
