<script setup lang="ts">
import { computed, ref, watch } from "vue";
import type { ArmId, ArmWorkspace } from "../lib/api";
import { importGravityProfile } from "../lib/api";
import BaseDialog from "./BaseDialog.vue";

const props = defineProps<{ arm: ArmId; handId: string; handName: string }>();
const emit = defineEmits<{ close: []; imported: [workspace: ArmWorkspace] }>();
const mode = ref("path");
const sourcePath = ref("");
const fileName = ref("");
const content = ref<Record<string, unknown> | null>(null);
const name = ref("");
const error = ref("");
const busy = ref(false);
const chosenName = computed(() => mode.value === "path"
  ? sourcePath.value.trim().replaceAll("\\", "/").split("/").pop() || ""
  : fileName.value);
watch(chosenName, (value, old) => {
  if (!name.value || name.value === old) name.value = value;
});
watch(mode, () => { error.value = ""; });
async function chooseFile(event: Event) {
  const file = (event.target as HTMLInputElement).files?.[0];
  content.value = null; fileName.value = ""; error.value = "";
  if (!file) return;
  try {
    if (file.size > 1024 * 1024) throw new Error("补偿文件不能超过 1 MiB");
    const parsed = JSON.parse((await file.text()).replace(/^\uFEFF/, ""));
    if (!parsed || typeof parsed !== "object" || Array.isArray(parsed)) throw new Error("文件必须是 JSON 对象");
    content.value = parsed;
    fileName.value = file.name;
  } catch (err) { error.value = err instanceof Error ? err.message : String(err); }
}
async function submit() {
  if (busy.value) return;
  busy.value = true; error.value = "";
  try {
    const payload = await importGravityProfile({
      arm: props.arm, hand_id: props.handId, label: name.value.trim(),
      ...(mode.value === "path" ? { source_path: sourcePath.value.trim() }
        : { filename: fileName.value, content: content.value }),
    });
    emit("imported", payload.arm_workspace);
  } catch (err) { error.value = err instanceof Error ? err.message : String(err); }
  finally { busy.value = false; }
}
</script>
<template>
  <BaseDialog title="导入重力补偿 JSON" @close="!busy && emit('close')">
    <p class="scope">导入到{{ arm === 'left_arm' ? '左臂' : '右臂' }} · {{ handName }}</p>
    <div class="fields">
      <label class="field">导入方式
        <select v-model="mode" :disabled="busy" aria-label="补偿导入方式">
          <option value="path">机器人服务器上的文件</option>
          <option value="upload">选择电脑上的 JSON 文件</option>
        </select>
      </label>
      <label v-if="mode === 'path'" class="field">服务器文件路径
        <input v-model="sourcePath" :disabled="busy" aria-label="补偿服务器文件路径" placeholder="/home/robot/.../payloads/文件名.json" spellcheck="false" />
      </label>
      <label v-else class="field">选择 JSON 文件
        <input type="file" accept=".json,application/json" :disabled="busy" aria-label="选择补偿 JSON 文件" @change="chooseFile" />
      </label>
      <label class="field">补偿名称
        <input v-model="name" :disabled="busy" maxlength="100" aria-label="补偿名称" placeholder="默认使用原 JSON 文件名，可修改" />
      </label>
      <p class="note">默认名称保留原文件名（含 .json）。导入后保存到版本库并选中，点击对应侧“保存并激活”应用。</p>
      <p v-if="error" class="error" role="alert">{{ error }}</p>
    </div>
    <template #footer>
      <button class="btn ghost" :disabled="busy" @click="emit('close')">取消</button>
      <button class="btn primary" :disabled="busy || !handId || (mode === 'path' ? !sourcePath.trim() : !content)" @click="submit">{{ busy ? '正在导入…' : '导入并选中' }}</button>
    </template>
  </BaseDialog>
</template>
<style scoped>
.fields{display:grid;gap:16px}.scope{margin:0 0 18px;color:var(--text-dim)}.note{margin:0;font-size:12px;line-height:1.7;color:var(--text-dim)}.error{color:var(--red);white-space:pre-wrap;overflow-wrap:anywhere}input{width:100%;min-width:0}
</style>
