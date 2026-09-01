<script setup lang="ts">
import { reactive } from "vue";
import type { Hand } from "../lib/api";
import BaseDialog from "./BaseDialog.vue";

const props = defineProps<{ hand: Hand | null; busy: boolean }>();
const emit = defineEmits<{
  close: [];
  save: [body: Record<string, unknown>];
}>();

const form = reactive({
  id: props.hand?.id ?? "",
  name: props.hand?.name ?? "",
  design_side: props.hand?.design_side ?? "right",
  tool_out_mm: props.hand?.tool_out_mm ?? 15,
  notes: props.hand?.notes ?? "",
});

function submit() {
  emit("save", { ...form, tool_out_mm: Number(form.tool_out_mm) });
}
</script>

<template>
  <BaseDialog
    :title="hand ? `编辑手型号「${hand.name}」` : '新增手型号'"
    @close="emit('close')"
  >
    <div class="grid">
      <label class="field">标识 id（小写字母/数字/中划线，留空自动生成）
        <input
          v-model.trim="form.id"
          :readonly="!!hand"
          placeholder="qiangnao-1-right"
        />
      </label>
      <label class="field">名称
        <input v-model.trim="form.name" placeholder="强脑-右-1" />
      </label>
      <label class="field">设计侧
        <select v-model="form.design_side">
          <option value="right">右（right）</option>
          <option value="left">左（left）</option>
        </select>
      </label>
      <label class="field">TCP 外移 tool_out_mm
        <input v-model.number="form.tool_out_mm" type="number" step="0.1" />
      </label>
      <label class="field full">备注
        <input v-model.trim="form.notes" />
      </label>
    </div>
    <template #footer>
      <button class="btn ghost" @click="emit('close')">取消</button>
      <button
        class="btn primary"
        :disabled="busy || !form.name"
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
</style>
