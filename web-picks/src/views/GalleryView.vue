<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import {
  adjustmentMagnitude,
  fetchRecords,
  fileUrl,
  formatTime,
  type PickRecord,
} from "../lib/api";

const records = ref<PickRecord[]>([]);
const loading = ref(true);
const error = ref("");

const nameFilter = ref<string>("全部");
const slotFilter = ref<number | null>(null);
const sortKey = ref<"time" | "adj" | "conf">("time");

onMounted(async () => {
  try {
    records.value = await fetchRecords();
  } catch (e) {
    error.value = String(e);
  } finally {
    loading.value = false;
  }
});

const detectionNames = computed(() => {
  const s = new Set<string>();
  for (const r of records.value) {
    if (r.meta.matched_detection_name) s.add(r.meta.matched_detection_name);
  }
  return ["全部", ...s];
});

const slots = computed(() => {
  const s = new Set<number>();
  for (const r of records.value) {
    if (r.meta.target_point_slot != null) s.add(r.meta.target_point_slot);
  }
  return [...s].sort();
});

function bestConf(r: PickRecord): number | null {
  const boxes = r.meta.yolo_boxes;
  if (!boxes?.length) return null;
  return Math.max(...boxes.map((b) => b.conf));
}

const filtered = computed(() => {
  let list = records.value.filter((r) => {
    if (
      nameFilter.value !== "全部" &&
      r.meta.matched_detection_name !== nameFilter.value
    )
      return false;
    if (
      slotFilter.value !== null &&
      r.meta.target_point_slot !== slotFilter.value
    )
      return false;
    return true;
  });
  if (sortKey.value === "adj") {
    list = [...list].sort(
      (a, b) =>
        (adjustmentMagnitude(b.meta) ?? -1) -
        (adjustmentMagnitude(a.meta) ?? -1),
    );
  } else if (sortKey.value === "conf") {
    list = [...list].sort((a, b) => (bestConf(b) ?? -1) - (bestConf(a) ?? -1));
  }
  return list;
});

/** 微调量配色：越大越显眼，帮助一眼找到算法偏差大的记录 */
function adjClass(mm: number | null): string {
  if (mm === null) return "";
  if (mm >= 30) return "adj-high";
  if (mm >= 15) return "adj-mid";
  return "adj-low";
}

function flowSummary(r: PickRecord): string | null {
  const flow = r.meta.flow_context;
  if (!flow) return null;
  const parts: string[] = [];
  if (flow.distance_m != null) parts.push(`距柜面 ${flow.distance_m.toFixed(3)} m`);
  if (flow.opening_pose?.name) parts.push(flow.opening_pose.name);
  if (flow.target_lift_m != null) {
    const mm = flow.target_lift_m * 1000;
    parts.push(`本轮上抬 ${mm >= 0 ? "+" : ""}${mm.toFixed(1)} mm`);
  }
  return parts.join(" · ") || null;
}
</script>

<template>
  <div v-if="error" class="error-box">
    加载失败：{{ error }}<br />
    请确认服务已启动：<span class="mono">python tools/picks_server.py</span>
  </div>
  <div v-else-if="loading" class="loading">加载中…</div>
  <template v-else>
    <div class="filters">
      <div class="chip-row">
        <span class="filter-label">识别来源</span>
        <button
          v-for="n in detectionNames"
          :key="n"
          class="chip"
          :class="{ active: nameFilter === n }"
          @click="nameFilter = n"
        >
          {{ n }}
        </button>
      </div>
      <div class="chip-row">
        <span class="filter-label">目标槽位</span>
        <button
          class="chip"
          :class="{ active: slotFilter === null }"
          @click="slotFilter = null"
        >
          全部
        </button>
        <button
          v-for="s in slots"
          :key="s"
          class="chip"
          :class="{ active: slotFilter === s }"
          @click="slotFilter = s"
        >
          槽位 {{ s }}
        </button>
      </div>
      <div class="chip-row">
        <span class="filter-label">排序</span>
        <button
          class="chip"
          :class="{ active: sortKey === 'time' }"
          @click="sortKey = 'time'"
        >
          时间最新
        </button>
        <button
          class="chip"
          :class="{ active: sortKey === 'adj' }"
          @click="sortKey = 'adj'"
        >
          微调量最大
        </button>
        <button
          class="chip"
          :class="{ active: sortKey === 'conf' }"
          @click="sortKey = 'conf'"
        >
          置信度最高
        </button>
      </div>
    </div>

    <div v-if="!filtered.length" class="empty">没有符合条件的记录</div>
    <div class="grid">
      <RouterLink
        v-for="r in filtered"
        :key="r.name"
        class="card pick-card"
        :to="`/pick/${r.name}`"
      >
        <div class="thumb">
          <img :src="fileUrl(r.name, 'snapshot.jpg')" loading="lazy" />
          <span
            v-if="adjustmentMagnitude(r.meta) !== null"
            class="adj-tag"
            :class="adjClass(adjustmentMagnitude(r.meta))"
          >
            微调 {{ adjustmentMagnitude(r.meta)!.toFixed(1) }} mm
          </span>
        </div>
        <div class="info">
          <div class="row">
            <span
              v-if="r.meta.matched_detection_name"
              class="badge"
              :class="r.meta.matched_detection_name === '远方' ? 'remote' : 'local'"
            >
              {{ r.meta.matched_detection_name }}
            </span>
            <span v-if="r.meta.target_point_slot != null" class="badge slot">
              槽位 {{ r.meta.target_point_slot }}
            </span>
            <span
              v-if="r.flip?.after?.success != null"
              class="badge"
              :class="r.flip!.after!.success ? 'exec-done' : 'exec-error'"
            >
              {{ r.flip!.after!.success ? "拨动✓" : "拨动✗" }}
            </span>
            <span v-if="bestConf(r) !== null" class="conf">
              conf {{ bestConf(r)!.toFixed(2) }}
            </span>
          </div>
          <div v-if="flowSummary(r)" class="flow-summary">
            {{ flowSummary(r) }}
          </div>
          <div class="time mono">{{ formatTime(r.meta.saved_at) }}</div>
        </div>
      </RouterLink>
    </div>
  </template>
</template>

<style scoped>
.filters {
  display: flex;
  flex-direction: column;
  gap: 10px;
  margin-bottom: 24px;
}

.filter-label {
  color: var(--text-dim);
  font-size: 13px;
  min-width: 60px;
}

.grid {
  display: grid;
  grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
  gap: 18px;
}

.pick-card {
  display: block;
  overflow: hidden;
  color: var(--text);
  transition: transform 0.15s, border-color 0.15s, background 0.15s;
}

.pick-card:hover {
  transform: translateY(-3px);
  border-color: var(--accent);
  background: var(--card-hover);
}

.thumb {
  position: relative;
  aspect-ratio: 16 / 9;
  background: #000;
}

.thumb img {
  width: 100%;
  height: 100%;
  object-fit: cover;
  display: block;
}

.adj-tag {
  position: absolute;
  right: 10px;
  top: 10px;
  padding: 3px 10px;
  border-radius: 999px;
  font-size: 12px;
  font-weight: 700;
  backdrop-filter: blur(6px);
}

.adj-low {
  background: rgba(90, 212, 111, 0.25);
  color: #b8f5c4;
}

.adj-mid {
  background: rgba(242, 184, 75, 0.3);
  color: #ffe1a1;
}

.adj-high {
  background: rgba(255, 93, 93, 0.32);
  color: #ffc4c4;
}

.info {
  padding: 12px 14px 14px;
}

.row {
  display: flex;
  align-items: center;
  gap: 8px;
  margin-bottom: 8px;
}

.conf {
  color: var(--text-dim);
  font-size: 12px;
  margin-left: auto;
}

.time {
  color: var(--text-dim);
  font-size: 13px;
}

.flow-summary {
  margin: -1px 0 8px;
  color: var(--amber);
  font-size: 12px;
  font-weight: 650;
  overflow-wrap: anywhere;
}
</style>
