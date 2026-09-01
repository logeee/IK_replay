<script setup lang="ts">
import { onMounted, ref } from "vue";
import type { Capability, Hand, Payload } from "./lib/api";
import { apiGet, apiPost } from "./lib/api";
import ActiveCombo from "./components/ActiveCombo.vue";
import CalibDialog from "./components/CalibDialog.vue";
import CalibPanel from "./components/CalibPanel.vue";
import CapabilityDialog from "./components/CapabilityDialog.vue";
import CapabilityList from "./components/CapabilityList.vue";
import HandDialog from "./components/HandDialog.vue";
import HandsPanel from "./components/HandsPanel.vue";

const payload = ref<Payload | null>(null);
const loadError = ref("");
const busy = ref(false);

const toast = ref<{ text: string; error: boolean } | null>(null);
let toastTimer: ReturnType<typeof setTimeout> | undefined;

function showToast(text: string, error = false) {
  toast.value = { text, error };
  clearTimeout(toastTimer);
  toastTimer = setTimeout(() => (toast.value = null), 3200);
}

async function reload() {
  try {
    payload.value = await apiGet();
    loadError.value = "";
  } catch (err) {
    loadError.value = err instanceof Error ? err.message : String(err);
  }
}

async function mutate(
  path: string,
  body: unknown,
  okMessage: string,
): Promise<boolean> {
  busy.value = true;
  try {
    payload.value = await apiPost(path, body);
    showToast(okMessage);
    return true;
  } catch (err) {
    showToast(err instanceof Error ? err.message : String(err), true);
    return false;
  } finally {
    busy.value = false;
  }
}

// ---------- 弹窗状态 ----------
const handDialog = ref<{ hand: Hand | null } | null>(null);
const capDialog = ref<{ cap: Capability | null } | null>(null);
const calibDialog = ref(false);

async function saveHand(body: Record<string, unknown>) {
  if (await mutate("/api/capability/hands", body, "手型号已保存")) {
    handDialog.value = null;
  }
}

async function removeHand(hand: Hand) {
  if (!confirm(`确认删除手型号「${hand.name}」？`)) return;
  await mutate("/api/capability/hands/delete", { id: hand.id }, "手型号已删除");
}

async function saveCapability(body: Record<string, unknown>) {
  if (await mutate("/api/capability/capabilities", body, "能力已保存")) {
    capDialog.value = null;
  }
}

async function toggleCapability(cap: Capability) {
  await mutate(
    "/api/capability/capabilities",
    { ...cap, enabled: !cap.enabled },
    cap.enabled ? "能力已停用" : "能力已启用",
  );
}

async function removeCapability(cap: Capability) {
  if (!confirm(`确认删除能力「${cap.task.name}」？`)) return;
  await mutate(
    "/api/capability/capabilities/delete",
    { id: cap.id },
    "能力已删除",
  );
}

async function saveCalibration(body: Record<string, unknown>) {
  if (await mutate("/api/capability/calibrations", body, "标定已登记")) {
    calibDialog.value = false;
  }
}

async function applyActive(arm: string, handId: string) {
  await mutate(
    "/api/capability/active",
    { arm, hand_id: handId },
    "激活组合已切换（重启 17001/18001 生效）",
  );
}

onMounted(reload);
</script>

<template>
  <div class="topbar">
    <span class="brand">能力配置中心<span class="dot">·</span>18000</span>
    <span class="hint">
      四级：臂侧 → 手型号 → 任务配置 → 实现方式 ｜ 修改保存后重启 17001 / 18001 生效
    </span>
    <span class="spacer"></span>
  </div>

  <template v-if="payload">
    <ActiveCombo :payload="payload" :busy="busy" @apply="applyActive" />
    <div class="cols">
      <HandsPanel
        :payload="payload"
        @add="handDialog = { hand: null }"
        @edit="(hand) => (handDialog = { hand })"
        @remove="removeHand"
      />
      <CalibPanel :payload="payload" @register="calibDialog = true" />
    </div>
    <CapabilityList
      :payload="payload"
      :busy="busy"
      @add="capDialog = { cap: null }"
      @edit="(cap) => (capDialog = { cap })"
      @toggle="toggleCapability"
      @remove="removeCapability"
    />
  </template>
  <div v-else-if="loadError" class="load-state error">
    <p>注册表加载失败：{{ loadError }}</p>
    <button class="btn" @click="reload">重试</button>
  </div>
  <div v-else class="load-state">加载中…</div>

  <HandDialog
    v-if="handDialog"
    :hand="handDialog.hand"
    :busy="busy"
    @close="handDialog = null"
    @save="saveHand"
  />
  <CapabilityDialog
    v-if="capDialog"
    :payload="payload!"
    :cap="capDialog.cap"
    :busy="busy"
    @close="capDialog = null"
    @save="saveCapability"
  />
  <CalibDialog
    v-if="calibDialog"
    :payload="payload!"
    :busy="busy"
    @close="calibDialog = false"
    @save="saveCalibration"
    @error="(message) => showToast(message, true)"
  />

  <Transition name="toast">
    <div v-if="toast" class="toast" :class="{ error: toast.error }">
      {{ toast.text }}
    </div>
  </Transition>
</template>

<style scoped>
.load-state {
  padding: 80px 0;
  text-align: center;
  color: var(--text-dim);
}

.load-state.error p {
  color: #ffb4b4;
  margin-bottom: 16px;
}
</style>
