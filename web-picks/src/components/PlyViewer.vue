<script setup lang="ts">
import { onBeforeUnmount, onMounted, ref, watch } from "vue";
import * as THREE from "three";
import { OrbitControls } from "three/addons/controls/OrbitControls.js";
import { PLYLoader } from "three/addons/loaders/PLYLoader.js";

const props = defineProps<{
  url: string;
  /** 相机坐标系下的墙面轴（右/入墙/上），画在 origin 处便于对照 */
  wallAxes?: number[][] | null;
  /** 轴原点（一般用面板中心） */
  axesOrigin?: number[] | null;
}>();

const container = ref<HTMLDivElement>();
const loading = ref(true);
const error = ref("");
const pointCount = ref(0);
const pointSize = ref(2.2);

let renderer: THREE.WebGLRenderer | null = null;
let scene: THREE.Scene | null = null;
let camera: THREE.PerspectiveCamera | null = null;
let controls: OrbitControls | null = null;
let points: THREE.Points | null = null;
let animId = 0;
let resizeObs: ResizeObserver | null = null;

function buildAxes(): THREE.Object3D | null {
  if (!props.wallAxes || props.wallAxes.length !== 3 || !props.axesOrigin)
    return null;
  const origin = new THREE.Vector3(...(props.axesOrigin as [number, number, number]));
  const group = new THREE.Group();
  const colors = [0xff5d5d, 0x56d9c5, 0x5a8bff]; // 右=红 入墙=青 上=蓝
  const len = 0.06;
  props.wallAxes.forEach((axis, i) => {
    const dir = new THREE.Vector3(axis[0], axis[1], axis[2]).normalize();
    const geom = new THREE.BufferGeometry().setFromPoints([
      origin,
      origin.clone().addScaledVector(dir, len),
    ]);
    group.add(
      new THREE.Line(geom, new THREE.LineBasicMaterial({ color: colors[i] })),
    );
  });
  return group;
}

function init(): boolean {
  const el = container.value!;
  try {
    renderer = new THREE.WebGLRenderer({ antialias: true });
  } catch {
    error.value = "当前浏览器无法创建 WebGL 上下文，点云不可用";
    loading.value = false;
    return false;
  }
  renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
  renderer.setSize(el.clientWidth, el.clientHeight);
  renderer.setClearColor(0x0a1120);
  el.appendChild(renderer.domElement);

  scene = new THREE.Scene();
  camera = new THREE.PerspectiveCamera(
    50,
    el.clientWidth / el.clientHeight,
    0.005,
    50,
  );
  // 相机坐标系 y 朝下，翻转 up 让画面正立
  camera.up.set(0, -1, 0);

  controls = new OrbitControls(camera, renderer.domElement);
  controls.enableDamping = true;
  controls.dampingFactor = 0.1;

  resizeObs = new ResizeObserver(() => {
    if (!renderer || !camera) return;
    renderer.setSize(el.clientWidth, el.clientHeight);
    camera.aspect = el.clientWidth / el.clientHeight;
    camera.updateProjectionMatrix();
  });
  resizeObs.observe(el);

  const animate = () => {
    animId = requestAnimationFrame(animate);
    controls?.update();
    if (renderer && scene && camera) renderer.render(scene, camera);
  };
  animate();
  return true;
}

function loadCloud() {
  loading.value = true;
  error.value = "";
  new PLYLoader().load(
    props.url,
    (geometry) => {
      if (!scene || !camera || !controls) return;
      geometry.computeBoundingBox();
      const box = geometry.boundingBox!;
      const center = box.getCenter(new THREE.Vector3());
      const size = box.getSize(new THREE.Vector3()).length() || 0.4;

      const material = new THREE.PointsMaterial({
        size: pointSize.value / 1000,
        vertexColors: true,
      });
      points = new THREE.Points(geometry, material);
      scene.add(points);
      pointCount.value = geometry.getAttribute("position").count;

      const axes = buildAxes();
      if (axes) scene.add(axes);

      controls.target.copy(center);
      // 从目标前方稍偏上看过去（相机系 z 朝前、y 朝下）
      camera.position.set(
        center.x + size * 0.25,
        center.y - size * 0.35,
        center.z - size * 0.9,
      );
      controls.update();
      loading.value = false;
    },
    undefined,
    (err) => {
      error.value = `点云加载失败: ${err}`;
      loading.value = false;
    },
  );
}

watch(pointSize, (v) => {
  if (points) (points.material as THREE.PointsMaterial).size = v / 1000;
});

onMounted(() => {
  if (init()) loadCloud();
});

onBeforeUnmount(() => {
  cancelAnimationFrame(animId);
  resizeObs?.disconnect();
  controls?.dispose();
  if (points) {
    points.geometry.dispose();
    (points.material as THREE.Material).dispose();
  }
  renderer?.dispose();
  renderer?.domElement.remove();
});
</script>

<template>
  <div class="viewer">
    <div ref="container" class="canvas-host" />
    <div v-if="loading" class="overlay">点云加载中…</div>
    <div v-else-if="error" class="overlay err">{{ error }}</div>
    <div class="hud">
      <span class="mono">{{ pointCount.toLocaleString() }} 点</span>
      <label>
        点大小
        <input v-model.number="pointSize" type="range" min="0.5" max="6" step="0.1" />
      </label>
    </div>
    <div class="legend">
      <span><i style="background: var(--magenta)" />面板中心</span>
      <span><i style="background: var(--green)" />算法目标</span>
      <span><i style="background: var(--red)" />最终目的点</span>
    </div>
  </div>
</template>

<style scoped>
.viewer {
  position: relative;
  width: 100%;
  height: 100%;
  min-height: 380px;
  border-radius: var(--radius);
  overflow: hidden;
}

.canvas-host {
  position: absolute;
  inset: 0;
}

.overlay {
  position: absolute;
  inset: 0;
  display: flex;
  align-items: center;
  justify-content: center;
  color: var(--text-dim);
  background: rgba(10, 17, 32, 0.6);
  font-size: 14px;
}

.overlay.err {
  color: #ffb0b0;
}

.hud {
  position: absolute;
  top: 10px;
  left: 12px;
  display: flex;
  align-items: center;
  gap: 16px;
  font-size: 12px;
  color: var(--text-dim);
  background: rgba(10, 17, 32, 0.65);
  padding: 6px 12px;
  border-radius: 8px;
  backdrop-filter: blur(4px);
}

.hud label {
  display: flex;
  align-items: center;
  gap: 6px;
}

.hud input[type="range"] {
  width: 90px;
  accent-color: var(--accent);
}

.legend {
  position: absolute;
  bottom: 10px;
  left: 12px;
  display: flex;
  gap: 14px;
  font-size: 12px;
  color: var(--text-dim);
  background: rgba(10, 17, 32, 0.65);
  padding: 6px 12px;
  border-radius: 8px;
  backdrop-filter: blur(4px);
}

.legend span {
  display: flex;
  align-items: center;
  gap: 5px;
}

.legend i {
  width: 9px;
  height: 9px;
  border-radius: 50%;
  display: inline-block;
}
</style>
