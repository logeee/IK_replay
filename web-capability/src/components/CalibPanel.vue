<script setup lang="ts">
import type { Payload } from "../lib/api";

const props = defineProps<{ payload: Payload }>();
const emit = defineEmits<{ register: [] }>();

const CALIB_TEXT: Record<string, string> = {
  ready: "就绪",
  pending: "待补",
  missing: "未登记",
};

function handName(handId: string): string {
  return (
    props.payload.registry.hands.find((h) => h.id === handId)?.name ?? handId
  );
}
</script>

<template>
  <section class="card">
    <h2>手眼标定归档 <span class="lvl-tag">一级 + 二级</span></h2>
    <p class="sub">
      按「臂 + 手型号」组合归档，组合相同共用同一份；路径
      <span class="mono">config/hand_eye/{臂}__{手型号}/handeye3d_result.json</span>
    </p>
    <ul class="calib-list">
      <li v-for="c in payload.calibrations" :key="c.arm + c.hand_id">
        <div class="combo">
          <span class="combo-name">
            {{ payload.meta.arm_labels[c.arm] || c.arm }} ·
            {{ handName(c.hand_id) }}
          </span>
          <span class="badge" :class="c.status">
            {{ CALIB_TEXT[c.status] }}
          </span>
        </div>
        <div class="detail">
          <template v-if="c.status === 'ready'">
            <span v-if="c.solved_at" class="dim">解算 {{ c.solved_at }}</span>
            <span v-if="c.residual_mm != null" class="dim">
              残差 {{ c.residual_mm.toFixed(2) }}mm
            </span>
            <span v-if="c.num_samples != null" class="dim">
              {{ c.num_samples }} 样本
            </span>
          </template>
          <span v-else-if="c.source_path" class="dim mono src">
            来源 {{ c.source_path }}
          </span>
          <span v-else class="dim">等待上传标定文件</span>
        </div>
      </li>
      <li v-if="!payload.calibrations.length" class="dim empty">
        尚未登记任何标定
      </li>
    </ul>
    <button class="btn" @click="emit('register')">＋ 登记 / 上传标定</button>
  </section>
</template>

<style scoped>
.calib-list {
  list-style: none;
  margin: 0 0 14px;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.calib-list li {
  background: var(--bg-soft);
  border: 1px solid var(--border);
  border-radius: 11px;
  padding: 12px 14px;
}

.combo {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.combo-name {
  font-size: 14.5px;
  font-weight: 600;
}

.detail {
  margin-top: 5px;
  font-size: 12px;
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}

.src {
  word-break: break-all;
}

.empty {
  text-align: center;
  padding: 18px;
}
</style>
