<script setup lang="ts">
import { computed } from "vue";
import type { Capability, Payload } from "../lib/api";
import { DIRECTION_LABELS, PARAM_LABELS, SITE_LABELS } from "../lib/api";

const props = defineProps<{ payload: Payload; busy: boolean }>();
const emit = defineEmits<{
  add: [];
  edit: [cap: Capability];
  toggle: [cap: Capability];
  remove: [cap: Capability];
}>();

interface Group {
  key: string;
  arm: string;
  handId: string;
  label: string;
  calibStatus: string;
  isActive: boolean;
  caps: Capability[];
}

const CALIB_TEXT: Record<string, string> = {
  ready: "标定就绪",
  pending: "标定待补",
  missing: "标定未登记",
};

const groups = computed<Group[]>(() => {
  const registry = props.payload.registry;
  const map = new Map<string, Group>();
  for (const cap of registry.capabilities) {
    const key = `${cap.arm}__${cap.hand_id}`;
    if (!map.has(key)) {
      const hand = registry.hands.find((h) => h.id === cap.hand_id);
      const calib = props.payload.calibrations.find(
        (c) => c.arm === cap.arm && c.hand_id === cap.hand_id,
      );
      const active = registry.active;
      map.set(key, {
        key,
        arm: cap.arm,
        handId: cap.hand_id,
        label: `${props.payload.meta.arm_labels[cap.arm] || cap.arm} · ${
          hand?.name ?? cap.hand_id
        }`,
        calibStatus: calib?.status ?? "missing",
        isActive:
          !!active &&
          active.arm === cap.arm &&
          active.hand_id === cap.hand_id,
        caps: [],
      });
    }
    map.get(key)!.caps.push(cap);
  }
  return [...map.values()];
});

function methodLabel(cap: Capability): string {
  return props.payload.meta.method_labels[cap.method] ?? cap.method;
}

function isImplemented(cap: Capability): boolean {
  return props.payload.meta.implemented_methods.includes(cap.method);
}

function paramText(cap: Capability): string {
  const entries = Object.entries(cap.method_params);
  if (!entries.length) return "无参数";
  return entries
    .map(([key, value]) => `${PARAM_LABELS[key] ?? key} ${value}`)
    .join(" · ");
}
</script>

<template>
  <section class="card stack">
    <div class="head-row">
      <div>
        <h2>能力列表 <span class="lvl-tag">三级 + 四级</span></h2>
        <p class="sub">
          任务配置（如 旋钮右到左）+ 实现方式（拨动 / 拧）；每种实现方式有
          各自的参数块，起手式正则随能力条目注入流程。
        </p>
      </div>
      <button class="btn primary" @click="emit('add')">＋ 新增能力</button>
    </div>

    <div v-for="group in groups" :key="group.key" class="group">
      <div class="group-head">
        <span class="group-name">{{ group.label }}</span>
        <span class="badge" :class="group.calibStatus">
          {{ CALIB_TEXT[group.calibStatus] }}
        </span>
        <span v-if="group.isActive" class="badge on">当前激活</span>
      </div>
      <div class="cap-grid">
        <article
          v-for="cap in group.caps"
          :key="cap.id"
          class="cap"
          :class="{ disabled: !cap.enabled }"
        >
          <div class="cap-top">
            <span class="task-name">{{ cap.task.name }}</span>
            <label class="switch" :title="cap.enabled ? '停用' : '启用'">
              <input
                type="checkbox"
                :checked="cap.enabled"
                :disabled="busy"
                @change="emit('toggle', cap)"
              />
              <span class="track"></span>
            </label>
          </div>
          <div class="chips">
            <span class="tag accent">
              {{ DIRECTION_LABELS[cap.task.direction] || cap.task.direction }}
            </span>
            <span class="tag">
              {{ methodLabel(cap) }}
            </span>
            <span v-if="!isImplemented(cap)" class="badge pending plain">
              未实现
            </span>
            <span
              v-for="site in cap.task.sites"
              :key="site"
              class="tag"
            >{{ SITE_LABELS[site] || site }}</span>
          </div>
          <p class="params dim">{{ paramText(cap) }}</p>
          <p v-if="cap.notes" class="notes dim">{{ cap.notes }}</p>
          <div class="cap-foot">
            <span class="mono dim">{{ cap.id }}</span>
            <span class="ops">
              <button class="btn sm ghost" @click="emit('edit', cap)">
                编辑
              </button>
              <button
                class="btn sm ghost danger"
                @click="emit('remove', cap)"
              >
                删除
              </button>
            </span>
          </div>
        </article>
      </div>
    </div>
    <p v-if="!groups.length" class="dim empty">尚无能力条目</p>
  </section>
</template>

<style scoped>
.head-row {
  display: flex;
  align-items: flex-start;
  justify-content: space-between;
  gap: 16px;
}

.head-row .btn {
  flex: none;
  margin-top: 2px;
}

.group {
  margin-top: 6px;
}

.group + .group {
  margin-top: 20px;
}

.group-head {
  display: flex;
  align-items: center;
  gap: 10px;
  margin-bottom: 10px;
}

.group-name {
  font-size: 14px;
  font-weight: 700;
  color: var(--accent);
}

.cap-grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(320px, 1fr));
  gap: 12px;
}

.cap {
  background: var(--bg-soft);
  border: 1px solid var(--border);
  border-radius: 12px;
  padding: 14px 16px;
  display: flex;
  flex-direction: column;
  gap: 9px;
  transition: border-color 0.15s, opacity 0.15s;
}

.cap:hover {
  border-color: rgba(86, 217, 197, 0.4);
}

.cap.disabled {
  opacity: 0.55;
}

.cap-top {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.task-name {
  font-size: 15px;
  font-weight: 700;
}

.chips {
  display: flex;
  flex-wrap: wrap;
  gap: 6px;
  align-items: center;
}

.tag.accent {
  color: var(--accent);
  border-color: rgba(86, 217, 197, 0.35);
  background: var(--accent-soft);
}

.params {
  margin: 0;
  font-size: 12.5px;
  line-height: 1.6;
}

.notes {
  margin: 0;
  font-size: 12px;
}

.cap-foot {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 8px;
  margin-top: auto;
  padding-top: 4px;
}

.ops {
  display: flex;
  gap: 6px;
}

.empty {
  text-align: center;
  padding: 22px;
}
</style>
