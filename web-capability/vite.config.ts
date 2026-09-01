import vue from "@vitejs/plugin-vue";
import { defineConfig } from "vite";

export default defineConfig({
  plugins: [vue()],
  server: {
    // 开发模式直接代理到本机 18000 的能力配置服务
    proxy: {
      "/api": "http://127.0.0.1:18000",
    },
  },
});
