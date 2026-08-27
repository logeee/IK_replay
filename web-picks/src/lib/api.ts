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

let cache: PickRecord[] | null = null;

export async function fetchRecords(force = false): Promise<PickRecord[]> {
  if (cache && !force) return cache;
  const resp = await fetch(`${API_BASE}/api/picks?limit=500`);
  if (!resp.ok) throw new Error(`HTTP ${resp.status}`);
  const data = await resp.json();
  if (!data.ok) throw new Error(data.error || "接口返回失败");
  cache = data.records as PickRecord[];
  return cache;
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
