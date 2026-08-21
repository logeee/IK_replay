import * as THREE from "three";
import { OrbitControls } from "/web/vendor/OrbitControls.js";
import { STLLoader } from "/web/vendor/STLLoader.js";

const viewport = document.getElementById("compareViewport");
const placeholder = document.getElementById("comparePlaceholder");
const sampleSelect = document.getElementById("compareSampleSelect");
const previousButton = document.getElementById("comparePrevSample");
const nextButton = document.getElementById("compareNextSample");
const samplePosition = document.getElementById("compareSamplePosition");
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
const THEORY_TCP_COLOR = 0x1687ff;
const MEASURED_TCP_COLOR = 0xff8b5c;
const CONTEXT_COLOR = 0x7d8792;
const ERROR_COLOR = 0xf3bc5b;

const state = {
  currentRunId: null,
  requestSequence: 0,
  availableSamples: [],
  sampleIndex: null,
  robotId: null,
  metadata: null,
  meshBaseUrl: "/assets/",
  theoretical: null,
  measured: null,
  errorGroup: new THREE.Group(),
  sceneOffset: new THREE.Vector3(),
  lastBounds: null,
};

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x07111d);
scene.add(state.errorGroup);
scene.add(new THREE.HemisphereLight(0xe5f4ff, 0x182735, 2.5));
const keyLight = new THREE.DirectionalLight(0xffffff, 2.2);
keyLight.position.set(1.7, -1.5, 2.8);
scene.add(keyLight);
const fillLight = new THREE.DirectionalLight(0x8cc8ff, 0.75);
fillLight.position.set(-1.4, 1.3, 1.6);
scene.add(fillLight);
const grid = new THREE.GridHelper(2.4, 24, 0x365067, 0x1a2b3a);
grid.rotation.x = Math.PI / 2;
scene.add(grid);
scene.add(new THREE.AxesHelper(0.18));

const camera = new THREE.PerspectiveCamera(42, 1, 0.01, 30);
camera.up.set(0, 0, 1);
camera.position.set(1.2, -2.1, 1.25);
const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(Math.min(window.devicePixelRatio || 1, 2));
renderer.outputColorSpace = THREE.SRGBColorSpace;
renderer.shadowMap.enabled = true;
viewport.appendChild(renderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0, 0, 0.65);
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

function parseVector(value, fallback) {
  if (!value) return fallback;
  const parts = value.trim().split(/\s+/).map(Number);
  return parts.length === 3 && parts.every(Number.isFinite) ? parts : fallback;
}

function parseOrigin(element) {
  return {
    xyz: parseVector(element?.getAttribute("xyz"), [0, 0, 0]),
    rpy: parseVector(element?.getAttribute("rpy"), [0, 0, 0]),
  };
}

function applyOrigin(object, origin) {
  object.position.set(...origin.xyz);
  object.rotation.set(...origin.rpy, "XYZ");
}

function meshUrl(filename) {
  if (!filename) return "";
  if (/^https?:\/\//.test(filename) || filename.startsWith("/")) return filename;
  const clean = filename.replace(/^package:\/\/[^/]+\//, "").replace(/^file:\/\//, "");
  return `${state.meshBaseUrl}${clean.split("/").map(encodeURIComponent).join("/")}`;
}

function modelMaterial(color, opacity) {
  return new THREE.MeshStandardMaterial({
    color,
    emissive: color,
    emissiveIntensity: 0.035,
    transparent: true,
    opacity,
    roughness: 0.52,
    metalness: 0.08,
    depthWrite: false,
    side: THREE.DoubleSide,
  });
}

async function buildRobotInstance(xml, color, opacity, geometryCache) {
  const links = new Map();
  const jointsByParent = new Map();
  const childLinks = new Set();
  const jointNodes = new Map();
  const loader = new STLLoader();
  const meshTasks = [];

  function loadGeometry(url) {
    if (!geometryCache.has(url)) {
      geometryCache.set(
        url,
        loader.loadAsync(url).then((geometry) => {
          geometry.computeVertexNormals();
          return geometry;
        }),
      );
    }
    return geometryCache.get(url);
  }

  for (const linkElement of xml.querySelectorAll("link")) {
    const name = linkElement.getAttribute("name");
    const group = new THREE.Group();
    group.name = name;
    links.set(name, group);
    for (const visualElement of linkElement.querySelectorAll("visual")) {
      const meshElement = visualElement.querySelector("geometry > mesh");
      if (!meshElement) continue;
      const visualGroup = new THREE.Group();
      applyOrigin(visualGroup, parseOrigin(visualElement.querySelector("origin")));
      const scale = parseVector(meshElement.getAttribute("scale"), [1, 1, 1]);
      const filename = meshElement.getAttribute("filename");
      meshTasks.push(
        loadGeometry(meshUrl(filename))
          .then((geometry) => {
            const mesh = new THREE.Mesh(geometry, modelMaterial(color, opacity));
            mesh.scale.set(...scale);
            mesh.userData.urdfLinkName = name;
            mesh.castShadow = true;
            mesh.receiveShadow = true;
            visualGroup.add(mesh);
          })
          .catch((error) => console.warn(`mesh加载失败: ${filename}`, error)),
      );
      group.add(visualGroup);
    }
  }

  for (const jointElement of xml.querySelectorAll("joint")) {
    const parent = jointElement.querySelector("parent")?.getAttribute("link");
    const child = jointElement.querySelector("child")?.getAttribute("link");
    if (!parent || !child) continue;
    const joint = {
      name: jointElement.getAttribute("name"),
      type: jointElement.getAttribute("type") || "fixed",
      parent,
      child,
      axis: new THREE.Vector3(
        ...parseVector(jointElement.querySelector("axis")?.getAttribute("xyz"), [0, 0, 1]),
      ).normalize(),
      origin: parseOrigin(jointElement.querySelector("origin")),
    };
    childLinks.add(child);
    if (!jointsByParent.has(parent)) jointsByParent.set(parent, []);
    jointsByParent.get(parent).push(joint);
  }

  const rootLink = [...links.keys()].find((name) => !childLinks.has(name));
  if (!rootLink) throw new Error("URDF没有根link");
  const root = new THREE.Group();
  root.add(links.get(rootLink));

  function attachChildren(parentName) {
    const parent = links.get(parentName);
    for (const joint of jointsByParent.get(parentName) || []) {
      const origin = new THREE.Group();
      applyOrigin(origin, joint.origin);
      const motion = new THREE.Group();
      origin.add(motion);
      motion.add(links.get(joint.child));
      parent.add(origin);
      jointNodes.set(joint.name, { ...joint, motion });
      attachChildren(joint.child);
    }
  }
  attachChildren(rootLink);
  await Promise.all(meshTasks);
  return { root, links, jointNodes, toolGroup: null };
}

function defaultRobotJoints() {
  const values = {};
  Object.values(state.metadata?.chains || {}).forEach((chain) => {
    Object.assign(values, chain.default_current_joints || {});
  });
  return values;
}

function setRobotJoints(instance, values) {
  const complete = { ...defaultRobotJoints(), ...(values || {}) };
  for (const [name, joint] of instance.jointNodes.entries()) {
    const value = Number(complete[name] || 0);
    joint.motion.position.set(0, 0, 0);
    joint.motion.quaternion.identity();
    if (joint.type === "revolute" || joint.type === "continuous") {
      joint.motion.quaternion.setFromAxisAngle(joint.axis, value);
    } else if (joint.type === "prismatic") {
      joint.motion.position.copy(joint.axis).multiplyScalar(value);
    }
  }
  instance.root.updateMatrixWorld(true);
}

async function ensureFullRobots(robotId) {
  if (state.robotId === robotId && state.theoretical && state.measured) return;
  placeholder.style.display = "grid";
  placeholder.textContent = "正在加载理论与实测完整机器人模型…";
  const metadataResponse = await fetch(
    `/api/gravity/robot_metadata?robot=${encodeURIComponent(robotId)}`,
    { cache: "no-store" },
  );
  const metadataPayload = await metadataResponse.json();
  if (!metadataResponse.ok || metadataPayload.ok === false) {
    throw new Error(metadataPayload.error || "机器人元数据读取失败");
  }
  state.metadata = metadataPayload.metadata;
  state.meshBaseUrl = state.metadata.robot.mesh_base_url;
  const urdfResponse = await fetch(
    `${state.metadata.robot.urdf_url}?v=gravity-comparison-2`,
    { cache: "no-store" },
  );
  if (!urdfResponse.ok) throw new Error(`URDF读取失败 HTTP ${urdfResponse.status}`);
  const xml = new DOMParser().parseFromString(await urdfResponse.text(), "application/xml");
  if (xml.querySelector("parsererror")) throw new Error("URDF解析失败");

  if (state.theoretical) scene.remove(state.theoretical.root);
  if (state.measured) scene.remove(state.measured.root);
  const geometryCache = new Map();
  [state.theoretical, state.measured] = await Promise.all([
    buildRobotInstance(xml, THEORY_COLOR, 0.34, geometryCache),
    buildRobotInstance(xml, MEASURED_COLOR, 0.34, geometryCache),
  ]);
  scene.add(state.theoretical.root, state.measured.root);
  setRobotJoints(state.theoretical, {});
  setRobotJoints(state.measured, {});
  state.theoretical.root.position.set(0, 0, 0);
  state.theoretical.root.updateMatrixWorld(true);
  const bounds = new THREE.Box3().setFromObject(state.theoretical.root);
  state.sceneOffset.set(0, 0, bounds.isEmpty() ? 0 : -bounds.min.z);
  state.theoretical.root.position.copy(state.sceneOffset);
  state.measured.root.position.copy(state.sceneOffset);
  state.theoretical.root.updateMatrixWorld(true);
  state.measured.root.updateMatrixWorld(true);
  state.robotId = robotId;
}

function applyComparisonAppearance(chainId) {
  const chain = state.metadata?.chains?.[chainId];
  const activeLinks = new Set(
    (chain?.chain_links || chain?.display_links || []).filter(
      (name) => name !== chain?.base_link,
    ),
  );
  activeLinks.add(chainId === "left_arm" ? "left_hand_link" : "right_hand_link");

  state.theoretical.root.traverse((object) => {
    if (!object.isMesh || !object.userData.urdfLinkName) return;
    const active = activeLinks.has(object.userData.urdfLinkName);
    object.userData.comparisonRole = active ? "theoretical" : "context";
    object.visible = true;
    object.material.color.setHex(active ? THEORY_COLOR : CONTEXT_COLOR);
    object.material.emissive.setHex(active ? THEORY_COLOR : CONTEXT_COLOR);
    object.material.emissiveIntensity = active ? 0.035 : 0.01;
    object.material.opacity = active ? 0.38 : 0.42;
  });
  state.measured.root.traverse((object) => {
    if (!object.isMesh || !object.userData.urdfLinkName) return;
    const active = activeLinks.has(object.userData.urdfLinkName);
    object.userData.comparisonRole = active ? "measured" : "hidden-context";
    object.visible = active;
    object.material.color.setHex(MEASURED_COLOR);
    object.material.emissive.setHex(MEASURED_COLOR);
    object.material.opacity = 0.38;
  });
}

function setTheoreticalVisible(visible) {
  if (!state.theoretical) return;
  state.theoretical.root.visible = true;
  state.theoretical.root.traverse((object) => {
    if (object.userData.comparisonRole === "theoretical") {
      object.visible = visible;
    }
  });
  if (state.theoretical.toolGroup) state.theoretical.toolGroup.visible = visible;
}

function attachToolVisualization(instance, visual, modelColor, chainId) {
  instance.toolGroup?.removeFromParent();
  instance.toolGroup = null;
  const tcpOffset = visual?.tcp_offset;
  if (!Array.isArray(tcpOffset) || tcpOffset.length !== 3) return;
  const chain = state.metadata?.chains?.[chainId];
  const wrist = instance.links.get(visual.wrist_link || chain?.end_link);
  if (!wrist) return;
  const handName = chainId === "left_arm" ? "left_hand_link" : "right_hand_link";
  const hand = instance.links.get(handName);
  const flange = new THREE.Vector3();
  if (hand) {
    wrist.updateWorldMatrix(true, false);
    hand.updateWorldMatrix(true, false);
    wrist.worldToLocal(hand.getWorldPosition(flange));
  }
  const group = new THREE.Group();
  const normal = new THREE.Vector3(1, 0, 0);
  const tcp = new THREE.Vector3(...tcpOffset.map(Number));

  const tcpDot = new THREE.Mesh(
    new THREE.SphereGeometry(0.017, 20, 14),
    new THREE.MeshBasicMaterial({
      color: modelColor,
      depthTest: false,
    }),
  );
  tcpDot.position.copy(tcp);
  tcpDot.renderOrder = 32;
  group.add(tcpDot);

  const foot = tcp.clone().sub(
    normal.clone().multiplyScalar(tcp.clone().sub(flange).dot(normal)),
  );
  const axis = tcp.clone().sub(foot);
  if (axis.length() > 1e-6) {
    const capsuleMaterial = new THREE.MeshStandardMaterial({
      color: 0x111418,
      transparent: true,
      opacity: 0.46,
      roughness: 0.64,
      depthWrite: false,
    });
    const radius = 0.04;
    const shaft = new THREE.Mesh(
      new THREE.CylinderGeometry(radius, radius, axis.length(), 18, 1, true),
      capsuleMaterial,
    );
    shaft.position.copy(foot).lerp(tcp, 0.5);
    shaft.quaternion.setFromUnitVectors(
      new THREE.Vector3(0, 1, 0),
      axis.clone().normalize(),
    );
    const capA = new THREE.Mesh(
      new THREE.SphereGeometry(radius, 16, 11),
      capsuleMaterial,
    );
    capA.position.copy(foot);
    const capB = new THREE.Mesh(
      new THREE.SphereGeometry(radius, 16, 11),
      capsuleMaterial,
    );
    capB.position.copy(tcp);
    group.add(shaft, capA, capB);
  }
  wrist.add(group);
  instance.toolGroup = group;
}

function tcpConnector(a, b) {
  const start = new THREE.Vector3(...a);
  const end = new THREE.Vector3(...b);
  const direction = end.clone().sub(start);
  if (direction.length() < 1e-6) return null;
  const connector = new THREE.Mesh(
    new THREE.CylinderGeometry(0.002, 0.002, direction.length(), 10),
    new THREE.MeshBasicMaterial({
      color: 0xff3b30,
      transparent: true,
      opacity: 0.96,
      depthTest: false,
    }),
  );
  connector.position.copy(start).lerp(end, 0.5);
  connector.quaternion.setFromUnitVectors(
    new THREE.Vector3(0, 1, 0),
    direction.normalize(),
  );
  connector.renderOrder = 25;
  return connector;
}

function disposeGroup(group) {
  group.traverse((object) => {
    object.geometry?.dispose?.();
    if (Array.isArray(object.material)) {
      object.material.forEach((entry) => entry.dispose?.());
    } else {
      object.material?.dispose?.();
    }
  });
}

function buildErrors(theoretical, measured) {
  const group = new THREE.Group();
  group.position.copy(state.sceneOffset);
  const measuredByName = new Map((measured.links || []).map((item) => [item.name, item]));
  for (const expected of theoretical.links || []) {
    const actual = measuredByName.get(expected.name);
    if (!actual) continue;
    const geometry = new THREE.BufferGeometry().setFromPoints([
      new THREE.Vector3(...expected.xyz),
      new THREE.Vector3(...actual.xyz),
    ]);
    const line = new THREE.Line(
      geometry,
      new THREE.LineBasicMaterial({
        color: ERROR_COLOR,
        transparent: true,
        opacity: 0.95,
        depthTest: false,
      }),
    );
    line.renderOrder = 24;
    group.add(line);
  }
  const connector = tcpConnector(theoretical.tcp_root_m, measured.tcp_root_m);
  if (connector) group.add(connector);
  return group;
}

function replaceErrorGroup(next) {
  scene.remove(state.errorGroup);
  disposeGroup(state.errorGroup);
  state.errorGroup = next;
  scene.add(next);
}

function frameComparison() {
  if (!state.theoretical || !state.measured) return;
  const bounds = new THREE.Box3();
  bounds.expandByObject(state.theoretical.root);
  bounds.expandByObject(state.measured.root);
  if (bounds.isEmpty()) return;
  state.lastBounds = bounds.clone();
  const center = bounds.getCenter(new THREE.Vector3());
  const size = bounds.getSize(new THREE.Vector3());
  const radius = Math.max(size.length(), 1.0);
  controls.target.copy(center);
  camera.position.copy(center).add(
    new THREE.Vector3(1.05, -1.7, 0.72).normalize().multiplyScalar(radius * 1.08),
  );
  camera.near = Math.max(0.01, radius / 100);
  camera.far = Math.max(20, radius * 10);
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
      const color = Math.abs(error) > 2
        ? "#ff8b5c"
        : Math.abs(error) > 0.5
          ? "#f3bc5b"
          : "#54d68b";
      return `<div class="joint-error"><span>${name.replace("right_", "").replace("_joint", "")}</span><b style="color:${color}">${error >= 0 ? "+" : ""}${error.toFixed(2)}°</b></div>`;
    })
    .join("");
}

function updateSampleControls(comparison) {
  state.availableSamples = comparison.available_samples || [];
  state.sampleIndex = Number(comparison.sample_index);
  const optionKey = state.availableSamples
    .map((item) => `${item.index}:${item.trajectory_fraction}`)
    .join("|");
  if (sampleSelect.dataset.key !== `${state.currentRunId}:${optionKey}`) {
    sampleSelect.innerHTML = state.availableSamples
      .map((item) => {
        const percentage = Math.round(Number(item.trajectory_fraction) * 100);
        const label = item.type === "final" ? "终点" : `中途 ${percentage}%`;
        return `<option value="${item.index}">${item.index}. ${label} · ${item.sample_count}帧</option>`;
      })
      .join("");
    sampleSelect.dataset.key = `${state.currentRunId}:${optionKey}`;
  }
  sampleSelect.value = String(state.sampleIndex);
  sampleSelect.disabled = false;
  const position = state.availableSamples.findIndex(
    (item) => Number(item.index) === state.sampleIndex,
  );
  samplePosition.textContent = `${position + 1} / ${state.availableSamples.length}`;
  previousButton.disabled = position <= 0;
  nextButton.disabled = position < 0 || position >= state.availableSamples.length - 1;
  return position;
}

async function renderComparison(comparison, resetView) {
  await ensureFullRobots(comparison.robot);
  setRobotJoints(state.theoretical, comparison.theoretical.named_joints);
  setRobotJoints(state.measured, comparison.measured.named_joints);
  applyComparisonAppearance(comparison.chain_id);
  attachToolVisualization(
    state.theoretical,
    comparison.tool_visualization,
    THEORY_TCP_COLOR,
    comparison.chain_id,
  );
  attachToolVisualization(
    state.measured,
    comparison.tool_visualization,
    MEASURED_TCP_COLOR,
    comparison.chain_id,
  );
  replaceErrorGroup(buildErrors(comparison.theoretical, comparison.measured));
  setTheoreticalVisible(showTheoretical.checked);
  state.measured.root.visible = showMeasured.checked;
  state.errorGroup.visible = showTheoretical.checked && showMeasured.checked;
  const position = updateSampleControls(comparison);
  runLabel.textContent = `${comparison.point_name || comparison.run_id} · 重力版本 ${comparison.gravity_version || "未记录"} · 第${position + 1}个采样点`;
  renderMetrics(comparison);
  placeholder.style.display = "none";
  resetButton.disabled = false;
  if (resetView || state.lastBounds === null) frameComparison();
}

async function loadComparison(runId, sampleIndex, resetView = false) {
  const requestSequence = ++state.requestSequence;
  placeholder.style.display = "grid";
  placeholder.textContent = `正在加载第${sampleIndex}个采样点的双完整机器人…`;
  previousButton.disabled = true;
  nextButton.disabled = true;
  try {
    const response = await fetch(
      `/api/gravity/runs/${encodeURIComponent(runId)}/comparison?sample_index=${encodeURIComponent(sampleIndex)}`,
      { cache: "no-store" },
    );
    const payload = await response.json();
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    if (requestSequence !== state.requestSequence) return;
    await renderComparison(payload.comparison, resetView);
  } catch (error) {
    if (requestSequence !== state.requestSequence) return;
    placeholder.style.display = "grid";
    placeholder.textContent = `无法显示姿态对比：${error.message}`;
  }
}

function adjacentSample(delta) {
  if (!state.currentRunId) return;
  const position = state.availableSamples.findIndex(
    (item) => Number(item.index) === state.sampleIndex,
  );
  const next = state.availableSamples[position + delta];
  if (next) loadComparison(state.currentRunId, Number(next.index), false);
}

window.addEventListener("gravity:open-comparison", (event) => {
  state.currentRunId = event.detail.runId;
  state.availableSamples = [];
  state.sampleIndex = null;
  sampleSelect.disabled = true;
  samplePosition.textContent = "— / —";
  loadComparison(state.currentRunId, 1, true);
});
sampleSelect.addEventListener("change", () => {
  if (state.currentRunId) {
    loadComparison(state.currentRunId, Number(sampleSelect.value), false);
  }
});
previousButton.addEventListener("click", () => adjacentSample(-1));
nextButton.addEventListener("click", () => adjacentSample(1));
showTheoretical.addEventListener("change", () => {
  setTheoreticalVisible(showTheoretical.checked);
  state.errorGroup.visible = showTheoretical.checked && showMeasured.checked;
});
showMeasured.addEventListener("change", () => {
  if (state.measured) state.measured.root.visible = showMeasured.checked;
  state.errorGroup.visible = showTheoretical.checked && showMeasured.checked;
});
resetButton.addEventListener("click", frameComparison);
