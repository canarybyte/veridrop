import { defineConfig } from "vite";
import react from "@vitejs/plugin-react";

export default defineConfig({
  base: "/app/",
  plugins: [react()],
  server: {
    port: 5173,
    proxy: {
      // 注意顺序：更具体的前缀放在前面。
      // SSE 由 http-proxy 原样转发，这里不额外加缓冲相关配置。
      "/app/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
      "/api": {
        target: "http://127.0.0.1:8000",
        changeOrigin: true,
      },
    },
  },
  build: {
    outDir: "../web/static/app",
    emptyOutDir: true,
    sourcemap: false,
  },
});
