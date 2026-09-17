<script setup lang="ts">
import { onMounted, onUnmounted, ref, watch } from "vue";
import type { ArmId, ArmSelection, ArmWorkspace, Payload } from "../lib/api";
import { reachUrl, runtimeWorkspaceApi, saveArmSelection } from "../lib/api";
import ArmActivationCard from "./ArmActivationCard.vue";
const props = defineProps<{ payload: Payload; busy: boolean }>();
const emit = defineEmits<{ updated: [payload: Payload] }>();
const workspace = ref<ArmWorkspace>(props.payload.arm_workspace);
const error = ref("");
const message = ref("");
const saving = ref<ArmId | null>(null);
let polling = false;
let requestVersion = 0;
let timer: ReturnType<typeof setInterval>;
function runtimeSynced(arm: ArmId, runtime: ArmWorkspace): boolean {
  const desired = workspace.value.arms[arm];
  const applied = runtime.arms[arm];
  if (!applied || desired.enabled !== applied.enabled) return false;
  const fields: (keyof ArmSelection)[] = ["hand_id", "camera_role", "motion_backend", "mount_profile_id", "gravity_version"];
  if (!fields.every(key => (desired.selection[key] ?? "") === (applied.selection[key] ?? ""))) return false;
  for (const key of ["gravity_file", "hand_service_url", "hand_port"] as const) {
    if (desired.selection[key] && desired.selection[key] !== applied.selection[key]) return false;
  }
  return true;
}
async function refresh() {
  if (polling || saving.value) return;
  polling = true;
  const version = ++requestVersion;
  try {
    const runtime = await runtimeWorkspaceApi();
    if (version === requestVersion && !saving.value) {
      workspace.value = {
        ...workspace.value,
        runtime_available: true,
        shared_control_active: runtime.shared_control_active,
        control_error: runtime.control_error,
        arms: Object.fromEntries((['left_arm', 'right_arm'] as const).map(arm => [arm, {
          ...workspace.value.arms[arm],
          status: runtime.arms[arm]?.status ?? null,
          runtime_synced: runtimeSynced(arm, runtime),
        }])) as ArmWorkspace['arms'],
      };
      error.value = "";
    }
  }
  catch {
    if (version === requestVersion) {
      workspace.value = {...workspace.value, runtime_available: false, shared_control_active: false};
      error.value = "18001 执行服务离线；配置仍可查看和保存，服务启动后会从 18000 加载。";
    }
  }
  finally { polling = false; }
}
async function save(arm: ArmId, selection: ArmSelection) {
  if (saving.value) return;
  ++requestVersion;
  const wasEnabled = Boolean(workspace.value?.arms[arm]?.enabled);
  saving.value = arm; message.value = "";
  try {
    const payload = await saveArmSelection(arm, selection);
    workspace.value = payload.arm_workspace;
    emit("updated", payload);
    const result = selection.enabled ? (wasEnabled ? "配置已更新" : "已激活") : (wasEnabled ? "已取消激活" : "配置已保存（未激活）");
    try {
      const runtime = await runtimeWorkspaceApi(`/api/dual/reload-config/${arm}`, {});
      workspace.value = {
        ...workspace.value,
        runtime_available: true,
        shared_control_active: runtime.shared_control_active,
        control_error: runtime.control_error,
        arms: {...workspace.value.arms, [arm]: {
          ...workspace.value.arms[arm], status: runtime.arms[arm]?.status ?? null,
          runtime_synced: runtimeSynced(arm, runtime),
        }},
      };
      error.value = "";
      message.value = `${arm === "left_arm" ? "左臂" : "右臂"}${result}，执行窗口已应用。`;
    } catch (err) {
      workspace.value = {...workspace.value, runtime_available: true};
      error.value = `配置已保存在 18000，但执行窗口暂未应用：${err instanceof Error ? err.message : String(err)}`;
      message.value = `${arm === "left_arm" ? "左臂" : "右臂"}${result}，等待执行服务应用。`;
    }
  } catch (err) { message.value = err instanceof Error ? err.message : String(err); }
  finally { saving.value = null; }
}
function imported(value: ArmWorkspace) {
  ++requestVersion;
  workspace.value = value;
}
watch(() => props.payload.arm_workspace, value => {
  if (!saving.value) {
    const previous = workspace.value;
    workspace.value = {
      ...value,
      runtime_available: previous.runtime_available,
      shared_control_active: previous.shared_control_active,
      control_error: previous.control_error,
      arms: Object.fromEntries((['left_arm', 'right_arm'] as const).map(arm => [arm, {
        ...value.arms[arm],
        status: previous.arms[arm]?.status ?? null,
        runtime_synced: previous.arms[arm]?.runtime_synced,
      }])) as ArmWorkspace['arms'],
    };
  }
});
onMounted(() => { refresh(); timer = setInterval(refresh, 2000); });
onUnmounted(() => clearInterval(timer));
</script>
<template>
  <section class="activation-section">
    <div class="section-heading"><div><h2>激活组合 <span class="lvl-tag">左臂 · 右臂</span></h2>
      <p class="sub">左右独立选择激活或不激活，可同时激活。配置始终保存在 18000；执行服务在线且已释放时即时应用，离线时下次启动加载。</p></div>
      <a class="btn" :href="reachUrl('/arms')" target="_blank" rel="noopener">打开双臂工作台 ↗</a></div>
    <p v-if="error" class="connection-note" role="status">{{ error }} <button class="btn sm" @click="refresh">检查执行服务</button></p>
    <p v-if="message" class="save-message" role="status">{{ message }}</p>
    <p v-if="workspace?.shared_control_active" class="holding-note">当前有手臂已接管。请在工作台释放双臂后保存配置。</p>
    <div class="arm-cards">
      <ArmActivationCard v-for="arm in (['left_arm', 'right_arm'] as const)" :key="arm" :arm="arm" :payload="payload" :entry="workspace.arms[arm]"
        :profiles="workspace.gravity_profiles" :active-gravity-version="workspace.gravity_active_version"
        :busy="busy || saving !== null" :saving="saving === arm" :connected="workspace.runtime_available === true" :controlled="workspace.shared_control_active" @save="save(arm, $event)" @imported="imported" />
    </div>
  </section>
</template>
<style scoped>
.activation-section{margin-bottom:24px}.section-heading{display:flex;align-items:center;justify-content:space-between;gap:18px}.section-heading h2{margin:0 0 10px}.section-heading .sub{margin-bottom:18px}.section-heading a{flex-shrink:0;text-decoration:none}.arm-cards{display:grid;grid-template-columns:1fr 1fr;gap:20px}.connection-note{color:var(--amber)}.save-message,.holding-note{padding:10px 14px;border:1px solid var(--border);border-radius:8px;background:var(--bg-soft)}.holding-note{color:var(--amber)}@media(max-width:900px){.arm-cards{grid-template-columns:1fr}.section-heading{align-items:flex-start;flex-direction:column}.section-heading a{margin-bottom:14px}}
</style>
