<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import type { Capability, Payload } from "../lib/api";
import {
  DEFAULT_POSE_PATTERNS,
  DIRECTION_LABELS,
  PARAM_LABELS,
  SIDE_LABELS,
  SITE_LABELS,
} from "../lib/api";
import BaseDialog from "./BaseDialog.vue";

const props = defineProps<{
  payload: Payload;
  cap: Capability | null;
  busy: boolean;
}>();
const emit = defineEmits<{
  close: [];
  save: [body: Record<string, unknown>];
}>();

const form = reactive({
  arm: props.cap?.arm ?? "right_arm",
  hand_id: props.cap?.hand_id ?? "",
  task_name: props.cap?.task.name ?? "",
  direction: props.cap?.task.direction ?? "rtl",
  sites: new Set(props.cap?.task.sites ?? ["factory"]),
  method: props.cap?.method ?? "flick",
  enabled: props.cap?.enabled ?? true,
  pose_pattern:
    props.cap?.assets.pose_pattern ?? DEFAULT_POSE_PATTERNS.rtl ?? "",
  notes: props.cap?.notes ?? "",
});

const showAllHands = ref(false);
const params = reactive<Record<string, number>>({});

const paramSpec = computed(
  () => props.payload.meta.method_param_specs[form.method] ?? {},
);

watch(
  () => form.method,
  () => {
    for (const key of Object.keys(params)) delete params[key];
    for (const [key, spec] of Object.entries(paramSpec.value)) {
      params[key] =
        props.cap && props.cap.method === form.method
          ? (props.cap.method_params[key] ?? spec.default)
          : spec.default;
    }
  },
  { immediate: true },
);

// 新建时切换方向自动带出对应内置正则（用户手动改过就不再覆盖）
watch(
  () => form.direction,
  (direction) => {
    if (props.cap) return;
    const untouched = [...Object.values(DEFAULT_POSE_PATTERNS), ""].includes(
      form.pose_pattern,
    );
    if (untouched) {
      form.pose_pattern = DEFAULT_POSE_PATTERNS[direction] ?? "";
    }
  },
);

const handOptions = computed(() => {
  const wantSide = form.arm === "left_arm" ? "left" : "right";
  return props.payload.registry.hands.filter(
    (h) => showAllHands.value || h.design_side === wantSide,
  );
});

watch(
  [handOptions, () => form.arm],
  () => {
    if (
      !handOptions.value.some((h) => h.id === form.hand_id) &&
      handOptions.value.length
    ) {
      form.hand_id = handOptions.value[0]!.id;
    }
  },
  { immediate: true },
);

function toggleSite(site: string) {
  if (form.sites.has(site)) form.sites.delete(site);
  else form.sites.add(site);
}

const canSave = computed(
  () => !!form.task_name && !!form.hand_id && form.sites.size > 0,
);

function submit() {
  emit("save", {
    id: props.cap?.id ?? "",
    arm: form.arm,
    hand_id: form.hand_id,
    task: {
      name: form.task_name,
      direction: form.direction,
      sites: [...form.sites],
    },
    method: form.method,
    method_params: { ...params },
    assets: { pose_pattern: form.pose_pattern.trim(), endpoint_pattern: "" },
    enabled: form.enabled,
    notes: form.notes,
  });
}
</script>

<template>
  <BaseDialog
    :title="cap ? `编辑能力「${cap.task.name}」` : '新增能力'"
    wide
    @close="emit('close')"
  >
    <div class="grid">
      <label class="field">臂侧（一级）
        <select v-model="form.arm">
          <option v-for="a in payload.meta.arms" :key="a" :value="a">
            {{ payload.meta.arm_labels[a] || a }}
          </option>
        </select>
      </label>
      <label class="field">手型号（二级）
        <select v-model="form.hand_id">
          <option v-for="h in handOptions" :key="h.id" :value="h.id">
            {{ h.name }}（设计侧:{{ SIDE_LABELS[h.design_side] }}）
          </option>
        </select>
      </label>
      <label class="check full">
        <input v-model="showAllHands" type="checkbox" />
        显示全部手型号（含与臂侧不同设计侧的跨侧组合）
      </label>

      <label class="field">任务名（三级）
        <input v-model.trim="form.task_name" placeholder="旋钮右到左" />
      </label>
      <label class="field">物理方向
        <select v-model="form.direction">
          <option
            v-for="d in payload.meta.directions"
            :key="d"
            :value="d"
          >
            {{ DIRECTION_LABELS[d] || d }}
          </option>
        </select>
      </label>

      <div class="field full">
        适用现场
        <div class="site-row">
          <label
            v-for="site in payload.meta.sites"
            :key="site"
            class="check"
          >
            <input
              type="checkbox"
              :checked="form.sites.has(site)"
              @change="toggleSite(site)"
            />
            {{ SITE_LABELS[site] || site }}（{{ site }}）
          </label>
        </div>
      </div>

      <label class="field">实现方式（四级）
        <select v-model="form.method">
          <option v-for="m in payload.meta.methods" :key="m" :value="m">
            {{ payload.meta.method_labels[m] || m }}
          </option>
        </select>
      </label>
      <label class="check bottom">
        <input v-model="form.enabled" type="checkbox" />
        启用该能力
      </label>

      <template v-if="Object.keys(paramSpec).length">
        <label
          v-for="(spec, key) in paramSpec"
          :key="key"
          class="field"
        >
          {{ PARAM_LABELS[key] || key }}（{{ spec.min }}~{{ spec.max }}）
          <input
            v-model.number="params[key]"
            type="number"
            step="0.1"
            :min="spec.min"
            :max="spec.max"
          />
        </label>
      </template>
      <p v-else class="dim full note">
        该实现方式暂无参数（尚未实现，仅登记配置位）。
      </p>

      <label class="field full">起手式命名正则（第 1 捕获组 = 档位距离 m）
        <input v-model="form.pose_pattern" class="mono" spellcheck="false" />
      </label>
      <label class="field full">备注
        <input v-model.trim="form.notes" />
      </label>
    </div>
    <template #footer>
      <button class="btn ghost" @click="emit('close')">取消</button>
      <button
        class="btn primary"
        :disabled="busy || !canSave"
        @click="submit"
      >
        保存
      </button>
    </template>
  </BaseDialog>
</template>

<style scoped>
.grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.full {
  grid-column: 1 / -1;
}

.site-row {
  display: flex;
  gap: 18px;
  padding: 4px 0;
}

.bottom {
  align-self: end;
  padding-bottom: 10px;
}

.note {
  margin: 0;
  font-size: 12.5px;
}
</style>
