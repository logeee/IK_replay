import * as THREE from "/web/vendor/three.module.js";
import { OrbitControls } from "/web/vendor/OrbitControls.js";
import { STLLoader } from "/web/vendor/STLLoader.js";
import { TransformControls } from "/web/vendor/TransformControls.js";

const PANEL_COLORS = {
  left: 0xd97706,
  right: 0x5c4c9f,
};

const dom = {
  viewport: document.getElementById("viewport"),
  stateLabel: document.getElementById("stateLabel"),
  frameLabel: document.getElementById("frameLabel"),
  robotName: document.getElementById("robotName"),
  robotSelect: document.getElementById("robotSelect"),
  resetViewButton: document.getElementById("resetViewButton"),
  targetMoveButton: document.getElementById("targetMoveButton"),
  targetRotateButton: document.getElementById("targetRotateButton"),
  leftPanel: document.getElementById("leftArmPanel"),
  rightPanel: document.getElementById("rightArmPanel"),
};

const state = {
  activeRobot: null,
  metadata: null,
  meshBaseUrl: "/assets/",
  robotGroup: null,
  helperRoot: new THREE.Group(),
  linkGroups: new Map(),
  jointNodes: new Map(),
  robotMaterials: [],
  robotJointState: {},
  panels: {},
  sceneOffset: new THREE.Vector3(),
  activeTargetPanelId: null,
};

const scene = new THREE.Scene();
scene.background = new THREE.Color(0x0c1014);
scene.add(state.helperRoot);

const camera = new THREE.PerspectiveCamera(48, 1, 0.01, 80);
camera.up.set(0, 0, 1);
camera.position.set(1.0, -2.2, 1.4);

const renderer = new THREE.WebGLRenderer({ antialias: true });
renderer.setPixelRatio(window.devicePixelRatio || 1);
renderer.shadowMap.enabled = true;
dom.viewport.appendChild(renderer.domElement);

const controls = new OrbitControls(camera, renderer.domElement);
controls.target.set(0.1, 0.0, 0.8);
controls.enableDamping = false;
controls.enablePan = true;
controls.screenSpacePanning = true;
controls.panSpeed = 1.0;
controls.rotateSpeed = 0.85;
controls.zoomSpeed = 1.15;
controls.mouseButtons = {
  LEFT: THREE.MOUSE.ROTATE,
  MIDDLE: THREE.MOUSE.DOLLY,
  RIGHT: THREE.MOUSE.PAN,
};
controls.addEventListener("change", () => publishRenderState("视角变化"));
controls.update();

const targetControls = new TransformControls(camera, renderer.domElement);
targetControls.setMode("translate");
targetControls.setSpace("world");
targetControls.setSize(0.82);
targetControls.visible = false;
targetControls.addEventListener("dragging-changed", (event) => {
  controls.enabled = !event.value;
});
targetControls.addEventListener("objectChange", () => syncActiveTargetFromGizmo());
targetControls.addEventListener("change", () => publishRenderState("目标控件变化"));
scene.add(targetControls);

const targetRaycaster = new THREE.Raycaster();
const targetPointer = new THREE.Vector2();

scene.add(new THREE.HemisphereLight(0xffffff, 0x7d8a9a, 2.2));

const keyLight = new THREE.DirectionalLight(0xffffff, 2.3);
keyLight.position.set(1.6, -1.3, 2.8);
keyLight.castShadow = true;
scene.add(keyLight);

const fillLight = new THREE.DirectionalLight(0xcee6ff, 0.9);
fillLight.position.set(-1.6, 1.4, 1.6);
scene.add(fillLight);

const grid = new THREE.GridHelper(2.6, 26, 0x3b4a54, 0x202a31);
grid.rotation.x = Math.PI / 2;
scene.add(grid);
scene.add(new THREE.AxesHelper(0.24));

init();
requestAnimationFrame(animate);

async function init() {
  resize();
  window.addEventListener("resize", resize);
  new ResizeObserver(resize).observe(dom.viewport);
  dom.robotSelect.addEventListener("change", () => loadRobotData(dom.robotSelect.value));
  dom.resetViewButton.addEventListener("click", () => resetView("手动视角复位"));
  dom.targetMoveButton.addEventListener("click", () => setTargetControlMode("translate"));
  dom.targetRotateButton.addEventListener("click", () => setTargetControlMode("rotate"));
  renderer.domElement.addEventListener("pointerdown", selectTargetFromPointer);
  await loadRobotData();
}

async function loadRobotData(robotId = null) {
  setState("正在加载机器人", "warn");
  try {
    const url = robotId ? `/api/robot/metadata?robot=${encodeURIComponent(robotId)}` : "/api/robot/metadata";
    const metadata = await fetchJson(url);
    state.activeRobot = metadata.active_robot;
    state.metadata = metadata;
    state.meshBaseUrl = metadata.robot.mesh_base_url;
    state.robotJointState = {};
    state.panels = {};
    state.sceneOffset.set(0, 0, 0);
    state.activeTargetPanelId = null;
    targetControls.detach();
    targetControls.visible = false;
    state.helperRoot.clear();
    dom.robotName.textContent = metadata.robot.display_name;
    renderRobotSelector(metadata);
    renderPanels(metadata);
    await loadRobot(metadata.robot.urdf_url);
    for (const panel of Object.values(state.panels)) {
      setJointInputs(panel, panel.chain.default_current_joints);
      Object.assign(state.robotJointState, panel.chain.default_current_joints);
      writePose(panel, "tcp", panel.chain.default_tcp_offset);
    }
    setRobotJoints(state.robotJointState, false);
    updateGroundFrame();
    for (const panel of Object.values(state.panels)) {
      ensureTargetHand(panel);
      if (panel.dom.switchHeight) {
        // This field is explicitly a ground-frame height. Convert it only when
        // building the pelvis-frame API request, using the loaded model offset.
        panel.dom.switchHeight.value = "1.70";
      }
      writePose(panel, "target", poseToScene(panel.chain.default_target_pose));
      updateTargetMarker(panel);
    }
    selectTargetPanel(Object.values(state.panels)[0]);
    resetView("URDF 加载完成");
    for (const panel of Object.values(state.panels)) {
      await updateFk(panel, "初始 FK");
      await planTrajectory(panel, { initial: true });
    }
    resetView("初始轨迹完成");
    setState("就绪", "success");
  } catch (error) {
    console.error(error);
    setState("加载失败", "error");
  }
}

function renderRobotSelector(metadata) {
  dom.robotSelect.replaceChildren();
  for (const robot of metadata.available_robots) {
    const option = document.createElement("option");
    option.value = robot.name;
    option.textContent = `${robot.display_name} (${robot.name})`;
    option.selected = robot.name === metadata.active_robot;
    dom.robotSelect.append(option);
  }
}

function renderPanels(metadata) {
  const bySide = {};
  for (const [chainId, chain] of Object.entries(metadata.chains)) {
    bySide[chain.panel_side] = { chainId, chain };
  }
  renderArmPanel(dom.leftPanel, "left", bySide.left);
  renderArmPanel(dom.rightPanel, "right", bySide.right);
}

function renderArmPanel(panelElement, side, entry) {
  panelElement.replaceChildren();
  if (!entry) {
    panelElement.textContent = "未配置手臂链";
    return;
  }

  const color = PANEL_COLORS[side];
  const { chainId, chain } = entry;
  const isH2SwitchDemo = state.activeRobot === "h2" && chainId === "right_arm";
  panelElement.className = `control-panel arm-panel ${side}`;
  panelElement.innerHTML = `
    <header class="panel-header ${side}">
      <h2>${chain.display_name}</h2>
      <span class="subtitle">${chain.subtitle}</span>
      <span class="subtitle">面向机器人时的屏幕${side === "left" ? "左" : "右"}侧工具栏</span>
    </header>

    <section class="panel-section">
      <div class="section-title">链路配置</div>
      <div class="info-grid">
        <span>Chain</span><strong>${chainId}</strong>
        <span>基座</span><strong>${chain.base_link}</strong>
        <span>末端</span><strong>${chain.end_link}</strong>
        <span>目标模型</span><strong>${chain.target_visual_link || chain.end_link}</strong>
      </div>
    </section>

    <section class="panel-section">
      <div class="section-row">
        <div class="section-title">当前关节</div>
        <div class="mini-actions">
          <button data-role="captureStart" type="button">取当前</button>
          <button data-role="reset" type="button">重置</button>
          <button data-role="random" type="button">随机</button>
        </div>
      </div>
      <div data-role="jointFields" class="joint-grid"></div>
    </section>

    <section class="panel-section">
      <div class="section-title">目标位姿（地面坐标）</div>
      <div class="row three">
        <label><span>X</span><input data-role="targetX" type="number" step="0.01" /></label>
        <label><span>Y</span><input data-role="targetY" type="number" step="0.01" /></label>
        <label><span>Z</span><input data-role="targetZ" type="number" step="0.01" /></label>
      </div>
      <div class="row three">
        <label><span>横滚</span><input data-role="targetRoll" type="number" step="0.05" /></label>
        <label><span>俯仰</span><input data-role="targetPitch" type="number" step="0.05" /></label>
        <label><span>偏航</span><input data-role="targetYaw" type="number" step="0.05" /></label>
      </div>
    </section>

    <section class="panel-section">
      <div class="section-title">TCP 偏移</div>
      <div class="row three">
        <label><span>X</span><input data-role="tcpX" type="number" step="0.01" /></label>
        <label><span>Y</span><input data-role="tcpY" type="number" step="0.01" /></label>
        <label><span>Z</span><input data-role="tcpZ" type="number" step="0.01" /></label>
      </div>
      <div class="row three">
        <label><span>横滚</span><input data-role="tcpRoll" type="number" step="0.05" /></label>
        <label><span>俯仰</span><input data-role="tcpPitch" type="number" step="0.05" /></label>
        <label><span>偏航</span><input data-role="tcpYaw" type="number" step="0.05" /></label>
      </div>
    </section>

    <section class="panel-section">
      <div class="section-title">IK</div>
      <div class="row two">
        <label><span>求解器</span><select data-role="solver"></select></label>
        <button data-role="solve" class="primary-button" type="button">求解 IK</button>
      </div>
      <div class="metric-grid">
        <div><span>状态</span><strong data-role="ikStatus">-</strong></div>
        <div><span>位置误差</span><strong data-role="ikError">-</strong></div>
        <div><span>姿态误差</span><strong data-role="ikRotation">-</strong></div>
        <div><span>迭代</span><strong data-role="ikIterations">-</strong></div>
      </div>
      <div data-role="ikMessage" class="message-line">-</div>
    </section>

    <section class="panel-section">
      <div class="section-title">碰撞</div>
      <div class="metric-grid">
        <div data-role="collisionCard"><span>状态</span><strong data-role="collisionStatus">-</strong></div>
        <div><span>最小距离</span><strong data-role="collisionDistance">-</strong></div>
        <div><span>最近对象</span><strong data-role="collisionPair">-</strong></div>
        <div><span>轨迹告警</span><strong data-role="collisionCounts">-</strong></div>
      </div>
      <div data-role="collisionMessage" class="message-line">-</div>
    </section>

    ${isH2SwitchDemo ? `
    <section class="panel-section switch-demo-section">
      <div class="section-title">H2 开关两关键动作 IK</div>
      <div class="row three">
        <label><span>X 前方距离（m）</span><input data-role="switchDistance" type="number" min="0.22" max="0.55" step="0.01" value="0.40" /></label>
        <label><span>Y 左侧偏移（m）</span><input data-role="switchLateral" type="number" min="-0.20" max="0.24" step="0.01" value="0.08" /></label>
        <label><span>Z 地面高度（m）</span><input data-role="switchHeight" type="number" min="0.5" max="2.4" step="0.01" value="1.70" /></label>
      </div>
      <div class="row three">
        <label><span>初始动作角</span><input data-role="startAngle" type="number" min="-80" max="80" step="5" value="-25" /></label>
        <label><span>最终动作角</span><input data-role="endAngle" type="number" min="-80" max="80" step="5" value="25" /></label>
        <label><span>中间轨迹</span><select data-role="keyframePath"><option value="arc">圆弧</option><option value="linear">直线</option></select></label>
      </div>
      <button data-role="switchPlan" class="switch-button" type="button">求解两个动作并规划</button>
      <div class="switch-export-row">
        <button data-role="exportTask" type="button" disabled>导出 IK 任务 JSON</button>
        <button data-role="exportJoints" type="button" disabled>导出参考关节 CSV</button>
      </div>
      <div class="switch-status">
        <span data-role="switchStage">等待生成</span>
        <strong data-role="contactState">指尖：未接触</strong>
      </div>
      <div data-role="postureHint" class="message-line">默认自然 IK：位置 1.0、手腕 0.08、姿态 0.008、连续性 0.002。</div>
      <div class="message-line">坐标方向：X 向机器人前方，Y 向机器人左侧（右侧为负），Z 从地面向上。</div>
      <div class="message-line"><span class="keyframe-dot start"></span>绿色：初始动作　<span class="keyframe-dot end"></span>红色：最终动作</div>
    </section>` : ""}

    <section class="panel-section">
      <div class="section-title">轨迹</div>
      <div class="row three">
        <label><span>规划器</span><select data-role="planner"></select></label>
        <label><span>时长</span><input data-role="duration" type="number" min="0.1" step="0.1" /></label>
        <label><span>点数</span><input data-role="steps" type="number" min="2" max="1000" /></label>
      </div>
      <div class="transport">
        <button data-role="plan" class="primary-button" type="button">规划轨迹</button>
        <button data-role="replay" type="button">回放</button>
        <button data-role="pause" type="button">暂停</button>
        <button data-role="step" type="button">单步</button>
      </div>
      <label class="speed-row">
        <span>速度</span>
        <input data-role="speed" type="range" min="0.25" max="3" step="0.25" value="1" />
        <strong data-role="speedLabel">1.00x</strong>
      </label>
    </section>

    <section class="panel-section debug-section">
      <div class="section-title">调试信息</div>
      <pre data-role="debug" class="debug-console">{}</pre>
    </section>
  `;

  const panel = {
    side,
    color,
    chainId,
    chain,
    element: panelElement,
    currentIk: null,
    currentCollision: null,
    frames: [],
    frameIndex: 0,
    playing: false,
    lastFrameTime: 0,
    targetGroup: createPoseMarker(color, 0.026),
    targetHandGroup: null,
    targetHandLink: null,
    targetHandMaterial: null,
    tcpGroup: createPoseMarker(color, 0.02),
    trajectoryGroup: new THREE.Group(),
    skeletonGroup: new THREE.Group(),
    switchGroup: new THREE.Group(),
    switchHandle: null,
    switchTurnAngle: 0,
    fingertipMarker: null,
    lastSwitchData: null,
    lastSwitchRequest: null,
    fkTimer: 0,
    dom: {
      jointFields: panelElement.querySelector('[data-role="jointFields"]'),
      captureStart: panelElement.querySelector('[data-role="captureStart"]'),
      reset: panelElement.querySelector('[data-role="reset"]'),
      random: panelElement.querySelector('[data-role="random"]'),
      target: [
        panelElement.querySelector('[data-role="targetX"]'),
        panelElement.querySelector('[data-role="targetY"]'),
        panelElement.querySelector('[data-role="targetZ"]'),
        panelElement.querySelector('[data-role="targetRoll"]'),
        panelElement.querySelector('[data-role="targetPitch"]'),
        panelElement.querySelector('[data-role="targetYaw"]'),
      ],
      tcp: [
        panelElement.querySelector('[data-role="tcpX"]'),
        panelElement.querySelector('[data-role="tcpY"]'),
        panelElement.querySelector('[data-role="tcpZ"]'),
        panelElement.querySelector('[data-role="tcpRoll"]'),
        panelElement.querySelector('[data-role="tcpPitch"]'),
        panelElement.querySelector('[data-role="tcpYaw"]'),
      ],
      solver: panelElement.querySelector('[data-role="solver"]'),
      solve: panelElement.querySelector('[data-role="solve"]'),
      ikStatus: panelElement.querySelector('[data-role="ikStatus"]'),
      ikError: panelElement.querySelector('[data-role="ikError"]'),
      ikRotation: panelElement.querySelector('[data-role="ikRotation"]'),
      ikIterations: panelElement.querySelector('[data-role="ikIterations"]'),
      ikMessage: panelElement.querySelector('[data-role="ikMessage"]'),
      collisionCard: panelElement.querySelector('[data-role="collisionCard"]'),
      collisionStatus: panelElement.querySelector('[data-role="collisionStatus"]'),
      collisionDistance: panelElement.querySelector('[data-role="collisionDistance"]'),
      collisionPair: panelElement.querySelector('[data-role="collisionPair"]'),
      collisionCounts: panelElement.querySelector('[data-role="collisionCounts"]'),
      collisionMessage: panelElement.querySelector('[data-role="collisionMessage"]'),
      switchDistance: panelElement.querySelector('[data-role="switchDistance"]'),
      switchLateral: panelElement.querySelector('[data-role="switchLateral"]'),
      switchHeight: panelElement.querySelector('[data-role="switchHeight"]'),
      startAngle: panelElement.querySelector('[data-role="startAngle"]'),
      endAngle: panelElement.querySelector('[data-role="endAngle"]'),
      keyframePath: panelElement.querySelector('[data-role="keyframePath"]'),
      switchPlan: panelElement.querySelector('[data-role="switchPlan"]'),
      switchStage: panelElement.querySelector('[data-role="switchStage"]'),
      contactState: panelElement.querySelector('[data-role="contactState"]'),
      postureHint: panelElement.querySelector('[data-role="postureHint"]'),
      exportTask: panelElement.querySelector('[data-role="exportTask"]'),
      exportJoints: panelElement.querySelector('[data-role="exportJoints"]'),
      planner: panelElement.querySelector('[data-role="planner"]'),
      duration: panelElement.querySelector('[data-role="duration"]'),
      steps: panelElement.querySelector('[data-role="steps"]'),
      plan: panelElement.querySelector('[data-role="plan"]'),
      replay: panelElement.querySelector('[data-role="replay"]'),
      pause: panelElement.querySelector('[data-role="pause"]'),
      step: panelElement.querySelector('[data-role="step"]'),
      speed: panelElement.querySelector('[data-role="speed"]'),
      speedLabel: panelElement.querySelector('[data-role="speedLabel"]'),
      debug: panelElement.querySelector('[data-role="debug"]'),
    },
  };
  panel.targetGroup.userData.chainId = chainId;
  panel.targetGroup.traverse((object) => {
    object.userData.chainId = chainId;
    object.userData.targetMarker = true;
  });

  state.helperRoot.add(panel.targetGroup, panel.tcpGroup, panel.trajectoryGroup, panel.skeletonGroup, panel.switchGroup);
  state.panels[chainId] = panel;
  renderSelectors(panel);
  renderJointInputs(panel);
  bindPanelEvents(panel);
}

function renderSelectors(panel) {
  fillSelect(panel.dom.solver, state.metadata.available_ik_solvers, state.metadata.active_ik_solver);
  fillSelect(panel.dom.planner, state.metadata.available_planners, state.metadata.active_planner);
  panel.dom.duration.value = formatNumber(state.metadata.trajectory_defaults.duration);
  panel.dom.steps.value = String(state.metadata.trajectory_defaults.steps);
}

function fillSelect(select, values, active) {
  select.replaceChildren();
  for (const value of values) {
    const option = document.createElement("option");
    option.value = value;
    option.textContent = optionLabel(value);
    option.selected = value === active;
    select.append(option);
  }
}

function renderJointInputs(panel) {
  panel.dom.jointFields.replaceChildren();
  for (const limit of panel.chain.joint_limits) {
    const value = Number(panel.chain.default_current_joints[limit.name] ?? 0);
    const row = document.createElement("div");
    row.className = "joint-row";

    const label = document.createElement("div");
    label.className = "joint-name";
    label.title = limit.name;
    label.textContent = shortJointName(limit.name);

    const slider = document.createElement("input");
    slider.className = "joint-slider";
    slider.type = "range";
    slider.min = String(limit.lower);
    slider.max = String(limit.upper);
    slider.step = "0.01";
    slider.value = formatNumber(value);

    const input = document.createElement("input");
    input.className = "joint-value";
    input.type = "number";
    input.min = String(limit.lower);
    input.max = String(limit.upper);
    input.step = "0.01";
    input.value = formatNumber(value);
    input.setAttribute("data-joint-name", limit.name);
    input.setAttribute("data-joint-value", "true");

    slider.addEventListener("input", () => {
      input.value = formatNumber(slider.value);
      onJointEdit(panel);
    });
    input.addEventListener("input", () => {
      const next = clamp(Number(input.value || 0), Number(slider.min), Number(slider.max));
      input.value = formatNumber(next);
      slider.value = formatNumber(next);
      onJointEdit(panel);
    });

    row.append(label, slider, input);
    panel.dom.jointFields.append(row);
  }
}

function bindPanelEvents(panel) {
  panel.dom.captureStart.addEventListener("click", () => {
    pause(panel);
    syncJointInputsFromRobot(panel);
    panel.frames = [];
    panel.trajectoryGroup.clear();
    panel.currentIk = null;
    panel.currentCollision = null;
    updateCollisionMetrics(panel, null);
    scheduleFk(panel);
    updateFrameLabel();
    setState(`${panel.chain.display_name} 起点已设为当前姿态`, "success");
  });
  panel.dom.reset.addEventListener("click", () => {
    setJointInputs(panel, panel.chain.default_current_joints);
    onJointEdit(panel);
  });
  panel.dom.random.addEventListener("click", () => {
    const values = {};
    for (const limit of panel.chain.joint_limits) {
      const lower = Math.max(Number(limit.lower), -1.6);
      const upper = Math.min(Number(limit.upper), 1.6);
      values[limit.name] = lower + Math.random() * (upper - lower);
    }
    setJointInputs(panel, values);
    onJointEdit(panel);
  });
  panel.dom.solve.addEventListener("click", () => solveIk(panel));
  panel.dom.plan.addEventListener("click", () => planTrajectory(panel));
  panel.dom.switchPlan?.addEventListener("click", () => planH2SwitchOperation(panel));
  panel.dom.exportTask?.addEventListener("click", () => exportSwitchTask(panel));
  panel.dom.exportJoints?.addEventListener("click", () => exportSwitchJoints(panel));
  panel.dom.replay.addEventListener("click", () => replay(panel));
  panel.dom.pause.addEventListener("click", () => pause(panel));
  panel.dom.step.addEventListener("click", () => stepFrame(panel));
  panel.dom.speed.addEventListener("input", () => {
    panel.dom.speedLabel.textContent = `${Number(panel.dom.speed.value).toFixed(2)}x`;
  });
  for (const input of [
    panel.dom.switchDistance,
    panel.dom.switchLateral,
    panel.dom.switchHeight,
    panel.dom.startAngle,
    panel.dom.endAngle,
    panel.dom.keyframePath,
  ].filter(Boolean)) {
    input.addEventListener("change", () => invalidateSwitchExport(panel));
  }
  for (const input of panel.dom.target) {
    input.addEventListener("input", () => {
      panel.currentIk = null;
      updateTargetMarker(panel);
    });
  }
  for (const input of panel.dom.tcp) {
    input.addEventListener("input", () => {
      panel.currentIk = null;
      updateTargetHandPose(panel);
      scheduleFk(panel);
    });
  }
}

function onJointEdit(panel) {
  pause(panel);
  panel.currentIk = null;
  panel.currentCollision = null;
  panel.frames = [];
  panel.trajectoryGroup.clear();
  updateCollisionMetrics(panel, null);
  invalidateSwitchExport(panel);
  applyJointInputsToRobot(panel);
  updateFrameLabel();
}

function setJointInputs(panel, namedValues) {
  for (const input of panel.dom.jointFields.querySelectorAll("[data-joint-value]")) {
    const name = input.getAttribute("data-joint-name");
    const value = Number(namedValues[name] ?? 0);
    input.value = formatNumber(value);
    const slider = input.parentElement.querySelector(".joint-slider");
    slider.value = formatNumber(value);
  }
}

function syncJointInputsFromRobot(panel) {
  const values = {};
  for (const jointName of panel.chain.joint_names) {
    values[jointName] = Number(state.robotJointState[jointName] ?? panel.chain.default_current_joints[jointName] ?? 0);
  }
  setJointInputs(panel, values);
  return values;
}

function readJointInputs(panel) {
  const values = {};
  for (const input of panel.dom.jointFields.querySelectorAll("[data-joint-value]")) {
    values[input.getAttribute("data-joint-name")] = Number(input.value || 0);
  }
  return values;
}

function readPose(panel, role) {
  const inputs = role === "target" ? panel.dom.target : panel.dom.tcp;
  return {
    xyz: inputs.slice(0, 3).map((input) => Number(input.value || 0)),
    rpy: inputs.slice(3).map((input) => Number(input.value || 0)),
  };
}

function writePose(panel, role, pose) {
  const inputs = role === "target" ? panel.dom.target : panel.dom.tcp;
  [...pose.xyz, ...pose.rpy].forEach((value, index) => {
    inputs[index].value = formatNumber(value);
  });
}

function applyJointInputsToRobot(panel, fetchFk = true) {
  Object.assign(state.robotJointState, readJointInputs(panel));
  setRobotJoints(state.robotJointState, false);
  if (fetchFk) {
    scheduleFk(panel);
  }
}

function scheduleFk(panel) {
  window.clearTimeout(panel.fkTimer);
  panel.fkTimer = window.setTimeout(() => updateFk(panel, "正向运动学"), 120);
}

async function updateFk(panel, stage) {
  const joints = readJointInputs(panel);
  const tcpOffset = readPose(panel, "tcp");
  const payload = {
    robot: state.activeRobot,
    chain_id: panel.chainId,
    joints,
    tcp_offset: tcpOffset,
  };
  try {
    const data = await fetchJson("/api/fk", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    setPoseGroup(panel.tcpGroup, poseToScene(data.tcp_pose));
    updateSkeleton(panel, data.link_poses);
    const collision = await checkCollision(panel, joints, tcpOffset);
    writeDebug(panel, stage, summarizeFkRequest(payload), { ...summarizeFk(data), 碰撞: summarizeCollision(collision) });
    publishRenderState("FK 更新");
  } catch (error) {
    console.error(error);
    setState("FK 错误", "error");
    writeDebug(panel, stage, summarizeFkRequest(payload), { 错误: error.message });
  }
}

async function solveIk(panel, options = {}) {
  panel.dom.solve.disabled = true;
  setState(`正在求解 ${panel.chain.display_name}`, "warn");
  const payload = {
    robot: state.activeRobot,
    chain_id: panel.chainId,
    current_joints: readJointInputs(panel),
    target_pose: poseToRobot(readPose(panel, "target")),
    tcp_offset: readPose(panel, "tcp"),
    solver: panel.dom.solver.value,
    solver_options: {},
  };
  try {
    const data = await fetchJson("/api/ik/solve", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    panel.currentIk = data;
    updateIkMetrics(panel, data);
    setPoseGroup(panel.tcpGroup, poseToScene(data.tcp_pose));
    setState(data.success ? `${panel.chain.display_name} IK 已求解` : `${panel.chain.display_name} IK 未到达目标`, data.success ? "success" : "warn");
    if (!options.quiet) {
      writeDebug(panel, "IK 求解", summarizeIkRequest(payload), summarizeIk(data));
    }
    return data;
  } catch (error) {
    console.error(error);
    panel.currentIk = null;
    updateIkMetrics(panel, null, error.message);
    setState("IK 错误", "error");
    writeDebug(panel, "IK 求解", summarizeIkRequest(payload), { 错误: error.message });
    throw error;
  } finally {
    panel.dom.solve.disabled = false;
  }
}

async function planTrajectory(panel, options = {}) {
  panel.dom.plan.disabled = true;
  pause(panel);
  setState(`正在规划 ${panel.chain.display_name}`, "warn");
  try {
    const ik = await solveIk(panel, { quiet: true });
    const payload = {
      robot: state.activeRobot,
      chain_id: panel.chainId,
      current_joints: readJointInputs(panel),
      target_joints: ik.target_joints,
      tcp_offset: readPose(panel, "tcp"),
      duration: Number(panel.dom.duration.value || 4),
      steps: Number(panel.dom.steps.value || 80),
      planner_type: panel.dom.planner.value,
    };
    const data = await fetchJson("/api/trajectory/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    panel.frames = data.waypoints;
    panel.frameIndex = 0;
    panel.currentCollision = data.collision;
    updateCollisionMetrics(panel, data.collision);
    updateTrajectoryLine(panel);
    applyFrame(panel, 0);
    const collisionKind = data.collision?.status;
    const hasCollisionAlert = collisionKind === "collision" || collisionKind === "near";
    const stateText = ik.success ? `${panel.chain.display_name} 轨迹已生成` : `${panel.chain.display_name} 已规划到最近解`;
    setState(hasCollisionAlert ? `${stateText}，碰撞状态：${data.collision.status_label}` : stateText, ik.success && !hasCollisionAlert ? "success" : "warn");
    if (!ik.success) {
      const errorMm = Number(ik.error_mm).toFixed(2);
      const rotationDeg = radToDeg(ik.error_rotation).toFixed(2);
      setIkMessage(panel, `目标点未到达，当前轨迹只回放最近解：位置误差 ${errorMm} mm，姿态误差 ${rotationDeg} 度。`, "warn");
    }
    writeDebug(panel, options.initial ? "初始规划" : "轨迹规划", summarizeTrajectoryRequest(payload), summarizeTrajectory(data, ik));
  } catch (error) {
    console.error(error);
    setState("规划错误", "error");
  } finally {
    panel.dom.plan.disabled = false;
  }
}

function invalidateSwitchExport(panel) {
  panel.lastSwitchData = null;
  panel.lastSwitchRequest = null;
  if (panel.dom.exportTask) panel.dom.exportTask.disabled = true;
  if (panel.dom.exportJoints) panel.dom.exportJoints.disabled = true;
}

async function planH2SwitchOperation(panel) {
  if (state.activeRobot !== "h2" || panel.chainId !== "right_arm") {
    return;
  }
  pause(panel);
  panel.dom.switchPlan.disabled = true;
  panel.dom.switchStage.textContent = "正在求解初始与最终动作";
  setState("正在规划 H2 两关键动作", "warn");
  const payload = {
    robot: "h2",
    chain_id: "right_arm",
    current_joints: readJointInputs(panel),
    duration: Math.max(2, Number(panel.dom.duration.value || 8)),
    steps: Math.max(70, Number(panel.dom.steps.value || 120)),
    turn_angle_deg: Number(panel.dom.endAngle.value || 25) - Number(panel.dom.startAngle.value || -25),
    approach_distance: 0.08,
    switch_distance_m: Number(panel.dom.switchDistance.value || 0.40),
    switch_lateral_m: Number(panel.dom.switchLateral.value || 0.08),
    switch_height_m: xyzToRobot([0, 0, Number(panel.dom.switchHeight.value || 0)])[2],
    lever_length: 0.075,
    fingertip_length: 0.15,
    use_current_posture: false,
    use_natural_posture: true,
    posture_weight: 0.008,
    orientation_weight: 0.08,
    start_angle_deg: Number(panel.dom.startAngle.value || -25),
    end_angle_deg: Number(panel.dom.endAngle.value || 25),
    path_type: panel.dom.keyframePath.value,
  };
  try {
    const data = await fetchJson("/api/demo/h2_switch_operation", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    panel.frames = data.waypoints;
    panel.frameIndex = 0;
    panel.currentCollision = data.collision;
    panel.lastSwitchData = data;
    panel.lastSwitchRequest = payload;
    panel.switchTurnAngle = THREE.MathUtils.degToRad(data.switch.turn_angle_deg);
    panel.dom.switchDistance.value = formatNumber(data.switch.pivot_xyz[0]);
    panel.dom.switchLateral.value = formatNumber(data.switch.pivot_xyz[1]);
    panel.dom.switchHeight.value = formatNumber(xyzToScene(data.switch.pivot_xyz)[2]);
    panel.dom.exportTask.disabled = false;
    panel.dom.exportJoints.disabled = false;
    const elbowDeltaMm = Number(data.reference_posture.elbow_minus_wrist_z_m) * 1000;
    const postureText = elbowDeltaMm <= 0
      ? `参考解：肘部低于腕部 ${Math.abs(elbowDeltaMm).toFixed(1)} mm。`
      : `参考解：肘部仍高于腕部 ${elbowDeltaMm.toFixed(1)} mm，建议交由任务型 IK 继续优化。`;
    const relaxationText = data.reference_posture.constraints_relaxed
      ? ` 为满足指尖精度，姿态约束自动降至 ${(Number(data.reference_posture.constraint_scale) * 100).toFixed(0)}%。`
      : "";
    panel.dom.postureHint.textContent = postureText + relaxationText;
    writePose(panel, "tcp", data.switch.fingertip_tcp);
    updateTargetHandPose(panel);
    panel.dom.duration.value = formatNumber(data.duration);
    panel.dom.steps.value = String(data.waypoint_count);
    createSwitchVisual(panel, data.switch);
    updateCollisionMetrics(panel, data.collision);
    updateTrajectoryLine(panel);
    applyFrame(panel, 0);
    writeDebug(panel, "H2 指尖拨动竖直开关", payload, {
      轨迹点数: data.waypoint_count,
      旋钮: data.switch,
      碰撞: summarizeCollision(data.collision),
      阶段: [...new Set(data.waypoints.map((frame) => frame.stage))],
    });
    setState("H2 指尖拨动轨迹已生成，开始离线回放", "success");
    replay(panel);
  } catch (error) {
    panel.dom.switchStage.textContent = `生成失败：${error.message}`;
    setState("H2 旋钮轨迹生成失败", "error");
  } finally {
    panel.dom.switchPlan.disabled = false;
  }
}

function createSwitchVisual(panel, switchData) {
  panel.switchGroup.clear();
  const pivot = new THREE.Vector3().fromArray(xyzToScene(switchData.pivot_xyz));

  const cabinet = new THREE.Mesh(
    new THREE.BoxGeometry(0.018, 0.30, 0.34),
    new THREE.MeshStandardMaterial({ color: 0xb9c3c9, roughness: 0.78, metalness: 0.18 }),
  );
  cabinet.position.copy(pivot).add(new THREE.Vector3(0.045, 0, 0));

  const collar = new THREE.Mesh(
    new THREE.CylinderGeometry(0.035, 0.035, 0.035, 30),
    new THREE.MeshStandardMaterial({ color: 0x202a32, roughness: 0.55 }),
  );
  collar.position.copy(pivot).add(new THREE.Vector3(0.016, 0, 0));
  collar.rotation.z = Math.PI / 2;

  const handle = new THREE.Group();
  handle.position.copy(pivot).add(new THREE.Vector3(-0.012, 0, 0));
  const lever = new THREE.Mesh(
    new THREE.BoxGeometry(0.025, 0.025, switchData.lever_length),
    new THREE.MeshStandardMaterial({ color: 0x172027, roughness: 0.42 }),
  );
  lever.position.z = -switchData.lever_length / 2;
  const leverTip = new THREE.Mesh(
    new THREE.SphereGeometry(0.025, 20, 14),
    new THREE.MeshStandardMaterial({ color: 0x252f36, roughness: 0.38 }),
  );
  leverTip.position.z = -switchData.lever_length;
  handle.add(lever, leverTip);

  const startMarker = new THREE.Mesh(
    new THREE.SphereGeometry(0.018, 18, 12),
    new THREE.MeshStandardMaterial({ color: 0x16a34a, emissive: 0x0c4f25 }),
  );
  startMarker.position.fromArray(xyzToScene(switchData.start_tip_xyz));
  const endMarker = new THREE.Mesh(
    new THREE.SphereGeometry(0.018, 18, 12),
    new THREE.MeshStandardMaterial({ color: 0xdc2626, emissive: 0x641313 }),
  );
  endMarker.position.fromArray(xyzToScene(switchData.end_tip_xyz));

  const fingertipMarker = new THREE.Mesh(
    new THREE.SphereGeometry(0.014, 18, 12),
    new THREE.MeshStandardMaterial({ color: 0x22d3ee, emissive: 0x0d5260 }),
  );

  panel.switchHandle = handle;
  panel.fingertipMarker = fingertipMarker;
  panel.switchGroup.add(cabinet, collar, handle, startMarker, endMarker, fingertipMarker);
}

function updateSwitchVisual(panel, frame) {
  if (!panel.switchHandle) {
    return;
  }
  panel.switchHandle.rotation.x = THREE.MathUtils.degToRad(Number(frame.switch_angle_deg || 0));
  panel.fingertipMarker?.position.fromArray(xyzToScene(frame.tcp_pose.xyz));
  if (panel.dom.switchStage) {
    panel.dom.switchStage.textContent = frame.stage || "轨迹回放";
    const contactLabel = frame.contact === "pushing" ? "推拨中" : frame.contact === "touch" ? "已接触" : "未接触";
    panel.dom.contactState.textContent = `指尖：${contactLabel}`;
  }
}

function exportSwitchTask(panel) {
  const data = panel.lastSwitchData;
  if (!data) return;
  const jointNames = panel.chain.joint_names;
  const first = data.waypoints[0];
  const last = data.waypoints[data.waypoints.length - 1];
  const keyframe = (name, frame, targetPosition) => ({
    name,
    time_s: frame.t,
    switch_angle_deg: frame.switch_angle_deg,
    target_fingertip_position_pelvis_m: targetPosition,
    solved_fingertip_position_pelvis_m: frame.tcp_pose.xyz,
    position_error_m: Math.hypot(
      ...frame.tcp_pose.xyz.map((value, index) => value - targetPosition[index]),
    ),
    reference_fingertip_rpy_rad: frame.tcp_pose.rpy,
    reference_joint_positions_rad: Object.fromEntries(
      jointNames.map((name) => [name, frame.named_joints[name]]),
    ),
  });
  const task = {
    schema: "h2_switch_two_keyframe_ik_task/v2",
    generated_at: new Date().toISOString(),
    robot: "h2",
    chain: "right_arm",
    urdf: state.metadata.robot.urdf_path || state.metadata.robot.urdf_url,
    coordinate_frame: "pelvis",
    axis_convention: {
      x: "forward from robot",
      y: "left from robot (right is negative)",
      z: "up from pelvis origin",
    },
    ground_height_note: "The UI ground height is converted to pelvis-frame z before export.",
    units: {
      cartesian_position: "m",
      switch_angle: "deg",
      cartesian_orientation_rpy: "rad",
      joint_position: "rad",
      time: "s",
    },
    switch_geometry: data.switch,
    switch_position_input_ground_m: {
      x_forward: Number(panel.dom.switchDistance.value),
      y_left: Number(panel.dom.switchLateral.value),
      z_height: Number(panel.dom.switchHeight.value),
    },
    resolved_api_request_pelvis_frame: panel.lastSwitchRequest,
    trajectory_definition: {
      required_keyframes: ["initial", "final"],
      interpolation: data.switch.path_type,
      intermediate_waypoints_are_optional: true,
      switch_angle_positive_direction: "clockwise when viewed from the robot toward the panel",
    },
    keyframes: {
      initial: keyframe("initial", first, data.switch.start_tip_xyz),
      final: keyframe("final", last, data.switch.end_tip_xyz),
    },
    ik_requirements: {
      primary_task: "track target_fingertip_position_pelvis_m",
      position_weight: 1.0,
      position_tolerance_m: 0.002,
      posture_reference_mode: panel.lastSwitchRequest.use_natural_posture
        ? "natural_pointing_preset"
        : panel.lastSwitchRequest.use_current_posture
          ? "current_joint_state"
          : "built_in_safe_seed",
      posture_reference_joints_rad: data.reference_posture.posture_reference_joints,
      posture_weight: panel.lastSwitchRequest.posture_weight,
      soft_orientation_weight: panel.lastSwitchRequest.orientation_weight,
      adjacent_point_regularization_weight: 0.002,
      fingertip_axis_preference: "+X points toward the panel during approach and push",
      elbow_posture_preference: "right elbow below wrist when feasible; target margin 0.02-0.08 m",
      posture_priority: ["collision_free", "fingertip_position", "elbow_down", "pointing_orientation", "joint_continuity"],
      preferred_self_collision_clearance_m: 0.05,
      note: "The exported joint trajectory is a reference solution, not a required IK result.",
    },
    joint_names: jointNames,
    task_waypoints: data.waypoints.map((frame) => ({
      index: frame.index,
      time_s: frame.t,
      stage: frame.stage,
      contact: frame.contact,
      switch_angle_deg: frame.switch_angle_deg,
      fingertip_position_m: frame.tcp_pose.xyz,
      reference_fingertip_rpy_rad: frame.tcp_pose.rpy,
    })),
    reference_joint_trajectory: data.waypoints.map((frame) => ({
      index: frame.index,
      time_s: frame.t,
      positions_rad: jointNames.map((name) => frame.named_joints[name]),
    })),
    reference_posture: data.reference_posture,
    reference_collision_summary: summarizeCollision(data.collision),
  };
  downloadText("h2_switch_two_keyframe_ik_task.json", JSON.stringify(task, null, 2), "application/json");
}

function exportSwitchJoints(panel) {
  const data = panel.lastSwitchData;
  if (!data) return;
  const jointNames = panel.chain.joint_names;
  const header = [
    "index",
    "time_s",
    "stage",
    "contact",
    "waypoint_role",
    "path_type",
    "switch_angle_deg",
    "fingertip_x_pelvis_m",
    "fingertip_y_pelvis_m",
    "fingertip_z_pelvis_m",
    ...jointNames.map((name) => `${name}_rad`),
  ];
  const rows = data.waypoints.map((frame) => [
    frame.index,
    frame.t,
    frame.stage,
    frame.contact,
    frame.index === 0 ? "initial" : frame.index === data.waypoints.length - 1 ? "final" : "intermediate",
    data.switch.path_type,
    frame.switch_angle_deg,
    ...frame.tcp_pose.xyz,
    ...jointNames.map((name) => frame.named_joints[name]),
  ]);
  const csv = `\uFEFF${[header, ...rows].map((row) => row.map(csvCell).join(",")).join("\r\n")}`;
  downloadText("h2_switch_two_keyframe_reference_joints.csv", csv, "text/csv;charset=utf-8");
}

function csvCell(value) {
  const text = String(value ?? "");
  return /[",\r\n]/.test(text) ? `"${text.replaceAll('"', '""')}"` : text;
}

function downloadText(filename, content, mimeType) {
  const blob = new Blob([content], { type: mimeType });
  const url = URL.createObjectURL(blob);
  const anchor = document.createElement("a");
  anchor.href = url;
  anchor.download = filename;
  document.body.append(anchor);
  anchor.click();
  anchor.remove();
  window.setTimeout(() => URL.revokeObjectURL(url), 1000);
}

function replay(panel) {
  if (!panel.frames.length) {
    return;
  }
  panel.frameIndex = 0;
  applyFrame(panel, 0);
  panel.playing = true;
  panel.lastFrameTime = performance.now();
  setState(`正在回放 ${panel.chain.display_name}`, "success");
}

function pause(panel) {
  panel.playing = false;
}

function stepFrame(panel) {
  if (!panel.frames.length) {
    return;
  }
  pause(panel);
  applyFrame(panel, Math.min(panel.frameIndex + 1, panel.frames.length - 1));
}

function applyFrame(panel, index) {
  if (!panel.frames.length) {
    updateFrameLabel();
    return;
  }
  panel.frameIndex = Math.max(0, Math.min(index, panel.frames.length - 1));
  const frame = panel.frames[panel.frameIndex];
  Object.assign(state.robotJointState, frame.named_joints);
  setRobotJoints(state.robotJointState, false);
  setJointInputs(panel, frame.named_joints);
  setPoseGroup(panel.tcpGroup, poseToScene(frame.tcp_pose));
  updateSkeleton(panel, frame.link_poses);
  if (frame.collision) {
    updateCollisionMetrics(panel, frame.collision, panel.currentCollision);
  }
  updateSwitchVisual(panel, frame);
  updateFrameLabel();
  publishRenderState("轨迹帧更新");
}

function animate(now) {
  requestAnimationFrame(animate);
  controls.update();
  for (const panel of Object.values(state.panels)) {
    if (!panel.playing || panel.frames.length <= 1) {
      continue;
    }
    const duration = Number(panel.dom.duration.value || 4);
    const speed = Number(panel.dom.speed.value || 1);
    const frameMs = Math.max(12, (duration * 1000) / (panel.frames.length - 1) / speed);
    if (now - panel.lastFrameTime >= frameMs) {
      const next = panel.frameIndex + 1;
      if (next >= panel.frames.length) {
        panel.playing = false;
        setState(`${panel.chain.display_name} 回放完成`, "success");
      } else {
        applyFrame(panel, next);
      }
      panel.lastFrameTime = now;
    }
  }
  renderer.render(scene, camera);
}

function updateIkMetrics(panel, data, errorMessage = "") {
  if (!data) {
    panel.dom.ikStatus.textContent = "错误";
    panel.dom.ikError.textContent = "-";
    panel.dom.ikRotation.textContent = "-";
    panel.dom.ikIterations.textContent = "-";
    setIkMessage(panel, errorMessage || "IK 求解失败。", "error");
    return;
  }
  const errorMm = Number(data.error_mm);
  const rotationDeg = radToDeg(data.error_rotation);
  panel.dom.ikStatus.textContent = data.success ? "成功" : "未到达";
  panel.dom.ikError.textContent = `${errorMm.toFixed(2)} mm`;
  panel.dom.ikRotation.textContent = `${rotationDeg.toFixed(2)} 度`;
  panel.dom.ikIterations.textContent = String(data.iterations);
  if (data.success) {
    setIkMessage(panel, `IK 求解成功：位置误差 ${errorMm.toFixed(2)} mm，姿态误差 ${rotationDeg.toFixed(2)} 度，迭代 ${data.iterations} 次。`, "success");
  } else {
    setIkMessage(panel, `目标点当前不可达或未收敛：最近解的位置误差 ${errorMm.toFixed(2)} mm，姿态误差 ${rotationDeg.toFixed(2)} 度，迭代 ${data.iterations} 次。`, "warn");
  }
}

function updateTargetMarker(panel) {
  setPoseGroup(panel.targetGroup, readPose(panel, "target"));
  if (state.activeTargetPanelId === panel.chainId) {
    targetControls.attach(panel.targetGroup);
  }
  publishRenderState("目标更新");
}

function updateTrajectoryLine(panel) {
  panel.trajectoryGroup.clear();
  if (!panel.frames.length) {
    return;
  }
  const points = panel.frames.map((frame) => new THREE.Vector3().fromArray(xyzToScene(frame.tcp_pose.xyz)));
  panel.trajectoryGroup.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(points), new THREE.LineBasicMaterial({ color: panel.color })));
  const dotMaterials = new Map();
  for (let idx = 0; idx < panel.frames.length; idx += Math.max(1, Math.floor(panel.frames.length / 18))) {
    const status = panel.frames[idx].collision?.status || "safe";
    const color = colorForCollision(status, panel.color);
    if (!dotMaterials.has(color)) {
      dotMaterials.set(color, transparentMaterial(color, 0.44));
    }
    const dot = new THREE.Mesh(new THREE.SphereGeometry(0.01, 12, 8), dotMaterials.get(color));
    dot.position.fromArray(xyzToScene(panel.frames[idx].tcp_pose.xyz));
    panel.trajectoryGroup.add(dot);
  }
}

function updateSkeleton(panel, linkPoses) {
  panel.skeletonGroup.clear();
  if (!linkPoses) {
    return;
  }
  const points = panel.chain.display_links
    .map((name) => linkPoses[name]?.xyz)
    .filter(Boolean)
    .map((xyz) => new THREE.Vector3().fromArray(xyzToScene(xyz)));
  if (points.length < 2) {
    return;
  }
  panel.skeletonGroup.add(new THREE.Line(new THREE.BufferGeometry().setFromPoints(points), new THREE.LineBasicMaterial({ color: panel.color })));
}

async function loadRobot(urdfUrl) {
  const urdfText = await fetchText(urdfUrl);
  const xml = new DOMParser().parseFromString(urdfText, "application/xml");
  if (xml.querySelector("parsererror")) {
    throw new Error("URDF 解析失败");
  }

  if (state.robotGroup) {
    scene.remove(state.robotGroup);
  }
  state.jointNodes.clear();
  state.robotMaterials = [];
  state.linkGroups = new Map();

  const linkGroups = new Map();
  const jointsByParent = new Map();
  const childLinks = new Set();
  const stlLoader = new STLLoader();
  const meshTasks = [];

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
      meshTasks.push(() => stlLoader
        .loadAsync(meshUrl(filename))
        .then((geometry) => {
          geometry.computeVertexNormals();
          const mesh = new THREE.Mesh(geometry, material);
          mesh.scale.set(scale[0], scale[1], scale[2]);
          mesh.castShadow = true;
          mesh.receiveShadow = true;
          visualGroup.add(mesh);
        })
        .catch((error) => console.warn(`加载 mesh 失败: ${filename}`, error)));
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
    childLinks.add(child);
    if (!jointsByParent.has(parent)) {
      jointsByParent.set(parent, []);
    }
    jointsByParent.get(parent).push(joint);
  }

  const rootLink = [...linkGroups.keys()].find((name) => !childLinks.has(name));
  if (!rootLink) {
    throw new Error("URDF 没有 root link");
  }
  const rootGroup = new THREE.Group();
  rootGroup.name = state.metadata.robot.name;
  rootGroup.add(linkGroups.get(rootLink));
  attachChildren(rootLink);
  state.robotGroup = rootGroup;
  scene.add(rootGroup);
  await runLimited(meshTasks, 4);
  state.robotGroup.updateMatrixWorld(true);
  state.linkGroups = linkGroups;

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

function ensureTargetHand(panel) {
  const handLink = targetHandLink(panel);
  const source = state.linkGroups.get(handLink);
  if (!source) {
    return;
  }
  if (panel.targetHandGroup) {
    panel.targetGroup.remove(panel.targetHandGroup);
  }

  const ghost = source.clone(true);
  const material = targetHandMaterial(panel);
  ghost.name = `${panel.chainId}_target_hand`;
  ghost.traverse((object) => {
    object.userData.chainId = panel.chainId;
    object.userData.targetMarker = true;
    if (object.isMesh) {
      object.material = material;
      object.castShadow = false;
      object.receiveShadow = false;
      object.renderOrder = 4;
    }
  });

  panel.targetHandLink = handLink;
  panel.targetHandGroup = ghost;
  panel.targetGroup.add(ghost);
  updateTargetHandPose(panel);
}

function targetHandLink(panel) {
  return panel.chain.target_visual_link || panel.chain.end_link;
}

function targetHandMaterial(panel) {
  if (!panel.targetHandMaterial) {
    panel.targetHandMaterial = new THREE.MeshStandardMaterial({
      color: panel.color,
      transparent: true,
      opacity: 0.28,
      depthWrite: false,
      roughness: 0.55,
      metalness: 0.02,
      side: THREE.DoubleSide,
    });
  }
  return panel.targetHandMaterial;
}

function updateTargetHandPose(panel) {
  if (!panel.targetHandGroup || !panel.targetHandLink) {
    return;
  }
  const endGroup = state.linkGroups.get(panel.chain.end_link);
  const handGroup = state.linkGroups.get(panel.targetHandLink);
  if (!endGroup || !handGroup) {
    return;
  }

  state.robotGroup?.updateMatrixWorld(true);
  endGroup.updateMatrixWorld(true);
  handGroup.updateMatrixWorld(true);
  const endToHand = new THREE.Matrix4().copy(endGroup.matrixWorld).invert().multiply(handGroup.matrixWorld);
  const targetToHand = new THREE.Matrix4().copy(matrixFromPose(readPose(panel, "tcp"))).invert().multiply(endToHand);
  panel.targetHandGroup.matrixAutoUpdate = false;
  panel.targetHandGroup.matrix.copy(targetToHand);
  panel.targetHandGroup.matrixWorldNeedsUpdate = true;
}

function createPoseMarker(color, radius) {
  const group = new THREE.Group();
  group.add(new THREE.Mesh(new THREE.SphereGeometry(radius, 24, 16), transparentMaterial(color, 0.92)));
  group.add(new THREE.AxesHelper(radius * 4.5));
  return group;
}

function setPoseGroup(group, pose) {
  applyPose(group, pose);
}

function matrixFromPose(pose) {
  const matrix = rotationMatrixFromRpy(pose.rpy);
  matrix.setPosition(Number(pose.xyz[0] || 0), Number(pose.xyz[1] || 0), Number(pose.xyz[2] || 0));
  return matrix;
}

function applyPose(object, pose) {
  const matrix = matrixFromPose(pose);
  matrix.decompose(object.position, object.quaternion, object.scale);
  object.scale.set(1, 1, 1);
}

function rotationMatrixFromRpy(rpy) {
  const roll = Number(rpy?.[0] || 0);
  const pitch = Number(rpy?.[1] || 0);
  const yaw = Number(rpy?.[2] || 0);
  const cr = Math.cos(roll);
  const sr = Math.sin(roll);
  const cp = Math.cos(pitch);
  const sp = Math.sin(pitch);
  const cy = Math.cos(yaw);
  const sy = Math.sin(yaw);
  return new THREE.Matrix4().set(
    cy * cp,
    cy * sp * sr - sy * cr,
    cy * sp * cr + sy * sr,
    0,
    sy * cp,
    sy * sp * sr + cy * cr,
    sy * sp * cr - cy * sr,
    0,
    -sp,
    cp * sr,
    cp * cr,
    0,
    0,
    0,
    0,
    1,
  );
}

function rpyFromQuaternion(quaternion) {
  const matrix = new THREE.Matrix4().makeRotationFromQuaternion(quaternion);
  const te = matrix.elements;
  const pitch = Math.asin(clamp(-te[2], -1, 1));
  const cp = Math.cos(pitch);
  if (Math.abs(cp) > 1e-8) {
    return [Math.atan2(te[6], te[10]), pitch, Math.atan2(te[1], te[0])];
  }
  return [Math.atan2(-te[9], te[5]), pitch, 0];
}

function selectTargetPanel(panel) {
  if (!panel) {
    return;
  }
  state.activeTargetPanelId = panel.chainId;
  targetControls.attach(panel.targetGroup);
  targetControls.visible = true;
  setState(`正在编辑 ${panel.chain.display_name} 目标`, "success");
  publishRenderState("目标选择");
}

function setTargetControlMode(mode) {
  targetControls.setMode(mode);
  dom.targetMoveButton.classList.toggle("active", mode === "translate");
  dom.targetRotateButton.classList.toggle("active", mode === "rotate");
  const panel = activeTargetPanel();
  if (panel) {
    selectTargetPanel(panel);
  }
}

function activeTargetPanel() {
  return state.panels[state.activeTargetPanelId] || Object.values(state.panels)[0] || null;
}

function syncActiveTargetFromGizmo() {
  const panel = activeTargetPanel();
  if (!panel || targetControls.object !== panel.targetGroup) {
    return;
  }
  writePose(panel, "target", {
    xyz: panel.targetGroup.position.toArray(),
    rpy: rpyFromQuaternion(panel.targetGroup.quaternion),
  });
  panel.currentIk = null;
  panel.frames = [];
  panel.trajectoryGroup.clear();
  updateFrameLabel();
}

function selectTargetFromPointer(event) {
  if (event.button !== 0 || targetControls.dragging) {
    return;
  }
  const rect = renderer.domElement.getBoundingClientRect();
  targetPointer.x = ((event.clientX - rect.left) / rect.width) * 2 - 1;
  targetPointer.y = -((event.clientY - rect.top) / rect.height) * 2 + 1;
  targetRaycaster.setFromCamera(targetPointer, camera);
  const targetMeshes = [];
  for (const panel of Object.values(state.panels)) {
    panel.targetGroup.traverse((object) => {
      if (object.isMesh && object.userData.targetMarker) {
        targetMeshes.push(object);
      }
    });
  }
  const hit = targetRaycaster.intersectObjects(targetMeshes, false)[0];
  if (!hit) {
    return;
  }
  const panel = state.panels[hit.object.userData.chainId];
  selectTargetPanel(panel);
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
  applyPose(object, origin);
}

function materialFromVisual(visualEl) {
  const colorAttr = visualEl.querySelector("material > color")?.getAttribute("rgba");
  const rgba = colorAttr ? colorAttr.trim().split(/\s+/).map(Number) : [0.68, 0.72, 0.76, 1];
  return new THREE.MeshStandardMaterial({
    color: new THREE.Color(rgba[0], rgba[1], rgba[2]),
    transparent: rgba[3] < 1,
    opacity: rgba[3],
    roughness: 0.68,
    metalness: 0.05,
  });
}

function meshUrl(filename) {
  if (!filename) {
    return "";
  }
  if (/^https?:\/\//.test(filename) || filename.startsWith("/")) {
    return filename;
  }
  const clean = filename.replace(/^package:\/\/[^/]+\//, "").replace(/^file:\/\//, "");
  return `${state.meshBaseUrl}${clean.split("/").map(encodeURIComponent).join("/")}`;
}

function updateGroundFrame() {
  if (!state.robotGroup) {
    return;
  }
  state.robotGroup.position.set(0, 0, 0);
  state.robotGroup.updateMatrixWorld(true);
  const box = new THREE.Box3().setFromObject(state.robotGroup);
  if (box.isEmpty()) {
    state.sceneOffset.set(0, 0, 0);
    grid.position.z = 0;
    return;
  }
  const size = box.getSize(new THREE.Vector3());
  const gridSize = Math.max(2.6, size.x * 2.8, size.y * 2.8, size.z * 1.8);
  state.sceneOffset.set(0, 0, -box.min.z);
  state.robotGroup.position.copy(state.sceneOffset);
  grid.position.z = 0;
  grid.scale.setScalar(gridSize / 2.6);
  state.robotGroup.updateMatrixWorld(true);
}

function xyzToScene(xyz) {
  return [
    Number(xyz[0] || 0) + state.sceneOffset.x,
    Number(xyz[1] || 0) + state.sceneOffset.y,
    Number(xyz[2] || 0) + state.sceneOffset.z,
  ];
}

function xyzToRobot(xyz) {
  return [
    Number(xyz[0] || 0) - state.sceneOffset.x,
    Number(xyz[1] || 0) - state.sceneOffset.y,
    Number(xyz[2] || 0) - state.sceneOffset.z,
  ];
}

function poseToScene(pose) {
  return { xyz: xyzToScene(pose.xyz), rpy: [...pose.rpy] };
}

function poseToRobot(pose) {
  return { xyz: xyzToRobot(pose.xyz), rpy: [...pose.rpy] };
}

function resetView(stage = "视角复位") {
  resize();
  frameRobotInView(true);
  publishRenderState(stage);
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
  const maxDim = Math.max(size.x, size.y, size.z, 0.2);
  const focus = center.clone();
  focus.z = box.min.z + size.z * 0.55;
  controls.target.copy(focus);
  if (resetCamera) {
    const fitHeightDistance = maxDim / (2 * Math.tan(THREE.MathUtils.degToRad(camera.fov * 0.5)));
    const fitWidthDistance = fitHeightDistance / Math.max(camera.aspect, 0.1);
    const distance = Math.max(fitHeightDistance, fitWidthDistance) * 1.75;
    const direction = new THREE.Vector3(0.85, -1.65, 0.72).normalize();
    camera.zoom = 1;
    camera.position.copy(focus).addScaledVector(direction, distance);
    camera.near = Math.max(distance / 100, 0.001);
    camera.far = Math.max(distance * 20, 20);
    controls.minDistance = Math.max(maxDim * 0.25, 0.1);
    controls.maxDistance = Math.max(maxDim * 6, 3);
  }
  camera.lookAt(focus);
  camera.updateProjectionMatrix();
  controls.update();
}

function resize() {
  const rect = dom.viewport.getBoundingClientRect();
  const width = Math.max(1, rect.width);
  const height = Math.max(1, rect.height);
  renderer.setSize(width, height, true);
  camera.aspect = width / height;
  camera.updateProjectionMatrix();
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

async function runLimited(tasks, limit) {
  const workers = Array.from({ length: Math.min(limit, tasks.length) }, async (_, workerIndex) => {
    for (let index = workerIndex; index < tasks.length; index += limit) {
      await tasks[index]();
    }
  });
  await Promise.all(workers);
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

function setState(text, kind = "") {
  dom.stateLabel.textContent = text;
  dom.stateLabel.className = kind;
}

function setIkMessage(panel, text, kind = "") {
  panel.dom.ikMessage.textContent = text;
  panel.dom.ikMessage.className = `message-line ${kind}`.trim();
}

async function checkCollision(panel, joints = readJointInputs(panel), tcpOffset = readPose(panel, "tcp")) {
  const payload = {
    robot: state.activeRobot,
    chain_id: panel.chainId,
    joints,
    tcp_offset: tcpOffset,
  };
  try {
    const data = await fetchJson("/api/collision/check", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    panel.currentCollision = data;
    updateCollisionMetrics(panel, data);
    return data;
  } catch (error) {
    console.error(error);
    updateCollisionMetrics(panel, null, null, error.message);
    return null;
  }
}

function updateCollisionMetrics(panel, data, trajectorySummary = null, errorMessage = "") {
  const summary = trajectorySummary || data;
  panel.dom.collisionCard.className = "";
  if (!data) {
    panel.dom.collisionStatus.textContent = errorMessage ? "错误" : "-";
    panel.dom.collisionDistance.textContent = "-";
    panel.dom.collisionPair.textContent = "-";
    panel.dom.collisionCounts.textContent = "-";
    setCollisionMessage(panel, errorMessage || "等待碰撞检查。", errorMessage ? "error" : "");
    return;
  }

  const status = data.status || "unconfigured";
  const statusLabel = data.status_label || collisionStatusLabel(status);
  const minDistance = data.min_distance_mm;
  const pair = data.pair;
  panel.dom.collisionCard.className = `collision-card ${status}`;
  panel.dom.collisionStatus.textContent = statusLabel;
  panel.dom.collisionDistance.textContent = Number.isFinite(Number(minDistance)) ? `${Number(minDistance).toFixed(1)} mm` : "-";
  panel.dom.collisionPair.textContent = pair ? `${shortCollisionName(pair.a)} / ${shortCollisionName(pair.b)}` : "-";
  if (summary?.checks) {
    panel.dom.collisionCounts.textContent = `碰撞 ${summary.collision_count || 0} / 接近 ${summary.near_count || 0}`;
  } else {
    panel.dom.collisionCounts.textContent = "-";
  }

  if (status === "collision") {
    setCollisionMessage(panel, "当前姿态或轨迹已经进入简化碰撞区域，需要调整目标或关节。", "error");
  } else if (status === "near") {
    setCollisionMessage(panel, "当前姿态或轨迹接近碰撞区域，建议留出更多余量。", "warn");
  } else if (status === "safe") {
    setCollisionMessage(panel, "简化碰撞检查通过。", "success");
  } else {
    setCollisionMessage(panel, "这个机器人还没有配置碰撞区域。", "warn");
  }
}

function setCollisionMessage(panel, text, kind = "") {
  panel.dom.collisionMessage.textContent = text;
  panel.dom.collisionMessage.className = `message-line ${kind}`.trim();
}

function writeDebug(panel, stage, request, response) {
  panel.dom.debug.textContent = JSON.stringify({ 阶段: stage, 请求: request, 响应: response }, null, 2);
}

function summarizeFkRequest(payload) {
  return { 机器人: payload.robot, 手臂: payload.chain_id, 关节: payload.joints, tcp偏移: payload.tcp_offset };
}

function summarizeIkRequest(payload) {
  return {
    机器人: payload.robot,
    手臂: payload.chain_id,
    求解器: payload.solver,
    当前关节: payload.current_joints,
    目标位姿: payload.target_pose,
    tcp偏移: payload.tcp_offset,
  };
}

function summarizeTrajectoryRequest(payload) {
  return {
    机器人: payload.robot,
    手臂: payload.chain_id,
    规划器: payload.planner_type,
    时长: payload.duration,
    点数: payload.steps,
    当前关节: payload.current_joints,
    目标关节: payload.target_joints,
    tcp偏移: payload.tcp_offset,
  };
}

function summarizeFk(data) {
  return { tcp位姿: data.tcp_pose, link数量: Object.keys(data.link_poses || {}).length };
}

function summarizeIk(data) {
  return {
    求解器: data.solver,
    成功: data.success,
    位置误差mm: data.error_mm,
    姿态误差deg: radToDeg(data.error_rotation),
    迭代次数: data.iterations,
    消息: data.message,
    tcp位姿: data.tcp_pose,
    目标关节: data.named_target_joints,
  };
}

function summarizeTrajectory(data, ik) {
  return {
    规划器: data.planner,
    轨迹点数: data.waypoint_count,
    时长: data.duration,
    起点tcp: data.waypoints[0]?.tcp_pose,
    终点tcp: data.waypoints[data.waypoints.length - 1]?.tcp_pose,
    碰撞: summarizeCollision(data.collision),
    ik: summarizeIk(ik),
  };
}

function summarizeCollision(data) {
  if (!data) {
    return null;
  }
  return {
    状态: data.status_label || collisionStatusLabel(data.status),
    最小距离mm: data.min_distance_mm,
    最近对象: data.pair ? `${data.pair.a} / ${data.pair.b}` : null,
    碰撞帧数: data.collision_count,
    接近帧数: data.near_count,
  };
}

function updateFrameLabel() {
  const left = Object.values(state.panels).find((panel) => panel.side === "left");
  const right = Object.values(state.panels).find((panel) => panel.side === "right");
  const text = (panel) => (panel?.frames.length ? `${panel.frameIndex + 1}/${panel.frames.length}` : "0/0");
  dom.frameLabel.textContent = `左 ${text(left)} | 右 ${text(right)}`;
}

function publishRenderState(stage) {
  let objectCount = 0;
  let meshCount = 0;
  let lineCount = 0;
  scene.traverse((object) => {
    objectCount += 1;
    if (object.isMesh) meshCount += 1;
    if (object.isLine) lineCount += 1;
  });
  let robotBox = null;
  if (state.robotGroup) {
    const box = new THREE.Box3().setFromObject(state.robotGroup);
    if (!box.isEmpty()) {
      robotBox = {
        min: box.min.toArray(),
        max: box.max.toArray(),
        center: box.getCenter(new THREE.Vector3()).toArray(),
        size: box.getSize(new THREE.Vector3()).toArray(),
      };
    }
  }
  document.body.setAttribute(
    "data-ik-replay-scene",
    JSON.stringify({
      stage,
      robot: state.activeRobot,
      objectCount,
      meshCount,
      lineCount,
      joints: state.jointNodes.size,
      chains: Object.fromEntries(
        Object.values(state.panels).map((panel) => [
          panel.chainId,
          {
            side: panel.side,
            frames: panel.frames.length,
            frameIndex: panel.frameIndex,
            targetVisualLink: panel.targetHandLink,
            targetVisualDelta: targetVisualDelta(panel),
            target: panel.targetGroup.position.toArray(),
            tcp: panel.tcpGroup.position.toArray(),
            targetRpy: rpyFromQuaternion(panel.targetGroup.quaternion),
            tcpRpy: rpyFromQuaternion(panel.tcpGroup.quaternion),
          },
        ]),
      ),
      robotBox,
      sceneOffset: state.sceneOffset.toArray(),
      controlsTarget: controls.target.toArray(),
    }),
  );
}

function targetVisualDelta(panel) {
  if (!panel.targetHandGroup || !panel.targetHandLink) {
    return null;
  }
  const linkGroup = state.linkGroups.get(panel.targetHandLink);
  if (!linkGroup) {
    return null;
  }
  state.robotGroup?.updateMatrixWorld(true);
  panel.targetGroup.updateMatrixWorld(true);
  panel.targetHandGroup.updateMatrixWorld(true);
  linkGroup.updateMatrixWorld(true);
  const delta = new THREE.Matrix4().copy(linkGroup.matrixWorld).invert().multiply(panel.targetHandGroup.matrixWorld);
  const te = delta.elements;
  const positionMm = Math.hypot(te[12], te[13], te[14]) * 1000;
  const trace = te[0] + te[5] + te[10];
  const rotationDeg = radToDeg(Math.acos(clamp((trace - 1) / 2, -1, 1)));
  return {
    positionMm,
    rotationDeg,
  };
}

function formatNumber(value) {
  return Number(value).toFixed(3).replace(/0+$/, "").replace(/\.$/, "");
}

function clamp(value, min, max) {
  return Math.min(max, Math.max(min, value));
}

function radToDeg(value) {
  return (Number(value) * 180) / Math.PI;
}

function colorForCollision(status, fallback) {
  if (status === "collision") return 0xc53f3f;
  if (status === "near") return 0xd9ab34;
  if (status === "safe") return 0x1c8b4c;
  return fallback;
}

function collisionStatusLabel(status) {
  const labels = {
    safe: "安全",
    near: "接近",
    collision: "碰撞",
    unconfigured: "未配置",
  };
  return labels[status] || status || "-";
}

function shortCollisionName(name) {
  return String(name || "")
    .replace(/^left_arm_/, "L ")
    .replace(/^right_arm_/, "R ")
    .replaceAll("_", " ");
}

function shortJointName(name) {
  return name
    .replace(/^left_/, "L ")
    .replace(/^right_/, "R ")
    .replace(/_joint$/, "")
    .replaceAll("_", " ");
}

function optionLabel(value) {
  const labels = {
    numerical: "数值求解器",
    dummy: "占位求解器",
    linear: "线性插值",
    quintic: "五次多项式",
  };
  return labels[value] || value;
}
