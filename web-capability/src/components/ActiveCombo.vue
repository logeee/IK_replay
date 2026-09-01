<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type { CalibInfo, Payload } from "../lib/api";
import { SIDE_LABELS } from "../lib/api";

const props = defineProps<{ payload: Payload; busy: boolean }>();
const emit = defineEmits<{ apply: [arm: string, handId: string] }>();

const arm = ref("right_arm");
const handId = ref("");

watch(
  () => props.payload.registry.active,
  (active) => {
    if (active) {
      arm.value = active.arm;
      handId.value = active.hand_id;
    }
  },
  { immediate: true },
);

const hands = computed(() => props.payload.registry.hands);

watch(hands, (list) => {
  if (!list.some((h) => h.id === handId.value) && list.length) {
    handId.value = list[0]!.id;
  }
});

const calib = computed<CalibInfo | null>(
  () =>
    props.payload.calibrations.find(
      (c) => c.arm === arm.value && c.hand_id === handId.value,
    ) ?? null,
);

const calibStatus = computed(() => calib.value?.status ?? "missing");

const CALIB_TEXT: Record<string, string> = {
  ready: "标定就绪",
  pending: "标定待补",
  missing: "标定未登记",
};

const isCurrent = computed(() => {
  const active = props.payload.registry.active;
  return (
    !!active && active.arm === arm.value && active.hand_id === handId.value
  );
});

const activeCapCount = computed(
  () =>
    props.payload.registry.capabilities.filter(
      (c) => c.arm === arm.value && c.hand_id === handId.value && c.enabled,
    ).length,
);
</script>

<template>
  <section class="card hero">
    <div class="head">
      <h2>激活组合 <span class="lvl-tag">一级 + 二级</span></h2>
      <p class="sub">
        17001 派发与 18001 执行使用的当前组合；切换保存后重启 17001 / 18001 生效。
      </p>
    </div>
    <div class="controls">
      <label class="field">臂侧
        <select v-model="arm">
          <option v-for="a in payload.meta.arms" :key="a" :value="a">
            {{ payload.meta.arm_labels[a] || a }}
          </option>
        </select>
      </label>
      <span class="arrow">→</span>
      <label class="field">手型号
        <select v-model="handId">
          <option v-for="h in hands" :key="h.id" :value="h.id">
            {{ h.name }}（设计侧:{{ SIDE_LABELS[h.design_side] }}）
          </option>
        </select>
      </label>
      <div class="status">
        <span class="badge" :class="calibStatus">
          {{ CALIB_TEXT[calibStatus] }}
          <template v-if="calib?.residual_mm != null">
            · 残差 {{ calib.residual_mm.toFixed(1) }}mm
          </template>
        </span>
        <span v-if="isCurrent" class="badge on">当前激活</span>
        <span class="badge plain off">{{ activeCapCount }} 项已启用能力</span>
      </div>
      <button
        class="btn primary"
        :disabled="busy || isCurrent || !handId"
        @click="emit('apply', arm, handId)"
      >
        {{ isCurrent ? "已是激活组合" : "切换激活组合" }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.hero {
  background:
    linear-gradient(120deg, rgba(86, 217, 197, 0.06), transparent 55%),
    var(--card);
}

.controls {
  display: flex;
  align-items: flex-end;
  gap: 14px;
  flex-wrap: wrap;
}

.controls .field {
  min-width: 170px;
}

.arrow {
  color: var(--text-dim);
  padding-bottom: 10px;
  font-size: 15px;
}

.status {
  display: flex;
  align-items: center;
  gap: 8px;
  flex-wrap: wrap;
  padding-bottom: 6px;
}

.controls .btn {
  margin-left: auto;
}
</style>
