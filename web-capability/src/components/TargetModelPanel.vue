<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type {
  ParamSpec,
  Payload,
  TargetModelConfig,
  VectorParamSpec,
} from "../lib/api";

const props = defineProps<{ payload: Payload; busy: boolean }>();
const emit = defineEmits<{ save: [config: TargetModelConfig] }>();

const FALLBACK_METHOD = "knob_mask_center";

const methods = computed<string[]>(
  () => props.payload.meta.target_models ?? [FALLBACK_METHOD],
);
const labels = computed<Record<string, string>>(
  () => props.payload.meta.target_model_labels ?? {},
);
const versions = computed<Record<string, string>>(
  () => props.payload.meta.target_model_versions ?? {},
);
const specs = computed<Record<string, Record<string, ParamSpec>>>(
  () => props.payload.meta.target_model_param_specs ?? {},
);
const vectorSpecs = computed<Record<string, Record<string, VectorParamSpec>>>(
  () => props.payload.meta.target_model_vector_params ?? {},
);

const server = computed(() => props.payload.registry.target_model);
const method = ref(FALLBACK_METHOD);

watch(
  server,
  (config) => {
    method.value =
      config?.method ?? props.payload.meta.target_model_default ?? FALLBACK_METHOD;
  },
  { immediate: true },
);

// 参数只读：标量取注册表（当前模型）或代码默认；偏移向量永远是代码常量
const shownParams = computed<Record<string, number | number[]>>(() => {
  const id = method.value;
  const out: Record<string, number | number[]> = {};
  for (const [key, s] of Object.entries(specs.value[id] ?? {})) out[key] = s.default;
  if (server.value && server.value.method === id)
    for (const [key, v] of Object.entries(server.value.params ?? {}))
      if (key in out && !Array.isArray(v)) out[key] = v;
  for (const [key, s] of Object.entries(vectorSpecs.value[id] ?? {}))
    out[key] = s.default ?? Array.from({ length: s.length ?? 3 }, () => 0);
  return out;
});

function formatValue(value: unknown): string {
  if (Array.isArray(value)) return value.map((v) => String(v)).join(", ");
  if (value === undefined || value === null) return "—";
  return String(value);
}

const rows = computed(() => {
  const id = method.value;
  const out: { key: string; label: string; value: string }[] = [];
  for (const [key, spec] of Object.entries(specs.value[id] ?? {}))
    out.push({ key, label: spec.label || key, value: formatValue(shownParams.value[key]) });
  for (const [key, spec] of Object.entries(vectorSpecs.value[id] ?? {}))
    out.push({ key, label: spec.label || key, value: formatValue(shownParams.value[key]) });
  return out;
});

const calibrated = computed(() => {
  if (method.value !== "panel_anchor") return true;
  return ["anchor_offset_wall_mm", "point1_offset_wall_mm", "point3_offset_wall_mm"].some(
    (key) => {
      const v = shownParams.value[key];
      return Array.isArray(v) && v.some((n) => Math.abs(Number(n)) > 1e-9);
    },
  );
});

const isCurrent = computed(() => server.value?.method === method.value);

function submit() {
  // 只提交模型名：同模型服务端保留现有参数，切换模型服务端恢复该模型上次保存的参数
  emit("save", { method: method.value, params: {} });
}
</script>

<template>
  <section class="card stack">
    <h2>自动选点模型 <span class="lvl-tag">7005 粉点→绿点</span></h2>
    <p class="sub">
      7005 从哪个参考点、沿柜面坐标系加什么偏移得到目的点（绿点）。
      <strong>knob_mask_center</strong> = 现有 0.2.0-s（旋钮 mask 中心 + 内置常量）；
      <strong>panel_anchor</strong> = 面板矩形中心 → 锚点 → 点 1/点 3，
      偏移与 0.2.0-s 一样写死在代码里（<code>core/target_models.py</code>，由
      <code>tools/calibrate_panel_anchor.py</code> 标定得到）。这里只切换模型。保存后重启 7005 生效。
    </p>
    <div class="controls">
      <label class="field method">模型
        <select v-model="method">
          <option v-for="id in methods" :key="id" :value="id">
            {{ labels[id] || id }}
          </option>
        </select>
      </label>
    </div>
    <dl v-if="rows.length" class="params mono">
      <template v-for="row in rows" :key="row.key">
        <dt>{{ row.label }}</dt>
        <dd>{{ row.value }}</dd>
      </template>
    </dl>
    <div class="foot">
      <span v-if="!calibrated" class="badge missing">panel_anchor 偏移全为 0：尚未标定，自动找点会失败</span>
      <span v-else-if="isCurrent" class="badge on">当前配置</span>
      <span v-else class="badge plain off">切换后需保存</span>
      <span v-if="server" class="dim mono">
        当前：{{ labels[server.method] || server.method }}
        <template v-if="versions[server.method]">（model_version {{ versions[server.method] }}）</template>
      </span>
      <span class="spacer"></span>
      <button class="btn primary" :disabled="busy || isCurrent" @click="submit">
        {{ isCurrent ? "已是当前配置" : "切换自动选点模型" }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.controls {
  display: flex;
  align-items: flex-end;
  gap: 14px;
  flex-wrap: wrap;
}

.controls .method {
  min-width: 320px;
  flex: 1 1 320px;
}

.params {
  display: grid;
  grid-template-columns: max-content 1fr;
  gap: 4px 16px;
  margin: 12px 0 0;
  font-size: 12px;
  opacity: 0.85;
}

.params dt {
  color: var(--dim, #9aa4b2);
}

.params dd {
  margin: 0;
}

.foot {
  display: flex;
  align-items: center;
  gap: 10px;
  flex-wrap: wrap;
  margin-top: 14px;
}

.foot .spacer {
  flex: 1;
}
</style>
