#!/usr/bin/env python3
"""把一侧手臂的位点 JSON 按关节镜像成另一侧（右 → 左 或 左 → 右）。

镜像规则（H2 URDF 左右臂关于机体 XZ 面严格对称，已用正运动学逐关节验证：
镜像后末端位姿与原位姿关于 XZ 面对称，误差 0）：

    绕 Y 轴的关节（shoulder_pitch / elbow / wrist_pitch）  → 角度不变
    绕 X、Z 轴的关节（shoulder_roll / shoulder_yaw / wrist_roll / wrist_yaw） → 取反

输出文件：
    名字前缀 R- ↔ L-，arm / chain_id 改为目标臂，关节名 right_ ↔ left_，
    附 mirrored_from 记录来源；文件名保留原时间戳后缀，便于左右配对。
    可选 --hand-id 写 recorded_combo（让 18001 在该组合下未认领也可见）。

用法：
    python tools/mirror_arm_waypoint.py data/waypoints/R-起手点测试_20260721_042250.json \
        [more.json ...] [--hand-id yinshi-1-left] [--force]
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from core import arm_assets  # noqa: E402

# 绕 Y 轴（机体俯仰方向）的关节：镜像后角度不变；其余（X/Z 轴）取反
SAME_SIGN_KEYWORDS = ("pitch", "elbow")

# 关节限位（rad，H2 URDF）——镜像结果越界则拒绝写文件
LEFT_LIMITS = {
    "left_shoulder_pitch_joint": (-2.618, 1.833),
    "left_shoulder_roll_joint": (-0.517, 2.494),
    "left_shoulder_yaw_joint": (-2.618, 2.618),
    "left_elbow_joint": (-0.986, 3.071),
    "left_wrist_roll_joint": (-2.618, 2.618),
    "left_wrist_pitch_joint": (-0.576, 0.576),
    "left_wrist_yaw_joint": (-1.22, 1.22),
}
RIGHT_LIMITS = {
    "right_shoulder_pitch_joint": (-2.618, 1.833),
    "right_shoulder_roll_joint": (-2.494, 0.517),
    "right_shoulder_yaw_joint": (-2.618, 2.618),
    "right_elbow_joint": (-0.986, 3.071),
    "right_wrist_roll_joint": (-2.618, 2.618),
    "right_wrist_pitch_joint": (-0.576, 0.576),
    "right_wrist_yaw_joint": (-1.22, 1.22),
}
LIMITS = {"left_arm": LEFT_LIMITS, "right_arm": RIGHT_LIMITS}
OTHER = {"right_arm": "left_arm", "left_arm": "right_arm"}
JOINT_PREFIX = {"right_arm": "right_", "left_arm": "left_"}


def mirror_joints(named: dict[str, float], src_arm: str) -> dict[str, float]:
    dst_arm = OTHER[src_arm]
    out: dict[str, float] = {}
    for name, value in named.items():
        if not name.startswith(JOINT_PREFIX[src_arm]):
            raise ValueError(f"关节名 {name} 不属于 {src_arm}，拒绝镜像")
        new_name = JOINT_PREFIX[dst_arm] + name[len(JOINT_PREFIX[src_arm]):]
        keep = any(k in name for k in SAME_SIGN_KEYWORDS)
        out[new_name] = float(value) if keep else -float(value)
    return out


def check_limits(named: dict[str, float], arm: str) -> list[str]:
    bad = []
    for name, value in named.items():
        lim = LIMITS[arm].get(name)
        if lim and not (lim[0] <= value <= lim[1]):
            bad.append(f"{name}={value:+.4f} 超出 [{lim[0]:+.3f}, {lim[1]:+.3f}]")
    return bad


def mirror_file(path: Path, hand_id: str | None, force: bool) -> Path:
    data = json.loads(path.read_text(encoding="utf-8"))
    src_arm = arm_assets.asset_arm(data)
    if src_arm is None:
        raise ValueError(f"{path.name}: 源文件没有有效 arm 归属，拒绝镜像")
    arm_assets.check_asset_arm(data, src_arm, "位点")   # 关节名与 arm 交叉校验
    dst_arm = OTHER[src_arm]
    joints = mirror_joints(data["named_joints"], src_arm)
    bad = check_limits(joints, dst_arm)
    if bad:
        raise ValueError(f"{path.name}: 镜像结果越限：" + "；".join(bad))

    base = arm_assets.strip_arm_prefix(data["name"])
    new_name = arm_assets.arm_asset_name(dst_arm, base)
    out = {
        "name": new_name,
        "chain_id": dst_arm,
        "named_joints": joints,
        "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "arm": dst_arm,
        "mirrored_from": path.name,
    }
    if hand_id:
        out["recorded_combo"] = {"arm": dst_arm, "hand_id": hand_id}

    # 文件名：前缀换掉，保留原时间戳后缀
    stem = path.stem
    stripped = arm_assets.strip_arm_prefix(stem)
    out_path = path.with_name(arm_assets.arm_prefix(dst_arm) + stripped + ".json")
    if out_path.exists() and not force:
        raise FileExistsError(f"{out_path.name} 已存在（加 --force 覆盖）")
    out_path.write_text(json.dumps(out, ensure_ascii=False, indent=2) + "\n",
                        encoding="utf-8")
    return out_path


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("files", nargs="+", type=Path)
    ap.add_argument("--hand-id", default=None,
                    help="写入 recorded_combo 的目标臂手 id（如 yinshi-1-left）")
    ap.add_argument("--force", action="store_true", help="覆盖已存在的目标文件")
    args = ap.parse_args()
    rc = 0
    for f in args.files:
        try:
            out = mirror_file(f, args.hand_id, args.force)
            print(f"[ok]   {f.name}  ->  {out.name}")
        except Exception as exc:  # noqa: BLE001
            rc = 1
            print(f"[skip] {exc}", file=sys.stderr)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
