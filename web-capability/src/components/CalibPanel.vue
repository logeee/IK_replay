<script setup lang="ts">
import { computed } from "vue";
import type { CalibrationArtifact, Payload, ResidualMm } from "../lib/api";

const props = defineProps<{ payload: Payload }>();
const emit = defineEmits<{ register: [] }>();

const CALIB_TEXT: Record<string, string> = {
  ready: "就绪",
  pending: "待补",
  missing: "未登记",
};

function handName(handId: string): string {
  return (
    props.payload.registry.hands.find((h) => h.id === handId)?.name ?? handId
  );
}

function residualText(value: ResidualMm | null | undefined): string | null {
  if (typeof value === "number") return value.toFixed(2);
  const rms = value?.rms;
  return typeof rms === "number" ? rms.toFixed(2) : null;
}

const TYPE_LABEL: Record<string, string> = {
  extrinsic: "相机外参",
  intrinsic: "相机内参",
  camera_transform: "RGB-D 转换",
  hand_mount: "手安装",
  tcp_profile: "TCP",
};

const legacyCalibrations = computed(() =>
  props.payload.calibrations.filter(
    (calibration) =>
      calibration.status !== "missing" ||
      Boolean(calibration.source_path || calibration.registered_at),
  ),
);

function artifactSubject(artifact: CalibrationArtifact): string {
  const subject = artifact.subject;
  if (subject.kind === "camera") {
    const role = String(subject.camera_role || "");
    return role === "head" ? "头部相机" : role === "waist" ? "腰部相机" : role;
  }
  const arm = String(subject.arm || "");
  const handId = String(subject.hand_id || "");
  return `${props.payload.meta.arm_labels[arm] || arm} · ${handName(handId)}`;
}
</script>

<template>
  <section class="card">
    <h2>独立标定产物 <span class="lvl-tag">当前架构</span></h2>
    <p class="sub">
      相机、手安装与 TCP 分别归档；运行时只组合引用，不生成合并标定文件。
    </p>
    <ul class="calib-list">
      <li
        v-for="artifact in payload.registry.calibration_artifacts"
        :key="artifact.artifact_id"
      >
        <div class="combo">
          <span class="combo-name">
            {{ TYPE_LABEL[artifact.type] || artifact.type }} ·
            {{ artifactSubject(artifact) }}
          </span>
          <span class="badge" :class="artifact.status === 'active' ? 'ready' : artifact.status">
            {{ artifact.status === "active" ? "生效中" : artifact.status }}
          </span>
        </div>
        <div class="detail">
          <span class="dim">{{ artifact.run_id }}</span>
          <span v-if="artifact.subject.camera_serial" class="dim mono">
            {{ artifact.subject.camera_serial }}
          </span>
        </div>
      </li>
      <li v-if="!payload.registry.calibration_artifacts.length" class="dim empty">
        尚未由标定工作站发布独立产物
      </li>
    </ul>
    <h3 class="binding-title">运行绑定</h3>
    <ul class="calib-list">
      <li
        v-for="binding in payload.registry.calibration_bindings"
        :key="`${binding.arm}:${binding.hand_id}:${binding.camera_role}`"
      >
        <div class="combo">
          <span class="combo-name">
            {{ payload.meta.arm_labels[binding.arm] || binding.arm }} ·
            {{ handName(binding.hand_id) }} · {{ binding.camera_role }}
          </span>
          <span class="badge ready">已绑定</span>
        </div>
        <div class="detail">
          <span v-for="(id, type) in binding.artifacts" :key="type" class="dim">
            {{ TYPE_LABEL[type] || type }}：<span class="mono">{{ id }}</span>
          </span>
        </div>
      </li>
      <li v-if="!payload.registry.calibration_bindings.length" class="dim empty">
        尚未完成手/工具安装与 TCP 标定，当前没有可用的运行组合。
      </li>
    </ul>

    <details v-if="legacyCalibrations.length" class="legacy">
      <summary>旧格式兼容归档（{{ legacyCalibrations.length }}）</summary>
      <p class="sub">
        旧格式把相机、手安装和 TCP 放在同一文件中，仅供迁移期间兼容。
      </p>
      <ul class="calib-list">
        <li v-for="c in legacyCalibrations" :key="c.arm + c.hand_id">
          <div class="combo">
            <span class="combo-name">
              {{ payload.meta.arm_labels[c.arm] || c.arm }} · {{ handName(c.hand_id) }}
            </span>
            <span class="badge" :class="c.status">{{ CALIB_TEXT[c.status] }}</span>
          </div>
          <div class="detail">
            <span v-if="c.solved_at" class="dim">解算 {{ c.solved_at }}</span>
            <span v-if="residualText(c.residual_mm)" class="dim">
              残差 {{ residualText(c.residual_mm) }}mm
            </span>
            <span v-if="c.num_samples != null" class="dim">{{ c.num_samples }} 样本</span>
            <span v-if="c.source_path" class="dim mono src">来源 {{ c.source_path }}</span>
          </div>
        </li>
      </ul>
      <button class="btn" @click="emit('register')">＋ 登记旧格式标定</button>
    </details>
  </section>
</template>

<style scoped>
.calib-list {
  list-style: none;
  margin: 0 0 14px;
  padding: 0;
  display: flex;
  flex-direction: column;
  gap: 10px;
}

.calib-list li {
  background: var(--bg-soft);
  border: 1px solid var(--border);
  border-radius: 11px;
  padding: 12px 14px;
}

.combo {
  display: flex;
  align-items: center;
  justify-content: space-between;
  gap: 10px;
}

.combo-name {
  font-size: 14.5px;
  font-weight: 600;
}

.detail {
  margin-top: 5px;
  font-size: 12px;
  display: flex;
  gap: 10px;
  flex-wrap: wrap;
}

.src {
  word-break: break-all;
}

.mount-detail {
  align-items: center;
}

.mount-badge {
  padding: 1px 7px;
  border: 1px solid #2f5a46;
  border-radius: 999px;
  color: #62dca1;
  font-size: 11px;
}

.binding-title {
  margin: 20px 0 10px;
  font-size: 14px;
}

.empty {
  text-align: center;
  padding: 18px;
}

.legacy {
  margin-top: 20px;
}

.legacy summary {
  cursor: pointer;
  color: var(--text-dim);
  font-size: 13px;
}
</style>
