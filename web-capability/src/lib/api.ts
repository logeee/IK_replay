/* 18000 能力配置服务的类型与请求封装。 */

export interface Hand {
  id: string;
  name: string;
  design_side: "left" | "right";
  tool_out_mm: number;
  hand_web_device_id: string;
  tcp_point_id: string;
  notes: string;
}

export interface Task {
  name: string;
  direction: string;
  sites: string[];
}

export interface Capability {
  id: string;
  arm: string;
  hand_id: string;
  task: Task;
  method: string;
  method_params: Record<string, number>;
  assets: { pose_pattern: string; endpoint_pattern: string };
  enabled: boolean;
  notes: string;
}

export interface ActiveCombo {
  arm: string;
  hand_id: string;
  camera_role?: string;
  /** 18001 运动后端：legacy=关节路点直发；pink=世界系 PINK 闭环跟踪（旧注册表可能缺省） */
  motion_backend?: MotionBackend;
}

export type CalibrationArtifactType =
  | "extrinsic"
  | "intrinsic"
  | "camera_transform"
  | "hand_mount"
  | "tcp_profile";

export interface CalibrationArtifact {
  artifact_id: string;
  type: CalibrationArtifactType;
  subject_key: string;
  subject: Record<string, unknown>;
  run_id: string;
  status: string;
  local_path: string;
  cloud_remote_id: string | number | null;
  registered_at: string;
}

export interface CalibrationBinding {
  arm: string;
  hand_id: string;
  camera_role: string;
  artifacts: Partial<Record<CalibrationArtifactType, string>>;
  updated_at: string;
}

export type MotionBackend = "legacy" | "pink";

/** 柜面坐标系构建配置（7005 启动时读取；改后重启 7005 生效） */
export interface CabinetFrameConfig {
  method: string;
  params: Record<string, number>;
}

/** 自动选点模型配置（粉点→绿点；7005 启动时读取）。标量参数 + 向量参数
 *  （mm，墙面系 x右/y入墙/z上；panel_size_mm 为 [长, 短]） */
export interface TargetModelConfig {
  method: string;
  params: Record<string, number | number[]>;
}

export interface VectorParamSpec {
  label?: string;
  length?: number;
  /** 代码内置常量（偏移写死在 core/target_models.py） */
  default?: number[];
}

/** 某能力条目的认领（严格：没认领的该条目不可用；拨/扭是不同条目，
 *  各认各的）。waypoint_names 只存手选位点；终点位点由已认领起手式推导 */
export interface SequenceClaim {
  capability_id: string;
  names: string[];
  waypoint_names: string[];
}

/** 公共动作池条目（data/sequences 按动作名聚合；同名多文件=多次录制） */
export interface SequencePoolEntry {
  name: string;
  files: number;
  latest_file: string;
  latest_created_at: string;
  chain_id: string | null;
  /** 臂归属（文件 arm 字段；null = 无标记 / 与名字前缀矛盾 → 任何臂不可用） */
  arm: string | null;
  recorded_combo: { arm?: string; hand_id?: string } | null;
  /** 配套终点位点名（由序列最后一个路点推导，与运行时规则一致） */
  endpoint_name: string;
}

/** 位点池条目（data/waypoints 按位点名聚合） */
export interface WaypointPoolEntry {
  name: string;
  files: number;
  latest_file: string;
  latest_created_at: string;
  chain_id: string | null;
  /** 臂归属（同上） */
  arm: string | null;
}

export interface Registry {
  schema_version: number;
  active: ActiveCombo | null;
  /** 旧注册表可能缺省：服务端会按方法一 + 默认参数补齐 */
  cabinet_frame?: CabinetFrameConfig;
  /** 旧注册表可能缺省：服务端按 knob_mask_center 补齐 */
  target_model?: TargetModelConfig;
  hands: Hand[];
  calibrations: unknown[];
  calibration_artifacts: CalibrationArtifact[];
  calibration_bindings: CalibrationBinding[];
  capabilities: Capability[];
  sequence_claims: SequenceClaim[];
}

export type CalibStatus = "ready" | "pending" | "missing";

/** 残差字段：旧归档是数字，hand_eye_3D 输出是 {rms, ...} 统计块 */
export type ResidualMm = number | { rms?: number; [key: string]: unknown };

export interface CalibInfo {
  arm: string;
  hand_id: string;
  path: string;
  status: CalibStatus;
  source_path: string;
  registered_at: string;
  solved_at: string | null;
  residual_mm: ResidualMm | null;
  num_samples: number | null;
  has_mount: boolean;
  mount_solved_at: string | null;
  mount_residual_mm: ResidualMm | null;
  suggested_tool_out_mm: number | null;
}

export interface ParamSpec {
  default: number;
  min: number;
  max: number;
  /** 柜面坐标系参数规格才有：整数项 / 中文标签 */
  integer?: boolean;
  label?: string;
}

export interface Meta {
  arms: string[];
  camera_roles?: string[];
  calibration_artifact_types?: CalibrationArtifactType[];
  arm_labels: Record<string, string>;
  /** 名字 / 文件名的臂归属前缀：right_arm → "R-"、left_arm → "L-" */
  arm_prefixes?: Record<string, string>;
  builtin_pose_patterns?: Record<string, string>;
  motion_backends?: MotionBackend[];
  motion_backend_labels?: Record<string, string>;
  cabinet_frame_methods?: string[];
  cabinet_frame_method_labels?: Record<string, string>;
  cabinet_frame_param_specs?: Record<string, Record<string, ParamSpec>>;
  cabinet_frame_default_method?: string;
  target_models?: string[];
  target_model_labels?: Record<string, string>;
  target_model_param_specs?: Record<string, Record<string, ParamSpec>>;
  target_model_vector_params?: Record<string, Record<string, VectorParamSpec>>;
  target_model_versions?: Record<string, string>;
  target_model_default?: string;
  design_sides: string[];
  sites: string[];
  directions: string[];
  methods: string[];
  method_labels: Record<string, string>;
  implemented_methods: string[];
  method_param_specs: Record<string, Record<string, ParamSpec>>;
}

export interface Payload {
  ok: boolean;
  registry: Registry;
  calibrations: CalibInfo[];
  sequence_pool: SequencePoolEntry[];
  waypoint_pool: WaypointPoolEntry[];
  meta: Meta;
}

export const DIRECTION_LABELS: Record<string, string> = {
  rtl: "右到左 · 向左拨",
  ltr: "左到右 · 向右拨",
  cw: "顺时针旋转",
  ccw: "逆时针旋转",
};

export const SITE_LABELS: Record<string, string> = {
  lab: "实验室柜",
  factory: "工厂柜",
};

export const SIDE_LABELS: Record<string, string> = {
  right: "右",
  left: "左",
};

export const PARAM_LABELS: Record<string, string> = {
  sidestep_cm: "横移距离 (cm)",
  push_force_n: "推力 (N)",
  push_hold_s: "推力保持 (s)",
  down_deg: "向下倾角 (°)",
};

// 与 core/capability_registry.py BUILTIN_POSE_PATTERNS 一致，新建能力时按方向
// 带出默认值。名字开头的 (?:[LR]-)? 是臂归属前缀（R-/L-），臂的校验靠文件
// 的 arm 字段而不靠正则。
export const DEFAULT_POSE_PATTERNS: Record<string, string> = {
  rtl: "^\\s*(?:[LR]-)?(\\d+(?:\\.\\d+)?)-起手式新\\s*$",
  ltr: "^\\s*(?:[LR]-)?(\\d+(?:\\.\\d+)?)-左-起手式\\s*$",
};

/** 名字前缀兜底（服务端 meta.arm_prefixes 缺省时用） */
export const ARM_PREFIXES: Record<string, string> = {
  right_arm: "R-",
  left_arm: "L-",
};

export async function apiGet(): Promise<Payload> {
  const res = await fetch("/api/capability/registry");
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `加载失败（HTTP ${res.status}）`);
  }
  return data as Payload;
}

export async function apiPost(
  path: string,
  body: unknown,
): Promise<Payload> {
  const res = await fetch(path, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const data = await res.json().catch(() => ({}));
  if (!res.ok || data.ok === false) {
    throw new Error(data.error || `请求失败（HTTP ${res.status}）`);
  }
  return data as Payload;
}
