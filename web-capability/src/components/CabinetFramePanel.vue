<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type { CabinetFrameConfig, ParamSpec, Payload } from "../lib/api";

const props = defineProps<{ payload: Payload; busy: boolean }>();
const emit = defineEmits<{ save: [config: CabinetFrameConfig] }>();

const FALLBACK_METHOD = "method1_plane_analysis";

const methods = computed<string[]>(
  () => props.payload.meta.cabinet_frame_methods ?? [FALLBACK_METHOD],
);
const labels = computed<Record<string, string>>(
  () => props.payload.meta.cabinet_frame_method_labels ?? {},
);
const specs = computed<Record<string, Record<string, ParamSpec>>>(
  () => props.payload.meta.cabinet_frame_param_specs ?? {},
);

const method = ref(FALLBACK_METHOD);
// 参数以字符串编辑，提交时转数字；空串 = 用默认值（服务端补齐）
const params = ref<Record<string, string>>({});

function defaultsFor(id: string): Record<string, string> {
  const spec = specs.value[id] ?? {};
  return Object.fromEntries(
    Object.entries(spec).map(([key, s]) => [key, String(s.default)]),
  );
}

function loadFromServer(config: CabinetFrameConfig | undefined) {
  const id = config?.method ?? props.payload.meta.cabinet_frame_default_method ?? FALLBACK_METHOD;
  method.value = id;
  const filled = defaultsFor(id);
  for (const [key, value] of Object.entries(config?.params ?? {})) {
    if (key in filled) filled[key] = String(value);
  }
  params.value = filled;
}

watch(
  () => props.payload.registry.cabinet_frame,
  (config) => loadFromServer(config),
  { immediate: true },
);

// 切换方法：参数集不同，直接换成新方法的默认值
watch(method, (id, previous) => {
  if (id !== previous) params.value = defaultsFor(id);
});

const currentSpec = computed(() => specs.value[method.value] ?? {});

function parsed(): CabinetFrameConfig | string {
  const out: Record<string, number> = {};
  for (const [key, spec] of Object.entries(currentSpec.value)) {
    const raw = (params.value[key] ?? "").trim();
    if (!raw) continue; // 留空 → 服务端按默认值补齐
    const num = Number(raw);
    const label = spec.label || key;
    if (!Number.isFinite(num)) return `「${label}」不是数字`;
    if (num < spec.min || num > spec.max)
      return `「${label}」超范围：限 ${spec.min}~${spec.max}`;
    if (spec.integer && !Number.isInteger(num))
      return `「${label}」必须是整数`;
    out[key] = num;
  }
  return { method: method.value, params: out };
}

const validationError = computed(() => {
  const result = parsed();
  return typeof result === "string" ? result : "";
});

const server = computed(() => props.payload.registry.cabinet_frame);

const isCurrent = computed(() => {
  const current = server.value;
  if (!current || current.method !== method.value) return false;
  const result = parsed();
  if (typeof result === "string") return false;
  const spec = currentSpec.value;
  return Object.keys(spec).every((key) => {
    const mine = result.params[key] ?? spec[key]!.default;
    const theirs = current.params[key] ?? spec[key]!.default;
    return Math.abs(mine - theirs) < 1e-9;
  });
});

function resetDefaults() {
  params.value = defaultsFor(method.value);
}

function submit() {
  const result = parsed();
  if (typeof result === "string") return;
  emit("save", result);
}
</script>

<template>
  <section class="card stack">
    <h2>柜面坐标系构建 <span class="lvl-tag">7005 点云</span></h2>
    <p class="sub">
      7005 在冻结 RGB-D 帧上拟合柜面坐标系（X=右、Y=入墙、Z=上）用哪种方法。
      自动定位的墙面系偏移、选点横移轴、扭转基都依赖它；
      <strong>墙面系偏移配置是在特定方法下标定的，换方法后需重新核对</strong>。保存后重启 7005 生效。
    </p>
    <div class="controls">
      <label class="field method">构建方法
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
    <div class="foot">
      <span v-if="validationError" class="badge missing">{{ validationError }}</span>
      <span v-else-if="isCurrent" class="badge on">当前配置</span>
      <span v-else class="badge plain off">有未保存修改</span>
      <span v-if="server" class="dim mono">
        当前：{{ labels[server.method] || server.method }}
      </span>
      <span class="spacer"></span>
      <button class="btn" :disabled="busy" @click="resetDefaults">恢复默认参数</button>
      <button
        class="btn primary"
        :disabled="busy || isCurrent || !!validationError"
        @click="submit"
      >
        {{ isCurrent ? "已是当前配置" : "保存柜面坐标系配置" }}
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
  min-width: 300px;
  flex: 1 1 300px;
}

.controls .param {
  width: 170px;
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
