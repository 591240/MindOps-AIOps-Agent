<script setup lang="ts">
import { computed, onBeforeUnmount, onMounted, ref, watch } from "vue";
import { RouterView, useRoute, useRouter } from "vue-router";
import GlowBackground from "@/components/GlowBackground.vue";
import { useIsMobile } from "@/composables/use-mobile";
import SideNavBar from "./SideNavBar.vue";
import TopNavBar from "./TopNavBar.vue";
import { API_BASE_URL } from "@/api/client";
import { authApi } from "@/api/auth";
import { clearSession, getAccessToken, readSession } from "@/lib/session";

const route = useRoute();
const router = useRouter();
const isMobile = useIsMobile();
const mobileNavOpen = ref(false);

const session = readSession();
const pollStatus = ref<"connected" | "polling" | "disconnected">("disconnected");
const unreadCount = ref(0);

let cancelled = false;
let timer: number | undefined;

async function loadUnread() {
  const token = getAccessToken();
  if (!token) return;
  try {
    const res = await fetch(`${API_BASE_URL}/incidents?status=human_needed&size=1`, {
      headers: { Authorization: `Bearer ${token}` },
    });
    if (!res.ok) throw new Error(String(res.status));
    const body = await res.json();
    if (cancelled) return;
    unreadCount.value = Number(body.total ?? body.incidents?.length ?? 0);
    pollStatus.value = "connected";
  } catch {
    if (!cancelled) pollStatus.value = "disconnected";
  }
}

onMounted(() => {
  if (!getAccessToken()) return;
  void loadUnread();
  // 未读数取自后端真实统计；轮询而非 SSE：后端尚未提供按用户维度的未读推送。
  timer = window.setInterval(loadUnread, 30_000);
});

onBeforeUnmount(() => {
  cancelled = true;
  window.clearInterval(timer);
});

// 切路由时收起移动端抽屉，避免遮住内容
watch(
  () => route.path,
  () => (mobileNavOpen.value = false),
);

async function handleLogout() {
  // 先尽力让后端吊销 jti（access + refresh），失败也继续清本地会话
  try {
    await authApi.logout();
  } catch {
    /* 后端不可达时忽略：本地会话仍会被清掉 */
  }
  clearSession();
  void router.push("/login");
}

const pathname = computed(() => route.path);
const role = computed(() => session?.user.role || "engineer");
const navUser = computed(() => ({
  username: session?.user.username || "用户",
  role: role.value,
}));
</script>

<template>
  <GlowBackground />
  <div v-if="!session" class="relative z-10">
    <RouterView />
  </div>
  <div v-else class="relative z-10 flex h-screen overflow-hidden">
    <!-- 桌面端常驻侧栏；移动端收起为抽屉 -->
    <SideNavBar
      v-if="!isMobile"
      :role="role"
      :current-path="pathname"
      :user="navUser"
      @logout="handleLogout"
    />
    <template v-if="isMobile && mobileNavOpen">
      <div
        class="fixed inset-0 z-40 bg-black/30"
        @click="mobileNavOpen = false"
        aria-hidden="true"
      />
      <div class="fixed inset-y-0 left-0 z-50">
        <SideNavBar
          :role="role"
          :current-path="pathname"
          :user="navUser"
          @logout="handleLogout"
        />
      </div>
    </template>
    <div class="flex-1 flex flex-col overflow-hidden min-w-0">
      <TopNavBar
        :pathname="pathname"
        :connection-status="pollStatus"
        :unread-count="unreadCount"
        :show-nav-toggle="isMobile"
        @toggle-nav="mobileNavOpen = !mobileNavOpen"
      />
      <main class="flex-1 overflow-auto">
        <div class="max-w-[1440px] mx-auto p-4 sm:p-6">
          <RouterView />
        </div>
      </main>
    </div>
  </div>
</template>
