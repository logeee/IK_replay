<script setup lang="ts">
import { computed, onMounted, ref } from "vue";
import { useRoute } from "vue-router";
import {
  adjustmentMagnitude,
  execResultKind,
  fetchExecutionDetail,
  fetchExecutions,
  fetchRecords,
  fileUrl,
  formatBytes,
  formatTime,
  formatVec,
  wallAdjustment,
  type ExecSummary,
  type PickRecord,
  type TraceSample,
} from "../lib/api";
import ExecTimeline from "../components/ExecTimeline.vue";
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
    loadExecutions(all[idx].meta.capture_id);
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
const flowContext = computed(() => meta.value.flow_context ?? null);

// ---- 该次选点对应的执行记录（按 capture_id 关联 18001 的 JSONL）----
const executions = ref<ExecSummary[]>([]);
const traces = ref<Record<string, TraceSample[]>>({});
const traceLoading = ref<string | null>(null);

async function loadExecutions(captureId?: string) {
  if (!captureId) return;
  try {
    const all = await fetchExecutions();
    executions.value = all.filter((e) => e.capture_id === captureId);
  } catch {
    /* 执行日志目录可能不存在，静默留空 */
  }
}

async function toggleTrace(exec: ExecSummary) {
  if (traces.value[exec.id]) {
    const next = { ...traces.value };
    delete next[exec.id];
    traces.value = next;
    return;
  }
  traceLoading.value = exec.id;
  try {
    const record = await fetchExecutionDetail(exec.id);
    traces.value = {
      ...traces.value,
      [exec.id]: (record.torso_trace ?? []) as TraceSample[],
    };
  } finally {
    traceLoading.value = null;
  }
}

function resultLabel(result?: string): string {
  if (result === "done") return "完成";
  if (result === "cancelled") return "已中止";
  return result || "异常";
}

function fmtMm(v?: number | null): string {
  return v != null ? `${v.toFixed(1)}` : "-";
}

function fmtMeters(v?: number | null): string {
  return v != null ? `${v.toFixed(3)} m` : "-";
}

function fmtSignedMetersAsMm(v?: number | null): string {
  if (v == null) return "-";
  const mm = v * 1000;
  return `${mm >= 0 ? "+" : ""}${mm.toFixed(1)} mm`;
}
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

    <section class="card key-config">
      <div class="key-config-head">
        <div>
          <h2>关键配置与实际执行参数</h2>
          <p>点云配置偏移与流程追加的根坐标偏移分开显示，避免漏算逐轮上抬。</p>
        </div>
        <span v-if="flowContext?.round" class="badge slot">
          第 {{ flowContext.round }}/{{ flowContext.max_rounds ?? "?" }} 轮
        </span>
      </div>
      <div class="key-config-grid">
        <article class="key-block">
          <div class="key-label">点云配置偏移</div>
          <div v-if="wallAdj" class="key-value wall-value">
            右 {{ wallAdj.x.toFixed(1) }} / 上 {{ wallAdj.z.toFixed(1) }} / 入墙
            {{ wallAdj.y.toFixed(1) }} mm
          </div>
          <div v-else class="key-value">墙面系 -</div>
          <div class="key-detail mono">
            相机系 {{ formatVec(meta.adjustment_mm, 1) }} mm
          </div>
          <div class="key-chips">
            <span>模长 {{ adjMag !== null ? adjMag.toFixed(1) + " mm" : "-" }}</span>
            <span>接近 {{ fmtMeters(meta.approach_offset_m ?? flowContext?.approach_offset_m) }}</span>
          </div>
        </article>

        <article class="key-block">
          <div class="key-label">距离与起手式</div>
          <template v-if="flowContext">
            <div class="key-value">{{ fmtMeters(flowContext.distance_m) }}</div>
            <div class="key-detail opening-name">
              {{ flowContext.opening_pose?.name ?? "未记录起手式" }}
            </div>
            <div class="key-chips">
              <span>
                适用档位
                {{ fmtMeters(flowContext.opening_pose?.min_distance_m) }}
              </span>
              <span v-if="flowContext.opening_pose?.manual">手动选择</span>
              <span v-else>自动选择</span>
            </div>
            <div v-if="flowContext.opening_pose?.file" class="key-file mono">
              {{ flowContext.opening_pose.file }}
            </div>
          </template>
          <div v-else class="key-missing">旧记录未保存距柜面和起手式</div>
        </article>

        <article class="key-block extra-offset">
          <div class="key-label">本轮流程附加偏移</div>
          <template v-if="flowContext">
            <div class="key-value">
              目标根坐标 Z {{ fmtSignedMetersAsMm(flowContext.target_lift_m) }}
            </div>
            <div class="key-detail">
              上抬规则：首轮
              {{ fmtSignedMetersAsMm(flowContext.lift_base_m) }}，每轮
              {{ fmtSignedMetersAsMm(flowContext.lift_step_m) }}，上限
              {{ fmtSignedMetersAsMm(flowContext.lift_max_m) }}
            </div>
            <div class="key-chips">
              <span>
                轨迹中段抬高
                {{ fmtSignedMetersAsMm(flowContext.planner_mid_lift_m) }}
              </span>
            </div>
            <div
              v-if="
                flowContext.picked_target_root_m?.length === 3 &&
                flowContext.effective_target_root_m?.length === 3
              "
              class="key-file mono"
            >
              目标 Z {{ flowContext.picked_target_root_m[2].toFixed(3) }} →
              {{ flowContext.effective_target_root_m[2].toFixed(3) }} m
            </div>
          </template>
          <div v-else class="key-missing">旧记录未保存逐轮上抬参数</div>
        </article>
      </div>
    </section>

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
        <h3>点云配置偏移明细</h3>
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

    <div v-if="record.flip" class="exec-section">
      <h2 class="section-title">
        拨动前后对比（{{ record.flip.flip_from ?? "未知" }} →
        {{ record.flip.flip_to ?? "未知"
        }}<template v-if="record.flip.round">，第 {{ record.flip.round }} 轮</template>）
        <span
          v-if="record.flip.after?.success != null"
          class="badge"
          :class="record.flip.after.success ? 'exec-done' : 'exec-error'"
        >
          {{ record.flip.after.success ? "拨动成功" : "拨动未成功" }}
        </span>
        <span
          v-else-if="record.flip.after?.error || record.flip.before?.error"
          class="badge exec-error"
        >
          核验失败
        </span>
      </h2>
      <div class="flip-grid">
        <section
          v-for="stage in (['before', 'after'] as const)"
          :key="stage"
          class="flip-stage"
        >
          <h3>{{ stage === "before" ? "横移前" : "复核帧" }}</h3>
          <div class="flip-result">
            YOLO：{{ record.flip[stage]?.scene ?? "无结论"
            }}<template v-if="record.flip[stage]?.conf != null">
              （conf {{ record.flip[stage]!.conf!.toFixed(2) }}）</template>
            <span class="mono flip-ts">{{ record.flip[stage]?.ts }}</span>
          </div>
          <div v-if="record.flip[stage]?.error" class="flip-error">
            核验失败：{{ record.flip[stage]!.error }}
          </div>
          <div class="flip-camera-grid" :class="{ 'head-only': stage === 'after' }">
            <figure
              v-for="camera in (stage === 'before'
                ? (['head', 'wrist'] as const)
                : (['head'] as const))"
              :key="camera"
            >
              <template
                v-if="
                  camera === 'head'
                    ? record.flip[stage]?.has_image
                    : record.flip[stage]?.has_wrist_image
                "
              >
                <a
                  :href="
                    fileUrl(
                      name,
                      camera === 'head'
                        ? `flip_${stage}.jpg`
                        : `flip_${stage}_wrist.jpg`,
                    )
                  "
                  target="_blank"
                >
                  <img
                    :src="
                      fileUrl(
                        name,
                        camera === 'head'
                          ? `flip_${stage}.jpg`
                          : `flip_${stage}_wrist.jpg`,
                      )
                    "
                    loading="lazy"
                  />
                </a>
              </template>
              <div v-else class="flip-missing">
                {{
                  camera === "wrist" && record.flip[stage]?.wrist_error
                    ? "腕部相机不可用"
                    : "无图像"
                }}
              </div>
              <figcaption>{{ camera === "head" ? "头部相机" : "右腕相机" }}</figcaption>
            </figure>
          </div>
        </section>
      </div>
    </div>

    <div v-if="executions.length" class="exec-section">
      <h2 class="section-title">执行记录（基坐标系漂移与误差归因）</h2>
      <div v-for="e in executions" :key="e.id" class="card exec-card">
        <div class="exec-head">
          <span class="badge" :class="`exec-${execResultKind(e.result)}`">
            {{ resultLabel(e.result) }}
          </span>
          <span class="mono seg">{{ e.segment }}</span>
          <span class="mono time">{{ e.ts?.replace("T", " ").slice(0, 19) }}</span>
          <div class="spacer" />
          <button
            v-if="e.trace_len"
            class="chip"
            :disabled="traceLoading === e.id"
            @click="toggleTrace(e)"
          >
            {{
              traceLoading === e.id
                ? "加载中…"
                : traces[e.id]
                  ? "收起时间线"
                  : `漂移时间线（${e.trace_len} 采样）`
            }}
          </button>
        </div>
        <div class="metrics">
          <div class="metric">
            <div class="num">{{ fmtMm(e.tcp_mm.ik_mm) }}</div>
            <div class="label">IK 残差 mm</div>
          </div>
          <div class="metric">
            <div class="num">{{ fmtMm(e.tcp_mm.track_mm) }}</div>
            <div class="label">关节跟踪 mm</div>
          </div>
          <div class="metric">
            <div class="num">{{ fmtMm(e.tcp_mm.total_mm) }}</div>
            <div class="label">总误差 mm</div>
          </div>
          <div class="metric">
            <div class="num">{{ fmtMm(e.tcp_mm.total_vs_drifted_mm) }}</div>
            <div class="label">对漂移后目标 mm</div>
          </div>
          <div class="metric">
            <div class="num">
              {{ e.torso_rotation_deg != null ? e.torso_rotation_deg.toFixed(2) : "-" }}
            </div>
            <div class="label">躯干旋转 °</div>
          </div>
          <div class="metric">
            <div class="num warn">{{ fmtMm(e.target_shift_mm) }}</div>
            <div class="label">目标漂移 mm</div>
          </div>
        </div>
        <div v-if="e.waist_delta_deg || e.imu_rpy_delta_deg" class="drift-detail mono">
          <span v-if="e.waist_delta_deg">
            腰关节变化 [{{ e.waist_delta_deg.map((v) => v.toFixed(2)).join(", ") }}]°
          </span>
          <span v-if="e.imu_rpy_delta_deg">
            IMU 变化 [{{ e.imu_rpy_delta_deg.map((v) => v.toFixed(2)).join(", ") }}]°
          </span>
        </div>
        <ExecTimeline v-if="traces[e.id]?.length" :trace="traces[e.id]" />
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

.key-config {
  padding: 18px;
  margin-bottom: 20px;
  border-color: color-mix(in srgb, var(--amber) 55%, var(--border));
  background:
    linear-gradient(135deg, color-mix(in srgb, var(--amber) 8%, transparent), transparent 45%),
    var(--card);
}

.key-config-head {
  display: flex;
  align-items: flex-start;
  gap: 14px;
  margin-bottom: 14px;
}

.key-config-head h2 {
  margin: 0;
  font-size: 18px;
  color: var(--amber);
}

.key-config-head p {
  margin: 4px 0 0;
  color: var(--text-dim);
  font-size: 12px;
}

.key-config-head .badge {
  margin-left: auto;
  flex: none;
}

.key-config-grid {
  display: grid;
  grid-template-columns: repeat(3, minmax(0, 1fr));
  gap: 12px;
}

.key-block {
  min-width: 0;
  padding: 14px;
  border: 1px solid var(--border);
  border-radius: 10px;
  background: var(--bg-soft);
}

.key-block.extra-offset {
  border-color: color-mix(in srgb, var(--amber) 40%, var(--border));
}

.key-label {
  margin-bottom: 8px;
  color: var(--text-dim);
  font-size: 12px;
  font-weight: 700;
}

.key-value {
  color: var(--accent);
  font-size: 20px;
  font-weight: 800;
  line-height: 1.25;
}

.wall-value {
  color: var(--amber);
  font-size: 18px;
}

.key-detail {
  margin-top: 8px;
  font-size: 13px;
  line-height: 1.5;
  overflow-wrap: anywhere;
}

.opening-name {
  font-size: 15px;
  font-weight: 700;
}

.key-chips {
  display: flex;
  gap: 6px;
  flex-wrap: wrap;
  margin-top: 10px;
}

.key-chips span {
  padding: 3px 8px;
  border-radius: 999px;
  background: color-mix(in srgb, var(--accent) 10%, transparent);
  color: var(--text-dim);
  font-size: 12px;
}

.key-file,
.key-missing {
  margin-top: 9px;
  color: var(--text-dim);
  font-size: 11px;
  overflow-wrap: anywhere;
}

@media (max-width: 1050px) {
  .key-config-grid {
    grid-template-columns: 1fr;
  }
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

.exec-section {
  margin-bottom: 20px;
}

.section-title .badge {
  margin-left: 8px;
}

.flip-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 16px;
}

.flip-stage {
  padding: 12px;
  background: var(--card);
  border: 1px solid var(--border);
  border-radius: var(--radius);
}

.flip-stage h3 {
  margin: 0 0 4px;
}

.flip-result {
  margin-bottom: 10px;
  color: var(--text-dim);
  font-size: 13px;
}

.flip-error {
  margin: -4px 0 10px;
  padding: 8px 10px;
  color: var(--red);
  background: color-mix(in srgb, var(--red) 10%, transparent);
  border: 1px solid color-mix(in srgb, var(--red) 35%, transparent);
  border-radius: 6px;
  font-size: 13px;
  overflow-wrap: anywhere;
}

.flip-camera-grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 10px;
}

.flip-camera-grid.head-only {
  grid-template-columns: 1fr;
}

@media (max-width: 900px) {
  .flip-grid {
    grid-template-columns: 1fr;
  }
}

.flip-camera-grid figure {
  margin: 0;
  background: var(--bg-soft);
  border: 1px solid var(--border);
  border-radius: var(--radius);
  overflow: hidden;
}

.flip-camera-grid img {
  width: 100%;
  display: block;
}

.flip-missing {
  aspect-ratio: 16 / 9;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-dim);
  background: var(--bg-soft);
}

.flip-camera-grid figcaption {
  padding: 10px 14px;
  font-size: 13px;
  color: var(--text-dim);
}

.flip-ts {
  margin-left: 10px;
  font-size: 12px;
}

.exec-card {
  padding: 14px 18px 16px;
  margin-bottom: 12px;
}

.exec-head {
  display: flex;
  align-items: center;
  gap: 12px;
  margin-bottom: 12px;
}

.exec-head .seg {
  color: var(--text-dim);
  font-size: 13px;
}

.exec-head .time {
  color: var(--text-dim);
  font-size: 13px;
}

.exec-head .spacer {
  flex: 1;
}

.badge.exec-done {
  background: rgba(90, 212, 111, 0.15);
  color: var(--green);
}

.badge.exec-cancelled {
  background: rgba(143, 163, 192, 0.15);
  color: var(--text-dim);
}

.badge.exec-error {
  background: rgba(255, 93, 93, 0.15);
  color: var(--red);
}

.metrics {
  display: grid;
  grid-template-columns: repeat(auto-fit, minmax(110px, 1fr));
  gap: 10px;
  margin-bottom: 10px;
}

.metric {
  background: var(--bg-soft);
  border-radius: 10px;
  padding: 10px 12px;
  text-align: center;
}

.metric .num {
  font-size: 19px;
  font-weight: 800;
  color: var(--accent);
}

.metric .num.warn {
  color: var(--amber);
}

.metric .label {
  margin-top: 2px;
  color: var(--text-dim);
  font-size: 12px;
}

.drift-detail {
  display: flex;
  gap: 20px;
  flex-wrap: wrap;
  color: var(--text-dim);
  font-size: 12px;
  margin-bottom: 6px;
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
