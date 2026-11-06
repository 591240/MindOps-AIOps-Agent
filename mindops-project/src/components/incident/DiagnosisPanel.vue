<script setup lang="ts">
import { computed, onBeforeUnmount, ref } from "vue";
import CommandBlock from "@/components/business/CommandBlock.vue";
import ConfidenceBadge from "@/components/business/ConfidenceBadge.vue";
import AppDialog from "@/components/ui/AppDialog.vue";
import Button from "@/components/ui/Button.vue";
import Checkbox from "@/components/ui/Checkbox.vue";
import {
  AlertCircle,
  AlertTriangle,
  Archive,
  CheckCircle2,
  Edit3,
  FileText,
  Lightbulb,
  Loader2,
  MinusCircle,
  RefreshCw,
} from "lucide-vue-next";
import { toast } from "vue-sonner";
import { incidentsApi } from "@/api/incidents";
import { ApiError } from "@/api/client";
import type { IncidentActionVM, IncidentStatus, IncidentVM } from "@/types/incident";

const props = defineProps<{ incident: IncidentVM }>();
const emit = defineEmits<{ refresh: [] }>();

/**
 * 按事件状态给出「接下来该怎么处理」的引导（面向非运维读者）。
 * 置信度不足转人工（human_needed）时给出可照做的排查顺序，其余状态说明系统正在做什么。
 */
const NEXT_STEP_HINTS: Record<IncidentStatus, { title: string; steps: string[] }> = {
  new: {
    title: "事件已创建，系统即将自动诊断",
    steps: [
      "无 Redis 时告警聚簇有约 30 秒防抖延迟，本页会自动刷新；也可点下方「重新诊断」立即开始。",
    ],
  },
  diagnosing: {
    title: "智能体正在诊断",
    steps: ["正在收集证据并定位根因，完成后本页自动更新结论与处置命令。"],
  },
  human_needed: {
    title: "AI 置信度不足，已转人工，建议按顺序排查",
    steps: [
      "① 看变化点：服务最近是否发过版、改过配置——是的话先回滚止血",
      "② 看应用错误日志：堆栈第一行通常直接指向出错的代码或依赖",
      "③ 查下游依赖：数据库、缓存、外部接口是否可连",
      "④ 查不出来可先重启服务恢复，再留日志慢慢查",
      "⑤ 处理完后点下方「关闭事件」并填写实际原因——真实原因会沉淀进知识库，下次同类故障 AI 即可自动命中",
    ],
  },
  resolved: {
    title: "事件已解决",
    steps: ["如未补录真实根因，可在关闭前通过「补录」反馈实际原因与正确处置，帮助校准置信度。"],
  },
  closed: {
    title: "事件已关闭",
    steps: ["本事件不再跟进；历史结论与复盘内容已保留，可在知识库中检索。"],
  },
};

const confirmDialog = ref(false);
/** 本次弹窗要确认的 action；null + confirmAll=true 表示"确认全部 pending" */
const confirmAction = ref<string | null>(null);
const confirmAll = ref(false);
const acknowledged = ref(false);
const confirming = ref(false);
/** 本轮会话里已确认/驳回过的 action（乐观置灰，最终以重拉后的服务端状态为准） */
const executedActions = ref<Set<string>>(new Set());
/** 重诊断后的静默轮询 timer：卸载/再次触发时要清掉，避免对已卸载组件调 refresh */
let refreshTimers: number[] = [];

onBeforeUnmount(() => {
  refreshTimers.forEach((id) => window.clearTimeout(id));
  refreshTimers = [];
});

// ---- 事件操作：重诊断 / 关闭 / 补录 ----
const diagLoading = ref(false);
const closeDialog = ref(false);
const closeReason = ref("");
const closing = ref(false);
const fbDialog = ref(false);
const fbCause = ref("");
const fbActions = ref("");
const fbSaving = ref(false);

const pendingActions = computed(() => props.incident.actions.filter((a) => a.status === "pending"));
const isDiagnosable = computed(
  () => props.incident.status === "new" || props.incident.status === "human_needed",
);
const isClosable = computed(
  () => props.incident.status === "new" || props.incident.status === "human_needed",
);
const isResolved = computed(() => props.incident.status === "resolved");

const openDialog = (actionId: string | null) => {
  confirmAction.value = actionId;
  confirmAll.value = actionId === null;
  acknowledged.value = false;
  confirmDialog.value = true;
};

const handleExecute = (action: IncidentActionVM) => {
  if (action.status !== "pending" || executedActions.value.has(action.id)) return;
  // 后端对高危 action 强制人工确认；万一出现 requires_confirm=false 的 pending
  // （历史数据），也走同一个弹窗，宁可多一次确认不可少一次。
  openDialog(action.id);
};

const handleReject = async (action: IncidentActionVM) => {
  if (action.status !== "pending" || executedActions.value.has(action.id)) return;
  if (!window.confirm(`驳回处置命令？\n${action.command}`)) return;
  const ok = await confirmOnServer(action.id, false);
  if (ok) emit("refresh");
};

const confirmOnServer = async (actionId: string, approve: boolean): Promise<boolean> => {
  const numericId = Number(actionId);
  if (!Number.isInteger(numericId)) {
    // 后端动作主键是自增整数；非数字 id 说明数据契约已损坏，不能静默放过
    toast.error("处置动作 id 非法，请刷新页面后重试");
    return false;
  }
  confirming.value = true;
  try {
    const res = await incidentsApi.confirm(props.incident.id, {
      action_id: numericId,
      decision: approve ? "approve" : "reject",
    });
    if (res.recap_triggered) {
      toast.success("已确认全部处置，事件已解决，复盘知识已回灌");
    } else {
      toast.success(approve ? "已确认该处置命令" : "已驳回该处置命令");
    }
    executedActions.value = new Set([...executedActions.value, actionId]);
    return true;
  } catch (err) {
    toast.error(err instanceof ApiError ? err.message : "确认失败，请重试");
    return false;
  } finally {
    confirming.value = false;
  }
};

const handleConfirm = async () => {
  if (!acknowledged.value || confirming.value) return;
  let anyOk = false;
  if (confirmAll.value) {
    // 顺序确认全部 pending：一条失败即中止，剩余的由用户重试（后端按 action
    // 逐条落状态，不会因为部分失败产生脏数据）
    for (const action of pendingActions.value) {
      if (executedActions.value.has(action.id)) continue;
      const ok = await confirmOnServer(action.id, true);
      if (!ok) break;
      anyOk = true;
    }
  } else if (confirmAction.value) {
    anyOk = await confirmOnServer(confirmAction.value, true);
  }
  confirmDialog.value = false;
  confirmAction.value = null;
  confirmAll.value = false;
  acknowledged.value = false;
  if (anyOk) emit("refresh");
};

const handleDiagnose = async () => {
  if (diagLoading.value) return;
  diagLoading.value = true;
  try {
    // human_needed 是"转人工"事件，重跑必须显式 force（后端门禁如此）
    const res = await incidentsApi.diagnose(props.incident.id, {
      force: props.incident.status === "human_needed",
    });
    toast.success(`已触发重诊断（${res.run_id.slice(0, 14)}…），完成后自动刷新`);
    // 诊断异步执行（六步 + 可能的回环），安排几次静默轮询拉取新结果。
    // 先清掉上一轮的 timer，避免连点重诊断后堆积多次刷新。
    refreshTimers.forEach((id) => window.clearTimeout(id));
    refreshTimers = [5000, 12000, 25000].map((delay) =>
      window.setTimeout(() => emit("refresh"), delay),
    );
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      toast.warning("该事件已有诊断在运行中，请稍候");
    } else {
      toast.error(err instanceof ApiError ? err.message : "触发重诊断失败");
    }
  } finally {
    diagLoading.value = false;
  }
};

const handleClose = async () => {
  if (closing.value) return;
  closing.value = true;
  try {
    await incidentsApi.close(props.incident.id, { reason: closeReason.value.trim() || undefined });
    toast.success("事件已关闭");
    closeDialog.value = false;
    closeReason.value = "";
    emit("refresh");
  } catch (err) {
    if (err instanceof ApiError && err.status === 409) {
      toast.error("仍有待确认的处置命令，先逐条确认或驳回后再关闭");
    } else {
      toast.error(err instanceof ApiError ? err.message : "关闭失败");
    }
  } finally {
    closing.value = false;
  }
};

const handleFeedback = async () => {
  if (fbSaving.value) return;
  if (!fbCause.value.trim() && !fbActions.value.trim()) {
    toast.error("真实根因与正确处置至少填写一项");
    return;
  }
  fbSaving.value = true;
  try {
    await incidentsApi.feedback(props.incident.id, {
      actual_cause: fbCause.value.trim() || undefined,
      correct_actions: fbActions.value.trim() || undefined,
    });
    toast.success("已补录校准样本，置信度校准会更准");
    fbDialog.value = false;
    fbCause.value = "";
    fbActions.value = "";
    emit("refresh");
  } catch (err) {
    toast.error(err instanceof ApiError ? err.message : "补录失败");
  } finally {
    fbSaving.value = false;
  }
};

const allDone = computed(
  () => props.incident.actions.length > 0 && pendingActions.value.length === 0,
);
const hint = computed(() => NEXT_STEP_HINTS[props.incident.status]);
const confirmCommand = computed(
  () => props.incident.actions.find((a) => a.id === confirmAction.value)?.command || "",
);
const sourceLabel = computed(() =>
  props.incident.source === "rag" ? "RAG" : props.incident.source === "llm" ? "LLM" : "Rule",
);
</script>

<template>
  <div class="space-y-4">
    <!-- 根因 -->
    <div class="glass rounded-xl p-5">
      <div class="eyebrow mb-2">诊断结论</div>
      <p class="text-[15px] font-semibold leading-relaxed mb-3">{{ incident.root_cause }}</p>
      <div class="flex items-center gap-3 pt-2 border-t border-border">
        <div class="flex items-center gap-1.5">
          <span class="text-[12px] text-muted-foreground">置信度</span>
          <ConfidenceBadge :value="incident.confidence" />
        </div>
        <div class="flex items-center gap-1.5">
          <span class="text-[12px] text-muted-foreground">来源</span>
          <span class="text-[12px] font-mono px-2 py-0.5 rounded-full bg-secondary">
            {{ sourceLabel }}
          </span>
        </div>
      </div>
    </div>

    <!-- 处理建议（按状态给「接下来怎么做」的引导） -->
    <div v-if="hint" class="glass rounded-xl p-5">
      <div class="flex items-center gap-1.5 mb-2">
        <Lightbulb :size="13" class="text-warning" />
        <div class="eyebrow">处理建议</div>
      </div>
      <div class="text-[13px] font-medium mb-1.5">{{ hint.title }}</div>
      <ul class="space-y-1.5">
        <li
          v-for="(step, i) in hint.steps"
          :key="i"
          class="text-[12px] text-muted-foreground leading-relaxed"
        >
          {{ step }}
        </li>
      </ul>
    </div>

    <!-- 处置命令 -->
    <div class="glass rounded-xl p-5">
      <div class="flex items-center justify-between mb-3">
        <div class="eyebrow">处置命令</div>
        <span class="text-[12px] text-muted-foreground">
          {{
            pendingActions.length > 0
              ? `${pendingActions.length} 条待确认 / 共 ${incident.actions.length} 条`
              : `${incident.actions.length} 条`
          }}
        </span>
      </div>
      <div class="space-y-3">
        <div v-for="action in incident.actions" :key="action.id">
          <CommandBlock
            :command="action.command"
            :description="action.description"
            :risk="action.risk"
            :disabled="action.status !== 'pending' || executedActions.has(action.id)"
            @execute="handleExecute(action)"
            @reject="void handleReject(action)"
          />
          <!-- 分级自动处置回执：只有平台真正跑过/跳过的动作才显示 -->
          <div
            v-if="action.exec_status"
            class="mt-1.5 rounded-lg p-2.5 text-[12px]"
            :class="
              action.exec_status === 'auto_executed'
                ? 'bg-success/5 border border-success/20'
                : action.exec_status === 'exec_failed'
                  ? 'bg-danger/5 border border-danger/20'
                  : 'bg-secondary/50 border border-border'
            "
          >
            <div class="flex items-center gap-2">
              <template v-if="action.exec_status === 'auto_executed'">
                <CheckCircle2 :size="14" class="text-success shrink-0" />
                <span class="text-success font-medium">
                  已自动执行
                  <span class="font-mono tabular-nums ml-1">
                    退出码 {{ action.exec_exit_code }} · {{ action.exec_duration_ms }}ms
                  </span>
                </span>
              </template>
              <template v-else-if="action.exec_status === 'exec_failed'">
                <AlertCircle :size="14" class="text-danger shrink-0" />
                <span class="text-danger font-medium">
                  自动执行失败
                  <span class="font-mono tabular-nums ml-1">退出码 {{ action.exec_exit_code }}</span>
                </span>
              </template>
              <template v-else>
                <MinusCircle :size="14" class="text-muted-foreground shrink-0" />
                <span class="text-muted-foreground">已跳过自动执行</span>
              </template>
            </div>
            <details v-if="action.exec_output" class="mt-1.5">
              <summary class="text-[11px] text-muted-foreground cursor-pointer select-none">
                查看执行输出
              </summary>
              <pre
                class="mt-1.5 max-h-48 overflow-auto rounded-lg bg-secondary/60 p-2.5 font-mono text-[11px] leading-relaxed whitespace-pre-wrap break-all"
                >{{ action.exec_output }}</pre
              >
            </details>
          </div>
        </div>
      </div>
    </div>

    <!-- 相似案例 -->
    <div v-if="incident.similar_incidents.length > 0" class="glass rounded-xl p-5">
      <div class="eyebrow mb-3">相似案例</div>
      <div class="space-y-2">
        <div
          v-for="sim in incident.similar_incidents"
          :key="sim.id"
          class="flex items-center gap-2 px-3 py-2 rounded-lg border border-border bg-card hover:bg-secondary/40 cursor-pointer transition-colors"
        >
          <FileText :size="14" class="text-muted-foreground shrink-0" />
          <span class="text-[13px] flex-1 truncate">{{ sim.title }}</span>
          <span class="text-[11px] font-mono text-muted-foreground tabular-nums">{{ sim.time }}</span>
          <span class="text-[11px] font-mono px-1.5 py-0.5 rounded-full bg-accent/10 text-accent">
            {{ (sim.similarity * 100).toFixed(0) }}%
          </span>
        </div>
      </div>
    </div>

    <!-- 底部操作：处置确认 -->
    <div class="glass rounded-xl p-4">
      <button
        v-if="pendingActions.length > 0"
        @click="openDialog(null)"
        :disabled="confirming"
        class="w-full py-2.5 rounded-full bg-primary text-primary-foreground text-[14px] font-medium btn-pill hover:bg-primary/90 transition-colors inline-flex items-center justify-center gap-2 disabled:opacity-60"
      >
        <Loader2 v-if="confirming" :size="14" class="animate-spin" />
        确认全部处置（{{ pendingActions.length }} 条待确认）
      </button>
      <button
        v-else
        disabled
        class="w-full py-2.5 rounded-full bg-muted text-muted-foreground text-[14px] font-medium btn-pill cursor-not-allowed"
      >
        {{ allDone ? "已全部确认" : "暂无待确认的处置命令" }}
      </button>
    </div>

    <!-- 事件操作：重诊断 / 关闭 / 补录（按状态显隐，其余交给后端门禁兜底） -->
    <div v-if="isDiagnosable || isClosable || isResolved" class="glass rounded-xl p-4">
      <div class="eyebrow mb-3">事件操作</div>
      <div class="flex flex-wrap gap-2">
        <button
          v-if="isDiagnosable"
          @click="void handleDiagnose()"
          :disabled="diagLoading"
          class="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-full border border-border bg-card text-[12.5px] font-medium hover:bg-secondary transition-colors btn-pill disabled:opacity-50"
          :title="
            incident.status === 'human_needed'
              ? 'force 重跑：human_needed 事件必须显式触发'
              : '重新跑一轮六智能体诊断'
          "
        >
          <Loader2 v-if="diagLoading" :size="13" class="animate-spin" />
          <RefreshCw v-else :size="13" />
          重新诊断
        </button>
        <button
          v-if="isClosable"
          @click="closeDialog = true"
          class="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-full border border-danger/30 text-danger text-[12.5px] font-medium hover:bg-danger/5 transition-colors btn-pill"
          title="显式关闭事件；存在待确认处置命令时后端会拒绝"
        >
          <Archive :size="13" />
          关闭事件
        </button>
        <button
          v-if="isResolved"
          @click="fbDialog = true"
          class="inline-flex items-center gap-1.5 px-3.5 py-2 rounded-full border border-border bg-card text-[12.5px] font-medium hover:bg-secondary transition-colors btn-pill"
          title="补录真实根因/正确处置，作为置信度校准样本"
        >
          <Edit3 :size="13" />
          补录真实根因
        </button>
      </div>
    </div>

    <!-- 二次确认弹窗（处置命令） -->
    <AppDialog v-model:open="confirmDialog" class="rounded-xl">
      <template #title>
        <span class="flex items-center gap-2 text-[16px] font-semibold">
          <AlertTriangle :size="18" class="text-danger" />
          确认执行高危命令
        </span>
      </template>
      <div class="space-y-3 py-2">
        <p class="text-[14px] text-muted-foreground">
          {{
            confirmAll
              ? `即将确认全部 ${pendingActions.length} 条待确认的处置命令，该操作可能影响生产环境：`
              : "即将执行以下命令，该操作可能影响生产环境："
          }}
        </p>
        <div v-if="confirmAll" class="space-y-1.5 max-h-40 overflow-y-auto">
          <pre
            v-for="a in pendingActions"
            :key="a.id"
            class="text-[13px] font-mono px-3 py-2 rounded-lg bg-[#1a1a1a] text-green-400 whitespace-pre-wrap break-all"
          >{{ a.command }}</pre>
        </div>
        <pre
          v-else
          class="text-[13px] font-mono px-3 py-2 rounded-lg bg-[#1a1a1a] text-green-400 whitespace-pre-wrap break-all"
        >{{ confirmCommand }}</pre>
        <label class="flex items-start gap-2 cursor-pointer">
          <Checkbox v-model="acknowledged" class="mt-0.5" />
          <span class="text-[13px] text-foreground">我已知悉该操作可能影响生产环境</span>
        </label>
      </div>
      <template #footer>
        <Button variant="outline" @click="confirmDialog = false" class="btn-pill">取消</Button>
        <Button
          @click="void handleConfirm()"
          :disabled="!acknowledged || confirming"
          class="btn-pill bg-danger text-danger-foreground hover:bg-danger/90"
        >
          <Loader2 v-if="confirming" :size="14" class="animate-spin" />
          确认执行
        </Button>
      </template>
    </AppDialog>

    <!-- 关闭事件弹窗 -->
    <AppDialog v-model:open="closeDialog" class="rounded-xl">
      <template #title>
        <span class="flex items-center gap-2 text-[16px] font-semibold">
          <Archive :size="18" class="text-muted-foreground" />
          关闭事件
        </span>
      </template>
      <div class="space-y-3 py-2">
        <p class="text-[14px] text-muted-foreground">
          关闭后事件进入终态，不再接收后续处理。可填写关闭原因（留空则不记录）：
        </p>
        <input
          type="text"
          v-model="closeReason"
          maxlength="64"
          placeholder="如：误聚类 / 已线下人工处理 / 重复事件"
          class="w-full px-3 py-2 rounded-lg border border-border bg-card text-[13px] focus:outline-none focus:ring-2 focus:ring-accent/30"
        />
      </div>
      <template #footer>
        <Button variant="outline" @click="closeDialog = false" class="btn-pill">取消</Button>
        <Button @click="void handleClose()" :disabled="closing" class="btn-pill">
          <Loader2 v-if="closing" :size="14" class="animate-spin" />
          确认关闭
        </Button>
      </template>
    </AppDialog>

    <!-- 补录真实根因弹窗 -->
    <AppDialog v-model:open="fbDialog" class="rounded-xl">
      <template #title>
        <span class="flex items-center gap-2 text-[16px] font-semibold">
          <Edit3 :size="18" class="text-accent" />
          补录真实根因
        </span>
      </template>
      <div class="space-y-3 py-2">
        <p class="text-[14px] text-muted-foreground">
          人工核实后补录，作为置信度校准样本——至少填写一项：
        </p>
        <div>
          <label class="block text-[13px] font-medium mb-1.5">真实根因</label>
          <textarea
            v-model="fbCause"
            maxlength="200"
            rows="3"
            placeholder="如：磁盘被备份任务写满，未限速"
            class="w-full px-3 py-2 rounded-lg border border-border bg-card text-[13px] focus:outline-none focus:ring-2 focus:ring-accent/30 resize-none"
          />
        </div>
        <div>
          <label class="block text-[13px] font-medium mb-1.5">正确的处置（可选）</label>
          <textarea
            v-model="fbActions"
            maxlength="200"
            rows="3"
            placeholder="如：终止备份任务并限速，清理日志目录"
            class="w-full px-3 py-2 rounded-lg border border-border bg-card text-[13px] focus:outline-none focus:ring-2 focus:ring-accent/30 resize-none"
          />
        </div>
      </div>
      <template #footer>
        <Button variant="outline" @click="fbDialog = false" class="btn-pill">取消</Button>
        <Button @click="void handleFeedback()" :disabled="fbSaving" class="btn-pill">
          <Loader2 v-if="fbSaving" :size="14" class="animate-spin" />
          提交补录
        </Button>
      </template>
    </AppDialog>
  </div>
</template>
