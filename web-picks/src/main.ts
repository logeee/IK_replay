import { createApp } from "vue";
import { createRouter, createWebHashHistory } from "vue-router";
import App from "./App.vue";
import "./style.css";

const router = createRouter({
  // hash 路由：FastAPI 静态托管无需配置 history 回退
  history: createWebHashHistory(),
  routes: [
    { path: "/", component: () => import("./views/GalleryView.vue") },
    { path: "/pick/:name", component: () => import("./views/DetailView.vue") },
    { path: "/stats", component: () => import("./views/StatsView.vue") },
  ],
});

createApp(App).use(router).mount("#app");
