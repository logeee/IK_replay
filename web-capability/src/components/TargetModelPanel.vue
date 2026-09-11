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

const method = ref(FALLBACK_METHOD);
// 标量以字符串编辑；向量以「a, b, c」文本编辑，提交时拆成数字数组
const params = ref<Record<string, string>>({});
const vectors = ref<Record<string, string>>({});

function defaultsFor(id: string): Record<string, string> {
  const spec = specs.value[id] ?? {};
  return Object.fromEntries(
    Object.entries(spec).map(([key, s]) => [key, String(s.default)]),
  );
}

function vectorDefaultsFor(id: string): Record<string, string> {
  const spec = vectorSpecs.value[id] ?? {};
  return Object.fromEntries(
    Object.entries(spec).map(([key, s]) => [
      key,
      Array.from({ length: s.length ?? 3 }, () => "0").join(", "),
    ]),
  );
}

function formatVector(value: unknown): string {
  return Array.isArray(value) ? value.map((v) => String(v)).join(", ") : "";
}

function loadFromServer(config: TargetModelConfig | undefined) {
  const id =
    config?.method ?? props.payload.meta.target_model_default ?? FALLBACK_METHOD;
  method.value = id;
  const filled = defaultsFor(id);
  const filledVectors = vectorDefaultsFor(id);
  for (const [key, value] of Object.entries(config?.params ?? {})) {
    if (key in filled && !Array.isArray(value)) filled[key] = String(value);
    if (key in filledVectors) filledVectors[key] = formatVector(value);
  }
  params.value = filled;
  vectors.value = filledVectors;
}

watch(
  () => props.payload.registry.target_model,
  (config) => loadFromServer(config),
  { immediate: true },
);

watch(method, (id, previous) => {
  if (id === previous) return;
  const server = props.payload.registry.target_model;
  // 切回服务端当前模型：恢复它的参数；否则用新模型默认值
  if (server && server.method === id) loadFromServer(server);
  else {
    params.value = defaultsFor(id);
    vectors.value = vectorDefaultsFor(id);
  }
});

const currentSpec = computed(() => specs.value[method.value] ?? {});
const currentVectorSpec = computed(() => vectorSpecs.value[method.value] ?? {});

function parsed(): TargetModelConfig | string {
  const out: Record<string, number | number[]> = {};
  for (const [key, spec] of Object.entries(currentSpec.value)) {
    const raw = (params.value[key] ?? "").trim();
    if (!raw) continue;
    const num = Number(raw);
    const label = spec.label || key;
    if (!Number.isFinite(num)) return `「${label}」不是数字`;
    if (num < spec.min || num > spec.max)
      return `「${label}」超范围：限 ${spec.min}~${spec.max}`;
    if (spec.integer && !Number.isInteger(num)) return `「${label}」必须是整数`;
    out[key] = num;
  }
  for (const [key, spec] of Object.entries(currentVectorSpec.value)) {
    const raw = (vectors.value[key] ?? "").trim();
    const length = spec.length ?? 3;
    const label = spec.label || key;
    if (!raw) continue;
    const parts = raw.split(/[\s,，;]+/).filter((p) => p.length > 0);
    if (parts.length !== length) return `「${label}」需要 ${length} 个数`;
    const nums = parts.map(Number);
    if (nums.some((n) => !Number.isFinite(n))) return `「${label}」含非数字`;
    out[key] = nums;
  }
  return { method: method.value, params: out };
}

const validationError = computed(() => {
  const result = parsed();
  return typeof result === "string" ? result : "";
});

const server = computed(() => props.payload.registry.target_model);

function sameNumber(a: unknown, b: unknown): boolean {
  if (Array.isArray(a) || Array.isArray(b)) {
    const x = Array.isArray(a) ? a : [];
    const y = Array.isArray(b) ? b : [];
    return x.length === y.length && x.every((v, i) => Math.abs(Number(v) - Number(y[i])) < 1e-9);
  }
  return Math.abs(Number(a) - Number(b)) < 1e-9;
}

const isCurrent = computed(() => {
  const current = server.value;
  if (!current || current.method !== method.value) return false;
  const result = parsed();
  if (typeof result === "string") return false;
  const scalarOk = Object.keys(currentSpec.value).every((key) => {
    const d = currentSpec.value[key]!.default;
    return sameNumber(result.params[key] ?? d, current.params[key] ?? d);
  });
  const vectorOk = Object.keys(currentVectorSpec.value).every((key) => {
    const length = currentVectorSpec.value[key]!.length ?? 3;
    const zeros = Array.from({ length }, () => 0);
    return sameNumber(result.params[key] ?? zeros, current.params[key] ?? zeros);
  });
  return scalarOk && vectorOk;
});

const calibrated = computed(() => {
  if (method.value !== "panel_anchor") return true;
  return ["anchor_offset_wall_mm", "point1_offset_wall_mm", "point3_offset_wall_mm"].some(
    (key) => (vectors.value[key] ?? "").split(/[\s,，;]+/).some((p) => Math.abs(Number(p)) > 1e-9),
  );
});

function resetDefaults() {
  params.value = defaultsFor(method.value);
  vectors.value = vectorDefaultsFor(method.value);
}

function submit() {
  const result = parsed();
  if (typeof result === "string") return;
  emit("save", result);
}
</script>

<template>
  <section class="card stack">
    <h2>自动选点模型 <span class="lvl-tag">7005 粉点→绿点</span></h2>
    <p class="sub">
      7005 从哪个参考点、沿柜面坐标系加什么偏移得到目的点（绿点）。
      <strong>knob_mask_center</strong> = 现有 0.2.0-s（旋钮 mask 中心 + 内置常量）；
      <strong>panel_anchor</strong> = 面板矩形中心 → 锚点（旋钮轴心）→ 点 1/点 3，
      偏移由 <code>tools/calibrate_panel_anchor.py</code> 标定后写入（也可加 <code>--write</code> 直接写到这里）。
      保存后重启 7005 生效。
    </p>
    <div class="controls">
      <label class="field method">模型
        <select v-model="method">
          <option v-for="id in methods" :key="id" :value="id">
            {{ labels[id] || id }}
          </option>
        </select>
      </label>
      <label
        v-for="(spec, key) in currentSpec"
        :key="key"
        class="field param"
        :title="`范围 ${spec.min}~${spec.max}，默认 ${spec.default}${spec.integer ? '，整数' : ''}`"
      >
        {{ spec.label || key }}
        <input
          v-model="params[key]"
          type="number"
          :min="spec.min"
          :max="spec.max"
          :step="spec.integer ? 1 : 'any'"
          :placeholder="String(spec.default)"
        />
      </label>
    </div>
    <div v-if="Object.keys(currentVectorSpec).length" class="controls">
      <label
        v-for="(spec, key) in currentVectorSpec"
        :key="key"
        class="field vector"
        :title="`${spec.length ?? 3} 个数，逗号分隔`"
      >
        {{ spec.label || key }}
        <input v-model="vectors[key]" type="text" placeholder="0, 0, 0" />
      </label>
    </div>
    <div class="foot">
      <span v-if="validationError" class="badge missing">{{ validationError }}</span>
      <span v-else-if="!calibrated" class="badge missing">panel_anchor 偏移全为 0：尚未标定，自动找点会失败</span>
      <span v-else-if="isCurrent" class="badge on">当前配置</span>
      <span v-else class="badge plain off">有未保存修改</span>
      <span v-if="server" class="dim mono">
        当前：{{ labels[server.method] || server.method }}
        <template v-if="versions[server.method]">（model_version {{ versions[server.method] }}）</template>
      </span>
      <span class="spacer"></span>
      <button class="btn" :disabled="busy" @click="resetDefaults">恢复默认参数</button>
      <button
        class="btn primary"
        :disabled="busy || isCurrent || !!validationError"
        @click="submit"
      >
        {{ isCurrent ? "已是当前配置" : "保存自动选点模型" }}
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

.controls + .controls {
  margin-top: 10px;
}

.controls .method {
  min-width: 320px;
  flex: 1 1 320px;
}

.controls .param {
  width: 190px;
}

.controls .vector {
  width: 300px;
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
