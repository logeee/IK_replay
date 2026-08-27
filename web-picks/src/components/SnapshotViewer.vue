<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import type { YoloBox } from "../lib/api";

const props = defineProps<{
  url: string;
  boxes?: YoloBox[];
}>();

const wrap = ref<HTMLDivElement>();
const img = ref<HTMLImageElement>();
const canvas = ref<HTMLCanvasElement>();
const showBoxes = ref(true);
const loaded = ref(false);
let resizeObs: ResizeObserver | null = null;

function draw() {
  const c = canvas.value;
  const im = img.value;
  if (!c || !im || !loaded.value) return;
  const rect = im.getBoundingClientRect();
  c.width = rect.width * devicePixelRatio;
  c.height = rect.height * devicePixelRatio;
  c.style.width = `${rect.width}px`;
  c.style.height = `${rect.height}px`;
  const ctx = c.getContext("2d")!;
  ctx.scale(devicePixelRatio, devicePixelRatio);
  ctx.clearRect(0, 0, rect.width, rect.height);
  if (!showBoxes.value || !props.boxes?.length) return;

  const sx = rect.width / im.naturalWidth;
  const sy = rect.height / im.naturalHeight;
  for (const box of props.boxes) {
    const [x1, y1, x2, y2] = box.xyxy;
    ctx.strokeStyle = "rgba(86, 217, 197, 0.95)";
    ctx.lineWidth = 2;
    // 轮廓多边形比外接框信息量大，优先画
    if (box.polygon?.length) {
      ctx.beginPath();
      box.polygon.forEach(([px, py], i) => {
        if (i === 0) ctx.moveTo(px * sx, py * sy);
        else ctx.lineTo(px * sx, py * sy);
      });
      ctx.closePath();
      ctx.fillStyle = "rgba(86, 217, 197, 0.12)";
      ctx.fill();
      ctx.stroke();
    } else {
      ctx.strokeRect(x1 * sx, y1 * sy, (x2 - x1) * sx, (y2 - y1) * sy);
    }
    const label = `${box.name} ${box.conf.toFixed(2)}`;
    ctx.font = "600 13px 'PingFang SC', sans-serif";
    const tw = ctx.measureText(label).width;
    ctx.fillStyle = "rgba(10, 17, 32, 0.85)";
    ctx.fillRect(x1 * sx, y1 * sy - 22, tw + 12, 20);
    ctx.fillStyle = "#56d9c5";
    ctx.fillText(label, x1 * sx + 6, y1 * sy - 7);
  }
}

watch(showBoxes, draw);
watch(() => props.boxes, draw);

onMounted(() => {
  resizeObs = new ResizeObserver(draw);
  if (wrap.value) resizeObs.observe(wrap.value);
});

onBeforeUnmount(() => resizeObs?.disconnect());
</script>

<template>
  <div ref="wrap" class="snapshot">
    <a :href="url" target="_blank" title="点击查看原图">
      <img
        ref="img"
        :src="url"
        @load="
          loaded = true;
          draw();
        "
      />
      <canvas ref="canvas" />
    </a>
    <label v-if="boxes?.length" class="toggle">
      <input v-model="showBoxes" type="checkbox" />
      YOLO 轮廓（{{ boxes.length }}）
    </label>
  </div>
</template>

<style scoped>
.snapshot {
  position: relative;
  border-radius: var(--radius);
  overflow: hidden;
  background: #000;
  line-height: 0;
}

.snapshot img {
  width: 100%;
  display: block;
}

.snapshot canvas {
  position: absolute;
  top: 0;
  left: 0;
  pointer-events: none;
}

.toggle {
  position: absolute;
  top: 10px;
  right: 12px;
  display: flex;
  align-items: center;
  gap: 6px;
  font-size: 12px;
  line-height: 1;
  color: var(--text);
  background: rgba(10, 17, 32, 0.7);
  padding: 7px 12px;
  border-radius: 8px;
  cursor: pointer;
  backdrop-filter: blur(4px);
  user-select: none;
}

.toggle input {
  accent-color: var(--accent);
}
</style>
