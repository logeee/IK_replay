<script setup lang="ts">
import type { Hand, Payload } from "../lib/api";
import { SIDE_LABELS } from "../lib/api";

defineProps<{ payload: Payload }>();
const emit = defineEmits<{
  add: [];
  edit: [hand: Hand];
  remove: [hand: Hand];
}>();
</script>

<template>
  <section class="card">
    <h2>手型号登记 <span class="lvl-tag">二级</span></h2>
    <p class="sub">
      设计侧只作过滤提示，不限制跨侧安装（右臂可装左版灵巧手）。
    </p>
    <ul class="hand-list">
      <li v-for="h in payload.registry.hands" :key="h.id" class="hand">
        <div class="info">
          <div class="name">
            {{ h.name }}
            <span class="tag">设计侧 {{ SIDE_LABELS[h.design_side] }}</span>
            <span class="tag">TCP 外移 {{ h.tool_out_mm }}mm</span>
            <span v-if="h.hand_web_device_id" class="tag">
              18089 {{ h.hand_web_device_id }}
            </span>
          </div>
          <div class="meta">
            <span class="mono dim">{{ h.id }}</span>
            <span v-if="h.notes" class="dim">· {{ h.notes }}</span>
          </div>
        </div>
        <div class="ops">
          <button class="btn sm ghost" @click="emit('edit', h)">编辑</button>
          <button class="btn sm ghost danger" @click="emit('remove', h)">
            删除
          </button>
        </div>
      </li>
    </ul>
    <button class="btn" @click="emit('add')">＋ 新增手型号</button>
  </section>
</template>

<style scoped>
.hand-list {
  list-style: none;
  margin: 0 0 14px;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.hand {
  display: flex;
  align-items: center;
  gap: 12px;
  background: var(--bg-soft);
  border: 1px solid var(--border);
  border-radius: 11px;
  padding: 12px 14px;
  transition: border-color 0.15s;
}

.hand:hover {
  border-color: rgba(86, 217, 197, 0.4);
}

.info {
  flex: 1;
  min-width: 0;
}

.name {
  font-size: 14.5px;
  font-weight: 600;
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
}

.meta {
  margin-top: 4px;
  font-size: 12px;
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
}

.ops {
  display: flex;
  gap: 6px;
  flex: none;
}
</style>
