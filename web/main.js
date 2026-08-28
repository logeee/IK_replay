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
  sequences: [],
  picking: false,
  pendingClick: null,
  plane: null,
  planeGroup: new THREE.Group(),
  execFrames: null, // 真机执行只跑主段（预演 frames 可能拼了横移预览段）
  finePick: false,  // 弹窗「再次选点」置位：下一次取点直达（跳过经由路点），执行时消费
  sideCache: null,  // 分段暂停时预取的横移规划 {stepCm, joints, seg}
  sidesteps: [],    // 落盘的横移录制（免 IK 回放）
  libraryMode: null, // 分类选择弹窗当前在选 waypoint 还是 sequence
  pickRevision: 0,  // 18001 最近选点版本，供不同电脑上的浏览器同步
  pickSyncTimer: null,
  pickSyncApplying: false,
};
const START_TEST_WAYPOINT_NAME = "起手点测试";

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
    planLeft: document.getElementById("reachPlanLeftBtn"),
    exec: document.getElementById("reachExecBtn"),
    stop: document.getElementById("reachStopBtn"),
    scan: document.getElementById("reachScanBtn"),
    clearObs: document.getElementById("reachClearObsBtn"),
    waypointSel: document.getElementById("reachWaypointSel"),
    addVia: document.getElementById("reachAddViaBtn"),
    viaList: document.getElementById("reachViaList"),
    endSel: document.getElementById("reachEndSel"),
    gotoSel: document.getElementById("reachGotoSel"),
    gotoPick: document.getElementById("reachGotoPickBtn"),
    gotoBtn: document.getElementById("reachGotoBtn"),
    startTest: document.getElementById("reachStartTestBtn"),
    seqSel: document.getElementById("reachSeqSel"),
    seqPick: document.getElementById("reachSeqPickBtn"),
    seqRun: document.getElementById("reachSeqRunBtn"),
    seqSave: document.getElementById("reachSeqSaveBtn"),
    seqDel: document.getElementById("reachSeqDelBtn"),
    seqMargin: document.getElementById("reachSeqMargin"),
    handMove: document.getElementById("reachHandMoveBtn"),
    record: document.getElementById("reachRecordBtn"),
    delWp: document.getElementById("reachDelWpBtn"),
    stepLen: document.getElementById("reachStepLen"),
    pushForce: document.getElementById("reachPushForce"),
    stepMode: document.getElementById("reachStepMode"),
    collisionCheck: document.getElementById("reachCollisionCheck"),
    stepNext: document.getElementById("reachStepNext"),
    nextSide: document.getElementById("reachNextSideBtn"),
    nextSideR: document.getElementById("reachNextSideRBtn"),
    nextPick: document.getElementById("reachNextPickBtn"),
    nextReturn: document.getElementById("reachNextReturnBtn"),
    nextDone: document.getElementById("reachNextDoneBtn"),
    msg: document.getElementById("reachMsg"),
    diag: document.getElementById("reachDiag"),
    fsBtn: document.getElementById("reachFullscreenBtn"),
    pointcloudBtn: document.getElementById("reachPointcloudBtn"),
    fsOverlay: document.getElementById("reachFsOverlay"),
    fsVideo: document.getElementById("reachFsVideo"),
    fsMark: document.getElementById("reachFsMark"),
    fsClose: document.getElementById("reachFsCloseBtn"),
    libraryModal: document.getElementById("reachLibraryModal"),
    libraryTitle: document.getElementById("reachLibraryTitle"),
    libraryGroup: document.getElementById("reachLibraryGroup"),
    libraryItem: document.getElementById("reachLibraryItem"),
    libraryHint: document.getElementById("reachLibraryHint"),
    libraryConfirm: document.getElementById("reachLibraryConfirmBtn"),
    libraryCancel: document.getElementById("reachLibraryCancelBtn"),
    libraryClose: document.getElementById("reachLibraryCloseBtn"),
  };
  const d = reach.dom;
  d.panel.classList.remove("hidden");
  d.video.src = "/api/reach/stream";
  d.video.addEventListener("click", onReachVideoClick);
  d.collapse.addEventListener("click", () => d.body.classList.toggle("hidden"));
  d.arm.addEventListener("click", () => toggleReachArm());
  d.replan.addEventListener("click", () => runReachPlan());
  d.planLeft.addEventListener("click", () => planReachLeft());
  d.exec.addEventListener("click", () => executeReach());
  d.stop.addEventListener("click", () => stopReach());
  d.scan.addEventListener("click", () => scanObstacles());
  d.clearObs.addEventListener("click", () => clearObstacles());
  d.handMove.addEventListener("click", () => toggleHandMove());
  // 卸力摆位时按空格 = 恢复保持（一只手扶着手臂时够不着鼠标）
  window.addEventListener("keydown", (e) => {
    if (e.code !== "Space" || !reach.status?.hand_move) {
      return;
    }
    const t = e.target;
    if (t && (t.tagName === "INPUT" || t.tagName === "TEXTAREA"
              || t.tagName === "SELECT" || t.isContentEditable)) {
      return;
    }
    e.preventDefault();   // 别让空格滚动页面
    toggleHandMove();     // hand_move=true → 走"恢复保持"分支，无确认弹窗
  });
  // 卸力摆位时鼠标右键 = 恢复保持（与空格等效；此状态下屏蔽右键菜单/视角平移）
  window.addEventListener("contextmenu", (e) => {
    if (!reach.status?.hand_move) {
      return;               // 非卸力状态：右键保持原有行为（菜单/平移视角）
    }
    e.preventDefault();
    e.stopPropagation();
    toggleHandMove();
  }, true);
  d.record.addEventListener("click", () => recordWaypoint());
  d.delWp.addEventListener("click", () => deleteWaypoint());
  d.addVia.addEventListener("click", () => addViaWaypoint());
  d.gotoPick.addEventListener("click", () => openReachLibrary("waypoint"));
  d.startTest.addEventListener("click", () => gotoStartTestWaypoint());
  d.seqPick.addEventListener("click", () => openReachLibrary("sequence"));
  d.libraryGroup.addEventListener("change", () => populateReachLibraryItems());
  d.libraryItem.addEventListener("change", () => updateReachLibraryHint());
  d.libraryConfirm.addEventListener("click", () => confirmReachLibrarySelection());
  d.libraryCancel.addEventListener("click", () => closeReachLibrary());
  d.libraryClose.addEventListener("click", () => closeReachLibrary());
  d.libraryModal.addEventListener("click", (event) => {
    if (event.target === d.libraryModal) closeReachLibrary();
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !d.libraryModal.classList.contains("hidden")) {
      closeReachLibrary();
    }
  });
  // 路点当终点：不从图像取点，从当前姿态直接去选中路点（关节插值，无 IK）
  d.gotoBtn.addEventListener("click", async () => {
    const wp = waypointByFile(d.gotoSel.value);
    if (!wp) {
      reachMsg("先在「路点终点」下拉框选一个路点", "error");
      return;
    }
    if (reach.status.armed
        && !window.confirm(`确认真机运动到路点「${wp.name}」？\n（从当前姿态关节插值直达）`)) {
      return;
    }
    d.gotoBtn.disabled = true;
    try {
      await moveToWaypoint(wp);
    } finally {
      d.gotoBtn.disabled = false;
    }
  });
  d.seqRun.addEventListener("click", (e) => runSequence(e.shiftKey));
  d.seqSel.addEventListener("change", updateSequenceUi);
  d.seqSave.addEventListener("click", () => saveSequence());
  d.seqDel.addEventListener("click", () => deleteSequence());
  d.nextSide.addEventListener("click", () => stepNextSidestep());
  d.nextSideR.addEventListener("click", () => stepNextSidestep(true));
  // 暂停期间改左移距离：刷新按钮文案并重新预取横移规划
  d.stepLen.addEventListener("change", () => {
    if (!d.stepNext.classList.contains("hidden")) {
      showStepNext();
    }
  });
  d.nextPick.addEventListener("click", () => stepNextRepick());
  d.nextReturn.addEventListener("click", () => stepNextReturn());
  d.nextDone.addEventListener("click", () => hideStepNext());
  d.fsBtn.addEventListener("click", () => openReachFullscreen());
  d.pointcloudBtn.addEventListener("click", () => {
    const url = new URL(window.location.href);
    url.port = "7005";
    url.pathname = "/";
    url.search = "";
    url.searchParams.set(
      "approach_offset_m",
      String(Number(d.offset.value || 0)),
    );
    window.open(url.toString(), "ik-replay-pointcloud", "width=1500,height=920");
  });
  window.addEventListener("message", async (event) => {
    if (event.data?.type !== "ik-replay-pointcloud-pick") return;
    try {
      if (new URL(event.origin).hostname !== window.location.hostname) return;
    } catch {
      return;
    }
    await acceptPointcloudPick(event.data.pick, "点云窗口");
  });
  d.fsClose.addEventListener("click", () => cancelReachFullscreen());
  d.fsVideo.addEventListener("click", (ev) => onReachFullscreenClick(ev));
  state.helperRoot.add(reach.obstacleGroup, reach.planeGroup);
  await refreshObstacles();
  await refreshWaypoints();
  await refreshSequences();
  await refreshSidesteps();
  await syncReachJointsOnStartup();
  showFlangeDebug();
  updateReachArmUi();
  refreshReachDiag();
  const rms = status.calib?.rms_mm;
  const markerCount = Object.keys(status.p_tool_wrist_m_by_marker || {}).length;
  reachMsg(`标定: ${status.calib?.solved_at || "?"} · RMS ${rms ? rms.toFixed(2) : "?"} mm · ` +
    `TCP=p_tool [${(status.p_tool || []).map((v) => v.toFixed(3)).join(", ")}] m` +
    (markerCount ? ` · 手部关键点 ${markerCount} 个` : ""));
  startReachPickSync();
}

async function acceptPointcloudPick(pick, sourceLabel) {
  if (!pick?.ok || !Array.isArray(pick.p_root) || pick.p_root.length !== 3) {
    reachMsg("点云选点返回的数据无效", "error");
    return;
  }
  if (reach.pickSyncApplying) return;
  reach.pickSyncApplying = true;
  if (Number.isFinite(Number(pick.revision))) {
    reach.pickRevision = Math.max(reach.pickRevision, Number(pick.revision));
  }
  try {
    reach.lastPick = pick;
    reach.plane = pick.plane || null;
    visualizeSurfacePlane(pick);
    reach.dom.replan.disabled = false;
    reach.dom.planLeft.disabled = false;
    reachMsg(`已接收${sourceLabel}目标，开始 IK 预演…`);
    await planReachLeft();
  } finally {
    reach.pickSyncApplying = false;
  }
}

function scheduleReachPickSync(delayMs = 1000) {
  window.clearTimeout(reach.pickSyncTimer);
  reach.pickSyncTimer = window.setTimeout(pollReachPick, delayMs);
}

async function startReachPickSync() {
  try {
    const latest = await fetchJson("/api/reach/latest_pick");
    reach.pickRevision = Number(latest.revision || 0);
  } catch (error) {
    console.warn(`初始化跨浏览器选点同步失败: ${error.message}`);
  }
  scheduleReachPickSync();
}

async function pollReachPick() {
  if (reach.pickSyncApplying || reach.picking) {
    scheduleReachPickSync();
    return;
  }
  try {
    const latest = await fetchJson("/api/reach/latest_pick");
    const revision = Number(latest.revision || 0);
    if (revision > reach.pickRevision) {
      reach.pickRevision = revision;
      const isManualPointcloud = latest.available
        && latest.selection_mode === "frozen_rgbd_pointcloud"
        && latest.selection_source !== "flow-auto";
      if (isManualPointcloud) {
        await acceptPointcloudPick(latest, "远程点云");
      }
    }
  } catch (error) {
    console.warn(`跨浏览器选点同步失败: ${error.message}`);
  } finally {
    scheduleReachPickSync(document.hidden ? 3000 : 1000);
  }
}

async function syncReachJointsOnStartup() {
  const st = reach.status;
  const panel = state.panels[st?.chain_id];
  if (!st?.joints_available || !panel) {
    return;
  }
  try {
    const data = await fetchJson("/api/reach/joints");
    if (!data.ok) {
      throw new Error(data.error || "后端未返回关节数据");
    }
    setJointInputs(panel, data.named_joints);
    Object.assign(state.robotJointState, data.named_joints);
    setRobotJoints(state.robotJointState, false);
  } catch (error) {
    console.warn(`网页启动时同步真机关节失败: ${error.message}`);
  }
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
  updateSequenceUi();
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
      reach.dom.planLeft.disabled = false;
      // 取点后默认跑左侧规划（平移在先+中段抬高，不刮底）；「右侧规划」按钮保留老逻辑
      await planReachLeft();
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
  // 分段模式下横移/收回都是到位后手动触发、执行时就地重规划的，
  // 取点时预演它们纯属白算（各多一轮插值+逐帧碰撞检查），跳过提速
  const skipPreviews = direct || Boolean(reach.dom.stepMode?.checked);
  const viaWps = direct ? [] : viaWaypoints();
  panel.solverOptions = { solve_orientation: false }; // 指尖是点目标，姿态放开
  try {
    if (viaWps.length) {
      await planViaWaypoints(panel, viaWps);
    } else {
      await planTrajectory(panel, { checkCollision: reachCollisionOn() });
    }
  } finally {
    panel.solverOptions = null;
  }

  const ik = panel.currentIk;

  // 横移段并入预演：执行时仍分两段（先到位校正，再按真机实际姿态重规划横移）
  reach.execFrames = panel.frames;
  const stepCm = Number(reach.dom.stepLen.value || 0);
  let sidestepOk = false;
  if (!skipPreviews && ik?.success && stepCm && reach.plane && panel.frames.length > 1
      && panel.currentCollision?.status !== "collision") {
    sidestepOk = await appendSidestepPreview(panel, stepCm);
  }

  // 收回段也并入预演（执行时同样按真机实际姿态就地重规划）
  const endWpPreview = skipPreviews ? null : selectedEndWaypoint();
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
      (skipPreviews ? "（分段模式：执行时再规划）"
                    : sidestepOk ? "（已并入预演）" : "（横移段规划失败）")] : []),
    ...(endWpPreview ? [`结束后收回到「${endWpPreview.name}」` +
      (returnOk ? "（已并入预演）" : "（收回段规划失败）")] : []),
    ...(String(panel.currentPlanner || "").endsWith("+rrt")
      ? ["直线撞障 → 已改用 RRT 绕障路径（形状请看预演）"] : []),
    `IK: ${ik ? (ik.success ? "成功" : "未到达") : "失败"}` +
      (ik ? ` · 误差 ${Number(ik.error_mm).toFixed(1)} mm` : ""),
    `碰撞: ${collision?.status_label || "-"} · 轨迹点 ${panel.frames.length}` +
      (collision?.rrt_error ? `（RRT 绕障失败: ${collision.rrt_error}）` : ""),
  ];
  lines.push(...describeReachCollision(panel, collision));
  reach.dom.info.textContent = lines.join("\n");

  const usedRrt = String(panel.currentPlanner || "").endsWith("+rrt");
  const planned = ik?.success && panel.frames.length > 1 && collision?.status !== "collision";
  if (planned) {
    reach.dom.exec.disabled = !st.armed;
    const rrtNote = usedRrt ? "直线撞障，已自动改走 RRT 绕障路径——请在预演里确认形状。" : "";
    reachMsg(st.armed
      ? `${rrtNote}预演回放中，确认无误后点「真机执行」`
      : `${rrtNote}预演回放中（未接管手臂，先点「接管手臂」才能执行）`,
      usedRrt ? "warn" : "success");
    replay(panel);
  } else {
    reach.dom.exec.disabled = true;
    reachMsg(ik?.success === false
      ? "目标不可达（IK 未收敛），换个目标或调整姿态"
      : (collision?.status === "collision"
        ? `轨迹有碰撞，已禁止执行${collision?.rrt_error ? `（RRT 也绕不过去: ${collision.rrt_error}）` : ""}`
        : "规划失败"), "error");
  }
}

// 「左侧规划」：平移在先、进出在后（先竖直+水平对齐，最后才沿根系 ±x 进/出；
// 拔出则相反：先拔出到目标深度再平移）。与「右侧规划」共用同一条执行链。
async function planReachLeft() {
  const st = reach.status;
  const pick = reach.lastPick;
  const panel = state.panels[st.chain_id];
  if (!pick || !panel) {
    reachMsg("请先在画面中点击目标", "error");
    return;
  }
  reachMsg("左侧规划中（平移在先、进出在后）…");
  reach.dom.exec.disabled = true;

  // 起点：优先真机当前关节角（与右侧规划一致）
  let joints = readJointInputs(panel);
  if (st.joints_available) {
    try {
      const j = await fetchJson("/api/reach/joints");
      if (j.ok) {
        joints = j.named_joints;
        setJointInputs(panel, joints);
        Object.assign(state.robotJointState, joints);
        setRobotJoints(state.robotJointState, false);
      }
    } catch (error) {
      reachMsg(`读真机关节失败（用面板当前值代替）: ${error.message}`, "error");
    }
  }
  writePose(panel, "tcp", { xyz: st.p_tool, rpy: [0, 0, 0] });
  writePose(panel, "target", { xyz: xyzToScene(pick.p_root), rpy: readPose(panel, "target").rpy });
  updateTargetMarker(panel);
  if (panel.targetHandGroup) {
    panel.targetHandGroup.visible = false;
  }

  let res;
  try {
    const liftCm = parseFloat(document.getElementById("reachLiftCm")?.value);
    res = await fetchJson("/api/reach/plan_axis_last", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        start_joints: joints,
        target_root: pick.p_root,
        // 1cm 一步，别放宽：2cm 时关节插值与笛卡尔直线的偏差肉眼可见地
        // 影响到位精度。提速要从 FK/碰撞本身省，不能拿路径保真度换。
        step_m: 0.01,
        check_collision: reachCollisionOn(),
        lift_m: Number.isFinite(liftCm) ? Math.max(0, liftCm) / 100 : 0.02,
      }),
    });
  } catch (error) {
    reach.dom.exec.disabled = true;
    reachMsg(`左侧规划失败: ${error.message}`, "error");
    return;
  }

  panel.frames = res.waypoints;
  panel.frameIndex = 0;
  panel.currentPlanner = res.planner;
  // 逐步 IK 每步都收敛才会走到这里，用最大步误差充当 IK 指标（供确认框显示）
  panel.currentIk = { success: true, error_mm: res.max_ik_error_mm,
                      error_rotation: 0, iterations: 0 };
  panel.currentCollision = res.collision;
  if (res.collision) {
    updateCollisionMetrics(panel, res.collision);
    visualizeCollision(panel, res.collision);
  }
  updateTrajectoryLine(panel);
  applyFrame(panel, 0);
  reach.execFrames = panel.frames;

  const pt = pick.p_torso;
  const usedRrt = res.planner === "axis_last+rrt";
  const order = usedRrt
    ? "直线撞障 → 已改用 RRT 绕障路径（不再保证两段式形状）"
    : res.mode === "push_in" ? "平移（竖直+水平）→ 进给（+x 往里伸）"
                             : "拔出（-x）→ 平移（竖直+水平）";
  reach.dom.info.textContent = [
    `左侧规划：${order}`,
    `目标(躯干系) [${pt.map((v) => v.toFixed(3)).join(", ")}] m`,
    `中间点(根系) [${res.mid_root.map((v) => v.toFixed(3)).join(", ")}] m`,
    `最大 IK 步误差 ${Number(res.max_ik_error_mm).toFixed(1)} mm · 轨迹点 ${panel.frames.length}`,
    `碰撞: ${res.collision?.status_label || "未检查"}` +
      (res.collision?.rrt_error ? `（RRT 绕障失败: ${res.collision.rrt_error}）` : ""),
  ].join("\n");

  const planned = panel.frames.length > 1 && res.collision?.status !== "collision";
  if (planned) {
    reach.dom.exec.disabled = !st.armed;
    const rrtNote = usedRrt ? "直线撞障，已自动改走 RRT 绕障路径——请在预演里确认形状。" : "";
    reachMsg(st.armed
      ? `${rrtNote}左侧规划预演回放中，确认无误后点「真机执行」`
      : `${rrtNote}左侧规划预演回放中（未接管手臂，先点「接管手臂」才能执行）`,
      usedRrt ? "warn" : "success");
    replay(panel);
  } else {
    reach.dom.exec.disabled = true;
    reachMsg(res.collision?.status === "collision"
      ? `轨迹有碰撞，已禁止执行${res.collision?.rrt_error ? `（RRT 也绕不过去: ${res.collision.rrt_error}）` : ""}`
      : "规划失败", "error");
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

// 语义点云沿柜面 ±X 横移并向柜面 -Z 偏 10°；旧链路保留向下倾 2°。
const SIDESTEP_TILT_DEG = 2;
const WALL_SIDESTEP_DOWN_DEG = 10;

function sidestepDirection(sign) {
  const right = reach.plane.right_root;
  if (Array.isArray(right) && right.length === 3) {
    const wallUp = reach.plane.wall_up_root;
    if (!Array.isArray(wallUp) || wallUp.length !== 3) {
      throw new Error("柜面坐标系缺少 Z 轴，无法计算向下偏移");
    }
    const t = (WALL_SIDESTEP_DOWN_DEG * Math.PI) / 180;
    // X 正=右、Z 正=上；左右两种横移均叠加 -Z 方向 10°。
    return right.map((value, i) =>
      -value * sign * Math.cos(t) - wallUp[i] * Math.sin(t));
  }
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
      check_collision: reachCollisionOn(),
    }),
  });
}

// ---- 横移录制回放：逐点 IK 只算第一次，之后按"当前起点 + 录制关节增量"直接回放 ----

async function refreshSidesteps() {
  try {
    reach.sidesteps = (await fetchJson("/api/reach/sidesteps")).sidesteps || [];
  } catch { /* 后端没有该接口时静默降级为每次现算 */ }
}

// 按距离找录制（距离是查找键：6cm 的录制回放 6cm，没有对应录制自然退回现算）。
// 防呆检查已按用户要求全部注释掉，由人保证工况一致（机器人正视电柜、起点姿态
// 与录制时相近）。要恢复保护就取消下面两段注释。
function matchSidestepRecording(stepCm, joints) {
  // 柜面坐标系可提供准确的水平 X 轴时必须重新做笛卡尔规划。旧关节增量
  // 在不同起点不是刚体平移，正是“右移变右上”的来源，不能覆盖柜面轴。
  if (reach.plane?.horizontal_axis_source === "wall_coordinate_x") {
    return null;
  }
  const rec = (reach.sidesteps || []).find((r) => Number(r.step_cm) === stepCm);
  if (!rec?.waypoints?.length) {
    return null;
  }
  // 防呆一（已停用）：当前平面法向与录制时夹角 >10° 说明没正视电柜，不回放
  // if (reach.plane) {
  //   const dirNow = sidestepDirection(Math.sign(stepCm));
  //   const d0 = rec.direction_root || [];
  //   const dot = dirNow[0] * (d0[0] ?? 0) + dirNow[1] * (d0[1] ?? 0) + dirNow[2] * (d0[2] ?? 0);
  //   if (dot < Math.cos((10 * Math.PI) / 180)) {
  //     return null;
  //   }
  // }
  // 防呆二（已停用）：起点关节和录制时偏差 >0.1 rad，增量回放不再近似直线，不回放
  // const start = rec.waypoints[0].named_joints;
  // const drift = Math.max(...Object.keys(start)
  //   .map((k) => Math.abs(Number(joints[k] ?? 0) - Number(start[k]))));
  // if (drift > 0.1) {
  //   return null;
  // }
  return rec;
}

function buildSidestepReplay(rec, joints) {
  const q0 = rec.waypoints[0].named_joints;
  const waypoints = rec.waypoints.map((wp, i) => ({
    index: i,
    named_joints: Object.fromEntries(Object.keys(wp.named_joints).map((k) => [
      k, Number(joints[k] ?? 0) + (Number(wp.named_joints[k]) - Number(q0[k] ?? 0)),
    ])),
    tcp_pose: wp.tcp_pose,   // 录制时的指尖线，起点略有平移时仅作示意
  }));
  return { waypoints, steps: waypoints.length - 1, max_ik_error_mm: 0,
           collision: null, replayed: true };
}

function saveSidestepRecording(stepCm, seg) {
  fetchJson("/api/reach/sidesteps", {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify({
      step_cm: stepCm,
      direction_root: sidestepDirection(Math.sign(stepCm)),
      waypoints: seg.waypoints.map((wp) => ({
        named_joints: wp.named_joints, tcp_pose: wp.tcp_pose,
      })),
    }),
  }).then(() => refreshSidesteps()).catch(() => {});
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

  // 取横移轨迹，按优先级：①录制回放（免 IK，瞬时）②弹窗预取 ③现算逐点 IK
  let seg = null;
  const rec = matchSidestepRecording(stepCm, joints);
  if (rec) {
    seg = buildSidestepReplay(rec, joints);
  }
  const cache = reach.sideCache;
  reach.sideCache = null;
  if (!seg && cache && cache.stepCm === stepCm) {
    const drift = Math.max(...Object.keys(cache.joints)
      .map((k) => Math.abs(Number(joints[k] ?? 0) - Number(cache.joints[k]))));
    if (drift < 0.02) {
      seg = cache.seg;
    }
  }
  if (!seg) {
    try {
      seg = await planCartesianSidestep(joints, stepCm);
    } catch (error) {
      reachMsg(`${dirName}移规划失败: ${error.message}`, "error");
      return;
    }
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
  if (!seg.replayed && reach.plane?.horizontal_axis_source !== "wall_coordinate_x") {
    saveSidestepRecording(stepCm, seg);   // 落盘录制：下次同工况免 IK 直接回放
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
  const pointcloudPick = reach.lastPick?.selection_mode === "frozen_rgbd_pointcloud";
  let flipEvidence = null;
  if (pointcloudPick) {
    if (!reach.lastPick?.record) {
      reachMsg("7005 选点记录名尚未同步，无法关联拨动核验证据，请重新确认选点", "error");
      return;
    }
    flipEvidence = {
      record: reach.lastPick.record,
      flip_from: reach.lastPick.matched_detection_name || null,
    };
  }
  try {
    const started = await fetchJson("/api/reach/execute", {
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
        ...(flipEvidence ? { flip_evidence: flipEvidence } : {}),
      }),
    });
    const before = started.flip_evidence;
    const evidenceNote = before?.ok
      ? "；拨动前头部+右腕已存档"
      : before
        ? `；拨动前证据失败：${before.error || "未知错误"}`
        : "";
    reachMsg(
      `${dirName}移${seg.replayed ? "（回放录制轨迹）" : ""}执行中${evidenceNote}…`,
      before && !before.ok ? "error" : "success",
    );
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
        check_collision: reachCollisionOn(),
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
  return moveToWaypoint(wp, { verb: `收回到「${wp.name}」`, label: `收回:${wp.name}` });
}

function startTestWaypoint() {
  return (reach.waypoints || []).find(
    (waypoint) => String(waypoint.name || "").trim() === START_TEST_WAYPOINT_NAME,
  ) || null;
}

function ordinaryWaypoints() {
  return (reach.waypoints || []).filter(
    (waypoint) => String(waypoint.name || "").trim() !== START_TEST_WAYPOINT_NAME,
  );
}

async function gotoStartTestWaypoint() {
  const wp = startTestWaypoint();
  if (!wp) {
    reachMsg(`没有找到固定路点「${START_TEST_WAYPOINT_NAME}」`, "error");
    return;
  }
  if (reach.status.armed
      && !window.confirm(
        `确认真机运动到路点「${START_TEST_WAYPOINT_NAME}」？\n` +
        "（从当前姿态关节插值直达）",
      )) {
    return;
  }
  reach.dom.startTest.disabled = true;
  try {
    await moveToWaypoint(wp, {
      verb: `到达「${START_TEST_WAYPOINT_NAME}」`,
      label: `前往:${START_TEST_WAYPOINT_NAME}`,
    });
  } finally {
    reach.dom.startTest.disabled = !startTestWaypoint();
  }
}

// 前往路点：读真机实测关节 → 关节空间直线插值到路点（全程无 IK）→ 碰撞
// 预检 → 执行。收回段和动作序列的每一段都走这里。返回是否成功到位。
async function moveToWaypoint(wp, options = {}) {
  const st = reach.status;
  const panel = state.panels[st.chain_id];
  if (!panel) {
    return false;
  }
  const verb = options.verb || `前往「${wp.name}」`;
  reachMsg(`${verb}规划中…`);
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
        duration: options.duration ?? 2.5,
        steps: 40,
        planner_type: panel.dom.planner.value,
        check_collision: reachCollisionOn(),
      }),
    });
  } catch (error) {
    reachMsg(`${verb}规划失败: ${error.message}`, "error");
    return false;
  }
  panel.frames = seg.waypoints;
  panel.frameIndex = 0;
  panel.currentCollision = seg.collision;
  updateCollisionMetrics(panel, seg.collision);
  updateTrajectoryLine(panel);
  visualizeCollision(panel, seg.collision);
  applyFrame(panel, 0);
  if (seg.collision?.status === "collision") {
    reachMsg(`${verb}轨迹有碰撞，已停在当前位置（可手动卸力摆位）`, "error");
    return false;
  }
  if (!st.armed) {
    replay(panel);
    reachMsg(`${verb}已预演（未接管手臂，无法真机执行）`, "success");
    return false;
  }
  try {
    await fetchJson("/api/reach/execute", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        waypoints: seg.waypoints.map((frame) => frame.named_joints),
        duration: options.duration ?? 2.5,
        max_speed_rad_s: options.maxSpeed ?? 0.4,  // 回放段精度要求低，放行到快档
        label: options.label || `前往:${wp.name}`,
      }),
    });
    reachMsg(`${verb}中…`, "success");
    const final = await pollReachExec();
    return Boolean(final?.message?.startsWith("完成"));
  } catch (error) {
    reachMsg(`${verb}执行失败: ${error.message}`, "error");
    return false;
  }
}

// ---- 路点终点 / 动作序列分类选择弹窗 ----

function reachLibraryItems() {
  return reach.libraryMode === "waypoint"
    ? ordinaryWaypoints()
    : (reach.sequences || []);
}

function reachLibraryDistance(item) {
  const text = String(item?.name || item?.file || "").trim();
  const match = text.match(/^(\d+(?:\.\d+)?)/);
  return match ? Number(match[1]) : null;
}

function reachLibraryGroupOf(item) {
  const text = `${item?.name || ""} ${item?.file || ""}`;
  if (text.includes("左")) return "left";
  if (text.includes("右")) return "right";
  // 早期右侧数据没有写“右”，名称是“0.50-起手式新/终点”等。
  if (reachLibraryDistance(item) !== null && /(起手式|终点|避障)/.test(text)) {
    return "right";
  }
  return "other";
}

function compareReachLibraryItems(a, b) {
  const da = reachLibraryDistance(a);
  const db = reachLibraryDistance(b);
  if (da !== null && db !== null && da !== db) return da - db;
  if (da !== null && db === null) return -1;
  if (da === null && db !== null) return 1;
  return String(a.name || a.file).localeCompare(
    String(b.name || b.file), "zh-CN", { numeric: true },
  );
}

function reachLibrarySelection() {
  const file = reach.libraryMode === "waypoint"
    ? reach.dom.gotoSel.value
    : reach.dom.seqSel.value;
  return reachLibraryItems().find((item) => item.file === file) || null;
}

function openReachLibrary(mode) {
  reach.libraryMode = mode;
  const d = reach.dom;
  d.libraryTitle.textContent = mode === "waypoint"
    ? "选择路点终点"
    : "选择动作序列";

  const items = reachLibraryItems();
  const counts = { left: 0, right: 0, other: 0 };
  for (const item of items) counts[reachLibraryGroupOf(item)] += 1;
  const labels = { left: "左", right: "右", other: "其他" };
  for (const option of d.libraryGroup.options) {
    option.textContent = `${labels[option.value]}（${counts[option.value]}）`;
    option.disabled = counts[option.value] === 0;
  }

  const selected = reachLibrarySelection();
  const preferred = selected ? reachLibraryGroupOf(selected) : "left";
  const firstAvailable = ["left", "right", "other"].find((key) => counts[key] > 0);
  d.libraryGroup.value = counts[preferred] > 0 ? preferred : (firstAvailable || "other");
  populateReachLibraryItems(selected?.file || "");
  d.libraryModal.classList.remove("hidden");
  d.libraryGroup.focus();
}

function populateReachLibraryItems(preferredFile = "") {
  const d = reach.dom;
  const items = reachLibraryItems()
    .filter((item) => reachLibraryGroupOf(item) === d.libraryGroup.value)
    .sort(compareReachLibraryItems);
  d.libraryItem.innerHTML = "";
  for (const item of items) {
    const option = document.createElement("option");
    const distance = reachLibraryDistance(item);
    const suffix = reach.libraryMode === "sequence"
      ? ` · ${(item.waypoints || []).length}段`
      : "";
    option.value = item.file;
    option.textContent = `${distance === null ? "未标距离" : `${distance.toFixed(2)} m`} · ${item.name}${suffix}`;
    d.libraryItem.append(option);
  }
  if (preferredFile && items.some((item) => item.file === preferredFile)) {
    d.libraryItem.value = preferredFile;
  }
  d.libraryConfirm.disabled = items.length === 0;
  updateReachLibraryHint();
}

function updateReachLibraryHint() {
  const d = reach.dom;
  const item = reachLibraryItems().find(
    (candidate) => candidate.file === d.libraryItem.value,
  );
  if (!item) {
    d.libraryHint.textContent = "这个分类下暂时没有可选项目。";
    return;
  }
  const position = [...d.libraryItem.options].findIndex(
    (option) => option.value === item.file,
  ) + 1;
  d.libraryHint.textContent = `已按距离从近到远排序 · 第 ${position}/${d.libraryItem.options.length} 项 · ${item.file}`;
}

function confirmReachLibrarySelection() {
  const d = reach.dom;
  const file = d.libraryItem.value;
  if (!file) return;
  const target = reach.libraryMode === "waypoint" ? d.gotoSel : d.seqSel;
  target.value = file;
  target.dispatchEvent(new Event("change"));
  updateReachLibraryTriggerLabels();
  closeReachLibrary();
}

function closeReachLibrary() {
  reach.dom.libraryModal.classList.add("hidden");
  reach.libraryMode = null;
}

function updateReachLibraryTriggerLabels() {
  if (!reach.dom) return;
  const waypoint = waypointByFile(reach.dom.gotoSel.value);
  const sequence = sequenceByFile(reach.dom.seqSel.value);
  reach.dom.gotoPick.textContent = waypoint
    ? `终点：${waypoint.name}`
    : "选择路点终点…";
  reach.dom.gotoPick.title = waypoint?.file || "按左 / 右 / 其他分类选择路点终点";
  reach.dom.gotoPick.disabled = !ordinaryWaypoints().length;
  reach.dom.seqPick.textContent = sequence
    ? `序列：${sequence.name}`
    : "选择动作序列…";
  reach.dom.seqPick.title = sequence?.file || "按左 / 右 / 其他分类选择动作序列";
  reach.dom.seqPick.disabled = !(reach.sequences || []).length;
}

// ---- 动作序列：一组路点按序回放（纯关节插值，无 IK），存盘后一键调用 ----

async function refreshSequences() {
  let data;
  try {
    data = await fetchJson("/api/reach/sequences");
  } catch {
    return;
  }
  reach.sequences = data.sequences || [];
  const sel = reach.dom.seqSel;
  const prev = sel.value;
  sel.innerHTML = `<option value="">（未选择）</option>` + reach.sequences
    .map((s) => `<option value="${s.file}">${s.name} · ${(s.waypoints || []).length}段</option>`)
    .join("");
  if ([...sel.options].some((o) => o.value === prev)) {
    sel.value = prev;
  }
  updateSequenceUi();
  updateReachLibraryTriggerLabels();
}

function updateSequenceUi() {
  if (!reach.dom?.seqRun || !reach.dom?.seqSel) return;
  const seq = sequenceByFile(reach.dom.seqSel.value);
  updateReachLibraryTriggerLabels();
  reach.dom.seqDel.disabled = !seq;
  if (!seq) {
    reach.dom.seqRun.disabled = true;
    reach.dom.seqRun.textContent = "选择序列";
    reach.dom.seqRun.title = "请先选择动作序列";
    return;
  }
  if (!reach.status?.armed) {
    reach.dom.seqRun.disabled = true;
    reach.dom.seqRun.textContent = "先接管";
    reach.dom.seqRun.title = "动作序列需要先接管手臂";
    return;
  }
  const recorded = Boolean(seq.trajectory?.frames?.length);
  reach.dom.seqRun.disabled = false;
  reach.dom.seqRun.textContent = recorded ? "▶ 执行" : "规划预演";
  reach.dom.seqRun.title = recorded
    ? "使用已验证轨迹执行；按住 Shift 点击可强制重新规划"
    : "首次运行先规划并在三维视图预演，不会立即驱动真机";
}

// reach 各段规划是否做逐帧碰撞检查（默认关：录制/工况一致性由人保证，检查很慢）
function reachCollisionOn() {
  return Boolean(reach.dom?.collisionCheck?.checked);
}

function sequenceByFile(file) {
  return file ? reach.sequences?.find((s) => s.file === file) || null : null;
}

async function saveSequence() {
  const wps = viaWaypoints();
  if (!wps.length) {
    reachMsg("先用「＋」把路点按顺序加入经由队列，再存为序列", "error");
    return;
  }
  const name = window.prompt(
    `把以下顺序存为动作序列：\n${wps.map((w) => w.name).join(" → ")}\n\n序列名字:`);
  if (!name || !name.trim()) {
    return;
  }
  try {
    await fetchJson("/api/reach/sequences", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ name: name.trim(), waypoints: wps.map((w) => w.file) }),
    });
    await refreshSequences();
    reach.dom.seqSel.value = reach.sequences[0]?.file || "";
    updateSequenceUi();
    reachMsg(`序列「${name.trim()}」已保存（${wps.length} 段）`, "success");
  } catch (error) {
    reachMsg(`保存序列失败: ${error.message}`, "error");
  }
}

async function deleteSequence() {
  const seq = sequenceByFile(reach.dom.seqSel.value);
  if (!seq) {
    reachMsg("先在下拉框选一个序列", "error");
    return;
  }
  if (!window.confirm(`删除序列「${seq.name}」？（路点本身不会被删）`)) {
    return;
  }
  try {
    await fetchJson(`/api/reach/sequences/${encodeURIComponent(seq.file)}`, { method: "DELETE" });
    await refreshSequences();
    reachMsg(`已删除序列「${seq.name}」`, "success");
  } catch (error) {
    reachMsg(`删除失败: ${error.message}`, "error");
  }
}

// 执行序列：全部逻辑在后端 /api/reach/sequences/run。首次运行会用
// 「直线优先、撞了才 RRT」规划无碰撞轨迹并录进序列文件，之后直接
// 回放录制轨迹（免 RRT/IK/碰撞检查，请求即执行）。前端只是调用方之一，
// 以后无界面的自动化封装直接 POST 同一个接口即可。
async function runSequence(replan = false) {
  const seq = sequenceByFile(reach.dom.seqSel.value);
  if (!seq) {
    reachMsg("先在下拉框选一个动作序列", "error");
    return;
  }
  if (!reach.status?.armed) {
    reachMsg("动作序列需要先点击「接管手臂」", "error");
    updateSequenceUi();
    return;
  }
  const names = (seq.waypoints || [])
    .map((f) => waypointByFile(f)?.name || f).join(" → ");
  const ok = window.confirm(
    `确认执行序列「${seq.name}」？\n\n${names}\n` +
    (replan
      ? "【Shift】丢弃已录轨迹重新 RRT 规划：先仿真回放，确认后再按一次 ▶ 才真机执行。"
      : "首次运行只规划并仿真回放（确认后再按一次 ▶ 执行）；已录制则手臂立即开始运动！\n" +
        "按住 Shift 点 ▶ 可强制重新规划。"));
  if (!ok) {
    return;
  }
  const marginM = (Number(reach.dom.seqMargin?.value) || 0) / 100;
  reach.dom.seqRun.disabled = true;
  try {
    const resp = await fetch("/api/reach/sequences/run", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ file: seq.file, replan, margin_m: marginM }),
    });
    const res = await resp.json();
    if (!resp.ok || !res.ok) {
      // 规划失败时后端会指出撞的路点：把该姿态摆进三维视图并标红碰撞处
      if (res.bad_waypoint?.named_joints) {
        await showBadTargetPose(res.bad_waypoint.named_joints);
        reachMsg(`${res.error || "序列执行失败"}（已在三维视图中标出）`, "error");
      } else {
        reachMsg(`序列执行失败: ${res.error || resp.statusText}`, "error");
      }
      return;
    }
    if (res.preview) {
      // 规划完成但未执行：把轨迹装进三维视图自动回放，用户确认后再按 ▶
      const panel = state.panels[reach.status?.chain_id];
      if (panel && res.preview_frames?.length) {
        pause(panel);
        panel.frames = res.preview_frames;
        panel.frameIndex = 0;
        updateTrajectoryLine(panel);
        applyFrame(panel, 0);
        panel.playing = true;
        panel.lastFrameTime = performance.now();
      }
      reachMsg(`已规划并录制（${res.frames} 帧，执行约 ${Number(res.duration_s).toFixed(1)}s）。` +
        "正在三维视图仿真回放——确认无误后再按一次 ▶ 即真机执行", "warn");
      await refreshSequences();
      reach.dom.seqSel.value = seq.file;
      updateSequenceUi();
      return;
    }
    const how = res.replayed ? "回放录制轨迹" : "RRT 规划完成并已录制";
    reachMsg(`序列「${seq.name}」执行中…（${how}，~${Number(res.duration_s).toFixed(1)}s）`, "success");
    const final = await pollReachExec();
    reachMsg(final?.message?.startsWith("完成")
      ? `序列「${seq.name}」完成`
      : (final?.message || "执行结束"),
    final?.message?.startsWith("完成") ? "success" : "error");
  } catch (error) {
    reachMsg(`序列执行失败: ${error.message}`, "error");
  } finally {
    updateSequenceUi();
  }
}

// 把"撞的目标姿态"摆到三维视图里并渲染碰撞标记：借用 /api/trajectory/plan
// （起点=终点=该姿态，2 帧）拿到带碰撞详情的帧，复用既有可视化管线
async function showBadTargetPose(namedJoints) {
  const st = reach.status;
  const panel = state.panels[st.chain_id];
  if (!panel) {
    return;
  }
  try {
    const seg = await fetchJson("/api/trajectory/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({
        robot: state.activeRobot,
        chain_id: panel.chainId,
        current_joints: namedJoints,
        target_joints: namedJoints,
        tcp_offset: readPose(panel, "tcp"),
        duration: 0.1,
        steps: 2,
        planner_type: "linear",
        check_collision: true,
      }),
    });
    panel.frames = seg.waypoints;
    panel.frameIndex = 0;
    panel.currentCollision = seg.collision;
    updateCollisionMetrics(panel, seg.collision);
    updateTrajectoryLine(panel);
    visualizeCollision(panel, seg.collision);
    applyFrame(panel, 0);
  } catch { /* 可视化失败不影响错误提示 */ }
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
      check_collision: reachCollisionOn(),
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

// 法兰盘、TCP 和标定得到的手部颜色关键点都挂在腕 link 下，随 FK/预演一起动。
function showFlangeDebug() {
  const chainId = reach.status?.chain_id;
  const pTool = reach.status?.p_tool;
  const handMarkers = reach.status?.p_tool_wrist_m_by_marker || {};
  const referenceMarker = reach.status?.p_tool_reference_marker;
  const panel = state.panels[chainId];
  if (!panel || !pTool) {
    return;
  }
  const wristLink = reach.status?.wrist_link || panel.chain.end_link;
  const wristGroup = state.linkGroups.get(wristLink);
  const handGroup = state.linkGroups.get(chainId === "left_arm" ? "left_hand_link" : "right_hand_link");
  if (!wristGroup) {
    console.warn(`手部关键点腕部 link ${wristLink} 不存在`);
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
  group.add(disc, ring);

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
  const tcpVec = new THREE.Vector3(...pTool);
  let referenceIsTcp = false;
  for (const [markerId, xyz] of Object.entries(handMarkers)) {
    if (!Array.isArray(xyz) || xyz.length !== 3) {
      continue;
    }
    const point = new THREE.Vector3(...xyz);
    const isReference = markerId === referenceMarker;
    if (isReference && point.distanceTo(tcpVec) < 1e-6) {
      referenceIsTcp = true;
    }
    const dot = new THREE.Mesh(
      new THREE.SphereGeometry(0.009, 18, 12),
      new THREE.MeshBasicMaterial({
        color: markerColors[markerId] ?? 0xffffff,
        depthTest: false,
      }),
    );
    dot.position.copy(point);
    dot.renderOrder = 20;
    group.add(dot);
  }

  // TCP 不与颜色关键点重合时（当前为红蓝中点），单独画一个稍大的白点。
  if (!referenceIsTcp) {
    const tcp = new THREE.Mesh(
      new THREE.SphereGeometry(0.013, 20, 14),
      new THREE.MeshBasicMaterial({ color: 0xffffff, depthTest: false }),
    );
    tcp.position.copy(tcpVec);
    group.add(tcp);
  }

  // hand 碰撞胶囊（TCP 向法兰盘平面的垂足 → TCP，半径同 h2.yaml 里的 0.04）：常驻显示
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
  const regularWaypoints = ordinaryWaypoints();
  const fill = (sel, placeholder) => {
    const prev = sel.value;
    sel.innerHTML = `<option value="">${placeholder}</option>` + regularWaypoints
      .map((w) => `<option value="${w.file}">${w.name} · ${w.created_at || w.file}</option>`)
      .join("");
    if ([...sel.options].some((o) => o.value === prev)) {
      sel.value = prev;
    }
  };
  fill(reach.dom.waypointSel, "（直达）");
  fill(reach.dom.endSel, "（不收回）");
  fill(reach.dom.gotoSel, "（路点终点）");
  reach.dom.delWp.disabled = !regularWaypoints.length;
  reach.dom.startTest.disabled = !startTestWaypoint();
  updateReachLibraryTriggerLabels();
  // 路点文件可能被删除，清掉队列里的失效项
  reach.viaList = (reach.viaList || []).filter(
    (file) => regularWaypoints.some((waypoint) => waypoint.file === file),
  );
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
      "确认卸力？\n\n重力前馈会让手臂近似失重（推到哪停哪），但补偿有偏差时仍可能缓慢飘移，请用手护住。\n摆好位置后点「恢复保持」、按空格键或点鼠标右键。");
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
    reachMsg(`已录制路点「${data.waypoint.name}」→ data/waypoints/${data.waypoint.file}`, "success");
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
    const wallNote = data.wall_count
      ? `，拟合墙面补全 ${data.wall_count} 个（红色，含视野下方）`
      : "，未拟合出墙面（点太散或没对着柜子）";
    reachMsg(`障碍扫描完成：${data.count} 个体素（${(data.voxel_m * 100).toFixed(0)}cm）`
      + `${wallNote}，已全部加入碰撞检查`, "success");
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
    reach.dom.clearObs.disabled = !data.count && !data.wall_count;
  }
}

function renderObstacles(data) {
  if (!reach.obstacleGroup.parent) {
    state.helperRoot.add(reach.obstacleGroup);
  }
  reach.obstacleGroup.clear();
  if (!data.count && !data.wall_count) {
    publishRenderState("障碍清除");
    return;
  }
  const size = data.voxel_m;
  const addVoxels = (centers, color, opacity) => {
    if (!centers?.length) {
      return;
    }
    const material = new THREE.MeshStandardMaterial({
      color,
      transparent: true,
      opacity,
      depthWrite: false,
      roughness: 0.8,
    });
    const instanced = new THREE.InstancedMesh(
      new THREE.BoxGeometry(size, size, size), material, centers.length);
    const m = new THREE.Matrix4();
    centers.forEach((center, index) => {
      m.setPosition(...xyzToScene(center));
      instanced.setMatrixAt(index, m);
    });
    instanced.instanceMatrix.needsUpdate = true;
    reach.obstacleGroup.add(instanced);
  };
  addVoxels(data.centers, 0x2f8fd9, 0.28);          // 蓝：相机实际扫到的
  addVoxels(data.wall_centers, 0xd94b3a, 0.30);     // 红：拟合竖直墙补全（含视野下方）

  // 红色半透明平面：把"竖直墙"画成连续面，比体素更容易认（你标注的那条）。
  // 本查看器是 Z-up（地面 XY），PlaneGeometry 默认法线 +Z、铺在本地 XY。
  const plane = data.wall_plane;
  if (plane?.width_m > 0.05 && plane?.height_m > 0.05) {
    const geo = new THREE.PlaneGeometry(plane.width_m, plane.height_m);
    const mat = new THREE.MeshBasicMaterial({
      color: 0xd94b3a,
      transparent: true,
      opacity: 0.22,
      side: THREE.DoubleSide,
      depthWrite: false,
    });
    const mesh = new THREE.Mesh(geo, mat);
    const n = new THREE.Vector3(...plane.normal).normalize();     // 水平法向
    const up = new THREE.Vector3(0, 0, 1);                        // 机器人/场景竖直
    const xAxis = new THREE.Vector3().crossVectors(up, n);
    if (xAxis.lengthSq() < 1e-8) {
      xAxis.set(1, 0, 0);
    } else {
      xAxis.normalize();
    }
    const yAxis = new THREE.Vector3().crossVectors(n, xAxis).normalize();
    mesh.quaternion.setFromRotationMatrix(
      new THREE.Matrix4().makeBasis(xAxis, yAxis, n));
    mesh.position.set(...xyzToScene(plane.center));
    reach.obstacleGroup.add(mesh);
  }
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
  d.nextSideR.textContent = stepCm
    ? `继续${stepCm > 0 ? "右" : "左"}移 ${Math.abs(stepCm).toFixed(0)}cm`
    : "继续反向横移";
  const endWp = selectedEndWaypoint();
  d.nextReturn.textContent = endWp ? `收回到「${endWp.name}」` : "收回到结束位点";
  d.stepNext.classList.remove("hidden");
  reachMsg("主段到位，已暂停（分段模式）", "success");
  prefetchSidestep();   // 暂停期间手臂静止，趁人看落点的工夫先把横移段算好
}

// 横移预规划：逐点 IK 一次约 1s（纯 Python FK + 数值雅可比），6cm 要 ~6s。
// 弹窗一出现就在后台算，点「继续横移」时若起点没动、距离没改则直接用。
async function prefetchSidestep() {
  reach.sideCache = null;
  const stepCm = Number(reach.dom.stepLen.value || 0);
  if (!stepCm || !reach.plane || !reach.status?.joints_available) {
    return;
  }
  let joints = null;
  try {
    const j = await fetchJson("/api/reach/joints");
    if (j.ok) {
      joints = j.named_joints;
    }
  } catch {
    return;
  }
  if (!joints) {
    return;
  }
  if (matchSidestepRecording(stepCm, joints)) {
    return;   // 已有可回放的录制（免 IK），不用预算
  }
  try {
    const seg = await planCartesianSidestep(joints, stepCm);
    reach.sideCache = { stepCm, joints, seg };
  } catch { /* 预取失败就退回点击时现算 */ }
}

function hideStepNext() {
  reach.dom?.stepNext?.classList.add("hidden");
}

function setStepNextBusy(busy) {
  const d = reach.dom;
  [d.nextSide, d.nextSideR, d.nextReturn, d.nextDone]
    .forEach((btn) => { btn.disabled = busy; });
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

// flip=true 为"反向横移"按钮：同一条链路，距离符号取反（左↔右完全对称）
async function stepNextSidestep(flip = false) {
  const raw = Number(reach.dom.stepLen.value || 0);
  if (!raw) {
    reachMsg("左移(cm) 为 0，没有可执行的横移", "error");
    return;
  }
  const stepCm = flip ? -raw : raw;
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
    const urdfUrl = `${metadata.robot.urdf_url}${metadata.robot.urdf_url.includes("?") ? "&" : "?"}v=2`;
    await loadRobot(urdfUrl);
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
      check_collision: options.checkCollision ?? true,
    };
    const data = await fetchJson("/api/trajectory/plan", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    panel.frames = data.waypoints;
    panel.frameIndex = 0;
    panel.currentPlanner = data.planner;   // 带 "+rrt" 后缀 = 撞障后自动绕障
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
      const body = await response.json();
      detail = body.detail || body.error || detail;   // reach 接口用 error 字段
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
