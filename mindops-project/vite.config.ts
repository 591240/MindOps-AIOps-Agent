import tailwindcss from "@tailwindcss/vite";
import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";
import { fileURLToPath, URL } from "node:url";

/**
 * Vue 3 + Vite 构建配置
 *
 * 硬约束（沿用原工程）：
 * - dev server 监听 3015 + strictPort（沙箱只开放一个代理端口）
 * - outDir 'dist' / assetsDir 'assets'
 */
export default defineConfig({
  plugins: [tailwindcss(), vue()],
  resolve: {
    alias: {
      "@": fileURLToPath(new URL("./src", import.meta.url)),
    },
  },
  server: {
    host: "0.0.0.0",
    port: 3015,
    strictPort: true,
    allowedHosts: true,
    // HMR 必须开：关掉会导致改代码后浏览器永远停在旧模块（2026-11-05 踩坑）
  },
  build: {
    outDir: "dist",
    assetsDir: "assets",
    emptyOutDir: true,
    rollupOptions: {
      output: {
        entryFileNames: "assets/[name]-[hash].js",
        chunkFileNames: "assets/[name]-[hash].js",
        assetFileNames: "assets/[name]-[hash][extname]",
        manualChunks(id) {
          if (!id.includes("node_modules")) return;
          if (id.includes("echarts") || id.includes("zrender")) return "charts";
          // Vue 全家桶必须进同一个 chunk：@vue/* 运行时与 vue-router 分家会产生
          // 循环初始化，生产包报 "Cannot access 'ae' before initialization" 白屏
          // （2026-11-05 实测）。vue / @vue/* / vue-router 三者一个都不能漏。
          if (/[\\/]node_modules[\\/](@vue|vue|vue-router)[\\/]/.test(id)) return "vue";
          return "vendor";
        },
      },
    },
  },
});
