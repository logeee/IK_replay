import * as THREE from "three";
import { OrbitControls } from "/web/vendor/OrbitControls.js";

const $ = (id) => document.getElementById(id);
const viewport = $("viewport");
const scene = new THREE.Scene();
scene.background = new THREE.Color(0x080b10);

const camera = new THREE.PerspectiveCamera(55, 1, 0.01, 100);
camera.position.set(0, 0, 0.2);
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
viewport.appendChild(renderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.enableDamping = true;
controls.dampingFactor = 0.08;
controls.target.set(0, 0, -1);
controls.update();

scene.add(new THREE.AxesHelper(0.25));
const raycaster = new THREE.Raycaster();
raycaster.params.Points.threshold = 0.012;
const pointer = new THREE.Vector2();
const marker = new THREE.Mesh(
  new THREE.SphereGeometry(0.008, 18, 12),
  new THREE.MeshBasicMaterial({ color: 0xffffff }),
);
marker.visible = false;
scene.add(marker);

const PALETTE = [
  [239, 83, 80], [66, 165, 245], [102, 187, 106], [255, 202, 40],
  [171, 71, 188], [255, 112, 67], [38, 198, 218], [141, 110, 99],
  [236, 64, 122], [124, 179, 66], [126, 87, 194], [255, 167, 38],
];

let points = null;
let rawPositions = null;
let displayPositions = null;
let rgbColors = null;
let semanticColors = null;
let pixels = null;
let classIds = null;
let captureMeta = null;
let colorMode = "rgb";
let viewMode = "live";

function resize() {
  const { clientWidth, clientHeight } = viewport;
  renderer.setSize(clientWidth, clientHeight, false);
  camera.aspect = Math.max(1, clientWidth) / Math.max(1, clientHeight);
  camera.updateProjectionMatrix();
  layoutSnapshot();
}
window.addEventListener("resize", resize);
resize();

function animate() {
  requestAnimationFrame(animate);
  controls.update();
  renderer.render(scene, camera);
}
animate();

function setStatus(text, kind = "") {
  $("status").textContent = text;
  $("status").className = `status ${kind}`;
}

function setViewMode(mode) {
  viewMode = mode;
  const live = mode === "live";
  const snapshot = mode === "snapshot";
  const pointcloud = !live && !snapshot;
  $("liveStream").classList.toggle("hidden", !live);
  $("snapshotStage").classList.toggle("hidden", !snapshot);
  viewport.classList.toggle("hidden", !pointcloud);
  $("liveMode").classList.toggle("active", live);
  $("snapshotMode").classList.toggle("active", snapshot);
  $("rgbMode").classList.toggle("active", mode === "rgb");
  $("semanticMode").classList.toggle("active", mode === "semantic");
  $("axisNote").classList.toggle("hidden", !pointcloud);
  $("viewerHelp").textContent = live
    ? "实时 ZMQ 彩色画面 · 点击“拍一下”生成点云"
    : snapshot
      ? "生成当前点云时使用的同帧 RGB 快照 · YOLO 检测框"
      : "左键旋转 · 右键平移 · 滚轮缩放 · 点击点云选点";
  controls.enabled = pointcloud;
  if (pointcloud) resize();
  if (snapshot) layoutSnapshot();
}

function decodeBinary(buffer) {
  const view = new DataView(buffer);
  if (buffer.byteLength < 16) throw new Error("点云数据头不完整");
  const magic = String.fromCharCode(
    view.getUint8(0), view.getUint8(1), view.getUint8(2), view.getUint8(3),
  );
  const version = view.getUint32(4, true);
  const count = view.getUint32(8, true);
  if (magic !== "PCV1" || version !== 1) {
    throw new Error(`不支持的点云协议 ${magic}/v${version}`);
  }
  if (buffer.byteLength !== 16 + count * 24) {
    throw new Error(`点云长度异常 ${buffer.byteLength}，点数 ${count}`);
  }
  let offset = 16;
  const positionsValue = new Float32Array(buffer, offset, count * 3);
  offset += count * 12;
  const rgbValue = new Uint8Array(buffer, offset, count * 3);
  offset += count * 3;
  const semanticValue = new Uint8Array(buffer, offset, count * 3);
  offset += count * 3;
  const pixelsValue = new Uint16Array(buffer, offset, count * 2);
  offset += count * 4;
  const classesValue = new Int16Array(buffer, offset, count);
  return {
    count,
    positions: positionsValue,
    rgb: rgbValue,
    semantic: semanticValue,
    pixels: pixelsValue,
    classIds: classesValue,
  };
}

function resetView() {
  if (!points) {
    camera.position.set(0, 0, 0.2);
    controls.target.set(0, 0, -1);
    controls.update();
    return;
  }
  points.geometry.computeBoundingSphere();
  const sphere = points.geometry.boundingSphere;
  const radius = Math.max(0.15, sphere.radius);
  controls.target.copy(sphere.center);
  camera.near = Math.max(0.005, radius / 100);
  camera.far = Math.max(20, radius * 30);
  camera.updateProjectionMatrix();
  camera.position.set(sphere.center.x, sphere.center.y, sphere.center.z + radius * 2.2);
  controls.update();
}

function installCloud(decoded) {
  if (points) {
    scene.remove(points);
    points.geometry.dispose();
    points.material.dispose();
  }
  rawPositions = decoded.positions;
  rgbColors = decoded.rgb;
  semanticColors = decoded.semantic;
  pixels = decoded.pixels;
  classIds = decoded.classIds;
  displayPositions = new Float32Array(rawPositions.length);
  for (let i = 0; i < decoded.count; i += 1) {
    displayPositions[3 * i] = rawPositions[3 * i];
    displayPositions[3 * i + 1] = -rawPositions[3 * i + 1];
    displayPositions[3 * i + 2] = -rawPositions[3 * i + 2];
  }
  const geometry = new THREE.BufferGeometry();
  geometry.setAttribute("position", new THREE.BufferAttribute(displayPositions, 3));
  const colors = colorMode === "rgb" ? rgbColors : semanticColors;
  geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3, true));
  const material = new THREE.PointsMaterial({
    size: Number($("pointSize").value),
    sizeAttenuation: false,
    vertexColors: true,
  });
  points = new THREE.Points(geometry, material);
  scene.add(points);
  marker.visible = false;
  $("selection").textContent = "点击点云选择一个点";
  $("selection").classList.add("muted");
  resetView();
}

function setColorMode(mode) {
  colorMode = mode;
  if (!points) {
    setStatus("请先点击“拍一下”生成点云", "error");
    return;
  }
  const colors = mode === "rgb" ? rgbColors : semanticColors;
  points.geometry.setAttribute("color", new THREE.BufferAttribute(colors, 3, true));
  points.geometry.attributes.color.needsUpdate = true;
  setViewMode(mode);
}

function renderBoxes(meta) {
  const root = $("boxes");
  root.innerHTML = "";
  const boxes = meta.boxes || [];
  if (!boxes.length) {
    root.textContent = "本帧没有 YOLO 检测框";
    root.className = "box-list muted";
    return;
  }
  root.className = "box-list";
  for (const box of boxes) {
    const row = document.createElement("div");
    row.className = "box-row";
    const swatch = document.createElement("span");
    swatch.className = "swatch";
    const color = PALETTE[((box.cls % PALETTE.length) + PALETTE.length) % PALETTE.length];
    swatch.style.background = `rgb(${color.join(",")})`;
    const title = document.createElement("span");
    const count = meta.class_point_counts?.[String(box.cls)] || 0;
    title.textContent = `${box.name} · ${count.toLocaleString()} 点`;
    const confidence = document.createElement("span");
    confidence.textContent = `${(box.conf * 100).toFixed(1)}%`;
    row.append(swatch, title, confidence);
    root.appendChild(row);
  }
}

function layoutSnapshot() {
  const image = $("snapshotImage");
  const stage = $("snapshotStage");
  if (!image.naturalWidth || !stage.clientWidth || !stage.clientHeight) return;
  const scale = Math.min(
    stage.clientWidth / image.naturalWidth,
    stage.clientHeight / image.naturalHeight,
  );
  $("snapshotFrame").style.width = `${image.naturalWidth * scale}px`;
  $("snapshotFrame").style.height = `${image.naturalHeight * scale}px`;
}

function drawSnapshotBoxes() {
  const root = $("snapshotBoxes");
  const image = $("snapshotImage");
  root.replaceChildren();
  if (!captureMeta || !image.naturalWidth) return;
  for (const box of captureMeta.boxes || []) {
    const [x1, y1, x2, y2] = box.xyxy;
    const color = PALETTE[((box.cls % PALETTE.length) + PALETTE.length) % PALETTE.length];
    const cssColor = `rgb(${color.join(",")})`;
    const element = document.createElement("div");
    element.className = "snapshot-box";
    element.style.left = `${x1 / image.naturalWidth * 100}%`;
    element.style.top = `${y1 / image.naturalHeight * 100}%`;
    element.style.width = `${(x2 - x1) / image.naturalWidth * 100}%`;
    element.style.height = `${(y2 - y1) / image.naturalHeight * 100}%`;
    element.style.borderColor = cssColor;
    const label = document.createElement("span");
    label.style.background = cssColor;
    label.textContent = `${box.name} ${(box.conf * 100).toFixed(0)}%`;
    element.appendChild(label);
    root.appendChild(element);
  }
}

async function capture() {
  const button = $("captureBtn");
  button.disabled = true;
  setStatus("正在获取同帧 RGB-D 并运行 YOLO…");
  try {
    const response = await fetch("/api/pointcloud/capture", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        stride: Number($("stride").value),
        z_min_m: Number($("zMin").value),
        z_max_m: Number($("zMax").value),
        conf: Number($("conf").value),
      }),
    });
    const meta = await response.json();
    if (!response.ok || !meta.ok) throw new Error(meta.error || `HTTP ${response.status}`);
    const binaryResponse = await fetch(meta.data_url, { cache: "no-store" });
    if (!binaryResponse.ok) throw new Error(`点云下载失败 HTTP ${binaryResponse.status}`);
    const decoded = decodeBinary(await binaryResponse.arrayBuffer());
    captureMeta = meta;
    const snapshotImage = $("snapshotImage");
    snapshotImage.onload = () => {
      layoutSnapshot();
      drawSnapshotBoxes();
    };
    snapshotImage.src = meta.image_url;
    installCloud(decoded);
    setColorMode("rgb");
    renderBoxes(meta);
    $("captureInfo").classList.remove("muted");
    $("captureInfo").innerHTML = [
      `点数: ${meta.point_count.toLocaleString()}`,
      `YOLO框: ${meta.boxes.length}`,
      `stride: ${meta.stride}`,
      `范围: ${meta.z_min_m.toFixed(2)}–${meta.z_max_m.toFixed(2)} m`,
      `耗时: ${meta.capture_ms.toFixed(1)} ms`,
      `源帧: ${meta.source?.frame_id ?? "?"}`,
      `模型: ${meta.model}`,
    ].join("<br>");
    setStatus(`捕获成功：${meta.point_count.toLocaleString()} 点`, "ok");
  } catch (error) {
    setStatus(error.message || String(error), "error");
  } finally {
    button.disabled = false;
  }
}

function rootPoint(cameraPoint) {
  const transform = captureMeta?.T_cam2root;
  if (!transform) return null;
  return [
    transform[0][0] * cameraPoint[0] + transform[0][1] * cameraPoint[1]
      + transform[0][2] * cameraPoint[2] + transform[0][3],
    transform[1][0] * cameraPoint[0] + transform[1][1] * cameraPoint[1]
      + transform[1][2] * cameraPoint[2] + transform[1][3],
    transform[2][0] * cameraPoint[0] + transform[2][1] * cameraPoint[1]
      + transform[2][2] * cameraPoint[2] + transform[2][3],
  ];
}

function showSnapshot() {
  if (!captureMeta) {
    setStatus("请先点击“拍一下”生成快照", "error");
    return;
  }
  setViewMode("snapshot");
}

let pointerDown = null;
renderer.domElement.addEventListener("pointerdown", (event) => {
  pointerDown = [event.clientX, event.clientY];
});
renderer.domElement.addEventListener("pointerup", (event) => {
  if (!points || !pointerDown) return;
  const movement = Math.hypot(
    event.clientX - pointerDown[0],
    event.clientY - pointerDown[1],
  );
  pointerDown = null;
  if (movement > 4) return;
  const rect = renderer.domElement.getBoundingClientRect();
  pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
  pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const hit = raycaster.intersectObject(points, false)[0];
  if (!hit || hit.index == null) return;
  const index = hit.index;
  const pCamera = [
    rawPositions[3 * index],
    rawPositions[3 * index + 1],
    rawPositions[3 * index + 2],
  ];
  const pixel = [pixels[2 * index], pixels[2 * index + 1]];
  const cls = classIds[index];
  const className = cls >= 0 ? (captureMeta?.names?.[String(cls)] ?? String(cls)) : "背景";
  const pRoot = rootPoint(pCamera);
  marker.position.set(
    displayPositions[3 * index],
    displayPositions[3 * index + 1],
    displayPositions[3 * index + 2],
  );
  marker.visible = true;
  $("selection").classList.remove("muted");
  $("selection").innerHTML = [
    `点索引: ${index.toLocaleString()}`,
    `像素: (${pixel[0]}, ${pixel[1]})`,
    `类别: ${className}`,
    `深度: ${(pCamera[2] * 1000).toFixed(1)} mm`,
    `p_camera: [${pCamera.map((value) => value.toFixed(5)).join(", ")}] m`,
    pRoot ? `p_root: [${pRoot.map((value) => value.toFixed(5)).join(", ")}] m` : "p_root: 无手眼标定",
  ].join("<br>");
});

$("captureBtn").addEventListener("click", capture);
$("liveMode").addEventListener("click", () => setViewMode("live"));
$("snapshotMode").addEventListener("click", showSnapshot);
$("rgbMode").addEventListener("click", () => setColorMode("rgb"));
$("semanticMode").addEventListener("click", () => setColorMode("semantic"));
$("resetView").addEventListener("click", resetView);
$("pointSize").addEventListener("input", () => {
  const value = Number($("pointSize").value);
  $("pointSizeValue").textContent = `${value.toFixed(1)} px`;
  if (points) points.material.size = value;
});
$("liveStream").addEventListener("error", () => {
  setStatus("实时画面不可达，请检查 reach_server 的 8001 服务", "error");
});
$("snapshotImage").addEventListener("error", () => {
  setStatus("当前快照图像加载失败，请重新拍摄", "error");
});

fetch("/api/pointcloud/status")
  .then((response) => response.json())
  .then((value) => {
    if (value.ok) setStatus(`服务就绪 · ${value.model || "模型加载中"}`);
  })
  .catch((error) => setStatus(`状态检查失败: ${error}`, "error"));

setViewMode(viewMode);
