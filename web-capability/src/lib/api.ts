/* 18000 能力配置服务的类型与请求封装。 */

export interface Hand {
  id: string;
  name: string;
  design_side: "left" | "right";
  tool_out_mm: number;
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
}

export interface Registry {
  schema_version: number;
  active: ActiveCombo | null;
  hands: Hand[];
  calibrations: unknown[];
  capabilities: Capability[];
}

export type CalibStatus = "ready" | "pending" | "missing";

export interface CalibInfo {
  arm: string;
  hand_id: string;
  path: string;
  status: CalibStatus;
  source_path: string;
  registered_at: string;
  solved_at: string | null;
  residual_mm: number | null;
  num_samples: number | null;
}

export interface ParamSpec {
  default: number;
  min: number;
  max: number;
}

export interface Meta {
  arms: string[];
  arm_labels: Record<string, string>;
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

// 与 api/flow.py 内置正则一致，新建能力时按方向带出默认值
export const DEFAULT_POSE_PATTERNS: Record<string, string> = {
  rtl: "^\\s*(\\d+(?:\\.\\d+)?)-起手式新\\s*$",
  ltr: "^\\s*(\\d+(?:\\.\\d+)?)-左-起手式\\s*$",
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
