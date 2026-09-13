<script setup lang="ts">
import { computed } from "vue";
import type {
  CalibrationArtifact,
  CalibrationArtifactType,
  CalibrationBinding,
  Payload,
} from "../lib/api";

const props = defineProps<{ payload: Payload }>();
defineEmits<{ register: [] }>();

function handName(handId: string): string {
  return (
    props.payload.registry.hands.find((h) => h.id === handId)?.name ?? handId
  );
}

function roleLabel(role: string): string {
  return role === "head" ? "头部相机" : role === "waist" ? "腰部相机" : role;
}

function cameraArtifact(role: string, type: CalibrationArtifactType) {
  return props.payload.registry.calibration_artifacts.find(
    (artifact) =>
      artifact.type === type && String(artifact.subject.camera_role || "") === role,
  );
}

const cameraCalibrations = computed(() => {
  const roles = props.payload.meta.camera_roles?.length
    ? props.payload.meta.camera_roles
    : ["head", "waist"];
  return roles.map((role) => ({
    role,
    label: roleLabel(role),
    extrinsic: cameraArtifact(role, "extrinsic"),
    intrinsic: cameraArtifact(role, "intrinsic"),
  }));
});

function bindingArtifact(
  binding: CalibrationBinding,
  type: CalibrationArtifactType,
): CalibrationArtifact | undefined {
  const id = binding.artifacts[type];
  return props.payload.registry.calibration_artifacts.find(
    (artifact) => artifact.artifact_id === id,
  );
}

const toolCalibrations = computed(() => {
  const grouped = new Map<string, {
    arm: string;
    handId: string;
    mount?: CalibrationArtifact;
    tcp?: CalibrationArtifact;
  }>();
  for (const binding of props.payload.registry.calibration_bindings) {
    const key = `${binding.arm}:${binding.hand_id}`;
    grouped.set(key, {
      arm: binding.arm,
      handId: binding.hand_id,
      mount: bindingArtifact(binding, "hand_mount"),
      tcp: bindingArtifact(binding, "tcp_profile"),
    });
  }
  return [...grouped.values()];
});
</script>

<template>
  <section class="card">
    <h2>标定</h2>
    <h3 class="section-title">相机标定</h3>
    <ul class="calib-list">
      <li v-for="camera in cameraCalibrations" :key="camera.role">
        <div class="combo">
          <span class="combo-name">{{ camera.label }}</span>
          <span class="badge" :class="camera.extrinsic?.status === 'active' ? 'ready' : 'missing'">
            {{ camera.extrinsic?.status === "active" ? "生效中" : "未标定" }}
          </span>
        </div>
        <div class="calibration-lines">
          <div>
            <span class="line-label">外参</span>
            <span>{{ camera.extrinsic?.run_id || "未标定" }}</span>
          </div>
          <div>
            <span class="line-label">SDK内参存档</span>
            <span>{{ camera.intrinsic ? "已记录" : "未记录" }}</span>
          </div>
          <div v-if="camera.extrinsic?.subject.camera_serial || camera.intrinsic?.subject.camera_serial" class="dim mono">
            {{ camera.extrinsic?.subject.camera_serial || camera.intrinsic?.subject.camera_serial }}
          </div>
        </div>
      </li>
    </ul>

    <h3 class="section-title">工具标定</h3>
    <ul class="calib-list">
      <li v-for="tool in toolCalibrations" :key="`${tool.arm}:${tool.handId}`">
        <div class="combo">
          <span class="combo-name">
            {{ payload.meta.arm_labels[tool.arm] || tool.arm }} · {{ handName(tool.handId) }}
          </span>
          <span class="badge" :class="tool.mount && tool.tcp ? 'ready' : 'missing'">
            {{ tool.mount && tool.tcp ? "生效中" : "未完成" }}
          </span>
        </div>
        <div class="calibration-lines">
          <div><span class="line-label">安装</span><span>{{ tool.mount?.run_id || "未标定" }}</span></div>
          <div><span class="line-label">TCP</span><span>{{ tool.tcp?.run_id || "未标定" }}</span></div>
        </div>
      </li>
      <li v-if="!toolCalibrations.length && payload.registry.active">
        <div class="combo">
          <span class="combo-name">
            {{ payload.meta.arm_labels[payload.registry.active.arm] || payload.registry.active.arm }} ·
            {{ handName(payload.registry.active.hand_id) }}
          </span>
          <span class="badge missing">未标定</span>
        </div>
      </li>
      <li v-else-if="!toolCalibrations.length" class="dim empty">未选择工具</li>
    </ul>
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

.section-title {
  margin: 20px 0 10px;
  font-size: 14px;
}

.section-title:first-of-type {
  margin-top: 12px;
}

.calibration-lines {
  margin-top: 8px;
  display: grid;
  gap: 5px;
  font-size: 12px;
}

.calibration-lines > div {
  display: flex;
  gap: 10px;
}

.line-label {
  min-width: 78px;
  color: var(--text-dim);
}

.empty {
  text-align: center;
  padding: 18px;
}

</style>
