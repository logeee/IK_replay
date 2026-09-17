<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import type { ArmId, ArmSelection, ArmWorkspace, ArmWorkspaceEntry, CalibrationArtifactType, GravityProfile, Payload } from "../lib/api";
import { SIDE_LABELS } from "../lib/api";
import GravityImportDialog from "./GravityImportDialog.vue";
const props = defineProps<{arm: ArmId; payload: Payload; entry?: ArmWorkspaceEntry; profiles?: GravityProfile[]; activeGravityVersion?: string; busy: boolean; saving: boolean; connected: boolean; controlled: boolean}>();
const emit = defineEmits<{save: [selection: ArmSelection]; imported: [workspace: ArmWorkspace]}>();
const importing = ref(false);
const importedProfiles = ref<GravityProfile[]>([]);
const importMessage = ref("");
const draft = reactive<ArmSelection>({enabled:false, hand_id:"", camera_role:"head", motion_backend:"legacy", mount_profile_id:"", gravity_file:"", gravity_version:"", hand_service_url:"", hand_port:""});
const gravityMode = ref("version");
const savedSelectionKey = ref("");
const label = computed(()=>props.arm === "left_arm" ? "左臂" : "右臂");
const hands = computed(()=>props.payload.registry.hands.filter(h=>h.design_side === props.arm.replace("_arm", "")));
const hand = computed(()=>hands.value.find(h=>h.id===draft.hand_id));
const mountProfiles = computed(()=>hand.value?.mount_profiles ?? []);
const gravityProfiles = computed(()=>[...new Map([...(props.profiles ?? props.payload.meta.gravity_profiles ?? []), ...importedProfiles.value].map(p=>[p.version,p])).values()].filter(p=>!p.compatibility || (p.compatibility.arm===props.arm && p.compatibility.hand_id===draft.hand_id)));
function normalizeChoices(){
  if(!hands.value.some(h=>h.id===draft.hand_id))draft.hand_id=hands.value[0]?.id ?? "";
  if(!mountProfiles.value.some(p=>p.id===draft.mount_profile_id))draft.mount_profile_id=mountProfiles.value[0]?.id ?? "";
  if(!gravityProfiles.value.some(p=>p.version===draft.gravity_version))draft.gravity_version=gravityProfiles.value.find(p=>p.version===(props.activeGravityVersion ?? props.payload.meta.gravity_active_version))?.version ?? gravityProfiles.value[0]?.version ?? "";
}
watch(()=>JSON.stringify(props.entry?.selection), ()=>{
  const selected=props.entry?.selection;
  const legacy=props.payload.registry.active?.arm===props.arm ? props.payload.registry.active : null;
  const initial=selected ?? (legacy ? {...legacy, gravity_version:legacy.gravity_profile_version} : {});
  Object.assign(draft, {enabled:props.entry?.enabled ?? !!legacy, hand_id:hands.value[0]?.id ?? "", camera_role:"head", motion_backend:"legacy", mount_profile_id:"", gravity_file:"", gravity_version:"", hand_service_url:"", hand_port:""}, initial);
  draft.enabled=props.entry?.enabled ?? !!legacy;
  gravityMode.value=draft.gravity_file && !draft.gravity_file.endsWith("/gravity_compensation.json") ? "file" : "version";
  normalizeChoices();
  savedSelectionKey.value = JSON.stringify(selectionToSave());
  importMessage.value = "";
}, {immediate:true});
watch(()=>draft.hand_id, normalizeChoices);
watch(()=>props.payload.registry.hands, normalizeChoices);
const binding=computed(()=>props.payload.registry.calibration_bindings.find(b=>b.arm===props.arm && b.hand_id===draft.hand_id && b.camera_role===draft.camera_role));
const required:CalibrationArtifactType[]=["extrinsic","hand_mount","tcp_profile"];
const calibStatus=computed(()=>{
  if(binding.value)return required.every(type=>props.payload.registry.calibration_artifacts.some(a=>a.artifact_id===binding.value?.artifacts[type]&&a.status==="active")) ? "ready" : "pending";
  return props.payload.calibrations.find(c=>c.arm===props.arm && c.hand_id===draft.hand_id)?.status ?? "missing";
});
const calibLabel=computed(()=>({ready:"标定就绪",pending:"标定待补",missing:"标定未登记"})[calibStatus.value]);
const activeCapCount=computed(()=>props.payload.registry.capabilities.filter(c=>c.arm===props.arm&&c.hand_id===draft.hand_id&&c.enabled).length);
const runtimeLabel=computed(()=>props.entry?.status?.armed ? (props.entry.status.hand_move ? "已卸力" : props.entry.status.exec?.running ? "执行中" : "保持中") : "未接管");
function selectionToSave(): ArmSelection {
  return {
    arm: props.arm, enabled: Boolean(draft.enabled), hand_id: draft.hand_id,
    camera_role: draft.camera_role, motion_backend: draft.motion_backend,
    mount_profile_id: draft.mount_profile_id,
    gravity_file: gravityMode.value === 'file' ? (draft.gravity_file ?? '').trim() : '',
    gravity_version: gravityMode.value === 'version' ? draft.gravity_version : '',
    hand_service_url: (draft.hand_service_url ?? '').trim(),
    hand_port: (draft.hand_port ?? '').trim(),
  };
}
const changed = computed(() => JSON.stringify(selectionToSave()) !== savedSelectionKey.value);
const unchangedActive = computed(() => Boolean(props.entry?.enabled) && !changed.value);
const actionBlocked = computed(() => props.busy || props.controlled);
const saveLabel = computed(() => {
  if (props.saving) return '正在保存…';
  if (unchangedActive.value) return '已激活（未改动）';
  if (!props.entry?.enabled && !changed.value) return '激活本侧';
  if (!draft.enabled) return props.entry?.enabled ? '保存并取消激活' : '保存配置';
  return props.entry?.enabled ? '保存更改' : '保存并激活';
});
function save() {
  if (actionBlocked.value || unchangedActive.value) return;
  const selection = selectionToSave();
  if (!props.entry?.enabled && !changed.value) selection.enabled = true;
  emit('save', selection);
}
function deactivate() {
  if (actionBlocked.value || !props.entry?.enabled) return;
  // Deactivation changes only this side's enabled flag, using its saved config.
  emit('save', {enabled: false});
}
function activate() {
  if (actionBlocked.value || !draft.hand_id) return;
  emit('save', {...selectionToSave(), enabled: true});
}
function imported(workspace: ArmWorkspace) {
  const profile = workspace.imported_profile;
  if (!profile) return;
  importedProfiles.value = workspace.gravity_profiles ?? [profile];
  gravityMode.value = "version";
  draft.gravity_version = profile.version;
  draft.gravity_file = "";
  importMessage.value = `已${workspace.created === false ? '找到已有版本并' : '导入并'}选中「${profile.label}」，保存本侧配置后生效。`;
  importing.value = false;
  emit('imported', workspace);
}
</script>
<template>
  <section :id="`active-${arm}`" class="card arm-card" :class="[arm, {inactive:!entry?.enabled}]">
    <div class="card-title"><h3>{{ label }}激活组合</h3><span class="badge" :class="connected && entry?.enabled && entry.runtime_synced !== false ? 'on' : 'off'">{{ !connected ? (entry?.enabled ? '已保存 · 待执行服务' : '未激活') : entry?.runtime_synced === false ? '已保存 · 待应用' : entry?.enabled ? '已激活' : '未激活' }}</span><span v-if="entry?.enabled && connected" class="runtime">{{ runtimeLabel }}</span></div>
    <form @submit.prevent="save">
      <div class="form-fields">
        <label class="field">激活状态<select v-model="draft.enabled" :aria-label="`${label}激活状态`"><option :value="true">激活</option><option :value="false">不激活</option></select></label>
        <label class="field">手型号<select v-model="draft.hand_id" :aria-label="`${label}手型号`"><option v-for="h in hands" :key="h.id" :value="h.id">{{ h.name }}（设计侧：{{ SIDE_LABELS[h.design_side] }}）</option></select></label>
        <label class="field">任务相机<select v-model="draft.camera_role"><option v-for="role in payload.meta.camera_roles ?? ['head','waist']" :key="role" :value="role">{{ role==='head'?'头部相机':role==='waist'?'腰部相机':role }}</option></select></label>
        <label class="field">运动后端<select v-model="draft.motion_backend"><option v-for="backend in payload.meta.motion_backends ?? ['legacy','legacy_timed','pink']" :key="backend" :value="backend">{{ payload.meta.motion_backend_labels?.[backend] ?? backend }}</option></select></label>
        <label class="field">安装方案<select v-model="draft.mount_profile_id"><option v-for="profile in mountProfiles" :key="profile.id" :value="profile.id">{{ profile.name }}</option></select></label>
        <label class="field">重力补偿来源<select v-model="gravityMode"><option value="version">补偿版本库</option><option value="file">独立补偿文件</option></select></label>
        <label v-if="gravityMode==='version'" class="field wide">重力补偿版本<select v-model="draft.gravity_version" :aria-label="`${label}重力补偿版本`"><option v-for="profile in gravityProfiles" :key="profile.version" :value="profile.version">{{ profile.version }} · {{ profile.label }}</option></select></label>
        <label v-else class="field wide">本侧补偿文件<input v-model="draft.gravity_file" :required="draft.enabled" placeholder="补偿 JSON 文件的绝对路径" :aria-label="`${label}补偿文件`" /></label>
        <div class="wide import-row"><button type="button" class="btn" :disabled="busy || !draft.hand_id" @click="importing = true">导入补偿 JSON</button><span>默认名称使用原文件名</span></div>
        <p v-if="importMessage" class="wide import-message" role="status">{{ importMessage }}</p>
      </div>
      <details class="connection"><summary>灵巧手连接设置</summary><label class="field">服务地址<input v-model="draft.hand_service_url" placeholder="http://127.0.0.1:18089" /></label><label class="field">独立串口<input v-model="draft.hand_port" placeholder="DDS / 第一只串口手可留空" /></label></details>
      <div class="status"><span class="badge" :class="calibStatus">{{ calibLabel }}</span><span class="badge plain off">{{ activeCapCount }} 项已启用能力</span><span v-if="draft.motion_backend==='pink'" class="badge plain off">PINK：执行前锚定世界系</span></div>
      <footer>
        <span>仅操作{{ label }}，另一侧保持原配置</span>
        <div class="activation-actions">
          <button v-if="entry?.enabled" type="button" class="btn ghost danger" :disabled="actionBlocked" @click="deactivate">取消激活</button>
          <button v-else-if="changed && !draft.enabled" type="button" class="btn" :disabled="actionBlocked || !draft.hand_id" @click="activate">激活本侧</button>
          <button type="submit" class="btn primary" :disabled="actionBlocked || unchangedActive || ((draft.enabled || !changed) && !draft.hand_id)">{{ saveLabel }}</button>
        </div>
      </footer>
    </form>
  </section>
  <GravityImportDialog v-if="importing" :arm="arm" :hand-id="draft.hand_id!" :hand-name="hand?.name ?? draft.hand_id ?? ''" @close="importing = false" @imported="imported" />
</template>
<style scoped>
.activation-actions{display:flex;flex-wrap:wrap;justify-content:flex-end;gap:10px}.activation-actions .danger{color:var(--red);border-color:var(--red)}.activation-actions button:disabled{cursor:default}.activation-actions button.primary:disabled{background:var(--bg-soft);border-color:var(--border);color:var(--text-dim)}
.import-row{display:flex;gap:12px;align-items:center}.import-row span,.import-message{font-size:12px;color:var(--text-dim)}.import-message{margin:0;line-height:1.6;overflow-wrap:anywhere}
.arm-card{margin-bottom:0;background:linear-gradient(120deg,rgba(86,217,197,.045),transparent 55%),var(--card);border-top:3px solid #55bce8;scroll-margin-top:90px;min-width:0}.arm-card.right_arm{border-top-color:var(--violet)}.card-title{display:flex;align-items:center;gap:10px;margin-bottom:22px}.card-title h3{font-size:20px;margin:0;flex:1}.runtime{color:var(--text-dim);font-size:12px}.form-fields{display:grid;grid-template-columns:minmax(0,1fr) minmax(0,1fr);gap:16px}.field{min-width:0}.field select,.field input{width:100%;min-width:0}.wide{grid-column:1/-1}.status{display:flex;flex-wrap:wrap;gap:8px;margin:18px 0}.connection{margin-top:18px;color:var(--text-dim);font-size:13px}.connection summary{cursor:pointer}.connection .field{margin-top:12px}footer{display:flex;align-items:center;justify-content:space-between;gap:12px;padding-top:14px;border-top:1px solid var(--border)}footer span{color:var(--text-dim);font-size:12px}footer button{flex-shrink:0}@media(max-width:500px){.form-fields{grid-template-columns:1fr}.card-title{flex-wrap:wrap}footer{align-items:stretch;flex-direction:column}}
</style>
