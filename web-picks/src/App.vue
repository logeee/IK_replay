<script setup lang="ts">
import { onMounted, ref } from "vue";
import { fetchRecords } from "./lib/api";

const total = ref<number | null>(null);

onMounted(async () => {
  try {
    total.value = (await fetchRecords()).length;
  } catch {
    /* 列表页会展示具体错误 */
  }
});
</script>

<template>
  <header class="topbar">
    <div class="brand">选点记录<span class="dot">·</span>可视化</div>
    <nav>
      <RouterLink to="/">画廊</RouterLink>
      <RouterLink to="/stats">统计分析</RouterLink>
    </nav>
    <div class="spacer" />
    <div v-if="total !== null" class="count">共 {{ total }} 条记录</div>
  </header>
  <RouterView />
</template>
