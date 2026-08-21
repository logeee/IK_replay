import * as THREE from "three";
import { OrbitControls } from "/web/vendor/OrbitControls.js";
import { STLLoader } from "/web/vendor/STLLoader.js";

const viewport = document.getElementById("planRobotViewport");
const placeholder = document.getElementById("planRobotPlaceholder");
const playButton = document.getElementById("planReplayBtn");
const resetButton = document.getElementById("planResetViewBtn");
const showCollisions = document.getElementById("planShowCollisions");
const slider = document.getElementById("planFrameSlider");
const frameLabel = document.getElementById("planFrameLabel");

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x07111d);
scene.add(new THREE.HemisphereLight(0xeaf5ff, 0x263544, 2.5));
const keyLight = new THREE.DirectionalLight(0xffffff, 2.2);
keyLight.position.set(1.8, -1.4, 2.8);
scene.add(keyLight);
const fillLight = new THREE.DirectionalLight(0x91c9ff, 0.8);
fillLight.position.set(-1.5, 1.2, 1.5);
scene.add(fillLight);
const grid = new THREE.GridHelper(2.4, 24, 0x3b566d, 0x1c3040);
grid.rotation.x = Math.PI / 2;
scene.add(grid);
scene.add(new THREE.AxesHelper(0.18));
const collisionGroup = new THREE.Group();
scene.add(collisionGroup);

const camera = new THREE.PerspectiveCamera(43, 1, 0.01, 30);
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

const state = {
  robotId: null,
  metadata: null,
  urdfXml: null,
  robotGroup: null,
  jointNodes: new Map(),
  linkGroups: new Map(),
  toolGroup: null,
  toolVisual: {},
  chainId: "right_arm",
  meshBaseUrl: "/assets/",
  sceneOffset: new THREE.Vector3(),
  frames: [],
  sampleFractions: [],
  collision: null,
  blocked: false,
  duration: 4,
  frameIndex: 0,
  playing: false,
  startedAt: 0,
  startedFrame: 0,
  multiMode: false,
  multiOverlays: [],
};

function resize() {
  const width = Math.max(1, viewport.clientWidth);
  const height = Math.max(1, viewport.clientHeight);
  renderer.setSize(width, height, false);
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
}
new ResizeObserver(resize).observe(viewport);
resize();

function animate(now) {
  if (state.playing && state.frames.length > 1) {
    const frameDuration = (state.duration * 1000) / (state.frames.length - 1);
    const elapsedFrames = Math.floor((now - state.startedAt) / Math.max(frameDuration, 1));
    const next = state.startedFrame + elapsedFrames;
    if (next >= state.frames.length) {
      applyFrame(state.frames.length - 1);
      state.playing = false;
      playButton.textContent = "↻ 重播";
    } else if (next !== state.frameIndex) {
      applyFrame(next);
    }
  }
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

function visualMaterial(visualElement) {
  const attribute = visualElement
    .querySelector("material > color")
    ?.getAttribute("rgba");
  const rgba = attribute
    ? attribute.trim().split(/\s+/).map(Number)
    : [0.58, 0.68, 0.76, 1];
  return new THREE.MeshStandardMaterial({
    color: new THREE.Color(rgba[0], rgba[1], rgba[2]),
    transparent: rgba[3] < 1,
    opacity: rgba[3],
    roughness: 0.58,
    metalness: 0.08,
  });
}

async function loadRobot(robotId) {
  if (state.robotId === robotId && state.robotGroup) return;
  placeholder.style.display = "grid";
  placeholder.textContent = "正在加载完整URDF机器人模型…";
  const metadataResponse = await fetch(
    `/api/gravity/robot_metadata?robot=${encodeURIComponent(robotId)}`,
    { cache: "no-store" },
  );
  const metadataPayload = await metadataResponse.json();
  if (!metadataResponse.ok || metadataPayload.ok === false) {
    throw new Error(metadataPayload.error || "机器人元数据读取失败");
  }
  const metadata = metadataPayload.metadata;
  const urdfResponse = await fetch(`${metadata.robot.urdf_url}?v=gravity-preview-1`);
  if (!urdfResponse.ok) throw new Error(`URDF读取失败 HTTP ${urdfResponse.status}`);
  const xml = new DOMParser().parseFromString(
    await urdfResponse.text(),
    "application/xml",
  );
  if (xml.querySelector("parsererror")) throw new Error("URDF解析失败");
  state.urdfXml = xml;

  if (state.robotGroup) scene.remove(state.robotGroup);
  state.jointNodes.clear();
  state.metadata = metadata;
  state.meshBaseUrl = metadata.robot.mesh_base_url;
  const links = new Map();
  const jointsByParent = new Map();
  const childLinks = new Set();
  const loader = new STLLoader();
  const meshTasks = [];

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
      const material = visualMaterial(visualElement);
      const filename = meshElement.getAttribute("filename");
      meshTasks.push(
        loader
          .loadAsync(meshUrl(filename))
          .then((geometry) => {
            geometry.computeVertexNormals();
            const mesh = new THREE.Mesh(geometry, material);
            mesh.scale.set(...scale);
            mesh.userData.urdfLinkName = name;
            mesh.userData.originalColor = material.color.getHex();
            mesh.userData.originalEmissive = material.emissive.getHex();
            mesh.userData.originalOpacity = material.opacity;
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
  root.name = metadata.robot.name;
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
      state.jointNodes.set(joint.name, { ...joint, motion });
      attachChildren(joint.child);
    }
  }
  attachChildren(rootLink);
  state.robotGroup = root;
  state.linkGroups = links;
  scene.add(root);
  await Promise.all(meshTasks);

  const initial = {};
  Object.values(metadata.chains || {}).forEach((chain) => {
    Object.assign(initial, chain.default_current_joints || {});
  });
  setRobotJoints(initial);
  updateGroundAndView();
  state.robotId = robotId;
}

function activeArmLinks(chainId) {
  const chain = state.metadata?.chains?.[chainId];
  const links = new Set(
    (chain?.chain_links || chain?.display_links || []).filter(
      (name) => name !== chain?.base_link,
    ),
  );
  links.add(chainId === "left_arm" ? "left_hand_link" : "right_hand_link");
  return links;
}

function clearMultiOverlays() {
  for (const overlay of state.multiOverlays) {
    scene.remove(overlay.root);
    overlay.root.traverse((object) => {
      object.geometry?.dispose?.();
      if (Array.isArray(object.material)) {
        object.material.forEach((entry) => entry.dispose?.());
      } else {
        object.material?.dispose?.();
      }
    });
  }
  state.multiOverlays = [];
  state.multiMode = false;
}

function restoreSingleAppearance() {
  clearMultiOverlays();
  state.robotGroup?.traverse((object) => {
    if (!object.isMesh || object.userData.originalColor === undefined) return;
    object.visible = true;
    object.material.color.setHex(object.userData.originalColor);
    object.material.emissive.setHex(object.userData.originalEmissive);
    object.material.opacity = object.userData.originalOpacity;
  });
  showCollisions.disabled = false;
}

function applyMultiContextAppearance(chainId) {
  const active = activeArmLinks(chainId);
  state.robotGroup?.traverse((object) => {
    if (!object.isMesh || !object.userData.urdfLinkName) return;
    object.visible = !active.has(object.userData.urdfLinkName);
    object.material.color.setHex(0x303a44);
    object.material.emissive.setHex(0x202830);
    object.material.opacity = 0.54;
  });
}

async function buildArmOverlay(chainId, color) {
  if (!state.urdfXml) throw new Error("URDF尚未加载");
  const links = new Map();
  const jointsByParent = new Map();
  const childLinks = new Set();
  const jointNodes = new Map();
  const loader = new STLLoader();
  const meshTasks = [];
  const visibleLinks = activeArmLinks(chainId);
  const modelColor = new THREE.Color(color);

  for (const linkElement of state.urdfXml.querySelectorAll("link")) {
    const name = linkElement.getAttribute("name");
    const group = new THREE.Group();
    group.name = name;
    links.set(name, group);
    if (!visibleLinks.has(name)) continue;
    for (const visualElement of linkElement.querySelectorAll("visual")) {
      const meshElement = visualElement.querySelector("geometry > mesh");
      if (!meshElement) continue;
      const visualGroup = new THREE.Group();
      applyOrigin(visualGroup, parseOrigin(visualElement.querySelector("origin")));
      const scale = parseVector(meshElement.getAttribute("scale"), [1, 1, 1]);
      const filename = meshElement.getAttribute("filename");
      meshTasks.push(
        loader.loadAsync(meshUrl(filename))
          .then((geometry) => {
            geometry.computeVertexNormals();
            const material = new THREE.MeshStandardMaterial({
              color: modelColor,
              emissive: modelColor,
              emissiveIntensity: 0.035,
              transparent: true,
              opacity: 0.42,
              roughness: 0.62,
              metalness: 0.08,
              depthWrite: false,
            });
            const mesh = new THREE.Mesh(geometry, material);
            mesh.scale.set(...scale);
            mesh.userData.urdfLinkName = name;
            visualGroup.add(mesh);
          })
          .catch((error) => console.warn(`叠加手臂mesh加载失败: ${filename}`, error)),
      );
      group.add(visualGroup);
    }
  }

  for (const jointElement of state.urdfXml.querySelectorAll("joint")) {
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
  return { root, links, jointNodes, toolGroup: null, frames: [], name: "", color };
}

function setInstanceJoints(instance, values) {
  const defaults = {};
  Object.values(state.metadata?.chains || {}).forEach((chain) => {
    Object.assign(defaults, chain.default_current_joints || {});
  });
  const complete = { ...defaults, ...(values || {}) };
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

function jointsAtComparisonProgress(instance, targetProgress) {
  const frames = instance.frames || [];
  if (!frames.length) return {};
  if (frames.length === 1) return frames[0];
  const progress = instance.comparisonProgress?.length === frames.length
    ? instance.comparisonProgress
    : frames.map((_, index) => index / (frames.length - 1));
  let upper = progress.findIndex((value) => Number(value) >= targetProgress);
  if (upper <= 0) return frames[0];
  if (upper < 0) return frames[frames.length - 1];
  const lower = upper - 1;
  const start = Number(progress[lower]);
  const end = Number(progress[upper]);
  const blend = end - start > 1e-9
    ? Math.max(0, Math.min(1, (targetProgress - start) / (end - start)))
    : 0;
  const values = {};
  const names = new Set([
    ...Object.keys(frames[lower] || {}),
    ...Object.keys(frames[upper] || {}),
  ]);
  names.forEach((name) => {
    const a = Number(frames[lower]?.[name] || 0);
    const b = Number(frames[upper]?.[name] || 0);
    values[name] = a + (b - a) * blend;
  });
  return values;
}

function attachComparisonTool(instance, visual, color, chainId) {
  const tcpOffset = visual?.tcp_offset;
  if (!Array.isArray(tcpOffset) || tcpOffset.length !== 3) return;
  const chain = state.metadata?.chains?.[chainId];
  const wrist = instance.links.get(visual.wrist_link || chain?.end_link);
  if (!wrist) return;
  const hand = instance.links.get(
    chainId === "left_arm" ? "left_hand_link" : "right_hand_link",
  );
  const flange = new THREE.Vector3();
  if (hand) {
    wrist.updateWorldMatrix(true, false);
    hand.updateWorldMatrix(true, false);
    wrist.worldToLocal(hand.getWorldPosition(flange));
  }
  const group = new THREE.Group();
  const tcp = new THREE.Vector3(...tcpOffset.map(Number));
  const tcpDot = new THREE.Mesh(
    new THREE.SphereGeometry(0.018, 24, 16),
    new THREE.MeshBasicMaterial({ color, depthTest: false }),
  );
  tcpDot.position.copy(tcp);
  tcpDot.renderOrder = 32;
  group.add(tcpDot);
  const normal = new THREE.Vector3(1, 0, 0);
  const foot = tcp.clone().sub(
    normal.clone().multiplyScalar(tcp.clone().sub(flange).dot(normal)),
  );
  const axis = tcp.clone().sub(foot);
  if (axis.length() > 1e-6) {
    const material = new THREE.MeshStandardMaterial({
      color: 0x111418,
      transparent: true,
      opacity: 0.72,
      roughness: 0.64,
      depthWrite: false,
    });
    const radius = 0.04;
    const shaft = new THREE.Mesh(
      new THREE.CylinderGeometry(radius, radius, axis.length(), 18, 1, true),
      material,
    );
    shaft.position.copy(foot).lerp(tcp, 0.5);
    shaft.quaternion.setFromUnitVectors(
      new THREE.Vector3(0, 1, 0),
      axis.clone().normalize(),
    );
    const capA = new THREE.Mesh(
      new THREE.SphereGeometry(radius, 16, 11),
      material,
    );
    capA.position.copy(foot);
    const capB = new THREE.Mesh(
      new THREE.SphereGeometry(radius, 16, 11),
      material,
    );
    capB.position.copy(tcp);
    group.add(shaft, capA, capB);
  }
  wrist.add(group);
  instance.toolGroup = group;
}

function attachToolVisualization() {
  state.toolGroup?.removeFromParent();
  state.toolGroup = null;
  const visual = state.toolVisual || {};
  const tcpOffset = visual.tcp_offset;
  if (!Array.isArray(tcpOffset) || tcpOffset.length !== 3) return;
  const chain = state.metadata?.chains?.[state.chainId];
  const wristLink = visual.wrist_link || chain?.end_link;
  const wrist = state.linkGroups.get(wristLink);
  if (!wrist) return;
  const handLinkName = state.chainId === "left_arm" ? "left_hand_link" : "right_hand_link";
  const hand = state.linkGroups.get(handLinkName);
  const flange = new THREE.Vector3(0, 0, 0);
  if (hand) {
    wrist.updateWorldMatrix(true, false);
    hand.updateWorldMatrix(true, false);
    wrist.worldToLocal(hand.getWorldPosition(flange));
  }
  const group = new THREE.Group();
  group.name = "gravity_plan_tool_markers";
  const normal = new THREE.Vector3(1, 0, 0);

  const disc = new THREE.Mesh(
    new THREE.CircleGeometry(0.06, 36),
    new THREE.MeshBasicMaterial({
      color: 0x35d07f,
      transparent: true,
      opacity: 0.28,
      side: THREE.DoubleSide,
      depthTest: false,
    }),
  );
  disc.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), normal);
  disc.position.copy(flange);
  group.add(disc);

  const markerColors = {
    blue: 0x1687ff,
    brown: 0x8b4513,
    gold: 0xffd700,
    gray: 0xb0b0b0,
    green: 0x26c95c,
    orange: 0xff8c00,
    pink: 0xff69b4,
    purple: 0x9b59ff,
    red: 0xff3030,
  };
  const tcp = new THREE.Vector3(...tcpOffset.map(Number));
  let referenceIsTcp = false;
  for (const [markerId, xyz] of Object.entries(visual.markers || {})) {
    if (!Array.isArray(xyz) || xyz.length !== 3) continue;
    const point = new THREE.Vector3(...xyz.map(Number));
    if (
      markerId === visual.reference_marker
      && point.distanceTo(tcp) < 1e-6
    ) {
      referenceIsTcp = true;
    }
    const dot = new THREE.Mesh(
      new THREE.SphereGeometry(0.012, 20, 14),
      new THREE.MeshBasicMaterial({
        color: markerColors[markerId] ?? 0xffffff,
        depthTest: false,
      }),
    );
    dot.position.copy(point);
    dot.renderOrder = 20;
    group.add(dot);
  }
  if (!referenceIsTcp) {
    const tcpDot = new THREE.Mesh(
      new THREE.SphereGeometry(0.018, 24, 16),
      new THREE.MeshBasicMaterial({ color: 0xffffff, depthTest: false }),
    );
    tcpDot.position.copy(tcp);
    tcpDot.renderOrder = 21;
    group.add(tcpDot);
  }

  const foot = tcp.clone().sub(
    normal.clone().multiplyScalar(tcp.clone().sub(flange).dot(normal)),
  );
  const axis = tcp.clone().sub(foot);
  if (axis.length() < 1e-6) {
    wrist.add(group);
    state.toolGroup = group;
    return;
  }
  const capsuleMaterial = new THREE.MeshStandardMaterial({
    color: 0x171a1e,
    transparent: true,
    opacity: 0.62,
    roughness: 0.62,
  });
  const radius = 0.04;
  const shaft = new THREE.Mesh(
    new THREE.CylinderGeometry(
      radius,
      radius,
      Math.max(axis.length(), 1e-4),
      20,
      1,
      true,
    ),
    capsuleMaterial,
  );
  shaft.position.copy(foot).lerp(tcp, 0.5);
  shaft.quaternion.setFromUnitVectors(
    new THREE.Vector3(0, 1, 0),
    axis.clone().normalize(),
  );
  const capA = new THREE.Mesh(
    new THREE.SphereGeometry(radius, 18, 12),
    capsuleMaterial,
  );
  capA.position.copy(foot);
  const capB = new THREE.Mesh(
    new THREE.SphereGeometry(radius, 18, 12),
    capsuleMaterial,
  );
  capB.position.copy(tcp);
  group.add(shaft, capA, capB);
  wrist.add(group);
  state.toolGroup = group;
}

function setRobotJoints(values) {
  if (!state.robotGroup) return;
  setInstanceJoints(
    { jointNodes: state.jointNodes, root: state.robotGroup },
    values,
  );
}

function clearCollisionGroup() {
  collisionGroup.traverse((object) => {
    object.geometry?.dispose?.();
    if (Array.isArray(object.material)) {
      object.material.forEach((entry) => entry.dispose?.());
    } else {
      object.material?.dispose?.();
    }
  });
  collisionGroup.clear();
}

function collisionMaterial(shape, highlighted, status) {
  let color = shape.role === "body" ? 0x9aa5b2 : 0x43d6e8;
  if (highlighted) {
    color = status === "collision" ? 0xff3b30 : 0xf3bc5b;
  }
  return new THREE.MeshBasicMaterial({
    color,
    transparent: true,
    opacity: highlighted ? 0.42 : 0.14,
    depthWrite: false,
    side: THREE.DoubleSide,
  });
}

function collisionPrimitive(shape, material) {
  if (shape.kind === "sphere") {
    const mesh = new THREE.Mesh(
      new THREE.SphereGeometry(Number(shape.radius), 20, 14),
      material,
    );
    mesh.position.set(...shape.center.map(Number));
    return mesh;
  }
  if (shape.kind === "box") {
    const [hx, hy, hz] = shape.half_extents.map(Number);
    const mesh = new THREE.Mesh(
      new THREE.BoxGeometry(hx * 2, hy * 2, hz * 2),
      material,
    );
    const r = shape.rotation;
    const matrix = new THREE.Matrix4().set(
      r[0][0], r[0][1], r[0][2], 0,
      r[1][0], r[1][1], r[1][2], 0,
      r[2][0], r[2][1], r[2][2], 0,
      0, 0, 0, 1,
    );
    mesh.quaternion.setFromRotationMatrix(matrix);
    mesh.position.set(...shape.center.map(Number));
    return mesh;
  }
  if (shape.kind === "capsule") {
    const group = new THREE.Group();
    const a = new THREE.Vector3(...shape.a.map(Number));
    const b = new THREE.Vector3(...shape.b.map(Number));
    const radius = Number(shape.radius);
    const length = a.distanceTo(b);
    const capA = new THREE.Mesh(
      new THREE.SphereGeometry(radius, 16, 12),
      material,
    );
    capA.position.copy(a);
    const capB = new THREE.Mesh(
      new THREE.SphereGeometry(radius, 16, 12),
      material,
    );
    capB.position.copy(b);
    const shaft = new THREE.Mesh(
      new THREE.CylinderGeometry(radius, radius, Math.max(length, 1e-4), 16, 1, true),
      material,
    );
    shaft.position.copy(a).lerp(b, 0.5);
    if (length > 1e-6) {
      shaft.quaternion.setFromUnitVectors(
        new THREE.Vector3(0, 1, 0),
        b.clone().sub(a).normalize(),
      );
    }
    group.add(capA, capB, shaft);
    return group;
  }
  return null;
}

function collisionCheckAt(index) {
  const checks = state.collision?.checks || [];
  return checks.find((check) => Number(check.index) === Number(index))
    || checks[Math.max(0, Math.min(index, checks.length - 1))]
    || null;
}

function updateCollisionOverlay() {
  clearCollisionGroup();
  if (state.multiMode || !showCollisions.checked) return;
  const check = collisionCheckAt(state.frameIndex);
  if (!check?.shapes) return;
  const pairNames = new Set([check.pair?.a, check.pair?.b].filter(Boolean));
  for (const [name, shape] of Object.entries(check.shapes)) {
    if (!["sphere", "box", "capsule"].includes(shape.kind)) continue;
    const mesh = collisionPrimitive(
      shape,
      collisionMaterial(shape, pairNames.has(name), check.status),
    );
    if (mesh) collisionGroup.add(mesh);
  }
  collisionGroup.position.copy(state.sceneOffset);
}

function updateGroundAndView() {
  state.robotGroup.position.set(0, 0, 0);
  state.robotGroup.updateMatrixWorld(true);
  const bounds = new THREE.Box3().setFromObject(state.robotGroup);
  if (bounds.isEmpty()) return;
  state.sceneOffset.set(0, 0, -bounds.min.z);
  state.robotGroup.position.copy(state.sceneOffset);
  state.robotGroup.updateMatrixWorld(true);
  frameRobot();
}

function frameRobot() {
  if (!state.robotGroup) return;
  const bounds = new THREE.Box3().setFromObject(state.robotGroup);
  state.multiOverlays.forEach((overlay) => bounds.expandByObject(overlay.root));
  const center = bounds.getCenter(new THREE.Vector3());
  const size = bounds.getSize(new THREE.Vector3());
  const radius = Math.max(size.length(), 1.0);
  controls.target.copy(center);
  camera.position.copy(center).add(
    new THREE.Vector3(1.0, -1.7, 0.65).normalize().multiplyScalar(radius * 1.05),
  );
  camera.near = Math.max(0.01, radius / 100);
  camera.far = Math.max(20, radius * 10);
  camera.updateProjectionMatrix();
  controls.update();
}

function applyFrame(index) {
  if (!state.frames.length) return;
  state.frameIndex = Math.max(0, Math.min(Number(index), state.frames.length - 1));
  slider.value = String(state.frameIndex);
  const fraction = state.frameIndex / Math.max(1, state.frames.length - 1);
  if (state.multiMode) {
    const firstJoints = jointsAtComparisonProgress(
      state.multiOverlays[0],
      fraction,
    );
    setRobotJoints(firstJoints);
    for (const overlay of state.multiOverlays) {
      setInstanceJoints(
        overlay,
        jointsAtComparisonProgress(overlay, fraction),
      );
    }
    frameLabel.textContent = `对比帧 ${state.frameIndex + 1} / ${state.frames.length} · TCP路径 ${(fraction * 100).toFixed(0)}%`;
    clearCollisionGroup();
    return;
  }
  setRobotJoints(state.frames[state.frameIndex]);
  const isSample = state.sampleFractions.some(
    (sample) => Math.abs(Number(sample) - fraction) <= 0.5 / Math.max(1, state.frames.length - 1),
  );
  const check = collisionCheckAt(state.frameIndex);
  const collisionLabel = check?.status === "collision"
    ? ` · 碰撞 ${Number(check.min_distance_mm).toFixed(1)}mm`
    : check?.status === "near"
      ? ` · 接近 ${Number(check.min_distance_mm).toFixed(1)}mm`
      : "";
  frameLabel.textContent = `${state.frameIndex + 1} / ${state.frames.length}${isSample ? " · 采样点" : ""}${collisionLabel}`;
  updateCollisionOverlay();
}

async function loadMultiple(items) {
  state.playing = false;
  playButton.textContent = "▶ 播放";
  placeholder.style.display = "grid";
  placeholder.textContent = `正在加载${items.length}条轨迹的多彩手臂…`;
  try {
    const previews = await Promise.all(items.map(async (item) => {
      const response = await fetch(item.previewUrl, { cache: "no-store" });
      const payload = await response.json();
      if (!response.ok || payload.ok === false) {
        throw new Error(`${item.name}：${payload.error || `HTTP ${response.status}`}`);
      }
      return { ...item, plan: payload.plan };
    }));
    const first = previews[0].plan;
    if (previews.some((item) => (
      item.plan.robot !== first.robot || item.plan.chain_id !== first.chain_id
    ))) {
      throw new Error("只能叠加同一机器人、同一手臂的轨迹");
    }
    state.chainId = first.chain_id || "right_arm";
    await loadRobot(first.robot);
    clearMultiOverlays();
    state.toolGroup?.removeFromParent();
    state.toolGroup = null;
    applyMultiContextAppearance(state.chainId);
    for (const item of previews) {
      const overlay = await buildArmOverlay(state.chainId, item.color);
      overlay.frames = item.plan.frames || [];
      overlay.comparisonProgress = item.plan.comparison_progress || [];
      overlay.name = item.name;
      overlay.root.position.copy(state.sceneOffset);
      setInstanceJoints(overlay, overlay.frames[0] || {});
      attachComparisonTool(
        overlay,
        item.plan.tool_visualization || {},
        item.color,
        state.chainId,
      );
      scene.add(overlay.root);
      state.multiOverlays.push(overlay);
    }
    state.multiMode = true;
    state.frames = Array.from({ length: 101 }, () => ({}));
    state.sampleFractions = [];
    state.collision = null;
    state.blocked = false;
    state.duration = Math.max(
      ...previews.map((item) => Number(item.plan.duration_s || 4)),
    );
    showCollisions.checked = false;
    showCollisions.disabled = true;
    slider.min = "0";
    slider.max = String(Math.max(0, state.frames.length - 1));
    slider.disabled = state.frames.length < 2;
    playButton.disabled = state.frames.length < 2;
    resetButton.disabled = false;
    applyFrame(0);
    frameRobot();
    placeholder.style.display = "none";
    window.dispatchEvent(new CustomEvent("gravity:preview-multiple-loaded", {
      detail: { count: state.multiOverlays.length, plans: previews.map((item) => item.plan) },
    }));
  } catch (error) {
    placeholder.style.display = "grid";
    placeholder.textContent = `多轨迹叠加回放失败：${error.message}`;
    state.frames = [];
    clearMultiOverlays();
    playButton.disabled = true;
    slider.disabled = true;
  }
}

async function loadPlan(planId, options = {}) {
  state.playing = false;
  playButton.textContent = "▶ 播放";
  placeholder.style.display = "grid";
  placeholder.textContent = options.loadingText || "正在加载机器人规划轨迹…";
  try {
    const previewUrl = options.previewUrl
      || `/api/gravity/plans/${encodeURIComponent(planId)}/preview`;
    const response = await fetch(
      previewUrl,
      { cache: "no-store" },
    );
    const payload = await response.json();
    if (!response.ok || payload.ok === false) {
      throw new Error(payload.error || `HTTP ${response.status}`);
    }
    const plan = payload.plan;
    state.chainId = plan.chain_id || "right_arm";
    state.toolVisual = plan.tool_visualization || {};
    state.collision = plan.collision || null;
    state.blocked = Boolean(plan.blocked);
    await loadRobot(plan.robot);
    restoreSingleAppearance();
    attachToolVisualization();
    state.frames = plan.frames || [];
    state.sampleFractions = plan.sample_fractions || [];
    state.duration = Number(plan.duration_s || 4);
    slider.min = "0";
    slider.max = String(Math.max(0, state.frames.length - 1));
    slider.disabled = state.frames.length < 2;
    playButton.disabled = state.frames.length < 2;
    resetButton.disabled = false;
    if (state.blocked) showCollisions.checked = true;
    const checks = state.collision?.checks || [];
    const worst = checks.reduce(
      (selected, check) => (
        selected === null
        || Number(check.min_distance_m) < Number(selected.min_distance_m)
          ? check
          : selected
      ),
      null,
    );
    applyFrame(state.blocked && worst ? Number(worst.index || 0) : 0);
    placeholder.style.display = "none";
    window.dispatchEvent(new CustomEvent("gravity:preview-loaded", { detail: { plan } }));
  } catch (error) {
    placeholder.style.display = "grid";
    placeholder.textContent = `${options.errorLabel || "机器人规划预览失败"}：${error.message}`;
    state.frames = [];
    state.collision = null;
    clearCollisionGroup();
    playButton.disabled = true;
    slider.disabled = true;
  }
}

window.addEventListener("gravity:preview-plan", (event) => {
  loadPlan(event.detail.planId, event.detail);
});
window.addEventListener("gravity:preview-multiple", (event) => {
  loadMultiple(event.detail.items || []);
});
playButton.addEventListener("click", () => {
  if (!state.frames.length) return;
  if (state.playing) {
    state.playing = false;
    playButton.textContent = "▶ 继续";
    return;
  }
  if (state.frameIndex >= state.frames.length - 1) applyFrame(0);
  state.playing = true;
  state.startedAt = performance.now();
  state.startedFrame = state.frameIndex;
  playButton.textContent = "Ⅱ 暂停";
});
slider.addEventListener("input", () => {
  state.playing = false;
  playButton.textContent = "▶ 播放";
  applyFrame(Number(slider.value));
});
showCollisions.addEventListener("change", updateCollisionOverlay);
resetButton.addEventListener("click", frameRobot);
