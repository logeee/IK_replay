<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type { Capability, Payload } from "../lib/api";
import { DEFAULT_POSE_PATTERNS, DIRECTION_LABELS } from "../lib/api";

const props = defineProps<{ payload: Payload; busy: boolean }>();
const emit = defineEmits<{
  save: [capabilityId: string, names: string[]];
}>();

const capabilities = computed(() => props.payload.registry.capabilities);

function handName(id: string | undefined): string {
  if (!id) return "?";
  return props.payload.registry.hands.find((h) => h.id === id)?.name ?? id;
}

function capLabel(cap: Capability): string {
  const arm = props.payload.meta.arm_labels[cap.arm] || cap.arm;
  const method = props.payload.meta.method_labels[cap.method] || cap.method;
  const off = cap.enabled ? "" : "（已停用）";
  return `${arm}+${handName(cap.hand_id)} · ${cap.task.name}·${method}${off}`;
}

const capabilityId = ref("");

/** 默认选中激活组合下第一个启用条目 */
watch(
  capabilities,
  (list) => {
    if (list.some((c) => c.id === capabilityId.value)) return;
    const active = props.payload.registry.active;
    const preferred = active
      ? list.find(
          (c) =>
            c.enabled &&
            c.arm === active.arm &&
            c.hand_id === active.hand_id,
        )
      : undefined;
    capabilityId.value = (preferred ?? list[0])?.id ?? "";
  },
  { immediate: true },
);

const currentCap = computed(
  () => capabilities.value.find((c) => c.id === capabilityId.value) ?? null,
);

/** 条目实际生效的起手式正则（自配优先，否则方向内置）；自动路由也用它 */
const effectivePattern = computed<string>(() => {
  const cap = currentCap.value;
  if (!cap) return "";
  return (
    cap.assets.pose_pattern || DEFAULT_POSE_PATTERNS[cap.task.direction] || ""
  );
});

const patternRegex = computed<RegExp | null>(() => {
  if (!effectivePattern.value) return null;
  try {
    return new RegExp(effectivePattern.value);
  } catch {
    return null;
  }
});

function hitsPattern(name: string): boolean {
  return !!patternRegex.value && patternRegex.value.test(name);
}

/** 该条目已保存的认领（注册表里的权威值） */
const savedNames = computed<string[]>(
  () =>
    props.payload.registry.sequence_claims.find(
      (c) => c.capability_id === capabilityId.value,
    )?.names ?? [],
);

/** 勾选草稿：切条目 / 数据刷新时重置为已保存值 */
const selected = ref<string[]>([]);
watch(
  [savedNames, capabilityId],
  () => {
    selected.value = [...savedNames.value];
  },
  { immediate: true },
);

const dirty = computed(() => {
  const a = [...selected.value].sort();
  const b = [...savedNames.value].sort();
  return a.length !== b.length || a.some((v, i) => v !== b[i]);
});

/** 已认领但池中无文件的名字（文件被删/改名后残留），也要能取消 */
const orphanNames = computed(() =>
  savedNames.value.filter(
    (name) => !props.payload.sequence_pool.some((e) => e.name === name),
  ),
);

/** 某动作被哪些条目认领（展示占用情况） */
function claimedBy(name: string): string[] {
  return props.payload.registry.sequence_claims
    .filter((c) => c.names.includes(name))
    .map((c) => {
      const cap = capabilities.value.find((x) => x.id === c.capability_id);
      return cap ? `${cap.task.name}·${cap.method}` : c.capability_id;
    });
}

function selectMatched() {
  selected.value = [
    ...props.payload.sequence_pool
      .filter((e) => hitsPattern(e.name))
      .map((e) => e.name),
  ];
}

function clearAll() {
  selected.value = [];
}
</script>

<template>
  <section class="card">
    <h2>起手式认领 <span class="lvl-tag">按能力条目 · 公共动作池</span></h2>
    <p class="sub">
      data/sequences 是全组合共享的动作池；认领挂在能力条目（任务+方式）上
      ——拨和扭各认各的，互不影响（严格：没认领 = 该条目选档时不可用）。
      18001 录制新序列会按动作名命中的正则自动认领给对应条目。保存后重启
      17001 生效。
    </p>
    <div class="controls">
      <label class="field grow">能力条目
        <select v-model="capabilityId">
          <option v-for="c in capabilities" :key="c.id" :value="c.id">
            {{ capLabel(c) }}
          </option>
        </select>
      </label>
      <span class="badge plain off">
        已勾选 {{ selected.length }} / 池中 {{ payload.sequence_pool.length }}
      </span>
      <span class="spacer"></span>
      <button class="btn sm ghost" :disabled="busy" @click="selectMatched">
        全选命中正则的
      </button>
      <button class="btn sm ghost" :disabled="busy" @click="clearAll">
        清空
      </button>
    </div>
    <p v-if="currentCap" class="dim pattern-line">
      {{ DIRECTION_LABELS[currentCap.task.direction] || currentCap.task.direction }}
      ｜自动路由正则：
      <span class="mono">{{ effectivePattern || "（未配置，不参与自动认领）" }}</span>
    </p>

    <ul v-if="payload.sequence_pool.length || orphanNames.length" class="pool">
      <li v-for="entry in payload.sequence_pool" :key="entry.name" class="row">
        <label class="pick">
          <input v-model="selected" type="checkbox" :value="entry.name" />
          <span class="mono name">{{ entry.name }}</span>
        </label>
        <span class="tags">
          <span v-if="hitsPattern(entry.name)" class="tag hit">命中正则</span>
          <span v-if="entry.files > 1" class="tag">×{{ entry.files }} 次录制</span>
          <span v-if="entry.latest_created_at" class="tag">
            最近 {{ entry.latest_created_at }}
          </span>
          <span v-if="entry.recorded_combo" class="tag">
            录自 {{ handName(entry.recorded_combo.hand_id) }}
          </span>
          <span v-if="claimedBy(entry.name).length" class="dim claimers">
            已认领：{{ claimedBy(entry.name).join("、") }}
          </span>
          <span v-else class="dim claimers">无人认领</span>
        </span>
      </li>
      <li v-for="name in orphanNames" :key="`orphan-${name}`" class="row orphan">
        <label class="pick">
          <input v-model="selected" type="checkbox" :value="name" />
          <span class="mono name">{{ name }}</span>
        </label>
        <span class="tags">
          <span class="tag warn">池中无文件（已删除或改名）</span>
        </span>
      </li>
    </ul>
    <p v-else class="dim empty">动作池是空的——先在 18001 页面录制序列。</p>

    <div class="foot">
      <button
        class="btn primary"
        :disabled="busy || !dirty || !capabilityId"
        @click="emit('save', capabilityId, [...selected])"
      >
        {{ dirty ? "保存认领" : "认领无改动" }}
      </button>
    </div>
  </section>
</template>

<style scoped>
.controls {
  display: flex;
  align-items: flex-end;
  gap: 12px;
  flex-wrap: wrap;
  margin-bottom: 8px;
}

.controls .field.grow {
  min-width: 320px;
}

.controls .badge {
  margin-bottom: 6px;
}

.spacer {
  flex: 1;
}

.pattern-line {
  font-size: 12px;
  margin: 0 0 12px;
}

.pool {
  list-style: none;
  margin: 0 0 14px;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 6px;
  max-height: 420px;
  overflow-y: auto;
}

.row {
  display: flex;
  align-items: center;
  gap: 12px;
  background: var(--bg-soft);
  border: 1px solid var(--border);
  border-radius: 9px;
  padding: 8px 12px;
}

.row:hover {
  border-color: rgba(86, 217, 197, 0.4);
}

.row.orphan {
  border-style: dashed;
  opacity: 0.85;
}

.pick {
  display: flex;
  align-items: center;
  gap: 9px;
  cursor: pointer;
  flex: none;
}

.pick input {
  accent-color: #56d9c5;
  width: 15px;
  height: 15px;
}

.name {
  font-size: 13.5px;
}

.tags {
  display: flex;
  align-items: center;
  gap: 6px;
  flex-wrap: wrap;
  font-size: 12px;
  min-width: 0;
}

.claimers {
  font-size: 11.5px;
}

.tag.hit {
  color: #7de3d0;
  border-color: rgba(86, 217, 197, 0.4);
}

.tag.warn {
  color: #ffcf7d;
  border-color: rgba(255, 207, 125, 0.35);
}

.empty {
  margin: 4px 0 14px;
}

.foot {
  display: flex;
  justify-content: flex-end;
}
</style>
