import * as THREE from "/web/vendor/three.module.js";
import { OrbitControls } from "/web/vendor/OrbitControls.js";
import { STLLoader } from "/web/vendor/STLLoader.js";

const STATUS_COLORS = {
  safe: 0x1f9d55,
  near: 0xc99616,
  collision: 0xd64545,
};

const STATUS_LABELS = {
  safe: "安全",
  near: "过近",
  collision: "碰撞",
};

const dom = {
  viewport: document.getElementById("viewport"),
  armSelect: document.getElementById("armSelect"),
  jointFields: document.getElementById("jointFields"),
  stepsInput: document.getElementById("stepsInput"),
  durationInput: document.getElementById("durationInput"),
  targetX: document.getElementById("targetX"),
  targetY: document.getElementById("targetY"),
  targetZ: document.getElementById("targetZ"),
  tcpX: document.getElementById("tcpX"),
  tcpY: document.getElementById("tcpY"),
  tcpZ: document.getElementById("tcpZ"),
  planBtn: document.getElementById("planBtn"),
  playBtn: document.getElementById("playBtn"),
  resetBtn: document.getElementById("resetBtn"),
  ikMetric: document.getElementById("ikMetric"),
  errorMetric: document.getElementById("errorMetric"),
  pointsMetric: document.getElementById("pointsMetric"),
  collisionMetric: document.getElementById("collisionMetric"),
  messageBox: document.getElementById("messageBox"),
  statusPill: document.getElementById("statusPill"),
  frameLabel: document.getElementById("frameLabel"),
  tcpLabel: document.getElementById("tcpLabel"),
};

const state = {
  metadata: null,
  activeArm: "left",
  jointNames: [],
  currentPlan: null,
  frames: [],
  frameIndex: 0,
  playing: false,
  lastFrameTime: 0,
  robotGroup: null,
  jointNodes: new Map(),
  robotMaterials: [],
};

const scene = new THREE.Scene();
scene.background = new THREE.Color(0xe9eef2);

const camera = new THREE.PerspectiveCamera(48, 1, 0.01, 50);
camera.up.set(0, 0, 1);
camera.position.set(1.1, -2.0, 1.45);

const renderer = new THREE.WebGLRenderer({ antialias: true, preserveDrawingBuffer: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio, 2));
renderer.shadowMap.enabled = true;
dom.viewport.appendChild(renderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0.08, 0.0, 0.95);
controls.enableDamping = false;
controls.enablePan = true;
controls.screenSpacePanning = false;
controls.autoRotate = false;
camera.lookAt(controls.target);
controls.update();

const ambient = new THREE.HemisphereLight(0xffffff, 0x8090a0, 2.1);
scene.add(ambient);

const keyLight = new THREE.DirectionalLight(0xffffff, 2.2);
keyLight.position.set(1.6, -1.2, 2.8);
keyLight.castShadow = true;
scene.add(keyLight);

const fillLight = new THREE.DirectionalLight(0xb8d6ff, 0.9);
fillLight.position.set(-1.8, 1.5, 1.7);
scene.add(fillLight);

const grid = new THREE.GridHelper(2.2, 22, 0x94a3b8, 0xc9d2dc);
grid.rotation.x = Math.PI / 2;
scene.add(grid);

const axes = new THREE.AxesHelper(0.22);
scene.add(axes);

const targetMarker = createSphere(0.028, 0x246bfe, 0.95);
scene.add(targetMarker);

const tcpMarker = createSphere(0.024, STATUS_COLORS.safe, 1.0);
scene.add(tcpMarker);

const trajectoryGroup = new THREE.Group();
scene.add(trajectoryGroup);

const collisionGroup = new THREE.Group();
scene.add(collisionGroup);

const skeletonGroup = new THREE.Group();
scene.add(skeletonGroup);

init();
requestAnimationFrame(animate);

async function init() {
  resize();
  window.addEventListener("resize", resize);
  dom.planBtn.addEventListener("click", planDemo);
  dom.playBtn.addEventListener("click", togglePlayback);
  dom.resetBtn.addEventListener("click", () => applyFrame(0));
  dom.armSelect.addEventListener("change", onArmChange);
  for (const input of [dom.targetX, dom.targetY, dom.targetZ]) {
    input.addEventListener("input", updateTargetMarkerFromInputs);
  }

  try {
    const metadata = await fetchJson("/api/robot/metadata");
    state.metadata = metadata;
    await loadRobot(metadata.urdf_url);
    frameRobotInView();
    publishRenderState("robot-loaded");
    setArm(metadata.default_arm || "left");
    setMessage("URDF 和 mesh 已加载完成。");
    await planDemo();
  } catch (error) {
    console.error(error);
    setMessage(`加载失败：${error.message}`);
  }
}

function resize() {
  const rect = dom.viewport.getBoundingClientRect();
  const width = Math.max(1, rect.width);
  const height = Math.max(1, rect.height);
  renderer.setSize(width, height, false);
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
  frameRobotInView(false);
}

function animate(now) {
  requestAnimationFrame(animate);
  controls.update();
  if (state.playing && state.frames.length > 1) {
    const duration = Number(dom.durationInput.value || 4);
    const stepMs = Math.max(20, (duration * 1000) / Math.max(1, state.frames.length - 1));
    if (now - state.lastFrameTime >= stepMs) {
      state.frameIndex = (state.frameIndex + 1) % state.frames.length;
      applyFrame(state.frameIndex);
      state.lastFrameTime = now;
    }
  }
  renderer.render(scene, camera);
}

function onArmChange() {
  setArm(dom.armSelect.value);
  applyJointInputsToRobot();
}

function setArm(arm) {
  state.activeArm = arm;
  dom.armSelect.value = arm;
  state.jointNames = state.metadata.arms[arm].joint_names;
  renderJointInputs();
  const target = state.metadata.default_targets[arm];
  [dom.targetX.value, dom.targetY.value, dom.targetZ.value] = target.map(formatNumber);
  updateTargetMarkerFromInputs();
  const tcp = state.metadata.default_tcp_offset;
  [dom.tcpX.value, dom.tcpY.value, dom.tcpZ.value] = tcp.map(formatNumber);
}

function renderJointInputs() {
  dom.jointFields.replaceChildren();
  const defaults = state.metadata.default_current_joints;
  const limits = state.metadata.arms[state.activeArm].limits;
  state.jointNames.forEach((name, index) => {
    const row = document.createElement("div");
    row.className = "joint-row";

    const label = document.createElement("div");
    label.className = "joint-name";
    label.title = name;
    label.textContent = jointDisplayName(name);

    const limit = limits[index] || { lower: -3.14, upper: 3.14 };
    const initialValue = Number(defaults[index] ?? 0);

    const slider = document.createElement("input");
    slider.className = "joint-slider";
    slider.type = "range";
    slider.min = String(limit.lower);
    slider.max = String(limit.upper);
    slider.step = "0.01";
    slider.value = formatNumber(initialValue);
    slider.dataset.jointName = name;

    const valueInput = document.createElement("input");
    valueInput.className = "joint-value";
    valueInput.type = "number";
    valueInput.min = String(limit.lower);
    valueInput.max = String(limit.upper);
    valueInput.step = "0.01";
    valueInput.value = formatNumber(initialValue);
    valueInput.dataset.jointName = name;
    valueInput.dataset.jointValue = "true";

    slider.addEventListener("input", () => {
      valueInput.value = formatNumber(slider.value);
      handleJointEdit();
    });
    valueInput.addEventListener("input", () => {
      const value = clamp(Number(valueInput.value || 0), Number(slider.min), Number(slider.max));
      slider.value = formatNumber(value);
      valueInput.value = formatNumber(slider.value);
      handleJointEdit();
    });

    row.append(label, slider, valueInput);
    dom.jointFields.append(row);
  });
}

function readJointInputs() {
  return Array.from(dom.jointFields.querySelectorAll("[data-joint-value]")).map((input) => Number(input.value || 0));
}

function readVec(inputs) {
  return inputs.map((input) => Number(input.value || 0));
}

function requestPayload() {
  return {
    arm: state.activeArm,
    current_joints: readJointInputs(),
    target_xyz: readVec([dom.targetX, dom.targetY, dom.targetZ]),
    tcp_offset: readVec([dom.tcpX, dom.tcpY, dom.tcpZ]),
    steps: Number(dom.stepsInput.value || 80),
    duration: Number(dom.durationInput.value || 4),
  };
}

async function planDemo() {
  dom.planBtn.disabled = true;
  setMessage("正在规划...");
  try {
    const payload = requestPayload();
    const data = await fetchJson("/api/demo/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    state.currentPlan = data;
    state.frames = data.trajectory.waypoints;
    state.frameIndex = 0;
    updateMetrics(data);
    updateTrajectory(data.trajectory.waypoints, data.collision.status);
    targetMarker.position.fromArray(data.target_xyz);
    applyFrame(0);
    state.playing = true;
    dom.playBtn.textContent = "暂停";
    setMessage(toChineseIkMessage(data.ik));
  } catch (error) {
    console.error(error);
    setMessage(`规划失败：${error.message}`);
  } finally {
    dom.planBtn.disabled = false;
  }
}

function togglePlayback() {
  state.playing = !state.playing;
  dom.playBtn.textContent = state.playing ? "暂停" : "播放";
  state.lastFrameTime = performance.now();
}

function pausePlayback() {
  state.playing = false;
  dom.playBtn.textContent = "播放";
}

function updateMetrics(data) {
  dom.ikMetric.textContent = data.ik.success ? "成功" : "最接近";
  dom.errorMetric.textContent = `${data.ik.error_mm.toFixed(1)} mm`;
  dom.pointsMetric.textContent = String(data.trajectory.waypoint_count);
  dom.collisionMetric.textContent = statusLabel(data.collision.status);
}

function updateTargetMarkerFromInputs() {
  targetMarker.position.fromArray(readVec([dom.targetX, dom.targetY, dom.targetZ]));
  publishRenderState("target-edited");
}

function handleJointEdit() {
  pausePlayback();
  applyJointInputsToRobot();
}

function applyJointInputsToRobot() {
  setRobotJoints(jointsToNamed(readJointInputs()));
  publishRenderState("joint-edited");
}

function applyFrame(index) {
  if (!state.frames.length) {
    return;
  }
  state.frameIndex = Math.max(0, Math.min(index, state.frames.length - 1));
  const frame = state.frames[state.frameIndex];
  setRobotJoints(frame.named_joints);
  tcpMarker.position.fromArray(frame.tcp_position);
  const status = frame.collision?.status || "safe";
  setStatus(status);
  updateCollisionHelpers(frame.collision?.shapes || {}, status);
  updateSkeleton(frame.link_positions, status);
  publishRenderState("frame-applied");
  dom.frameLabel.textContent = `帧 ${state.frameIndex + 1} / ${state.frames.length}`;
  dom.tcpLabel.textContent = `TCP ${frame.tcp_position.map((v) => v.toFixed(3)).join(", ")}`;
}

function setStatus(status) {
  const color = STATUS_COLORS[status] || STATUS_COLORS.safe;
  tcpMarker.material.color.setHex(color);
  dom.statusPill.className = `status-pill ${status}`;
  dom.statusPill.textContent = statusLabel(status);
  for (const material of state.robotMaterials) {
    material.emissive?.setHex(status === "collision" ? 0x3a0808 : status === "near" ? 0x312304 : 0x000000);
  }
}

function updateTrajectory(waypoints, status) {
  trajectoryGroup.clear();
  const points = waypoints.map((waypoint) => new THREE.Vector3().fromArray(waypoint.tcp_position));
  const geometry = new THREE.BufferGeometry().setFromPoints(points);
  const material = new THREE.LineBasicMaterial({
    color: STATUS_COLORS[status] || STATUS_COLORS.safe,
    linewidth: 2,
  });
  trajectoryGroup.add(new THREE.Line(geometry, material));
}

function updateCollisionHelpers(shapes, status) {
  collisionGroup.clear();
  const color = STATUS_COLORS[status] || STATUS_COLORS.safe;
  for (const [name, shape] of Object.entries(shapes)) {
    if (shape.kind === "box") {
      collisionGroup.add(createBoxHelper(shape, name === "torso_box" ? 0x5d738a : color));
    } else if (shape.kind === "sphere") {
      collisionGroup.add(createSphereHelper(shape, name.includes("head") ? 0x59606b : color));
    } else if (shape.kind === "capsule") {
      collisionGroup.add(createCapsuleHelper(shape, color));
    }
  }
}

function updateSkeleton(linkPositions, status) {
  skeletonGroup.clear();
  if (!linkPositions) {
    return;
  }
  const arm = state.activeArm;
  const names = [
    `${arm}_shoulder_yaw_link`,
    `${arm}_elbow_link`,
    `${arm}_wrist_roll_link`,
    `${arm}_wrist_yaw_link`,
    `${arm}_hand_palm_link`,
  ];
  const points = names
    .map((name) => linkPositions[name])
    .filter(Boolean)
    .map((value) => new THREE.Vector3().fromArray(value));
  if (points.length < 2) {
    return;
  }
  const geometry = new THREE.BufferGeometry().setFromPoints(points);
  const material = new THREE.LineBasicMaterial({ color: STATUS_COLORS[status] || STATUS_COLORS.safe });
  skeletonGroup.add(new THREE.Line(geometry, material));
}

function createBoxHelper(shape, color) {
  const [hx, hy, hz] = shape.half_extents;
  const geometry = new THREE.BoxGeometry(hx * 2, hy * 2, hz * 2);
  const material = transparentMaterial(color, 0.18);
  const mesh = new THREE.Mesh(geometry, material);
  const r = shape.rotation;
  mesh.matrixAutoUpdate = false;
  mesh.matrix.set(
    r[0][0],
    r[0][1],
    r[0][2],
    shape.center[0],
    r[1][0],
    r[1][1],
    r[1][2],
    shape.center[1],
    r[2][0],
    r[2][1],
    r[2][2],
    shape.center[2],
    0,
    0,
    0,
    1,
  );
  return mesh;
}

function createSphereHelper(shape, color) {
  const mesh = createSphere(shape.radius, color, 0.2);
  mesh.position.fromArray(shape.center);
  return mesh;
}

function createCapsuleHelper(shape, color) {
  const group = new THREE.Group();
  const a = new THREE.Vector3().fromArray(shape.a);
  const b = new THREE.Vector3().fromArray(shape.b);
  const radius = shape.radius;
  const direction = new THREE.Vector3().subVectors(b, a);
  const length = Math.max(0.001, direction.length());
  const cylinder = new THREE.Mesh(new THREE.CylinderGeometry(radius, radius, length, 16), transparentMaterial(color, 0.25));
  cylinder.position.copy(a).add(b).multiplyScalar(0.5);
  cylinder.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), direction.clone().normalize());
  const s1 = createSphere(radius, color, 0.25);
  const s2 = createSphere(radius, color, 0.25);
  s1.position.copy(a);
  s2.position.copy(b);
  group.add(cylinder, s1, s2);
  return group;
}

function createSphere(radius, color, opacity) {
  return new THREE.Mesh(new THREE.SphereGeometry(radius, 24, 16), transparentMaterial(color, opacity));
}

function transparentMaterial(color, opacity) {
  return new THREE.MeshStandardMaterial({
    color,
    transparent: opacity < 1,
    opacity,
    roughness: 0.65,
    metalness: 0.08,
    depthWrite: opacity >= 0.9,
  });
}

async function loadRobot(urdfUrl) {
  const urdfText = await fetchText(urdfUrl);
  const xml = new DOMParser().parseFromString(urdfText, "application/xml");
  const parserError = xml.querySelector("parsererror");
  if (parserError) {
    throw new Error("URDF parse error");
  }

  const linkGroups = new Map();
  const joints = [];
  const jointsByParent = new Map();
  const childLinks = new Set();
  const stlLoader = new STLLoader();
  const meshPromises = [];

  for (const linkEl of xml.querySelectorAll("link")) {
    const linkName = linkEl.getAttribute("name");
    const group = new THREE.Group();
    group.name = linkName;
    linkGroups.set(linkName, group);

    for (const visualEl of linkEl.querySelectorAll("visual")) {
      const meshEl = visualEl.querySelector("geometry > mesh");
      if (!meshEl) {
        continue;
      }
      const visualGroup = new THREE.Group();
      applyOrigin(visualGroup, parseOrigin(visualEl.querySelector("origin")));
      const material = materialFromVisual(visualEl);
      state.robotMaterials.push(material);
      const filename = meshEl.getAttribute("filename");
      const scale = parseVector(meshEl.getAttribute("scale"), [1, 1, 1]);
      const meshPromise = stlLoader
        .loadAsync(meshUrl(filename))
        .then((geometry) => {
          geometry.computeVertexNormals();
          const mesh = new THREE.Mesh(geometry, material);
          mesh.scale.set(scale[0], scale[1], scale[2]);
          mesh.castShadow = true;
          mesh.receiveShadow = true;
          visualGroup.add(mesh);
        })
        .catch((error) => console.warn(`Failed to load ${filename}`, error));
      meshPromises.push(meshPromise);
      group.add(visualGroup);
    }
  }

  for (const jointEl of xml.querySelectorAll("joint")) {
    const parent = jointEl.querySelector("parent")?.getAttribute("link");
    const child = jointEl.querySelector("child")?.getAttribute("link");
    if (!parent || !child) {
      continue;
    }
    const joint = {
      name: jointEl.getAttribute("name"),
      type: jointEl.getAttribute("type") || "fixed",
      parent,
      child,
      axis: new THREE.Vector3(...parseVector(jointEl.querySelector("axis")?.getAttribute("xyz"), [0, 0, 1])).normalize(),
      origin: parseOrigin(jointEl.querySelector("origin")),
    };
    joints.push(joint);
    childLinks.add(child);
    if (!jointsByParent.has(parent)) {
      jointsByParent.set(parent, []);
    }
    jointsByParent.get(parent).push(joint);
  }

  const rootLink = [...linkGroups.keys()].find((name) => !childLinks.has(name)) || "AGV_link";
  const rootGroup = new THREE.Group();
  rootGroup.name = "g1_d_robot";
  rootGroup.add(linkGroups.get(rootLink));
  attachChildren(rootLink);

  if (state.robotGroup) {
    scene.remove(state.robotGroup);
  }
  state.robotGroup = rootGroup;
  scene.add(rootGroup);
  await Promise.all(meshPromises);
  state.robotGroup.updateMatrixWorld(true);

  function attachChildren(parentLinkName) {
    const parentGroup = linkGroups.get(parentLinkName);
    for (const joint of jointsByParent.get(parentLinkName) || []) {
      const originGroup = new THREE.Group();
      originGroup.name = `${joint.name}_origin`;
      applyOrigin(originGroup, joint.origin);
      const motionGroup = new THREE.Group();
      motionGroup.name = `${joint.name}_motion`;
      originGroup.add(motionGroup);
      motionGroup.add(linkGroups.get(joint.child));
      parentGroup.add(originGroup);
      state.jointNodes.set(joint.name, { ...joint, motionGroup });
      attachChildren(joint.child);
    }
  }
}

function setRobotJoints(namedJoints) {
  for (const [name, node] of state.jointNodes.entries()) {
    const value = Number(namedJoints[name] || 0);
    node.motionGroup.position.set(0, 0, 0);
    node.motionGroup.quaternion.identity();
    if (node.type === "revolute" || node.type === "continuous") {
      node.motionGroup.quaternion.setFromAxisAngle(node.axis, value);
    } else if (node.type === "prismatic") {
      node.motionGroup.position.copy(node.axis).multiplyScalar(value);
    }
  }
}

function jointsToNamed(values) {
  return Object.fromEntries(state.jointNames.map((name, index) => [name, Number(values[index] || 0)]));
}

function parseOrigin(originEl) {
  return {
    xyz: parseVector(originEl?.getAttribute("xyz"), [0, 0, 0]),
    rpy: parseVector(originEl?.getAttribute("rpy"), [0, 0, 0]),
  };
}

function parseVector(value, fallback) {
  if (!value) {
    return fallback;
  }
  const parts = value.trim().split(/\s+/).map(Number);
  return parts.length === 3 && parts.every(Number.isFinite) ? parts : fallback;
}

function applyOrigin(object, origin) {
  object.position.set(origin.xyz[0], origin.xyz[1], origin.xyz[2]);
  object.rotation.set(origin.rpy[0], origin.rpy[1], origin.rpy[2], "XYZ");
}

function frameRobotInView(resetCamera = true) {
  if (!state.robotGroup) {
    return;
  }

  state.robotGroup.updateMatrixWorld(true);
  const box = new THREE.Box3().setFromObject(state.robotGroup);
  if (box.isEmpty()) {
    return;
  }

  const center = box.getCenter(new THREE.Vector3());
  const size = box.getSize(new THREE.Vector3());
  const maxDim = Math.max(size.x, size.y, size.z, 0.1);
  controls.target.copy(center);

  if (resetCamera) {
    const fitHeightDistance = maxDim / (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov * 0.5)));
    const fitWidthDistance = fitHeightDistance / Math.max(camera.aspect, 0.1);
    const distance = Math.max(fitHeightDistance, fitWidthDistance) * 1.35;
    const direction = new THREE.Vector3(0.85, -1.65, 0.72).normalize();
    camera.position.copy(center).addScaledVector(direction, distance);
    camera.near = Math.max(distance / 100, 0.001);
    camera.far = Math.max(distance * 20, 20);
    controls.minDistance = Math.max(maxDim * 0.25, 0.1);
    controls.maxDistance = Math.max(maxDim * 6, 3);
  }

  camera.lookAt(center);
  camera.updateProjectionMatrix();
  controls.update();
}

function materialFromVisual(visualEl) {
  const colorAttr = visualEl.querySelector("material > color")?.getAttribute("rgba");
  const rgba = colorAttr ? colorAttr.trim().split(/\s+/).map(Number) : [0.7, 0.7, 0.7, 1];
  return new THREE.MeshStandardMaterial({
    color: new THREE.Color(rgba[0], rgba[1], rgba[2]),
    transparent: rgba[3] < 1,
    opacity: rgba[3],
    roughness: 0.68,
    metalness: 0.05,
  });
}

function meshUrl(filename) {
  const clean = filename.replace(/^package:\/\/[^/]+\//, "");
  return `/assets/g1_d_description/${clean.split("/").map(encodeURIComponent).join("/")}`;
}

async function fetchJson(url, options) {
  const response = await fetch(url, options);
  if (!response.ok) {
    let detail = response.statusText;
    try {
      detail = (await response.json()).detail || detail;
    } catch {
      detail = await response.text();
    }
    throw new Error(detail);
  }
  return response.json();
}

async function fetchText(url) {
  const response = await fetch(url);
  if (!response.ok) {
    throw new Error(`HTTP ${response.status} for ${url}`);
  }
  return response.text();
}

function setMessage(text) {
  dom.messageBox.textContent = text;
}

function formatNumber(value) {
  return Number(value).toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function statusLabel(status) {
  return STATUS_LABELS[status] || status || "-";
}

function jointDisplayName(name) {
  const shortName = name.replace(`${state.activeArm}_`, "").replace("_joint", "");
  const labels = {
    shoulder_pitch: "肩俯仰",
    shoulder_roll: "肩横滚",
    shoulder_yaw: "肩偏航",
    elbow: "肘关节",
    wrist_roll: "腕横滚",
    wrist_pitch: "腕俯仰",
    wrist_yaw: "腕偏航",
  };
  return labels[shortName] || shortName;
}

function toChineseIkMessage(ik) {
  const error = Number(ik?.error_mm ?? 0).toFixed(1);
  return ik?.success ? `IK 求解成功，末端误差 ${error} mm。` : `IK 未完全收敛，当前最接近误差 ${error} mm。`;
}

function publishRenderState(stage) {
  let objectCount = 0;
  let meshCount = 0;
  let lineCount = 0;
  scene.traverse((object) => {
    objectCount += 1;
    if (object.isMesh) {
      meshCount += 1;
    }
    if (object.isLine) {
      lineCount += 1;
    }
  });
  const tcpNdc = tcpMarker.position.clone().project(camera).toArray();
  const targetNdc = targetMarker.position.clone().project(camera).toArray();
  let robotBox = null;
  if (state.robotGroup) {
    const box = new THREE.Box3().setFromObject(state.robotGroup);
    if (!box.isEmpty()) {
      const center = box.getCenter(new THREE.Vector3());
      const size = box.getSize(new THREE.Vector3());
      robotBox = {
        center: center.toArray(),
        size: size.toArray(),
        centerNdc: center.clone().project(camera).toArray(),
      };
    }
  }
  document.body.dataset.g1Scene = JSON.stringify({
    stage,
    objectCount,
    meshCount,
    lineCount,
    frames: state.frames.length,
    materials: state.robotMaterials.length,
    joints: state.jointNodes.size,
    tcpNdc,
    targetNdc,
    robotBox,
  });
}
