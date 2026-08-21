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
raycaster.params.Points.threshold = 0.02;
const pointer = new THREE.Vector2();
const markerCanvas = document.createElement("canvas");
markerCanvas.width = 64;
markerCanvas.height = 64;
function markerTexture(color) {
  const canvas = markerCanvas.cloneNode();
  const context = canvas.getContext("2d");
  context.beginPath();
  context.arc(32, 32, 20, 0, Math.PI * 2);
  context.fillStyle = color;
  context.fill();
  context.lineWidth = 8;
  context.strokeStyle = "#ffffff";
  context.stroke();
  return new THREE.CanvasTexture(canvas);
}
const draftMarkerTexture = markerTexture("#ff5964");
const confirmedMarkerTexture = markerTexture("#45d483");
const markerGeometry = new THREE.BufferGeometry();
markerGeometry.setAttribute(
  "position",
  new THREE.Float32BufferAttribute([0, 0, 0], 3),
);
const marker = new THREE.Points(
  markerGeometry,
  new THREE.PointsMaterial({
    color: 0xffffff,
    map: draftMarkerTexture,
    transparent: true,
    alphaTest: 0.1,
    depthTest: false,
    size: 16,
    sizeAttenuation: false,
  }),
);
marker.renderOrder = 1000;
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
let selection = null;
let replacementArmed = false;
let selectionPending = false;
let semanticLabels = [];
let restoringState = false;
const STORAGE_KEY = "ik-replay-pointcloud-state-v1";

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
  updateSemanticLabels();
}
animate();
controls.addEventListener("end", persistState);

function setStatus(text, kind = "") {
  $("status").textContent = text;
  $("status").className = `status ${kind}`;
}

function savedState() {
  try {
    return JSON.parse(localStorage.getItem(STORAGE_KEY) || "{}");
  } catch (_) {
    return {};
  }
}

function persistState() {
  if (restoringState || !captureMeta) return;
  try {
    localStorage.setItem(STORAGE_KEY, JSON.stringify({
      captureId: captureMeta.capture_id,
      viewMode,
      colorMode,
      cameraPosition: camera.position.toArray(),
      controlsTarget: controls.target.toArray(),
      selection,
    }));
  } catch (_) {
    // Browser privacy modes may disable localStorage; picking must still work.
  }
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
      ? "同帧 RGB 快照 · 点击图像可直接选择三维目标"
      : "左键旋转 · 右键平移 · 滚轮缩放 · 点击点云选点";
  controls.enabled = pointcloud;
  if (pointcloud) resize();
  if (snapshot) layoutSnapshot();
  persistState();
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

function installCloud(decoded, { preserveView = false } = {}) {
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
  selection = null;
  replacementArmed = false;
  selectionPending = false;
  $("confirmTarget").disabled = true;
  $("selection").textContent = "点击点云选择一个点";
  $("selection").classList.add("muted");
  updateSelectionLock();
  rebuildSemanticLabels();
  if (!preserveView) resetView();
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
  rebuildSemanticLabels();
  persistState();
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

function rebuildSemanticLabels() {
  const root = $("semanticLabels");
  root.replaceChildren();
  semanticLabels = [];
  for (const cluster of captureMeta?.semantic_clusters || []) {
    if (!Array.isArray(cluster.centroid_camera_m)
        || cluster.centroid_camera_m.length !== 3) continue;
    const element = document.createElement("div");
    element.className = "semantic-label";
    element.textContent =
      `${cluster.name} ${(Number(cluster.conf || 0) * 100).toFixed(0)}%`;
    root.appendChild(element);
    semanticLabels.push({
      element,
      point: new THREE.Vector3(
        cluster.centroid_camera_m[0],
        -cluster.centroid_camera_m[1],
        -cluster.centroid_camera_m[2],
      ),
    });
  }
}

function updateSemanticLabels() {
  const visible = viewMode === "semantic" && !viewport.classList.contains("hidden");
  const width = viewport.clientWidth;
  const height = viewport.clientHeight;
  for (const item of semanticLabels) {
    if (!visible || !width || !height) {
      item.element.classList.add("hidden");
      continue;
    }
    const projected = item.point.clone().project(camera);
    const onScreen = projected.z >= -1 && projected.z <= 1
      && Math.abs(projected.x) <= 1.05 && Math.abs(projected.y) <= 1.05;
    item.element.classList.toggle("hidden", !onScreen);
    if (!onScreen) continue;
    item.element.style.left = `${(projected.x * 0.5 + 0.5) * width}px`;
    item.element.style.top = `${(-projected.y * 0.5 + 0.5) * height}px`;
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
    if (Array.isArray(box.polygon) && box.polygon.length >= 3) {
      const svg = document.createElementNS("http://www.w3.org/2000/svg", "svg");
      svg.classList.add("snapshot-mask");
      svg.setAttribute("viewBox", `0 0 ${image.naturalWidth} ${image.naturalHeight}`);
      svg.setAttribute("preserveAspectRatio", "none");
      const polygon = document.createElementNS("http://www.w3.org/2000/svg", "polygon");
      polygon.setAttribute(
        "points",
        box.polygon.map((point) => `${point[0]},${point[1]}`).join(" "),
      );
      polygon.setAttribute("fill", cssColor);
      polygon.setAttribute("stroke", cssColor);
      svg.appendChild(polygon);
      root.appendChild(svg);
    }
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
  if (selection?.pixel) {
    const dot = document.createElement("div");
    dot.className = "snapshot-target";
    dot.style.left = `${selection.pixel[0] / image.naturalWidth * 100}%`;
    dot.style.top = `${selection.pixel[1] / image.naturalHeight * 100}%`;
    root.appendChild(dot);
  }
}

function renderCaptureInfo(meta) {
  $("captureInfo").classList.remove("muted");
  $("captureInfo").innerHTML = [
    `点数: ${meta.point_count.toLocaleString()}`,
    `YOLO实例: ${meta.boxes.length}（Mask ${meta.mask_instance_count || 0}）`,
    `stride: ${meta.stride}`,
    `YOLO邻域: 全像素（外扩 ${(meta.box_padding_ratio * 100).toFixed(0)}%）`,
    `范围: ${meta.z_min_m.toFixed(2)}–${meta.z_max_m.toFixed(2)} m`,
    `畸变补偿: ${meta.distortion_compensated ? "已启用" : "无需/无参数"}`,
    `耗时: ${meta.capture_ms.toFixed(1)} ms`,
    `源帧: ${meta.source?.frame_id ?? "?"}`,
    `模型: ${meta.model}`,
  ].join("<br>");
}

function restoredSelection(meta, stored) {
  if (stored?.captureId === meta.capture_id && stored.selection) {
    return stored.selection;
  }
  const confirmed = meta.confirmed_selection;
  if (!confirmed) return null;
  const pixel = Array.isArray(confirmed.pixel) ? confirmed.pixel : null;
  const detected = classAtPixel(pixel);
  return {
    baseCamera: confirmed.base_camera,
    pCamera: confirmed.p_camera,
    pixel,
    source: "restored",
    cls: detected.cls,
    className: detected.name,
    adjustment: confirmed.adjustment || [0, 0, 0],
    confirmed: confirmed.result || null,
  };
}

async function loadCapture(meta, { restore = false } = {}) {
  const binaryResponse = await fetch(meta.data_url, { cache: "no-store" });
  if (!binaryResponse.ok) {
    throw new Error(`点云下载失败 HTTP ${binaryResponse.status}`);
  }
  const decoded = decodeBinary(await binaryResponse.arrayBuffer());
  const stored = restore ? savedState() : {};
  const preserveView = stored.captureId === meta.capture_id
    && Array.isArray(stored.cameraPosition)
    && Array.isArray(stored.controlsTarget);
  captureMeta = meta;
  const snapshotImage = $("snapshotImage");
  snapshotImage.onload = () => {
    layoutSnapshot();
    drawSnapshotBoxes();
  };
  snapshotImage.src = `${meta.image_url}?t=${Date.now()}`;
  installCloud(decoded, { preserveView });
  renderBoxes(meta);
  renderCaptureInfo(meta);
  if (preserveView) {
    camera.position.fromArray(stored.cameraPosition);
    controls.target.fromArray(stored.controlsTarget);
    controls.update();
  }
  const recovered = restoredSelection(meta, stored);
  if (recovered) {
    selection = recovered;
    renderSelection();
  }
  const restoredColor = preserveView && ["rgb", "semantic"].includes(stored.colorMode)
    ? stored.colorMode : "rgb";
  setColorMode(restoredColor);
  if (preserveView && ["live", "snapshot", "rgb", "semantic"].includes(stored.viewMode)) {
    setViewMode(stored.viewMode);
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
    try {
      localStorage.removeItem(STORAGE_KEY);
    } catch (_) {
      // Persistence is optional; a successful capture must remain usable.
    }
    await loadCapture(meta);
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

function pointInPolygon(pixel, polygon) {
  if (!pixel || !Array.isArray(polygon) || polygon.length < 3) return false;
  let inside = false;
  for (let i = 0, j = polygon.length - 1; i < polygon.length; j = i, i += 1) {
    const [xi, yi] = polygon[i];
    const [xj, yj] = polygon[j];
    const crosses = ((yi > pixel[1]) !== (yj > pixel[1]))
      && (pixel[0] < (xj - xi) * (pixel[1] - yi) / (yj - yi) + xi);
    if (crosses) inside = !inside;
  }
  return inside;
}

function classAtPixel(pixel) {
  if (!pixel) return { cls: -1, name: "背景" };
  let best = null;
  for (const box of captureMeta?.boxes || []) {
    const [x1, y1, x2, y2] = box.xyxy;
    const matched = Array.isArray(box.polygon) && box.polygon.length >= 3
      ? pointInPolygon(pixel, box.polygon)
      : x1 <= pixel[0] && pixel[0] <= x2 && y1 <= pixel[1] && pixel[1] <= y2;
    if (matched) {
      if (!best || box.conf > best.conf) best = box;
    }
  }
  return best
    ? { cls: Number(best.cls), name: best.name }
    : { cls: -1, name: "背景" };
}

function updateSelectionLock() {
  const element = $("selectionLock");
  if (!selection) {
    element.textContent = "首次选点：直接点击点云或RGB图像";
    element.className = "selection-lock ready";
  } else if (replacementArmed) {
    element.textContent = "重新选点已解锁：下一次点击将替换当前点";
    element.className = "selection-lock armed";
  } else {
    element.textContent = "当前点已锁定；按 R 后才能重新选点";
    element.className = "selection-lock locked";
  }
}

function selectionClickAllowed() {
  if (selectionPending) {
    setStatus("正在读取上一次点击的冻结深度，请稍候");
    return false;
  }
  if (!selection || replacementArmed) return true;
  setStatus("当前点已锁定；如需重新选点，请先按 R", "error");
  updateSelectionLock();
  return false;
}

function setSelection(pCamera, pixel, source, cls = null, className = null) {
  const point = pCamera.map(Number);
  const detected = cls == null ? classAtPixel(pixel) : { cls, name: className };
  selection = {
    baseCamera: [...point],
    pCamera: [...point],
    pixel: pixel ? pixel.map(Number) : null,
    source,
    cls: Number(detected.cls),
    className: detected.name || "背景",
    adjustment: [0, 0, 0],
    confirmed: null,
  };
  replacementArmed = false;
  renderSelection();
  updateSelectionLock();
}

function renderSelection() {
  if (!selection) return;
  const pCamera = selection.pCamera;
  marker.position.set(pCamera[0], -pCamera[1], -pCamera[2]);
  marker.material.map = selection.confirmed
    ? confirmedMarkerTexture : draftMarkerTexture;
  marker.material.needsUpdate = true;
  marker.visible = true;
  const pRoot = rootPoint(pCamera);
  const adjustmentMm = selection.adjustment.map((value) => value * 1000);
  const lines = [
    `来源: ${selection.source === "rgb" ? "RGB 图像"
      : selection.source === "restored" ? "已恢复的确认点" : "三维点云"}`,
    selection.pixel ? `像素: (${selection.pixel[0]}, ${selection.pixel[1]})` : "像素: -",
    `类别: ${selection.className}`,
    `深度: ${(pCamera[2] * 1000).toFixed(1)} mm`,
    `p_camera: [${pCamera.map((value) => value.toFixed(5)).join(", ")}] m`,
    `微调(mm): [${adjustmentMm.map((value) => value.toFixed(1)).join(", ")}]`,
    pRoot
      ? `p_root: [${pRoot.map((value) => value.toFixed(5)).join(", ")}] m`
      : "p_root: 无手眼标定",
  ];
  if (selection.confirmed) {
    lines.push(
      `18001: 已确认 · p_root [${selection.confirmed.p_root
        .map((value) => value.toFixed(5)).join(", ")}] m`,
    );
  }
  $("selection").classList.remove("muted");
  $("selection").textContent = lines.join("\n");
  $("confirmTarget").disabled = !captureMeta?.T_cam2root;
  drawSnapshotBoxes();
  updateSelectionLock();
  persistState();
}

async function selectSnapshotPixel(event) {
  if (!captureMeta) {
    setStatus("请先拍摄 RGB-D 快照", "error");
    return;
  }
  if (!selectionClickAllowed()) return;
  const image = $("snapshotImage");
  const rect = image.getBoundingClientRect();
  const u = Math.max(0, Math.min(
    image.naturalWidth - 1,
    Math.round((event.clientX - rect.left) / rect.width * image.naturalWidth),
  ));
  const v = Math.max(0, Math.min(
    image.naturalHeight - 1,
    Math.round((event.clientY - rect.top) / rect.height * image.naturalHeight),
  ));
  selectionPending = true;
  setStatus(`正在读取 RGB 像素 (${u}, ${v}) 的冻结深度…`);
  try {
    const response = await fetch(`/api/pointcloud/pixel/${captureMeta.capture_id}`, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        u, v, search_radius: 6,
        z_min_m: captureMeta.z_min_m,
        z_max_m: captureMeta.z_max_m,
      }),
    });
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || `HTTP ${response.status}`);
    setSelection(result.p_camera, result.pixel, "rgb");
    setColorMode(colorMode);
    const shifted = result.search_distance_px > 0
      ? `，使用邻近像素 (${result.pixel.join(", ")})`
      : "";
    setStatus(`RGB 选点成功：深度 ${result.depth_mm.toFixed(1)} mm${shifted}`, "ok");
  } catch (error) {
    setStatus(error.message || String(error), "error");
  } finally {
    selectionPending = false;
  }
}

function nudgeSelection(code) {
  const commands = {
    KeyA: [0, -1], KeyD: [0, 1],
    KeyW: [1, -1], KeyS: [1, 1],
    KeyQ: [2, -1], KeyE: [2, 1],
  };
  const command = commands[code];
  if (!command) return false;
  if (!selection) {
    setStatus("请先从点云或 RGB 图像中选择目标", "error");
    return true;
  }
  const step = Number($("nudgeStep").value) / 1000;
  if (!Number.isFinite(step) || step <= 0) {
    setStatus("微调步长必须大于零", "error");
    return true;
  }
  const [axis, direction] = command;
  const next = [...selection.pCamera];
  next[axis] += direction * step;
  if (next[2] <= 0.05) {
    setStatus("Z 深度不能小于 50 mm", "error");
    return true;
  }
  selection.pCamera = next;
  selection.adjustment[axis] += direction * step;
  selection.confirmed = null;
  renderSelection();
  setStatus(
    `${["X", "Y", "Z"][axis]} ${direction > 0 ? "+" : "−"}`
      + `${(step * 1000).toFixed(1)} mm`,
    "ok",
  );
  return true;
}

async function confirmTarget() {
  if (!selection || !captureMeta) {
    setStatus("请先选择目标", "error");
    return;
  }
  const button = $("confirmTarget");
  button.disabled = true;
  setStatus("正在用冻结深度拟合表面，并提交给 18001…");
  try {
    const response = await fetch(
      `/api/pointcloud/confirm/${captureMeta.capture_id}`,
      {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          p_camera: selection.pCamera,
          surface_reference_camera: selection.baseCamera,
          pixel: selection.pixel,
          adjustment_camera_m: selection.adjustment,
          approach_offset_m: Number($("approachOffset").value || 0),
        }),
      },
    );
    const result = await response.json();
    if (!response.ok || !result.ok) throw new Error(result.error || `HTTP ${response.status}`);
    selection.confirmed = result;
    renderSelection();
    setStatus("18001 已确认目标；可以回到主界面查看 IK 预演", "ok");
    if (window.opener && !window.opener.closed) {
      window.opener.postMessage(
        { type: "ik-replay-pointcloud-pick", pick: result },
        "*",
      );
    }
  } catch (error) {
    setStatus(error.message || String(error), "error");
  } finally {
    button.disabled = false;
  }
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
  if (!selectionClickAllowed()) return;
  const rect = renderer.domElement.getBoundingClientRect();
  pointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
  pointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
  raycaster.setFromCamera(pointer, camera);
  const hits = raycaster.intersectObject(points, false);
  let hit = null;
  let bestScreenDistance2 = Infinity;
  const positionAttribute = points.geometry.getAttribute("position");
  const candidatePoint = new THREE.Vector3();
  for (const candidate of hits) {
    candidatePoint
      .fromBufferAttribute(positionAttribute, candidate.index)
      .applyMatrix4(points.matrixWorld);
    const projected = candidatePoint.clone().project(camera);
    const dx = (projected.x - pointer.x) * rect.width / 2;
    const dy = (projected.y - pointer.y) * rect.height / 2;
    const screenDistance2 = dx * dx + dy * dy;
    if (
      screenDistance2 < bestScreenDistance2
      || (screenDistance2 === bestScreenDistance2
          && candidate.distance < (hit?.distance ?? Infinity))
    ) {
      hit = candidate;
      bestScreenDistance2 = screenDistance2;
    }
  }
  if (bestScreenDistance2 > 12 * 12) hit = null;
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
  setSelection(pCamera, pixel, "pointcloud", cls, className);
  setStatus(`已选择点云索引 ${index.toLocaleString()}`, "ok");
});

$("captureBtn").addEventListener("click", capture);
$("snapshotImage").addEventListener("click", selectSnapshotPixel);
$("confirmTarget").addEventListener("click", confirmTarget);
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
  setStatus("实时画面不可达，请检查 reach_server 的 18001 服务", "error");
});
$("snapshotImage").addEventListener("error", () => {
  setStatus("当前快照图像加载失败，请重新拍摄", "error");
});
window.addEventListener("keydown", (event) => {
  if (event.ctrlKey || event.metaKey || event.altKey) return;
  const target = event.target;
  if (target instanceof HTMLElement
      && (target.matches("input, textarea, select") || target.isContentEditable)) {
    return;
  }
  if (event.code === "KeyR") {
    event.preventDefault();
    if (!selection) {
      setStatus("首次选点无需解锁，直接点击即可", "ok");
      return;
    }
    replacementArmed = true;
    updateSelectionLock();
    setStatus("重新选点已解锁；请点击一次点云或RGB图像", "ok");
    return;
  }
  if (event.code === "Escape" && replacementArmed) {
    event.preventDefault();
    replacementArmed = false;
    updateSelectionLock();
    setStatus("已取消重新选点，当前点保持锁定");
    return;
  }
  if (nudgeSelection(event.code)) event.preventDefault();
});

const requestedOffset = Number(
  new URLSearchParams(window.location.search).get("approach_offset_m"),
);
if (Number.isFinite(requestedOffset)) {
  $("approachOffset").value = String(requestedOffset);
}

async function initialize() {
  try {
    const response = await fetch("/api/pointcloud/status", { cache: "no-store" });
    const value = await response.json();
    if (!response.ok || !value.ok) {
      throw new Error(value.error || `HTTP ${response.status}`);
    }
    if (!value.latest_capture_id) {
      setStatus(`服务就绪 · ${value.model || "模型加载中"}`);
      return;
    }
    setStatus("正在恢复最近一次冻结点云…");
    const metadataResponse = await fetch(
      `/api/pointcloud/capture/${value.latest_capture_id}`,
      { cache: "no-store" },
    );
    const metadata = await metadataResponse.json();
    if (!metadataResponse.ok || !metadata.ok) {
      throw new Error(metadata.error || `HTTP ${metadataResponse.status}`);
    }
    restoringState = true;
    await loadCapture(metadata, { restore: true });
    restoringState = false;
    persistState();
    setStatus(`已恢复最近快照：${metadata.point_count.toLocaleString()} 点`, "ok");
  } catch (error) {
    restoringState = false;
    setStatus(`状态检查失败: ${error.message || error}`, "error");
  }
}

setViewMode(viewMode);
updateSelectionLock();
initialize();
