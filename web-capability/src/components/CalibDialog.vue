<script setup lang="ts">
import { computed, reactive, ref, watch } from "vue";
import type { Payload } from "../lib/api";
import { SIDE_LABELS } from "../lib/api";
import BaseDialog from "./BaseDialog.vue";

const props = defineProps<{ payload: Payload; busy: boolean }>();
const emit = defineEmits<{
  close: [];
  save: [body: Record<string, unknown>];
  error: [message: string];
}>();

const form = reactive({
  arm: "right_arm",
  hand_id: "",
  source_path: "",
});

const fileName = ref("");
const fileContent = ref<Record<string, unknown> | null>(null);

const hands = computed(() => props.payload.registry.hands);

watch(
  hands,
  (list) => {
    if (!list.some((h) => h.id === form.hand_id) && list.length) {
      form.hand_id = list[0]!.id;
    }
  },
  { immediate: true },
);

async function onFile(event: Event) {
  const input = event.target as HTMLInputElement;
  const file = input.files?.[0];
  fileName.value = "";
  fileContent.value = null;
  if (!file) return;
  try {
    const parsed = JSON.parse(await file.text());
    if (!parsed || typeof parsed !== "object" || !("T_cam2base" in parsed)) {
      emit("error", "标定文件不合法：缺少 T_cam2base");
      input.value = "";
      return;
    }
    fileContent.value = parsed;
    fileName.value = file.name;
  } catch {
    emit("error", "上传的文件不是合法 JSON");
    input.value = "";
  }
}

function submit() {
  const body: Record<string, unknown> = {
    arm: form.arm,
    hand_id: form.hand_id,
    source_path: form.source_path.trim(),
  };
  if (fileContent.value) body.content = fileContent.value;
  emit("save", body);
}
</script>

<template>
  <BaseDialog title="登记 / 上传手眼标定" @close="emit('close')">
    <div class="grid">
      <label class="field">臂侧
        <select v-model="form.arm">
          <option v-for="a in payload.meta.arms" :key="a" :value="a">
            {{ payload.meta.arm_labels[a] || a }}
          </option>
        </select>
      </label>
      <label class="field">手型号
        <select v-model="form.hand_id">
          <option v-for="h in hands" :key="h.id" :value="h.id">
            {{ h.name }}（设计侧:{{ SIDE_LABELS[h.design_side] }}）
          </option>
        </select>
      </label>
      <div class="field full">
        方式一：上传 handeye3d_result.json
        <label class="file-pick" :class="{ has: fileName }">
          <input type="file" accept=".json,application/json" @change="onFile" />
          <span v-if="fileName">已选择 {{ fileName }}</span>
          <span v-else>点击选择标定 JSON 文件</span>
        </label>
      </div>
      <label class="field full">方式二：机器人本机路径（服务端复制入库）
        <input
          v-model.trim="form.source_path"
          class="mono"
          placeholder="/home/robot/.../handeye3d_result.json"
          spellcheck="false"
        />
      </label>
      <p class="dim full note">
        两者给其一即可；都不给则登记为「待补」，之后可再补文件。
      </p>
    </div>
    <template #footer>
      <button class="btn ghost" @click="emit('close')">取消</button>
      <button
        class="btn primary"
        :disabled="busy || !form.hand_id"
        @click="submit"
      >
        保存
      </button>
    </template>
  </BaseDialog>
</template>

<style scoped>
.grid {
  display: grid;
  grid-template-columns: 1fr 1fr;
  gap: 12px;
}

.full {
  grid-column: 1 / -1;
}

.note {
  margin: 0;
  font-size: 12.5px;
}

.file-pick {
  display: block;
  border: 1px dashed var(--border);
  border-radius: 9px;
  padding: 16px;
  text-align: center;
  color: var(--text-dim);
  cursor: pointer;
  transition: all 0.15s;
  font-size: 13px;
}

.file-pick:hover {
  border-color: var(--accent);
  color: var(--accent);
}

.file-pick.has {
  border-style: solid;
  border-color: var(--accent);
  color: var(--accent);
}

.file-pick input {
  display: none;
}
</style>
