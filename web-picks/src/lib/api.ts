// 与 tools/picks_server.py 的只读接口对接。
// 开发模式（vite dev）直连 7010 端口；构建产物由同一服务托管，走同源。
export const API_BASE = import.meta.env.DEV
  ? `http://${location.hostname}:7010`
  : "";

export interface FitQuality {
  inlier_count?: number;
  inlier_ratio?: number;
  rms_m?: number;
  long_length_m?: number;
  short_length_m?: number;
  orientation_source?: string;
}

export interface YoloBox {
  cls: number;
  name: string;
  conf: number;
  xyxy: number[];
  polygon?: number[][];
}

export interface PickMeta {
  saved_at?: string;
  capture_id?: string;
  selection_source?: string | null;
  model_version?: string | null;
  target_point_slot?: number | null;
  matched_detection_name?: string | null;
  panel_center_camera_m?: number[] | null;
  reference_camera_m?: number[];
  adjustment_camera_m?: number[];
  adjustment_mm?: number[];
  adjustment_wall_mm?: { x: number; y: number; z: number } | null;
  final_p_camera_m?: number[];
  approach_offset_m?: number;
  confirm_result?: {
    p_root?: number[];
    p_root_surface?: number[];
    p_torso?: number[];
    offset_mode?: string;
    depth_mm?: number;
  } | null;
  auto_target?: {
    target_wall_m?: number[];
    panel_center_wall_m?: number[];
    offset_wall_m?: number[];
    wall_axes_camera?: number[][];
    panel_fit_quality?: FitQuality;
  } | null;
  yolo_boxes?: YoloBox[];
  crop_radius_m?: number;
}

export interface PickRecord {
  name: string;
  cloud_bytes: number;
  meta: PickMeta;
}

/** 18001 执行记录摘要（reach JSONL，经 picks_server 解析） */
export interface ExecSummary {
  id: string;
  ts?: string;
  segment?: string;
  result?: string;
  capture_id?: string | null;
  selection_source?: string | null;
  target_point_slot?: number | null;
  matched_detection_name?: string | null;
  duration_s?: number | null;
  tcp_mm: {
    ik_mm?: number | null;
    track_mm?: number | null;
    total_mm?: number | null;
    total_vs_drifted_mm?: number | null;
  };
  torso_rotation_deg?: number | null;
  target_shift_mm?: number | null;
  waist_delta_deg?: number[] | null;
  imu_rpy_delta_deg?: number[] | null;
  trace_len: number;
}

/** 执行期间 5Hz 躯干采样 */
export interface TraceSample {
  t: number;
  phase?: string;
  waist_deg?: number[];
  imu_rpy_deg?: number[];
  follow_sp_deg?: number;
  follow_max_deg?: number;
}

export interface ExecRecord {
  id: string;
  torso_trace?: TraceSample[] | null;
  [key: string]: unknown;
}

let cache: PickRecord[] | null = null;
let execCache: ExecSummary[] | null = null;

export async function fetchRecords(force = false): Promise<PickRecord[]> {
  if (cache && !force) return cache;
  const resp = await fetch(`${API_BASE}/api/picks?limit=500`);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const data = await resp.json();
  if (!data.ok) throw new Error(data.error || "接口返回失败");
  cache = data.records as PickRecord[];
  return cache;
}

export async function fetchExecutions(force = false): Promise<ExecSummary[]> {
  if (execCache && !force) return execCache;
  const resp = await fetch(`${API_BASE}/api/executions`);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const data = await resp.json();
  if (!data.ok) throw new Error(data.error || "接口返回失败");
  execCache = data.records as ExecSummary[];
  return execCache;
}

export async function fetchExecutionDetail(id: string): Promise<ExecRecord> {
  const resp = await fetch(`${API_BASE}/api/executions/${id}`);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const data = await resp.json();
  if (!data.ok) throw new Error(data.error || "接口返回失败");
  return data.record as ExecRecord;
}

/** 执行结果分类，用于角标配色 */
export function execResultKind(
  result?: string,
): "done" | "cancelled" | "error" {
  if (result === "done") return "done";
  if (result === "cancelled") return "cancelled";
  return "error";
}

export function fileUrl(name: string, filename: string): string {
  return `${API_BASE}/api/picks/${name}/${filename}`;
}

/** 微调量模长（mm），衡量算法目标与人最终确认点的偏差 */
export function adjustmentMagnitude(meta: PickMeta): number | null {
  const adj = meta.adjustment_mm;
  if (!adj || adj.length !== 3) return null;
  return Math.hypot(adj[0], adj[1], adj[2]);
}

export interface WallAdjustment {
  /** 右（mm） */
  x: number;
  /** 入墙（mm） */
  y: number;
  /** 上（mm） */
  z: number;
  /** true 表示由相机系微调量投影换算而来，false 表示流程下发的原始值 */
  derived: boolean;
}

/**
 * 墙面系微调分量（右/入墙/上，mm）。
 * 优先用记录里的原始 adjustment_wall_mm；缺失时把相机系微调向量
 * 投影到 wall_axes_camera 三根正交单位轴上，换算是无损的。
 */
export function wallAdjustment(meta: PickMeta): WallAdjustment | null {
  const raw = meta.adjustment_wall_mm;
  if (raw && ["x", "y", "z"].every((k) => k in raw)) {
    return { x: raw.x, y: raw.y, z: raw.z, derived: false };
  }
  const adj = meta.adjustment_camera_m;
  const axes = meta.auto_target?.wall_axes_camera;
  if (
    !adj ||
    adj.length !== 3 ||
    !axes ||
    axes.length !== 3 ||
    axes.some((a) => !Array.isArray(a) || a.length !== 3)
  )
    return null;
  const dot = (a: number[]) =>
    (a[0] * adj[0] + a[1] * adj[1] + a[2] * adj[2]) * 1000;
  return { x: dot(axes[0]), y: dot(axes[1]), z: dot(axes[2]), derived: true };
}

export function formatTime(saved_at?: string): string {
  if (!saved_at) return "-";
  return saved_at.replace("T", " ");
}

export function formatVec(v?: number[] | null, digits = 3): string {
  if (!v || v.length !== 3) return "-";
  return v.map((x) => x.toFixed(digits)).join(", ");
}

export function formatBytes(n: number): string {
  if (n >= 1 << 20) return `${(n / (1 << 20)).toFixed(1)} MB`;
  if (n >= 1 << 10) return `${(n / (1 << 10)).toFixed(0)} KB`;
  return `${n} B`;
}
