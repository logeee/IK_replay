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
  await initReach();
}

// ---- reach adapter：点击相机取目标 → IK 预演 → 确认后真机执行 ----

const reach = {
  status: null,
  lastPick: null,
  dom: null,
  obstacleGroup: new THREE.Group(),
  flangeDebugGroup: null,
  waypoints: [],
  picking: false,
  pendingClick: null,
  plane: null,
  planeGroup: new THREE.Group(),
  execFrames: null, // 真机执行只跑主段（预演 frames 可能拼了横移预览段）
  finePick: false,  // 弹窗「再次选点」置位：下一次取点直达（跳过经由路点），执行时消费
};

async function initReach() {
  let status = null;
  try {
    status = await fetchJson("/api/reach/status");
  } catch {
    return; // 没挂 reach adapter，保持纯离线查看器
  }
  if (!status?.enabled) {
    return;
  }
  reach.status = status;
  if (status.robot && status.robot !== state.activeRobot) {
    await loadRobotData(status.robot);
  }

  reach.dom = {
    panel: document.getElementById("reachPanel"),
    body: document.getElementById("reachBody"),
    badge: document.getElementById("reachBadge"),
    collapse: document.getElementById("reachCollapseBtn"),
    video: document.getElementById("reachVideo"),
    mark: document.getElementById("reachMark"),
    info: document.getElementById("reachInfo"),
    offset: document.getElementById("reachOffset"),
    duration: document.getElementById("reachDuration"),
    arm: document.getElementById("reachArmBtn"),
    replan: document.getElementById("reachReplanBtn"),
    exec: document.getElementById("reachExecBtn"),
    stop: document.getElementById("reachStopBtn"),
    scan: document.getElementById("reachScanBtn"),
    clearObs: document.getElementById("reachClearObsBtn"),
    waypointSel: document.getElementById("reachWaypointSel"),
    addVia: document.getElementById("reachAddViaBtn"),
    viaList: document.getElementById("reachViaList"),
    endSel: document.getElementById("reachEndSel"),
    handMove: document.getElementById("reachHandMoveBtn"),
    record: document.getElementById("reachRecordBtn"),
    delWp: document.getElementById("reachDelWpBtn"),
    stepLen: document.getElementById("reachStepLen"),
    pushForce: document.getElementById("reachPushForce"),
    stepMode: document.getElementById("reachStepMode"),
    stepNext: document.getElementById("reachStepNext"),
    nextSide: document.getElementById("reachNextSideBtn"),
    nextPick: document.getElementById("reachNextPickBtn"),
    nextReturn: document.getElementById("reachNextReturnBtn"),
    nextDone: document.getElementById("reachNextDoneBtn"),
    msg: document.getElementById("reachMsg"),
    diag: document.getElementById("reachDiag"),
    fsBtn: document.getElementById("reachFullscreenBtn"),
    fsOverlay: document.getElementById("reachFsOverlay"),
    fsVideo: document.getElementById("reachFsVideo"),
    fsMark: document.getElementById("reachFsMark"),
    fsClose: document.getElementById("reachFsCloseBtn"),
  };
  const d = reach.dom;
  d.panel.classList.remove("hidden");
  d.video.src = "/api/reach/stream";
  d.video.addEventListener("click", onReachVideoClick);
  d.collapse.addEventListener("click", () => d.body.classList.toggle("hidden"));
  d.arm.addEventListener("click", () => toggleReachArm());
  d.replan.addEventListener("click", () => runReachPlan());
  d.exec.addEventListener("click", () => executeReach());
  d.stop.addEventListener("click", () => stopReach());
  d.scan.addEventListener("click", () => scanObstacles());
  d.clearObs.addEventListener("click", () => clearObstacles());
  d.handMove.addEventListener("click", () => toggleHandMove());
  d.record.addEventListener("click", () => recordWaypoint());
  d.delWp.addEventListener("click", () => deleteWaypoint());
  d.addVia.addEventListener("click", () => addViaWaypoint());
  d.nextSide.addEventListener("click", () => stepNextSidestep());
  d.nextPick.addEventListener("click", () => stepNextRepick());
  d.nextReturn.addEventListener("click", () => stepNextReturn());
  d.nextDone.addEventListener("click", () => hideStepNext());
  d.fsBtn.addEventListener("click", () => openReachFullscreen());
  d.fsClose.addEventListener("click", () => cancelReachFullscreen());
  d.fsVideo.addEventListener("click", (ev) => onReachFullscreenClick(ev));
  state.helperRoot.add(reach.obstacleGroup, reach.planeGroup);
  await refreshObstacles();
  await refreshWaypoints();
  showFlangeDebug();
  updateReachArmUi();
  refreshReachDiag();
  const rms = status.calib?.rms_mm;
  reachMsg(`标定: ${status.calib?.solved_at || "?"} · RMS ${rms ? rms.toFixed(2) : "?"} mm · ` +
    `TCP=p_tool [${(status.p_tool || []).map((v) => v.toFixed(3)).join(", ")}] m`);
}

function updateReachArmUi() {
  const st = reach.status;
  const d = reach.dom;
  if (st.armed) {
    d.badge.textContent = st.hand_move ? "已接管（卸力中）" : "已接管手臂";
    d.badge.classList.add("exec");
    d.arm.textContent = "释放手臂";
    d.arm.classList.add("danger");
  } else {
    d.badge.textContent = st.arm_supported ? "未接管（仅模拟）" : "仅模拟";
    d.badge.classList.remove("exec");
    d.arm.textContent = "接管手臂";
    d.arm.classList.remove("danger");
  }
  d.arm.disabled = !st.arm_supported;
  d.stop.disabled = !st.armed;
  d.handMove.disabled = !st.armed;
  d.handMove.textContent = st.hand_move ? "恢复保持" : "卸力摆位";
  d.handMove.classList.toggle("danger", !!st.hand_move);
  // 录制只需要能读到关节（未接管也可以录，比如遥操作摆好后录）
  d.record.disabled = !st.joints_available;
  if (!st.armed) {
    d.exec.disabled = true;
  }
}

async function toggleReachArm() {
  const st = reach.status;
  const d = reach.dom;
  if (!st.armed) {
    const ok = window.confirm(
      "确认接管手臂？\n\n接管后本程序将发布 rt/arm_sdk，手臂在当前姿态刚性保持。\n" +
      "请确保没有其他程序（遥操作等）正在控制手臂，否则会抽搐！");
    if (!ok) {
      return;
    }
  } else {
    const ok = window.confirm(
      "确认释放手臂？\n\n控制权将交还本体控制器（权重 1 秒渐出）。\n请扶住手臂以防下坠。");
    if (!ok) {
      return;
    }
  }
  d.arm.disabled = true;
  try {
    const data = await fetchJson(st.armed ? "/api/reach/disarm" : "/api/reach/arm", { method: "POST" });
    reach.status.armed = data.armed;
    reachMsg(data.message || "-", "success");
  } catch (error) {
    reachMsg(`操作失败: ${error.message}`, "error");
  } finally {
    d.arm.disabled = false;
    updateReachArmUi();
    refreshReachDiag();
  }
}

function reachMsg(text, kind = "") {
  reach.dom.msg.textContent = text;
  reach.dom.msg.className = `reach-msg ${kind}`.trim();
}

// 每次真机动完刷新一次：跟随误差看重力前馈补够了没，躯干漂移看
// "够不着"是手臂没到位还是躯干自己转了（两者的解法完全不同）
async function refreshReachDiag() {
  const box = reach.dom.diag;
  if (!box) {
    return;
  }
  let data;
  try {
    data = await fetchJson("/api/reach/diagnostics");
  } catch {
    return;
  }
  const arm = data.arm || {};
  const parts = [];
  if (arm.armed) {
    const alpha = arm.grav_alpha ?? 0;
    const tau = (arm.tau_grav_nm || []).map((v) => Math.abs(v));
    parts.push(`重力前馈 α=${alpha}${tau.length ? ` · 峰值 ${Math.max(...tau).toFixed(1)} Nm` : ""}`);
    if (arm.follow_error_max_deg != null) {
      const err = arm.follow_error_max_deg;
      parts.push(`跟随误差 ${err.toFixed(2)}°${Math.abs(err) > 1.5 ? "（偏大，考虑加 --arm-payload-kg）" : ""}`);
    }
  }
  const drift = data.torso_drift;
  if (drift) {
    const rot = drift.torso_rotation_deg;
    const shift = drift.target_shift_mm;
    if (rot != null) {
      parts.push(`躯干较取点时转了 ${rot.toFixed(1)}°`
        + (shift != null ? ` → 目标漂移 ${shift.toFixed(0)} mm` : ""));
    }
    const waist = drift.waist_delta_deg || [];
    if (waist.some((v) => Math.abs(v) > 0.3)) {
      parts.push(`腰 ${waist.map((v) => v.toFixed(1)).join("/")}°`);
    }
  }
  box.textContent = parts.join(" · ");
  box.classList.toggle("hidden", parts.length === 0);
}

// 显示坐标 → 相机像素坐标（img 可能被缩放显示）
function reachPixelFromEvent(ev, img) {
  const st = reach.status;
  if (!st?.camera?.width || !img.clientWidth) {
    return null;
  }
  const rect = img.getBoundingClientRect();
  const relX = (ev.clientX - rect.left) / rect.width;
  const relY = (ev.clientY - rect.top) / rect.height;
  return {
    u: Math.round(relX * st.camera.width),
    v: Math.round(relY * st.camera.height),
    relX,
    relY,
  };
}

// 在小窗画面上放黄圈标记（rel 是 0~1 的相对位置）
function placeReachMark(relX, relY) {
  const img = reach.dom.video;
  // 按像素定位（图像与容器同顶同宽，容器可能被 flex 拉得更高，不能用百分比）
  reach.dom.mark.style.left = `${relX * img.clientWidth}px`;
  reach.dom.mark.style.top = `${relY * img.clientHeight}px`;
  reach.dom.mark.classList.remove("hidden");
}

async function onReachVideoClick(ev) {
  const px = reachPixelFromEvent(ev, reach.dom.video);
  if (!px) {
    return;
  }
  placeReachMark(px.relX, px.relY);
  await submitReachPick(px.u, px.v);
}

// ---- 全屏选点：放大画面精确点击，确认后自动退回小窗并走原有取点流程 ----

function openReachFullscreen() {
  const d = reach.dom;
  d.fsMark.classList.add("hidden");
  d.fsVideo.src = "/api/reach/stream";   // 独立的一路 MJPEG，关闭时断开
  d.fsOverlay.classList.remove("hidden");
  window.addEventListener("keydown", onReachFullscreenKey);
}

function onReachFullscreenKey(ev) {
  if (ev.key === "Escape") {
    ev.preventDefault();
    cancelReachFullscreen();
  }
}

// 取消全屏选点（Esc / × 退出）：若是从暂停弹窗的「再次选点」进来的，
// 等价于没点过——撤销直达标志并回到弹窗四选项。
function cancelReachFullscreen() {
  closeReachFullscreen();
  if (reach.finePick) {
    reach.finePick = false;
    showStepNext();
  }
}

function closeReachFullscreen() {
  const d = reach.dom;
  d.fsOverlay.classList.add("hidden");
  d.fsVideo.src = "";                    // 断开这路视频流
  d.fsMark.classList.add("hidden");
  window.removeEventListener("keydown", onReachFullscreenKey);
}

async function onReachFullscreenClick(ev) {
  const d = reach.dom;
  const px = reachPixelFromEvent(ev, d.fsVideo);
  if (!px) {
    return;
  }
  // 标记放在点击处（相对全屏舞台定位，图像在舞台里是居中的）
  const imgRect = d.fsVideo.getBoundingClientRect();
  const stageRect = d.fsVideo.parentElement.getBoundingClientRect();
  d.fsMark.style.left = `${imgRect.left - stageRect.left + px.relX * imgRect.width}px`;
  d.fsMark.style.top = `${imgRect.top - stageRect.top + px.relY * imgRect.height}px`;
  d.fsMark.classList.remove("hidden");

  const ok = window.confirm(`确定选这个点吗？像素 [${px.u}, ${px.v}]\n` +
    (reach.finePick ? "确定后退出全屏，规划通过将直接真机执行！"
                    : "确定后退出全屏并开始取点规划。"));
  if (!ok) {
    d.fsMark.classList.add("hidden");   // 留在全屏里重新点
    return;
  }
  const fine = reach.finePick;
  closeReachFullscreen();
  placeReachMark(px.relX, px.relY);     // 小窗上同步显示标记
  await submitReachPick(px.u, px.v);
  // 精定位：规划可执行就直接上真机（全屏确认框已当过安全确认）
  if (fine && reach.finePick) {
    if (!reach.dom.exec.disabled) {
      await executeReach({ skipConfirm: true });
    } else {
      reach.finePick = false;
      showStepNext();   // 规划失败/有碰撞：回到弹窗重新选
      reachMsg("精定位规划不可执行（IK 失败或有碰撞），已返回选择", "error");
    }
  }
}

// 取点 + 规划（小窗点击和全屏选点共用入口）
async function submitReachPick(u, v) {
  // 连续点击去重：只记录最新一次，正在计算时不重复触发；
  // 当前轮算完后若发现有更新的点击，跳过旧结果直接算最新的。
  reach.pendingClick = { u, v };
  if (reach.picking) {
    reachMsg("已更新目标，等待当前计算结束…");
    return;
  }
  reach.picking = true;
  reach.dom.exec.disabled = true;
  try {
    while (reach.pendingClick) {
      const click = reach.pendingClick;
      reach.pendingClick = null;
      reachMsg("取点中…");
      let data;
      try {
        data = await fetchJson("/api/reach/pick", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({
            u: click.u, v: click.v,
            approach_offset_m: Number(reach.dom.offset.value || 0),
          }),
        });
      } catch (error) {
        reach.lastPick = null;
        reachMsg(`取点失败: ${error.message}`, "error");
        continue; // 若期间又点了新目标，继续算新的
      }
      if (reach.pendingClick) {
        continue; // 已有更新的点击，旧结果不再规划
      }
      reach.lastPick = data;
      reach.plane = data.plane || null;
      visualizeSurfacePlane(data);
      reach.dom.replan.disabled = false;
      await runReachPlan();
    }
  } finally {
    reach.picking = false;
  }
}

async function runReachPlan() {
  const st = reach.status;
  const pick = reach.lastPick;
  const panel = state.panels[st.chain_id];
  if (!pick || !panel) {
    reachMsg("请先在画面中点击目标", "error");
    return;
  }
  reachMsg("解算 + 规划中…");
  reach.dom.exec.disabled = true;

  // IK 起点：优先真机当前关节角
  if (st.joints_available) {
    try {
      const j = await fetchJson("/api/reach/joints");
      if (j.ok) {
        setJointInputs(panel, j.named_joints);
        Object.assign(state.robotJointState, j.named_joints);
        setRobotJoints(state.robotJointState, false);
      }
    } catch (error) {
      reachMsg(`读真机关节失败（用面板当前值代替）: ${error.message}`, "error");
    }
  }

  // TCP = 标定出的指尖偏移；目标 = 反投影点（URDF 根系 → 场景系）
  writePose(panel, "tcp", { xyz: st.p_tool, rpy: [0, 0, 0] });
  updateTargetHandPose(panel);
  writePose(panel, "target", { xyz: xyzToScene(pick.p_root), rpy: readPose(panel, "target").rpy });
  updateTargetMarker(panel);
  if (panel.targetHandGroup) {
    // 点目标不解姿态，"幽灵手"显示的朝向没有意义，纯属干扰
    panel.targetHandGroup.visible = false;
  }
  const duration = Math.max(1, Number(reach.dom.duration.value || 6));
  panel.dom.duration.value = formatNumber(duration);

  // 直达模式（弹窗「再次选点」）：精定位小距离移动，跳过经由路点直接规划
  const direct = Boolean(reach.finePick);
  const viaWps = direct ? [] : viaWaypoints();
  panel.solverOptions = { solve_orientation: false }; // 指尖是点目标，姿态放开
  try {
    if (viaWps.length) {
      await planViaWaypoints(panel, viaWps);
    } else {
      await planTrajectory(panel);
    }
  } finally {
    panel.solverOptions = null;
  }

  const ik = panel.currentIk;

  // 横移段并入预演：执行时仍分两段（先到位校正，再按真机实际姿态重规划横移）
  reach.execFrames = panel.frames;
  const stepCm = Number(reach.dom.stepLen.value || 0);
  let sidestepOk = false;
  if (!direct && ik?.success && stepCm && reach.plane && panel.frames.length > 1
      && panel.currentCollision?.status !== "collision") {
    sidestepOk = await appendSidestepPreview(panel, stepCm);
  }

  // 收回段也并入预演（执行时同样按真机实际姿态就地重规划）
  const endWpPreview = direct ? null : selectedEndWaypoint();
  let returnOk = false;
  if (ik?.success && endWpPreview && panel.frames.length > 1
      && panel.currentCollision?.status !== "collision") {
    returnOk = await appendReturnPreview(panel, endWpPreview);
  }

  const collision = panel.currentCollision;
  const pt = pick.p_torso;
  const lines = [
    `像素 [${pick.pixel}] · 深度 ${Math.round(pick.depth_mm)} mm`,
    `目标(躯干系) [${pt.map((v) => v.toFixed(3)).join(", ")}] m`,
    `接近偏移 ${pick.approach_offset_m} m ` +
      (pick.offset_mode === "plane_normal" ? "沿表面法线（0 = 触碰，负 = 压入加力）"
                                           : "沿视线（平面拟合失败的兜底）"),
    ...(direct ? ["直达模式：从当前姿态直接规划（跳过经由路点）"] : []),
    ...(viaWps.length
      ? [`经由 ${viaWps.map((w) => `「${w.name}」`).join("→")} 分段规划`] : []),
    ...(stepCm ? [`到位后沿面${stepCm > 0 ? "左" : "右"}移 ${Math.abs(stepCm)}cm` +
      (sidestepOk ? "（已并入预演）" : "（横移段规划失败）")] : []),
    ...(endWpPreview ? [`结束后收回到「${endWpPreview.name}」` +
      (returnOk ? "（已并入预演）" : "（收回段规划失败）")] : []),
    `IK: ${ik ? (ik.success ? "成功" : "未到达") : "失败"}` +
      (ik ? ` · 误差 ${Number(ik.error_mm).toFixed(1)} mm` : ""),
    `碰撞: ${collision?.status_label || "-"} · 轨迹点 ${panel.frames.length}`,
  ];
  lines.push(...describeReachCollision(panel, collision));
  reach.dom.info.textContent = lines.join("\n");

  const planned = ik?.success && panel.frames.length > 1 && collision?.status !== "collision";
  if (planned) {
    reach.dom.exec.disabled = !st.armed;
    reachMsg(st.armed
      ? "预演回放中，确认无误后点「真机执行」"
      : "预演回放中（未接管手臂，先点「接管手臂」才能执行）", "success");
    replay(panel);
  } else {
    reach.dom.exec.disabled = true;
    reachMsg(ik?.success === false
      ? "目标不可达（IK 未收敛），换个目标或调整姿态"
      : (collision?.status === "collision" ? "轨迹有碰撞，已禁止执行" : "规划失败"), "error");
  }
}

// 拟合的目标表面平面 + "左"方向箭头（横移一步沿这个方向）
function visualizeSurfacePlane(pick) {
  reach.planeGroup.clear();
  const plane = pick.plane;
  if (!plane) {
    return;
  }
  const center = new THREE.Vector3(...xyzToScene(pick.p_root_surface));
  const normal = new THREE.Vector3(...plane.normal_root);
  const left = new THREE.Vector3(...plane.left_root);

  const patch = new THREE.Mesh(
    new THREE.PlaneGeometry(0.3, 0.3),
    new THREE.MeshBasicMaterial({ color: 0x35a2d0, transparent: true, opacity: 0.18, side: THREE.DoubleSide, depthWrite: false }),
  );
  patch.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), normal);
  patch.position.copy(center);
  const arrow = new THREE.ArrowHelper(left, center, 0.14, 0x35d07f, 0.04, 0.02);
  const label = makeLabelSprite("左");
  label.position.copy(center).addScaledVector(left, 0.18);
  reach.planeGroup.add(patch, arrow, label);
  publishRenderState("表面平面");
}

// 预演用：从主轨迹终点接着规划横移段并拼进回放（执行时会按真机实际姿态重规划）。
// 横移用笛卡尔直线插补：指尖钉在直线上，不会像关节空间插值那样中途下沉。
async function appendSidestepPreview(panel, stepCm) {
  const mainFrames = panel.frames;
  const last = mainFrames[mainFrames.length - 1];
  try {
    const seg = await planCartesianSidestep(last.named_joints, stepCm);
    panel.frames = [...mainFrames, ...seg.waypoints.slice(1)];
    panel.frameIndex = 0;
    panel.currentCollision = combineCollisionSummaries(panel.currentCollision, seg.collision);
    updateCollisionMetrics(panel, panel.currentCollision);
    updateTrajectoryLine(panel);
    visualizeCollision(panel, panel.currentCollision);
    applyFrame(panel, 0);
    return true;
  } catch (error) {
    console.error(error);
    return false;
  }
}

// 横移方向 = 拟合平面的"左"（右移取反）再向下倾 SIDESTEP_TILT_DEG 度
const SIDESTEP_TILT_DEG = 2;

function sidestepDirection(sign) {
  const t = (SIDESTEP_TILT_DEG * Math.PI) / 180;
  const l = reach.plane.left_root;
  const c = Math.cos(t);
  const s = Math.sin(t);
  // 先取实际移动方向（±左），再往下倾：保证右移时同样是"偏下"而不是偏上
  return [l[0] * sign * c, l[1] * sign * c, l[2] * sign * c - s];
}

function planCartesianSidestep(startJoints, stepCm) {
  return fetchJson("/api/reach/plan_cartesian", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      start_joints: startJoints,
      direction_root: sidestepDirection(Math.sign(stepCm)),
      distance_m: Math.abs(stepCm) / 100,
      step_m: 0.01,
    }),
  });
}

// 沿拟合平面横移（stepCm 正=左 负=右），从真机当前姿态就地规划执行
async function sidestepReach(stepCm, options = {}) {
  const st = reach.status;
  const plane = reach.plane;
  const panel = state.panels[st.chain_id];
  if (!plane || !panel) {
    reachMsg("还没有拟合出表面平面，无法横移", "error");
    return;
  }
  const step = stepCm / 100;
  const dirName = stepCm > 0 ? "左" : "右";
  reachMsg(`${dirName}移 ${(Math.abs(step) * 100).toFixed(0)}cm 规划中…`);

  // 起点 = 真机当前关节（读不到就用面板当前值，纯模拟联调用）
  let joints = readJointInputs(panel);
  if (st.joints_available) {
    try {
      const j = await fetchJson("/api/reach/joints");
      if (j.ok) {
        joints = j.named_joints;
      }
    } catch { /* 用面板值兜底 */ }
  }
  setJointInputs(panel, joints);
  Object.assign(state.robotJointState, joints);
  setRobotJoints(state.robotJointState, false);
  writePose(panel, "tcp", { xyz: st.p_tool, rpy: [0, 0, 0] });

  // 笛卡尔直线插补：指尖钉在"左"方向直线上（不会下沉绕行）
  let seg;
  try {
    seg = await planCartesianSidestep(joints, stepCm);
  } catch (error) {
    reachMsg(`${dirName}移规划失败: ${error.message}`, "error");
    return;
  }
  panel.frames = seg.waypoints;
  panel.frameIndex = 0;
  panel.currentCollision = seg.collision;
  updateCollisionMetrics(panel, seg.collision);
  updateTrajectoryLine(panel);
  visualizeCollision(panel, seg.collision);
  applyFrame(panel, 0);
  if (panel.targetHandGroup) {
    panel.targetHandGroup.visible = false;
  }
  if (seg.collision?.status === "collision") {
    reachMsg(`${dirName}移轨迹有碰撞，已禁止执行`, "error");
    return;
  }
  if (!st.armed) {
    replay(panel);
    reachMsg(`${dirName}移已预演（未接管手臂，无法真机执行）`, "success");
    return;
  }
  const pushN = Math.max(0, Number(reach.dom.pushForce?.value || 0));
  if (!options.skipConfirm) {
    const ok = window.confirm(
      `确认沿电柜表面${dirName}移 ${Math.abs(stepCm).toFixed(0)}cm？手臂将运动。\n` +
      `直线插补 ${seg.steps} 步 · 最大 IK 误差 ${Number(seg.max_ik_error_mm).toFixed(1)} mm · ` +
      `推力 ${pushN}N · 碰撞: ${seg.collision?.status_label || "-"}`);
    if (!ok) {
      return;
    }
  }
  try {
    await fetchJson("/api/reach/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        waypoints: panel.frames.map((frame) => frame.named_joints),
        label: `${dirName}移${stepCm}cm${pushN > 0 ? `+${pushN}N` : ""}`,
        // 带推力时快拨（0.06 m/s）：借冲量越过旋钮定位卡点，比慢慢顶有效；
        // 无推力的普通横移保持慢滑（0.02 m/s）
        duration: pushN > 0
          ? Math.max(1, Math.abs(stepCm) / 100 / 0.06)
          : Math.max(2, Math.abs(stepCm) / 100 / 0.02),
        // 沿移动方向的前馈力：接触旋钮后位置环刚度不够，靠它出力拨动
        ...(pushN > 0 ? {
          push: {
            direction_root: sidestepDirection(Math.sign(stepCm)),
            force_n: pushN,
          },
        } : {}),
      }),
    });
    reachMsg(`${dirName}移执行中…`, "success");
    await pollReachExec();
  } catch (error) {
    reachMsg(`${dirName}移执行失败: ${error.message}`, "error");
  }
}

// 预演用：从当前预演轨迹终点接着规划收回段并拼进回放
// （真机执行时会按实际姿态就地重规划，见 returnToWaypoint）
async function appendReturnPreview(panel, wp) {
  const mainFrames = panel.frames;
  const last = mainFrames[mainFrames.length - 1];
  try {
    const seg = await fetchJson("/api/trajectory/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        robot: state.activeRobot,
        chain_id: panel.chainId,
        current_joints: last.named_joints,
        target_joints: wp.named_joints,
        tcp_offset: readPose(panel, "tcp"),
        duration: 2.5,
        steps: 30,
        planner_type: panel.dom.planner.value,
      }),
    });
    panel.frames = [...mainFrames, ...seg.waypoints.slice(1)];
    panel.frameIndex = 0;
    panel.currentCollision = combineCollisionSummaries(panel.currentCollision, seg.collision);
    updateCollisionMetrics(panel, panel.currentCollision);
    updateTrajectoryLine(panel);
    visualizeCollision(panel, panel.currentCollision);
    applyFrame(panel, 0);
    return true;
  } catch (error) {
    console.error(error);
    return false;
  }
}

// 任务收尾：收回到选定的结束位点。推完开关后的真实姿态和预演会有偏差，
// 所以不复用预演帧，而是按真机实测关节就地重新规划（纯关节空间插值，
// 起点偏差自然在整段运动里被慢慢修掉——这段只是收手，精度要求不高）。
async function returnToWaypoint(wp) {
  const st = reach.status;
  const panel = state.panels[st.chain_id];
  if (!panel) {
    return;
  }
  reachMsg(`收回到「${wp.name}」规划中…`);
  let joints = readJointInputs(panel);
  try {
    const j = await fetchJson("/api/reach/joints");
    if (j.ok) {
      joints = j.named_joints;
    }
  } catch { /* 读不到真机就用面板值（纯模拟联调） */ }
  let seg;
  try {
    seg = await fetchJson("/api/trajectory/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        robot: state.activeRobot,
        chain_id: panel.chainId,
        current_joints: joints,
        target_joints: wp.named_joints,
        tcp_offset: readPose(panel, "tcp"),
        duration: 2.5,
        steps: 40,
        planner_type: panel.dom.planner.value,
      }),
    });
  } catch (error) {
    reachMsg(`收回规划失败: ${error.message}`, "error");
    return;
  }
  panel.frames = seg.waypoints;
  panel.frameIndex = 0;
  panel.currentCollision = seg.collision;
  updateCollisionMetrics(panel, seg.collision);
  updateTrajectoryLine(panel);
  visualizeCollision(panel, seg.collision);
  applyFrame(panel, 0);
  if (seg.collision?.status === "collision") {
    reachMsg("收回轨迹有碰撞，已停在当前位置（可手动卸力收回）", "error");
    return;
  }
  if (!st.armed) {
    replay(panel);
    reachMsg(`收回段已预演（未接管手臂）`, "success");
    return;
  }
  try {
    await fetchJson("/api/reach/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        waypoints: seg.waypoints.map((frame) => frame.named_joints),
        duration: 2.5,
        max_speed_rad_s: 0.4,   // 收回只是收手，精度要求低，放行到快档
        label: `收回:${wp.name}`,
      }),
    });
    reachMsg(`收回到「${wp.name}」中…`, "success");
    await pollReachExec();
  } catch (error) {
    reachMsg(`收回执行失败: ${error.message}`, "error");
  }
}

// 经由多个中间路点的分段规划：当前 → 路点1 → 路点2 → … → 目标。
// 路点间是纯关节空间插值，末段以最后一个路点为种子解 IK。
async function planViaWaypoints(panel, wps) {
  pause(panel);
  const names = wps.map((w) => `「${w.name}」`).join("→");
  setState(`经由 ${names} 分段规划中`, "warn");
  const currentJoints = readJointInputs(panel);
  const duration = Number(panel.dom.duration.value || 4);
  const steps = Math.max(20, Number(panel.dom.steps.value || 80));
  const nSeg = wps.length + 1;
  const segSteps = Math.max(10, Math.round(steps / nSeg));
  const segDur = duration / nSeg;

  const plan = (from, to) => fetchJson("/api/trajectory/plan", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      robot: state.activeRobot,
      chain_id: panel.chainId,
      current_joints: from,
      target_joints: to,
      tcp_offset: readPose(panel, "tcp"),
      duration: segDur,
      steps: segSteps,
      planner_type: panel.dom.planner.value,
    }),
  });

  try {
    let frames = null;
    let collision = null;
    let from = currentJoints;
    for (const wp of wps) {
      const seg = await plan(from, wp.named_joints);
      frames = frames ? [...frames, ...seg.waypoints.slice(1)] : seg.waypoints;
      collision = combineCollisionSummaries(collision, seg.collision);
      from = wp.named_joints;
    }

    // 末段：以最后一个路点为起点/种子解 IK，再规划到目标
    setJointInputs(panel, from);
    const ik = await solveIk(panel, { quiet: true });
    panel.currentIk = ik;
    const segEnd = await plan(from, ik.target_joints);

    panel.frames = [...frames, ...segEnd.waypoints.slice(1)];
    panel.frameIndex = 0;
    panel.currentCollision = combineCollisionSummaries(collision, segEnd.collision);
    updateCollisionMetrics(panel, panel.currentCollision);
    updateTrajectoryLine(panel);
    visualizeCollision(panel, panel.currentCollision);
    applyFrame(panel, 0);
    setState(`经由 ${wps.length} 个路点的轨迹已生成`, "success");
  } catch (error) {
    console.error(error);
    panel.frames = [];
    panel.currentIk = null;
    setState("分段规划失败", "error");
  }
}

function combineCollisionSummaries(a, b) {
  if (!a || !b) {
    return a || b || null;
  }
  const primary = (b.min_distance_m ?? 9e9) < (a.min_distance_m ?? 9e9) ? b : a;
  const status = (a.status === "collision" || b.status === "collision") ? "collision"
    : (a.status === "near" || b.status === "near") ? "near" : primary.status;
  return {
    ...primary,
    status,
    status_label: collisionStatusLabel(status),
    collision_count: (a.collision_count || 0) + (b.collision_count || 0),
    near_count: (a.near_count || 0) + (b.near_count || 0),
    checks: [...(a.checks || []), ...(b.checks || [])],
  };
}

// 调试：只画两个东西——法兰盘平面（手掌在腕上的安装面）和 TCP 点（p_tool 指尖）
// 都挂在腕 link 下，随手臂一起动。
function showFlangeDebug() {
  const chainId = reach.status?.chain_id;
  const pTool = reach.status?.p_tool;
  const panel = state.panels[chainId];
  if (!panel || !pTool) {
    return;
  }
  const wristGroup = state.linkGroups.get(panel.chain.end_link);
  const handGroup = state.linkGroups.get(chainId === "left_arm" ? "left_hand_link" : "right_hand_link");
  if (!wristGroup) {
    return;
  }
  if (reach.flangeDebugGroup) {
    reach.flangeDebugGroup.removeFromParent();
  }
  const group = new THREE.Group();
  reach.flangeDebugGroup = group;

  // 法兰盘位置 = hand_link 原点在腕系下的坐标（URDF 里 right_hand_joint 的 origin，约 x=0.058）
  const flangeLocal = new THREE.Vector3(0, 0, 0);
  if (handGroup) {
    wristGroup.updateWorldMatrix(true, false);
    handGroup.updateWorldMatrix(true, false);
    wristGroup.worldToLocal(handGroup.getWorldPosition(flangeLocal));
  }
  // 法兰盘平面法线 = 腕系 +x（hand_joint rpy 为 0，安装面即腕系 y-z 平面）
  const normal = new THREE.Vector3(1, 0, 0);

  const planeMat = new THREE.MeshBasicMaterial({
    color: 0x35d07f, transparent: true, opacity: 0.35,
    side: THREE.DoubleSide, depthTest: false,
  });
  const disc = new THREE.Mesh(new THREE.CircleGeometry(0.06, 40), planeMat);
  disc.quaternion.setFromUnitVectors(new THREE.Vector3(0, 0, 1), normal); // Circle 默认法线 +z
  disc.position.copy(flangeLocal);
  const ring = new THREE.Mesh(
    new THREE.RingGeometry(0.058, 0.062, 40),
    new THREE.MeshBasicMaterial({ color: 0x35d07f, side: THREE.DoubleSide, depthTest: false }),
  );
  ring.quaternion.copy(disc.quaternion);
  ring.position.copy(flangeLocal);
  const flangeLabel = makeLabelSprite("法兰盘平面");
  flangeLabel.position.copy(flangeLocal).add(new THREE.Vector3(0, 0, 0.09));
  group.add(disc, ring, flangeLabel);

  // TCP 点：标定出的 p_tool（腕系坐标，指尖）
  const tcp = new THREE.Mesh(
    new THREE.SphereGeometry(0.012, 20, 14),
    new THREE.MeshBasicMaterial({ color: 0xff4444, depthTest: false }),
  );
  tcp.position.set(...pTool);
  const tcpLabel = makeLabelSprite("TCP");
  tcpLabel.position.set(pTool[0], pTool[1], pTool[2] + 0.05);
  group.add(tcp, tcpLabel);

  // hand 碰撞胶囊（TCP 向法兰盘平面的垂足 → TCP，半径同 h2.yaml 里的 0.04）：常驻显示
  const tcpVec = new THREE.Vector3(...pTool);
  // 垂足：把 TCP 沿平面法线（腕系 +x）投影到法兰平面上
  const foot = tcpVec.clone().sub(
    normal.clone().multiplyScalar(tcpVec.clone().sub(flangeLocal).dot(normal)));
  const axis = tcpVec.clone().sub(foot);
  const capMat = new THREE.MeshStandardMaterial({
    color: 0x30343a, transparent: true, opacity: 0.45, roughness: 0.6,
  });
  const capRadius = 0.04;
  const shaft = new THREE.Mesh(
    new THREE.CylinderGeometry(capRadius, capRadius, Math.max(axis.length(), 1e-4), 20, 1, true), capMat);
  shaft.position.copy(foot).lerp(tcpVec, 0.5);
  shaft.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), axis.clone().normalize());
  const capA = new THREE.Mesh(new THREE.SphereGeometry(capRadius, 18, 12), capMat);
  capA.position.copy(foot);
  const capB = new THREE.Mesh(new THREE.SphereGeometry(capRadius, 18, 12), capMat);
  capB.position.copy(tcpVec);
  group.add(shaft, capA, capB);

  wristGroup.add(group);
  publishRenderState("法兰/TCP 调试");
}

// ---- 中间路点：卸力摆位 → 录制落盘 → 规划时经由 ----

async function refreshWaypoints() {
  let data;
  try {
    data = await fetchJson("/api/reach/waypoints");
  } catch {
    return;
  }
  reach.waypoints = data.waypoints || [];
  const fill = (sel, placeholder) => {
    const prev = sel.value;
    sel.innerHTML = `<option value="">${placeholder}</option>` + reach.waypoints
      .map((w) => `<option value="${w.file}">${w.name} · ${w.created_at || w.file}</option>`)
      .join("");
    if ([...sel.options].some((o) => o.value === prev)) {
      sel.value = prev;
    }
  };
  fill(reach.dom.waypointSel, "（直达）");
  fill(reach.dom.endSel, "（不收回）");
  reach.dom.delWp.disabled = !reach.waypoints.length;
  // 路点文件可能被删除，清掉队列里的失效项
  reach.viaList = (reach.viaList || []).filter((f) => reach.waypoints.some((w) => w.file === f));
  renderViaList();
}

function waypointByFile(file) {
  return file ? reach.waypoints?.find((w) => w.file === file) || null : null;
}

function selectedWaypoint() {
  return waypointByFile(reach.dom.waypointSel.value);
}

function selectedEndWaypoint() {
  return waypointByFile(reach.dom.endSel.value);
}

// ---- 经由队列：按加入顺序依次经过（空队列时退化为下拉框单选） ----

function addViaWaypoint() {
  const wp = selectedWaypoint();
  if (!wp) {
    reachMsg("先在下拉框选一个路点再按＋", "error");
    return;
  }
  reach.viaList = reach.viaList || [];
  reach.viaList.push(wp.file);
  renderViaList();
}

function renderViaList() {
  const box = reach.dom.viaList;
  const list = reach.viaList || [];
  if (!list.length) {
    box.classList.add("hidden");
    box.innerHTML = "";
    return;
  }
  box.classList.remove("hidden");
  box.innerHTML = "经由顺序: " + list.map((file, i) => {
    const w = waypointByFile(file);
    return `<span class="via-chip">${i + 1}. ${w ? w.name : file}` +
      `<button type="button" data-i="${i}" title="移除">×</button></span>`;
  }).join("");
  box.querySelectorAll("button").forEach((btn) => {
    btn.addEventListener("click", () => {
      reach.viaList.splice(Number(btn.dataset.i), 1);
      renderViaList();
    });
  });
}

// 规划要经过的路点序列：队列优先；队列为空则用下拉框的单选（老行为）
function viaWaypoints() {
  const fromQueue = (reach.viaList || []).map(waypointByFile).filter(Boolean);
  if (fromQueue.length) {
    return fromQueue;
  }
  const single = selectedWaypoint();
  return single ? [single] : [];
}

async function toggleHandMove() {
  const on = !reach.status.hand_move;
  if (on) {
    const ok = window.confirm(
      "确认卸力？\n\n手臂将失去支撑并下坠，请务必先用手扶住手臂！\n摆好位置后点「恢复保持」。");
    if (!ok) {
      return;
    }
  }
  try {
    const data = await fetchJson("/api/reach/hand_move", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ on }),
    });
    reach.status.hand_move = data.hand_move;
    reachMsg(data.message, on ? "error" : "success");
  } catch (error) {
    reachMsg(`卸力操作失败: ${error.message}`, "error");
  }
  updateReachArmUi();
}

async function recordWaypoint() {
  const name = window.prompt("路点名字（同名会覆盖）:", `中间点${(reach.waypoints?.length || 0) + 1}`);
  if (!name?.trim()) {
    return;
  }
  try {
    const data = await fetchJson("/api/reach/waypoints", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim() }),
    });
    await refreshWaypoints();
    reach.dom.waypointSel.value = data.waypoint.file;
    reachMsg(`已录制路点「${data.waypoint.name}」→ reach_waypoints/${data.waypoint.file}`, "success");
  } catch (error) {
    reachMsg(`录制失败: ${error.message}`, "error");
  }
}

async function deleteWaypoint() {
  const wp = selectedWaypoint();
  if (!wp) {
    reachMsg("先在下拉框选中要删除的路点", "error");
    return;
  }
  if (!window.confirm(`删除路点文件 ${wp.file}？`)) {
    return;
  }
  try {
    await fetchJson(`/api/reach/waypoints/${encodeURIComponent(wp.file)}`, { method: "DELETE" });
    await refreshWaypoints();
    reachMsg(`已删除路点「${wp.name}」`, "success");
  } catch (error) {
    reachMsg(`删除失败: ${error.message}`, "error");
  }
}

async function scanObstacles() {
  reach.dom.scan.disabled = true;
  reachMsg("扫描环境障碍中…");
  try {
    const data = await fetchJson("/api/reach/scan_obstacles", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({}),
    });
    await refreshObstacles();
    reachMsg(`障碍扫描完成：${data.count} 个体素（${(data.voxel_m * 100).toFixed(0)}cm），已加入碰撞检查`, "success");
  } catch (error) {
    reachMsg(`扫描失败: ${error.message}`, "error");
  } finally {
    reach.dom.scan.disabled = false;
  }
}

async function clearObstacles() {
  try {
    await fetchJson("/api/reach/clear_obstacles", { method: "POST" });
    await refreshObstacles();
    reachMsg("环境障碍已清除", "success");
  } catch (error) {
    reachMsg(`清除失败: ${error.message}`, "error");
  }
}

async function refreshObstacles() {
  let data;
  try {
    data = await fetchJson("/api/reach/obstacles");
  } catch {
    return;
  }
  renderObstacles(data);
  if (reach.dom) {
    reach.dom.clearObs.disabled = !data.count;
  }
}

function renderObstacles(data) {
  if (!reach.obstacleGroup.parent) {
    state.helperRoot.add(reach.obstacleGroup);
  }
  reach.obstacleGroup.clear();
  if (!data.count) {
    publishRenderState("障碍清除");
    return;
  }
  const size = data.voxel_m;
  const material = new THREE.MeshStandardMaterial({
    color: 0x2f8fd9,
    transparent: true,
    opacity: 0.22,
    depthWrite: false,
    roughness: 0.8,
  });
  const instanced = new THREE.InstancedMesh(
    new THREE.BoxGeometry(size, size, size), material, data.count);
  const m = new THREE.Matrix4();
  data.centers.forEach((center, index) => {
    m.setPosition(...xyzToScene(center));
    instanced.setMatrixAt(index, m);
  });
  instanced.instanceMatrix.needsUpdate = true;
  reach.obstacleGroup.add(instanced);
  publishRenderState("障碍更新");
}

function describeReachCollision(panel, collision) {
  if (!collision || (collision.status !== "collision" && collision.status !== "near")) {
    return [];
  }
  const lines = [];
  if (collision.pair) {
    lines.push(`  ↳ 最近对象: ${shortCollisionName(collision.pair.a)} ↔ ` +
      `${shortCollisionName(collision.pair.b)}` +
      (Number.isFinite(Number(collision.min_distance_mm))
        ? ` · 最小距离 ${Number(collision.min_distance_mm).toFixed(0)} mm` : ""));
  }
  const bad = panel.frames
    .map((frame, idx) => ({ idx, status: frame.collision?.status }))
    .filter((item) => item.status === "collision");
  if (bad.length) {
    const duration = Math.max(1, Number(reach.dom.duration.value || 6));
    const t0 = (bad[0].idx / (panel.frames.length - 1)) * duration;
    const t1 = (bad[bad.length - 1].idx / (panel.frames.length - 1)) * duration;
    lines.push(`  ↳ 碰撞帧 ${bad.length}/${panel.frames.length}` +
      `（约 ${t0.toFixed(1)}s ~ ${t1.toFixed(1)}s，3D 轨迹红点段）`);
  }
  return lines;
}

async function executeReach(options = {}) {
  const st = reach.status;
  const panel = state.panels[st.chain_id];
  if (!panel?.frames?.length || !reach.lastPick) {
    return;
  }
  const ik = panel.currentIk;
  const duration = Math.max(1, Number(reach.dom.duration.value || 6));
  const stepCm = Number(reach.dom.stepLen.value || 0);
  const stepMode = Boolean(reach.dom.stepMode?.checked);
  // 主段 = 取点规划的到位轨迹（预演 frames 可能已拼了横移预览段，真机不直接跑它）
  const mainFrames = (reach.execFrames && reach.execFrames[0] === panel.frames[0])
    ? reach.execFrames : panel.frames;
  const sidestepNote = stepMode
    ? "分段模式：到位后暂停，横移/收回需手动点「继续」\n"
    : (stepCm && reach.plane)
      ? `到位后将沿电柜表面${stepCm > 0 ? "左" : "右"}移 ${Math.abs(stepCm).toFixed(0)}cm\n`
      : "";
  const endWp = selectedEndWaypoint();
  const endNote = (endWp && !stepMode) ? `结束后收回到「${endWp.name}」\n` : "";
  const pt = reach.lastPick.p_torso;
  // 精定位自动执行时跳过确认框：全屏选点的确认已当过安全确认
  const ok = options.skipConfirm || window.confirm(
    "确认真机执行？手臂将开始运动！\n\n" +
    `目标(躯干系): [${pt.map((v) => v.toFixed(3)).join(", ")}] m\n` +
    `IK 误差: ${Number(ik?.error_mm || 0).toFixed(1)} mm\n` +
    `轨迹: ${mainFrames.length} 点 / ${duration}s\n` +
    sidestepNote + endNote +
    "\n请确保周围无人无障碍，手不要放在运动路径上。");
  if (!ok) {
    return;
  }
  hideStepNext();
  const fine = Boolean(reach.finePick);
  reach.finePick = false;   // 直达标志一次性消费：下次取点恢复经由路点
  reach.dom.exec.disabled = true;
  try {
    await fetchJson("/api/reach/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        waypoints: mainFrames.map((frame) => frame.named_joints),
        duration,
        label: fine ? "主轨迹(精定位)" : "主轨迹",
      }),
    });
    reachMsg("真机执行中…", "success");
    const final = await pollReachExec();
    if (!final?.message?.startsWith("完成")) {
      return; // 主段没成功：手臂留在原处等人处理，不自动接后续段
    }
    // 分段模式：到这里就停，后续横移/收回等人点「继续」
    if (stepMode) {
      showStepNext();
      return;
    }
    // 到位后可选的沿面横移（左移(cm) ≠ 0 时；已在上面的确认框里一并确认过）
    if (stepCm && reach.plane) {
      await sidestepReach(stepCm, { skipConfirm: true });
    }
    // 可选收尾：回到结束位点
    if (endWp) {
      await returnToWaypoint(endWp);
    }
  } catch (error) {
    reachMsg(`执行请求失败: ${error.message}`, "error");
    reach.dom.exec.disabled = false;
  }
}

// ---- 分段模式：主段到位后暂停，横移/收回由人逐段点击触发 ----
// 暂停期间可以随时改左移(cm)/推力(N)等参数，点「继续横移」时按最新值执行，
// 方便专注调某一段（比如反复调"手上去"的主段，不被后续动作打扰）。

function showStepNext() {
  const d = reach.dom;
  const stepCm = Number(d.stepLen.value || 0);
  d.nextSide.textContent = stepCm
    ? `继续${stepCm > 0 ? "左" : "右"}移 ${Math.abs(stepCm).toFixed(0)}cm`
    : "继续横移";
  const endWp = selectedEndWaypoint();
  d.nextReturn.textContent = endWp ? `收回到「${endWp.name}」` : "收回到结束位点";
  d.stepNext.classList.remove("hidden");
  reachMsg("主段到位，已暂停（分段模式）", "success");
}

function hideStepNext() {
  reach.dom?.stepNext?.classList.add("hidden");
}

function setStepNextBusy(busy) {
  const d = reach.dom;
  [d.nextSide, d.nextReturn, d.nextDone].forEach((btn) => { btn.disabled = busy; });
}

// 「再次选点」= 人当视觉闭环：粗定位后躯干已扭到新姿态，用现在的相机再点一次
// 开关，从当前姿态直达新点（几厘米的小移动），把躯干漂移和落点误差一起修掉。
// 交互：直接进全屏选点，确认即真机执行；Esc/退出则回到弹窗四选项。
function stepNextRepick() {
  reach.finePick = true;
  hideStepNext();
  openReachFullscreen();
  reachMsg("再次选点：全屏中点击新目标，确认后直接真机执行（Esc 返回）", "success");
}

async function stepNextSidestep() {
  const stepCm = Number(reach.dom.stepLen.value || 0);
  if (!stepCm) {
    reachMsg("左移(cm) 为 0，没有可执行的横移", "error");
    return;
  }
  if (!reach.plane) {
    reachMsg("还没有拟合出表面平面，无法横移", "error");
    return;
  }
  setStepNextBusy(true);
  try {
    await sidestepReach(stepCm, { skipConfirm: true });
    showStepNext(); // 刷新按钮文案，保持暂停态：还可以再横移或收回
  } finally {
    setStepNextBusy(false);
  }
}

async function stepNextReturn() {
  const endWp = selectedEndWaypoint();
  if (!endWp) {
    reachMsg("先在「结束位点」下拉框选一个路点", "error");
    return;
  }
  setStepNextBusy(true);
  try {
    await returnToWaypoint(endWp);
    hideStepNext();
  } finally {
    setStepNextBusy(false);
  }
}

async function pollReachExec() {
  for (;;) {
    await new Promise((resolve) => setTimeout(resolve, 150));
    let status;
    try {
      status = await fetchJson("/api/reach/exec_status");
    } catch {
      continue;
    }
    if (status.running) {
      reachMsg(`真机执行中… ${(status.progress * 100).toFixed(0)}%`, "success");
    } else {
      reachMsg(status.message || "执行结束", status.message?.includes("错") ? "error" : "success");
      reach.dom.exec.disabled = false;
      refreshReachDiag();
      return status;
    }
  }
}

async function stopReach() {
  hideStepNext();
  try {
    await fetchJson("/api/reach/stop", { method: "POST" });
    reachMsg("已急停（手臂刚性保持当前位置）", "error");
  } catch (error) {
    reachMsg(`急停请求失败: ${error.message}`, "error");
  }
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
    attachViewerFrames(metadata);
    for (const panel of Object.values(state.panels)) {
      setJointInputs(panel, panel.chain.default_current_joints);
      Object.assign(state.robotJointState, panel.chain.default_current_joints);
      writePose(panel, "tcp", panel.chain.default_tcp_offset);
    }
    setRobotJoints(state.robotJointState, false);
    updateGroundFrame();
    for (const panel of Object.values(state.panels)) {
      ensureTargetHand(panel);
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
    collisionGroup: new THREE.Group(),
    skeletonGroup: new THREE.Group(),
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

  state.helperRoot.add(panel.targetGroup, panel.tcpGroup, panel.trajectoryGroup, panel.collisionGroup, panel.skeletonGroup);
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
    panel.collisionGroup.clear();
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
  panel.dom.replay.addEventListener("click", () => replay(panel));
  panel.dom.pause.addEventListener("click", () => pause(panel));
  panel.dom.step.addEventListener("click", () => stepFrame(panel));
  panel.dom.speed.addEventListener("input", () => {
    panel.dom.speedLabel.textContent = `${Number(panel.dom.speed.value).toFixed(2)}x`;
  });
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
  panel.collisionGroup.clear();
  updateCollisionMetrics(panel, null);
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
    solver_options: panel.solverOptions || {},
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
    visualizeCollision(panel, data.collision);
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

function visualizeCollision(panel, collision) {
  panel.collisionGroup.clear();
  const checks = collision?.checks || [];
  const offending = checks.filter((c) => c.status === "collision");
  const nearOnly = !offending.length;
  const frames = offending.length
    ? offending
    : checks.filter((c) => c.status === "near");
  if (!frames.length) {
    return;
  }

  // 最严重的一帧：把相撞的两个几何体都画出来
  const worst = frames.reduce((a, b) =>
    (a.min_distance_m ?? 1e9) <= (b.min_distance_m ?? 1e9) ? a : b);
  const color = nearOnly ? 0xd9ab34 : 0xc53f3f;
  const strong = transparentMaterial(color, 0.4);
  strong.depthWrite = false;
  if (worst.pair && worst.shapes) {
    for (const name of [worst.pair.a, worst.pair.b]) {
      const shape = worst.shapes[name];
      if (shape) {
        panel.collisionGroup.add(primitiveMesh(shape, strong));
      }
    }
  }

  // 碰撞段：手臂侧几何体沿轨迹采样叠加，形成"扫过的红色体积"
  if (!nearOnly && worst.pair) {
    const faint = transparentMaterial(color, 0.12);
    faint.depthWrite = false;
    const step = Math.max(1, Math.floor(offending.length / 10));
    for (let i = 0; i < offending.length; i += step) {
      const check = offending[i];
      const shape = check.shapes?.[check.pair?.a || worst.pair.a];
      if (shape && check !== worst) {
        panel.collisionGroup.add(primitiveMesh(shape, faint));
      }
    }
  }
  publishRenderState("碰撞可视化");
}

function primitiveMesh(shape, material) {
  if (shape.kind === "sphere") {
    const mesh = new THREE.Mesh(new THREE.SphereGeometry(shape.radius, 20, 14), material);
    mesh.position.fromArray(xyzToScene(shape.center));
    return mesh;
  }
  if (shape.kind === "box") {
    const [hx, hy, hz] = shape.half_extents;
    const mesh = new THREE.Mesh(new THREE.BoxGeometry(hx * 2, hy * 2, hz * 2), material);
    const r = shape.rotation;
    const m = new THREE.Matrix4().set(
      r[0][0], r[0][1], r[0][2], 0,
      r[1][0], r[1][1], r[1][2], 0,
      r[2][0], r[2][1], r[2][2], 0,
      0, 0, 0, 1,
    );
    mesh.quaternion.setFromRotationMatrix(m);
    mesh.position.fromArray(xyzToScene(shape.center));
    return mesh;
  }
  if (shape.kind === "capsule") {
    const group = new THREE.Group();
    const a = new THREE.Vector3().fromArray(xyzToScene(shape.a));
    const b = new THREE.Vector3().fromArray(xyzToScene(shape.b));
    const length = a.distanceTo(b);
    const capA = new THREE.Mesh(new THREE.SphereGeometry(shape.radius, 16, 12), material);
    capA.position.copy(a);
    const capB = new THREE.Mesh(new THREE.SphereGeometry(shape.radius, 16, 12), material);
    capB.position.copy(b);
    const cyl = new THREE.Mesh(new THREE.CylinderGeometry(shape.radius, shape.radius, Math.max(length, 1e-4), 16, 1, true), material);
    cyl.position.copy(a).lerp(b, 0.5);
    cyl.quaternion.setFromUnitVectors(new THREE.Vector3(0, 1, 0), b.clone().sub(a).normalize());
    group.add(capA, capB, cyl);
    return group;
  }
  return new THREE.Group();
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

function attachViewerFrames(metadata) {
  for (const frame of metadata.viewer_frames || []) {
    const linkGroup = state.linkGroups.get(frame.link);
    if (!linkGroup) {
      console.warn(`viewer_frames: link ${frame.link} 不存在，跳过 ${frame.name}`);
      continue;
    }
    const group = new THREE.Group();
    group.name = `viewer_frame_${frame.name || "unnamed"}`;
    if (frame.T) {
      const m = new THREE.Matrix4();
      m.set(...frame.T.flat());
      group.matrixAutoUpdate = false;
      group.matrix.copy(m);
    } else {
      applyPose(group, { xyz: frame.xyz || [0, 0, 0], rpy: frame.rpy || [0, 0, 0] });
    }
    group.add(new THREE.AxesHelper(Number(frame.axis_length || 0.1)));
    if (frame.frustum) {
      group.add(buildFrustumLines(frame.frustum, frame.color || "#bf7fff"));
    }
    if (frame.name) {
      group.add(makeLabelSprite(frame.name));
    }
    linkGroup.add(group);
  }
}

function buildFrustumLines(frustum, color) {
  const depth = Number(frustum.depth || 0.4);
  const { fx, fy, cx, cy, width, height } = frustum;
  const corners = [[0, 0], [width, 0], [width, height], [0, height]].map(
    ([u, v]) => new THREE.Vector3(((u - cx) / fx) * depth, ((v - cy) / fy) * depth, depth),
  );
  const origin = new THREE.Vector3();
  const points = [];
  for (const corner of corners) {
    points.push(origin.clone(), corner.clone());
  }
  for (let i = 0; i < 4; i += 1) {
    points.push(corners[i].clone(), corners[(i + 1) % 4].clone());
  }
  const geometry = new THREE.BufferGeometry().setFromPoints(points);
  return new THREE.LineSegments(geometry, new THREE.LineBasicMaterial({ color, transparent: true, opacity: 0.75 }));
}

function makeLabelSprite(text) {
  const canvas = document.createElement("canvas");
  canvas.width = 256;
  canvas.height = 64;
  const ctx = canvas.getContext("2d");
  ctx.font = "bold 36px sans-serif";
  ctx.fillStyle = "#e8ecf2";
  ctx.textAlign = "center";
  ctx.textBaseline = "middle";
  ctx.fillText(text, 128, 32);
  const sprite = new THREE.Sprite(new THREE.SpriteMaterial({
    map: new THREE.CanvasTexture(canvas),
    transparent: true,
    depthTest: false,
  }));
  sprite.scale.set(0.2, 0.05, 1);
  sprite.position.set(0, 0.05, 0);
  return sprite;
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
