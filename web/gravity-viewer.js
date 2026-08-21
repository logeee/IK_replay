import * as THREE from "three";
import { OrbitControls } from "/web/vendor/OrbitControls.js";

const viewport = document.getElementById("compareViewport");
const placeholder = document.getElementById("comparePlaceholder");
const sampleSelect = document.getElementById("compareSampleSelect");
const runLabel = document.getElementById("compareRunLabel");
const tcpError = document.getElementById("compareTcpError");
const dx = document.getElementById("compareDx");
const dy = document.getElementById("compareDy");
const dz = document.getElementById("compareDz");
const jointErrors = document.getElementById("compareJointErrors");
const showTheoretical = document.getElementById("showTheoretical");
const showMeasured = document.getElementById("showMeasured");
const resetButton = document.getElementById("resetCompareView");

const THEORY_COLOR = 0x43d6e8;
const MEASURED_COLOR = 0xff8b5c;
const ERROR_COLOR = 0xf3bc5b;
let currentRunId = null;
let theoreticalGroup = new THREE.Group();
let measuredGroup = new THREE.Group();
let errorGroup = new THREE.Group();
let lastBounds = null;

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x07111d);
scene.add(theoreticalGroup, measuredGroup, errorGroup);
scene.add(new THREE.HemisphereLight(0xd9efff, 0x1a2734, 2.4));
const light = new THREE.DirectionalLight(0xffffff, 2.0);
light.position.set(1.5, -1.5, 2.5);
scene.add(light);
const grid = new THREE.GridHelper(1.8, 18, 0x365067, 0x1a2b3a);
grid.rotation.x = Math.PI / 2;
scene.add(grid);
scene.add(new THREE.AxesHelper(0.16));

const camera = new THREE.PerspectiveCamera(42, 1, 0.01, 20);
camera.up.set(0, 0, 1);
camera.position.set(1.0, -1.7, 1.15);
const renderer = new THREE.WebGLRenderer({ antialias: true, alpha: false });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;
viewport.appendChild(renderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0.0, 0.0, 0.45);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.screenSpacePanning = true;
controls.update();

function resize() {
  const width = Math.max(1, viewport.clientWidth);
  const height = Math.max(1, viewport.clientHeight);
  renderer.setSize(width, height, false);
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(viewport);
resize();

function animate() {
  controls.update();
  renderer.render(scene, camera);
  requestAnimationFrame(animate);
}
requestAnimationFrame(animate);

function material(color, opacity) {
  return new THREE.MeshStandardMaterial({
    color,
    transparent: true,
    opacity,
    roughness: 0.35,
    metalness: 0.12,
    depthWrite: false,
  });
}

function cylinderBetween(a, b, color, opacity, radius = 0.012) {
  const start = new THREE.Vector3(...a);
  const end = new THREE.Vector3(...b);
  const direction = end.clone().sub(start);
  const length = direction.length();
  if (length < 1e-6) return null;
  const mesh = new THREE.Mesh(
    new THREE.CylinderGeometry(radius, radius, length, 14),
    material(color, opacity),
  );
  mesh.position.copy(start).add(end).multiplyScalar(0.5);
  mesh.quaternion.setFromUnitVectors(
    new THREE.Vector3(0, 1, 0),
    direction.normalize(),
  );
  return mesh;
}

function buildArm(pose, color, opacity) {
  const group = new THREE.Group();
  const links = pose.links || [];
  const armMaterial = material(color, opacity);
  links.forEach((link, index) => {
    const marker = new THREE.Mesh(
      new THREE.SphereGeometry(index === 0 ? 0.023 : 0.018, 18, 12),
      armMaterial,
    );
    marker.position.set(...link.xyz);
    marker.userData.linkName = link.name;
    group.add(marker);
    if (index > 0) {
      const segment = cylinderBetween(
        links[index - 1].xyz,
        link.xyz,
        color,
        Math.min(0.82, opacity + 0.12),
      );
      if (segment) group.add(segment);
    }
  });
  const tcp = new THREE.Mesh(
    new THREE.SphereGeometry(0.027, 22, 14),
    new THREE.MeshStandardMaterial({
      color,
      emissive: color,
      emissiveIntensity: 0.38,
      transparent: true,
      opacity: 0.92,
      depthWrite: false,
    }),
  );
  tcp.position.set(...pose.tcp_root_m);
  group.add(tcp);
  const wrist = links.at(-1);
  if (wrist) {
    const tool = cylinderBetween(wrist.xyz, pose.tcp_root_m, color, 0.82, 0.009);
    if (tool) group.add(tool);
  }
  return group;
}

function buildErrors(theoretical, measured) {
  const group = new THREE.Group();
  const measuredByName = new Map((measured.links || []).map((item) => [item.name, item]));
  for (const expected of theoretical.links || []) {
    const actual = measuredByName.get(expected.name);
    if (!actual) continue;
    const geometry = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(...expected.xyz),
      new THREE.Vector3(...actual.xyz),
    ]);
    group.add(
      new THREE.Line(
        geometry,
        new THREE.LineBasicMaterial({
          color: ERROR_COLOR,
          transparent: true,
          opacity: 0.9,
        }),
      ),
    );
  }
  const tcpLine = cylinderBetween(
    theoretical.tcp_root_m,
    measured.tcp_root_m,
    0xffdf78,
    0.96,
    0.004,
  );
  if (tcpLine) group.add(tcpLine);
  return group;
}

function replaceGroup(oldGroup, nextGroup) {
  scene.remove(oldGroup);
  oldGroup.traverse((object) => {
    object.geometry?.dispose?.();
    if (Array.isArray(object.material)) {
      object.material.forEach((entry) => entry.dispose?.());
    } else {
      object.material?.dispose?.();
    }
  });
  scene.add(nextGroup);
  return nextGroup;
}

function frameComparison() {
  const bounds = new THREE.Box3();
  bounds.expandByObject(theoreticalGroup);
  bounds.expandByObject(measuredGroup);
  if (bounds.isEmpty()) return;
  lastBounds = bounds.clone();
  const center = bounds.getCenter(new THREE.Vector3());
  const size = bounds.getSize(new THREE.Vector3());
  const radius = Math.max(size.length(), 0.35);
  controls.target.copy(center);
  camera.position.copy(center).add(
    new THREE.Vector3(1.05, -1.65, 0.85).normalize().multiplyScalar(radius * 1.55),
  );
  camera.near = Math.max(0.005, radius / 100);
  camera.far = Math.max(10, radius * 12);
  camera.updateProjectionMatrix();
  controls.update();
}

function formatMillimetres(value) {
  const number = Number(value);
  return `${number >= 0 ? "+" : ""}${number.toFixed(1)} mm`;
}

function renderMetrics(comparison) {
  tcpError.textContent = `${Number(comparison.tcp_error_mm).toFixed(1)} mm`;
  dx.textContent = formatMillimetres(comparison.tcp_delta_mm[0]);
  dy.textContent = formatMillimetres(comparison.tcp_delta_mm[1]);
  dz.textContent = formatMillimetres(comparison.tcp_delta_mm[2]);
  jointErrors.innerHTML = comparison.joint_names
    .map((name, index) => {
      const error = Number(comparison.joint_error_deg[index]);
      const color = Math.abs(error) > 2 ? "#ff8b5c" : Math.abs(error) > 0.5 ? "#f3bc5b" : "#54d68b";
      return `<div class="joint-error"><span>${name.replace("right_", "").replace("_joint", "")}</span><b style="color:${color}">${error >= 0 ? "+" : ""}${error.toFixed(2)}°</b></div>`;
    })
    .join("");
}

function renderComparison(comparison, { resetView = false } = {}) {
  theoreticalGroup = replaceGroup(
    theoreticalGroup,
    buildArm(comparison.theoretical, THEORY_COLOR, 0.48),
  );
  measuredGroup = replaceGroup(
    measuredGroup,
    buildArm(comparison.measured, MEASURED_COLOR, 0.48),
  );
  errorGroup = replaceGroup(
    errorGroup,
    buildErrors(comparison.theoretical, comparison.measured),
  );
  theoreticalGroup.visible = showTheoretical.checked;
  measuredGroup.visible = showMeasured.checked;
  errorGroup.visible = theoreticalGroup.visible && measuredGroup.visible;
  placeholder.style.display = "none";
  resetButton.disabled = false;
  runLabel.textContent = `${comparison.point_name || comparison.run_id} · 重力版本 ${comparison.gravity_version || "未记录"}`;
  renderMetrics(comparison);
  if (resetView || lastBounds === null) frameComparison();
}

async function loadComparison(runId, sampleIndex, resetView = false) {
  placeholder.style.display = "grid";
  placeholder.textContent = "正在计算理论与实测机械臂姿态…";
  try {
    const response = await fetch(
      `/api/gravity/runs/${encodeURIComponent(runId)}/comparison?sample_index=${encodeURIComponent(sampleIndex)}`,
      { cache: "no-store" },
    );
    const payload = await response.json();
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    const comparison = payload.comparison;
    const optionKey = comparison.available_samples
      .map((item) => `${item.index}:${item.trajectory_fraction}`)
      .join("|");
    if (sampleSelect.dataset.key !== `${runId}:${optionKey}`) {
      sampleSelect.innerHTML = comparison.available_samples
        .map((item) => {
          const percentage = Math.round(Number(item.trajectory_fraction) * 100);
          const label = item.type === "final" ? "终点" : `中途 ${percentage}%`;
          return `<option value="${item.index}">${item.index}. ${label} · ${item.sample_count}帧</option>`;
        })
        .join("");
      sampleSelect.dataset.key = `${runId}:${optionKey}`;
    }
    sampleSelect.value = String(comparison.sample_index);
    sampleSelect.disabled = false;
    renderComparison(comparison, { resetView });
  } catch (error) {
    placeholder.style.display = "grid";
    placeholder.textContent = `无法显示姿态对比：${error.message}`;
  }
}

window.addEventListener("gravity:open-comparison", (event) => {
  currentRunId = event.detail.runId;
  sampleSelect.disabled = true;
  loadComparison(currentRunId, 1, true);
});
sampleSelect.addEventListener("change", () => {
  if (currentRunId) loadComparison(currentRunId, Number(sampleSelect.value), false);
});
showTheoretical.addEventListener("change", () => {
  theoreticalGroup.visible = showTheoretical.checked;
  errorGroup.visible = showTheoretical.checked && showMeasured.checked;
});
showMeasured.addEventListener("change", () => {
  measuredGroup.visible = showMeasured.checked;
  errorGroup.visible = showTheoretical.checked && showMeasured.checked;
});
resetButton.addEventListener("click", frameComparison);
