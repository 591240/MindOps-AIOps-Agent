<script setup lang="ts">
import { useRouter } from "vue-router";
import GlowBackground from "@/components/GlowBackground.vue";
import Dock from "@/components/ui/Dock.vue";
import { Mail, ArrowRight, Activity, Zap, GitBranch, KeyRound } from "lucide-vue-next";

const router = useRouter();

// 只放能从代码/部署里核对的事实，不放编造的运营指标
const metrics = [
  { label: "协同智能体", value: "6" },
  { label: "知识检索", value: "RAG" },
  { label: "实时推送", value: "SSE" },
];

const models = [
  { name: "DeepSeek", color: "bg-accent" },
  { name: "Qwen", color: "bg-success" },
  { name: "GLM", color: "bg-warning" },
  { name: "Kimi", color: "bg-glow-pink" },
  { name: "GPT", color: "bg-success" },
];

const cards = [
  {
    num: "01",
    title: "产品定位",
    subtitle: "智能告警诊断系统",
    icon: Activity,
    desc: "把零散告警聚合成事件，由六智能体协同定位根因，给出可执行的处置建议并把经验沉淀为可复用的知识库。",
  },
  {
    num: "02",
    title: "核心价值",
    subtitle: "替 SRE 扛住告警疲劳",
    icon: Zap,
    desc: "解决企业 SRE 团队被海量告警淹没、人均处理事件速度慢、跨团队协作路径重的痛点，让值班从「救火」变「守门」。",
  },
  {
    num: "03",
    title: "实现路径",
    subtitle: "F-01 → F-10 完整链路",
    icon: GitBranch,
    desc: "告警接入 → 聚类降噪 → 六智能体诊断 → 置信度回环 → RAG 检索 → 处置建议 → 知识回灌 → 实时推送。",
  },
];

const dockItems = cards.map((card) => ({ id: card.num, label: card.subtitle, card }));

function openLogin(tab: "password" | "email") {
  localStorage.setItem("login_tab", tab);
  void router.push("/login");
}
</script>

<template>
  <div class="min-h-screen relative">
    <GlowBackground />

    <div class="relative z-10">
      <!-- Hero 区 — 左右分栏 -->
      <section class="min-h-screen flex items-center px-8 lg:px-16">
        <div class="max-w-[1440px] mx-auto w-full grid lg:grid-cols-2 gap-12 items-center">
          <!-- 左侧 -->
          <div class="space-y-6">
            <div class="eyebrow">AI DIAGNOSIS · GOVERNED</div>
            <h1 class="text-[40px] lg:text-[44px] font-semibold heading-tight leading-[1.1]">
              发布能
              <span class="bg-gradient-to-r from-glow-pink to-glow-blue bg-clip-text text-transparent">
                瞬间诊断
              </span>
              <br />
              每一道告警
            </h1>
            <p class="text-[15px] text-muted-foreground leading-relaxed max-w-md">
              六智能体编排 + RAG 知识回灌 + 置信度回环，让 SRE 告别告警疲劳。
            </p>

            <!-- 数据指标 -->
            <div class="flex gap-8 pt-4">
              <div
                v-for="m in metrics"
                :key="m.label"
                class="transition-transform duration-200 ease-out hover:scale-[1.06] hover:-translate-y-0.5 cursor-default"
              >
                <div class="text-[28px] font-semibold heading-tight tabular-nums">{{ m.value }}</div>
                <div class="text-[12px] text-muted-foreground mt-1">{{ m.label }}</div>
              </div>
            </div>

            <!-- 模型标签 -->
            <div class="flex items-center gap-3 pt-2">
              <span class="text-[12px] text-muted-foreground">支持模型</span>
              <div class="flex items-center gap-3">
                <span
                  v-for="m in models"
                  :key="m.name"
                  class="glass-pill inline-flex items-center gap-1.5 px-2.5 py-1 rounded-full text-[12px] transition-transform duration-200 ease-out hover:scale-110 hover:-translate-y-0.5 cursor-default select-none"
                >
                  <span class="w-1.5 h-1.5 rounded-full" :class="m.color" />
                  {{ m.name }}
                </span>
              </div>
            </div>
          </div>

          <!-- 右侧 -->
          <div class="space-y-6">
            <div class="eyebrow">AI DIAGNOSIS · GOVERNED</div>
            <h2 class="text-[32px] lg:text-[36px] font-semibold heading-tight leading-[1.15]">
              Your alerts.
              <br />
              Your rules.
              <br />
              <span class="text-accent">Any LLM.</span>
            </h2>
            <p class="text-[15px] text-muted-foreground leading-relaxed max-w-md">
              MindOps 让 SRE 在一处即可设置告警守门、管理模型访问，并查看全部事件 ——
              让每个人都能基于任意大模型自信地运维，无需等待任何人。
            </p>

            <!-- 登录按钮 -->
            <div class="flex gap-3 pt-2">
              <button
                @click="openLogin('password')"
                class="inline-flex items-center gap-2 px-5 py-2.5 rounded-full bg-primary text-primary-foreground text-[13px] font-medium btn-pill hover:bg-primary/90 transition-[background-color,transform] duration-200 ease-out hover:scale-[1.04] hover:-translate-y-0.5 active:scale-95"
              >
                <KeyRound :size="16" :stroke-width="1.8" />
                密码登录
              </button>
              <button
                @click="openLogin('email')"
                class="inline-flex items-center gap-2 px-5 py-2.5 rounded-full glass text-foreground text-[13px] font-medium btn-pill hover:bg-white/80 transition-[background-color,transform] duration-200 ease-out hover:scale-[1.04] hover:-translate-y-0.5 active:scale-95"
              >
                <Mail :size="16" :stroke-width="1.8" />
                邮箱验证码登录
              </button>
            </div>
          </div>
        </div>
      </section>

      <!-- Dock 放大镜功能导航区 -->
      <section class="px-8 lg:px-16 pb-24 pt-8">
        <div class="max-w-[1440px] mx-auto">
          <div class="text-center mb-10 reveal">
            <div class="eyebrow mb-2">CORE FEATURES</div>
            <h2 class="text-[24px] font-semibold heading-tight">三步看清 MindOps</h2>
            <p class="text-[14px] text-muted-foreground mt-2">
              <span class="hidden lg:inline">鼠标靠近卡片，自动放大聚焦</span>
              <span class="lg:hidden">三个视角，快速了解这套系统</span>
            </p>
          </div>

          <!-- 桌面端：Dock 放大镜导航（依赖 hover，触屏不可用） -->
          <div class="hidden lg:flex justify-center reveal" data-reveal-delay="100">
            <Dock
              :items="dockItems"
              :magnification="320"
              :base-item-size="200"
              :distance="250"
              :panel-height="220"
              :dock-height="340"
              @select="router.push('/login')"
            >
              <template #item="{ item }">
                <div class="flex flex-col items-center justify-center gap-2 px-2">
                  <div class="flex items-center justify-between w-full">
                    <div
                      class="w-9 h-9 rounded-lg bg-secondary/80 flex items-center justify-center shrink-0"
                    >
                      <component
                        :is="item.card.icon"
                        :size="18"
                        class="text-foreground"
                        :stroke-width="1.8"
                      />
                    </div>
                    <span
                      class="text-[20px] font-semibold text-muted-foreground/25 heading-tight tabular-nums shrink-0"
                    >
                      {{ item.card.num }}
                    </span>
                  </div>
                  <div class="text-center">
                    <div
                      class="text-[9px] uppercase tracking-wider text-muted-foreground/70 mb-0.5"
                    >
                      {{ item.card.title }}
                    </div>
                    <div class="text-[12px] font-semibold heading-tight leading-tight">
                      {{ item.card.subtitle }}
                    </div>
                  </div>
                  <p class="text-[10px] text-muted-foreground/80 leading-snug line-clamp-3">
                    {{ item.card.desc }}
                  </p>
                </div>
              </template>
            </Dock>
          </div>

          <!-- 移动/触屏端：卡片网格（无 hover，Dock 的放大镜在此完全失效） -->
          <div class="lg:hidden grid gap-3 reveal" data-reveal-delay="100">
            <button
              v-for="card in cards"
              :key="card.num"
              @click="router.push('/login')"
              class="glass rounded-xl p-4 text-left flex items-start gap-3 hover:bg-white/80 transition-colors"
            >
              <div class="w-9 h-9 rounded-lg bg-secondary/80 flex items-center justify-center shrink-0">
                <component :is="card.icon" :size="18" class="text-foreground" :stroke-width="1.8" />
              </div>
              <div class="flex-1 min-w-0">
                <div class="flex items-baseline justify-between gap-2">
                  <span class="text-[9px] uppercase tracking-wider text-muted-foreground/70">
                    {{ card.title }}
                  </span>
                  <span class="text-[16px] font-semibold text-muted-foreground/25 tabular-nums">
                    {{ card.num }}
                  </span>
                </div>
                <div class="text-[14px] font-semibold heading-tight mt-0.5">{{ card.subtitle }}</div>
                <p class="text-[12px] text-muted-foreground leading-snug mt-1">{{ card.desc }}</p>
              </div>
            </button>
          </div>
        </div>
      </section>

      <!-- 底部 CTA -->
      <section class="px-8 lg:px-16 pb-16">
        <div class="max-w-[1440px] mx-auto flex items-center justify-between glass rounded-xl p-6">
          <div>
            <h3 class="text-[20px] font-semibold heading-tight mb-1">开始使用 MindOps</h3>
            <p class="text-[14px] text-muted-foreground">登录后即可查看告警看板与事件详情</p>
          </div>
          <button
            @click="router.push('/login')"
            class="inline-flex items-center gap-2 px-5 py-2.5 rounded-full bg-primary text-primary-foreground text-[13px] font-medium btn-pill hover:bg-primary/90 transition-colors"
          >
            立即登录
            <ArrowRight :size="16" :stroke-width="1.8" />
          </button>
        </div>
      </section>
    </div>
  </div>
</template>
