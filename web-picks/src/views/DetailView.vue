<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import {
  adjustmentMagnitude,
  fetchRecords,
  fileUrl,
  formatBytes,
  formatTime,
  formatVec,
  wallAdjustment,
  type PickRecord,
} from "../lib/api";
import PlyViewer from "../components/PlyViewer.vue";
import SnapshotViewer from "../components/SnapshotViewer.vue";

const route = useRoute();
const name = route.params.name as string;

const record = ref<PickRecord | null>(null);
const neighbors = ref<{ prev: string | null; next: string | null }>({
  prev: null,
  next: null,
});
const loading = ref(true);
const error = ref("");
const showRawJson = ref(false);

onMounted(async () => {
  try {
    const all = await fetchRecords();
    const idx = all.findIndex((r) => r.name === name);
    if (idx < 0) {
      error.value = "记录不存在";
      return;
    }
    record.value = all[idx];
    // 列表按时间倒序：idx+1 更旧，idx-1 更新
    neighbors.value = {
      prev: all[idx + 1]?.name ?? null,
      next: all[idx - 1]?.name ?? null,
    };
  } catch (e) {
    error.value = String(e);
  } finally {
    loading.value = false;
  }
});

const meta = computed(() => record.value?.meta ?? {});
const fit = computed(() => meta.value.auto_target?.panel_fit_quality ?? null);
const adjMag = computed(() =>
  record.value ? adjustmentMagnitude(record.value.meta) : null,
);

const wallAdj = computed(() => wallAdjustment(meta.value));
</script>

<template>
  <div v-if="error" class="error-box">{{ error }}</div>
  <div v-else-if="loading" class="loading">加载中…</div>
  <template v-else-if="record">
    <div class="head">
      <RouterLink class="back" to="/">← 返回画廊</RouterLink>
      <h1 class="mono">{{ name }}</h1>
      <span
        v-if="meta.matched_detection_name"
        class="badge"
        :class="meta.matched_detection_name === '远方' ? 'remote' : 'local'"
      >
        {{ meta.matched_detection_name }}
      </span>
      <span v-if="meta.target_point_slot != null" class="badge slot">
        槽位 {{ meta.target_point_slot }}
      </span>
      <div class="spacer" />
      <RouterLink
        v-if="neighbors.prev"
        class="nav-btn"
        :to="`/pick/${neighbors.prev}`"
      >
        ← 更旧
      </RouterLink>
      <RouterLink
        v-if="neighbors.next"
        class="nav-btn"
        :to="`/pick/${neighbors.next}`"
      >
        更新 →
      </RouterLink>
    </div>

    <div class="split">
      <div class="pane">
        <h2 class="section-title">确认时截图</h2>
        <SnapshotViewer
          :url="fileUrl(name, 'snapshot.jpg')"
          :boxes="meta.yolo_boxes"
        />
      </div>
      <div class="pane">
        <h2 class="section-title">
          目标附近点云（半径 {{ ((meta.crop_radius_m ?? 0.2) * 100).toFixed(0) }} cm，相机坐标系）
        </h2>
        <div class="ply-host card">
          <PlyViewer
            :url="fileUrl(name, 'cloud.ply')"
            :wall-axes="meta.auto_target?.wall_axes_camera"
            :axes-origin="meta.panel_center_camera_m"
          />
        </div>
      </div>
    </div>

    <div class="panels">
      <div class="card panel">
        <h3>基本信息</h3>
        <dl>
          <dt>保存时间</dt>
          <dd class="mono">{{ formatTime(meta.saved_at) }}</dd>
          <dt>capture ID</dt>
          <dd class="mono">{{ meta.capture_id?.slice(0, 12) }}…</dd>
          <dt>识别来源</dt>
          <dd class="mono">{{ meta.selection_source ?? "-" }}</dd>
          <dt>模型版本</dt>
          <dd class="mono">{{ meta.model_version ?? "-" }}</dd>
          <dt>点云大小</dt>
          <dd class="mono">{{ formatBytes(record.cloud_bytes) }}</dd>
        </dl>
      </div>

      <div class="card panel">
        <h3>微调量</h3>
        <dl>
          <dt>相机系 (mm)</dt>
          <dd class="mono">{{ formatVec(meta.adjustment_mm, 1) }}</dd>
          <template v-if="wallAdj">
            <dt>墙面系 (mm){{ wallAdj.derived ? "（换算）" : "" }}</dt>
            <dd class="mono">
              右 {{ wallAdj.x.toFixed(1) }} / 上 {{ wallAdj.z.toFixed(1) }} / 入墙
              {{ wallAdj.y.toFixed(1) }}
            </dd>
          </template>
          <dt>微调模长</dt>
          <dd class="mono highlight">
            {{ adjMag !== null ? adjMag.toFixed(1) + " mm" : "-" }}
          </dd>
          <dt>接近偏移</dt>
          <dd class="mono">{{ (meta.approach_offset_m ?? 0).toFixed(3) }} m</dd>
        </dl>
      </div>

      <div class="card panel">
        <h3>坐标（m）</h3>
        <dl>
          <dt>面板中心（相机）</dt>
          <dd class="mono">{{ formatVec(meta.panel_center_camera_m) }}</dd>
          <dt>算法目标（相机）</dt>
          <dd class="mono">{{ formatVec(meta.reference_camera_m) }}</dd>
          <dt>最终目的（相机）</dt>
          <dd class="mono">{{ formatVec(meta.final_p_camera_m) }}</dd>
          <dt>p_root</dt>
          <dd class="mono">{{ formatVec(meta.confirm_result?.p_root) }}</dd>
          <dt>p_torso</dt>
          <dd class="mono">{{ formatVec(meta.confirm_result?.p_torso) }}</dd>
          <dt>深度</dt>
          <dd class="mono">
            {{
              meta.confirm_result?.depth_mm != null
                ? meta.confirm_result.depth_mm.toFixed(1) + " mm"
                : "-"
            }}
          </dd>
        </dl>
      </div>

      <div v-if="fit" class="card panel">
        <h3>面板拟合质量</h3>
        <dl>
          <dt>内点数</dt>
          <dd class="mono">{{ fit.inlier_count ?? "-" }}</dd>
          <dt>内点率</dt>
          <dd class="mono">
            {{ fit.inlier_ratio != null ? (fit.inlier_ratio * 100).toFixed(1) + " %" : "-" }}
          </dd>
          <dt>RMS</dt>
          <dd class="mono">
            {{ fit.rms_m != null ? (fit.rms_m * 1000).toFixed(2) + " mm" : "-" }}
          </dd>
          <dt>面板尺寸</dt>
          <dd class="mono">
            {{
              fit.long_length_m != null && fit.short_length_m != null
                ? (fit.long_length_m * 1000).toFixed(0) +
                  " × " +
                  (fit.short_length_m * 1000).toFixed(0) +
                  " mm"
                : "-"
            }}
          </dd>
          <dt>朝向来源</dt>
          <dd class="mono">{{ fit.orientation_source ?? "-" }}</dd>
        </dl>
      </div>
    </div>

    <div class="raw">
      <button class="chip" @click="showRawJson = !showRawJson">
        {{ showRawJson ? "收起" : "查看" }}完整 meta.json
      </button>
      <a class="chip" :href="fileUrl(name, 'cloud.ply')" download>下载 cloud.ply</a>
      <a class="chip" :href="fileUrl(name, 'meta.json')" target="_blank">
        打开 meta.json
      </a>
      <pre v-if="showRawJson" class="card json mono">{{
        JSON.stringify(meta, null, 2)
      }}</pre>
    </div>
  </template>
</template>

<style scoped>
.head {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 20px;
  flex-wrap: wrap;
}

.head h1 {
  font-size: 17px;
  margin: 0;
  font-weight: 600;
}

.back {
  color: var(--text-dim);
  font-size: 14px;
}

.back:hover {
  color: var(--accent);
}

.spacer {
  flex: 1;
}

.nav-btn {
  padding: 6px 14px;
  border: 1px solid var(--border);
  border-radius: 999px;
  font-size: 13px;
  color: var(--text-dim);
}

.nav-btn:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.split {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 20px;
  margin-bottom: 20px;
}

@media (max-width: 1100px) {
  .split {
    grid-template-columns: 1fr;
  }
}

.ply-host {
  height: 100%;
  min-height: 420px;
  overflow: hidden;
}

.panels {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
  gap: 16px;
  margin-bottom: 20px;
}

.panel {
  padding: 16px 18px;
}

.panel h3 {
  margin: 0 0 12px;
  font-size: 14px;
  color: var(--accent);
  font-weight: 700;
}

.panel dl {
  margin: 0;
  display: grid;
  grid-template-columns: auto 1fr;
  gap: 7px 14px;
  font-size: 13px;
}

.panel dt {
  color: var(--text-dim);
  white-space: nowrap;
}

.panel dd {
  margin: 0;
  text-align: right;
  word-break: break-all;
}

.highlight {
  color: var(--amber);
  font-weight: 700;
}

.raw {
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
  align-items: flex-start;
}

.json {
  width: 100%;
  padding: 16px;
  font-size: 12px;
  overflow: auto;
  max-height: 480px;
  margin: 4px 0 0;
}
</style>
