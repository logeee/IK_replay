<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type { CalibInfo, MotionBackend, Payload } from "../lib/api";
import { SIDE_LABELS } from "../lib/api";

const props = defineProps<{ payload: Payload; busy: boolean }>();
const emit = defineEmits<{
  apply: [arm: string, handId: string, motionBackend: MotionBackend];
}>();

const arm = ref("right_arm");
const handId = ref("");
const motionBackend = ref<MotionBackend>("legacy");

const FALLBACK_BACKENDS: MotionBackend[] = ["legacy", "pink"];
const FALLBACK_BACKEND_LABELS: Record<string, string> = {
  legacy: "原方案（关节路点直发）",
  pink: "PINK 世界系闭环跟踪",
};
const backends = computed(
  () => props.payload.meta.motion_backends ?? FALLBACK_BACKENDS,
);
const backendLabels = computed(
  () => props.payload.meta.motion_backend_labels ?? FALLBACK_BACKEND_LABELS,
);

watch(
  () => props.payload.registry.active,
  (active) => {
    if (active) {
      arm.value = active.arm;
      handId.value = active.hand_id;
      motionBackend.value = active.motion_backend ?? "legacy";
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

const calibResidualText = computed<string | null>(() => {
  const value = calib.value?.residual_mm;
  if (typeof value === "number") return value.toFixed(1);
  const rms = value?.rms;
  return typeof rms === "number" ? rms.toFixed(1) : null;
});

const isCurrent = computed(() => {
  const active = props.payload.registry.active;
  return (
    !!active &&
    active.arm === arm.value &&
    active.hand_id === handId.value &&
    (active.motion_backend ?? "legacy") === motionBackend.value
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
      <label class="field" title="18001 执行路点的方式。pink：世界系 PINK 闭环跟踪，补偿执行中的躯干漂移，需要 pinocchio/pin-pink 且执行前先在 18001 页面锚定世界系；初始化失败会自动回退原方案">运动后端
        <select v-model="motionBackend">
          <option v-for="b in backends" :key="b" :value="b">
            {{ backendLabels[b] || b }}
          </option>
        </select>
      </label>
      <div class="status">
        <span class="badge" :class="calibStatus">
          {{ CALIB_TEXT[calibStatus] }}
          <template v-if="calibResidualText">
            · 残差 {{ calibResidualText }}mm
          </template>
        </span>
        <span v-if="isCurrent" class="badge on">当前激活</span>
        <span v-if="motionBackend === 'pink'" class="badge plain off">pink：执行前需锚定世界系</span>
        <span class="badge plain off">{{ activeCapCount }} 项已启用能力</span>
      </div>
      <button
        class="btn primary"
        :disabled="busy || isCurrent || !handId"
        @click="emit('apply', arm, handId, motionBackend)"
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
