from __future__ import annotations

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
ASSETS_DIR = PROJECT_ROOT / "assets"
DESCRIPTION_DIR = ASSETS_DIR / "g1_d_description"
URDF_PATH = DESCRIPTION_DIR / "g1_d.urdf"
WEB_DIR = PROJECT_ROOT / "web"

ARM_JOINTS = {
    "left": [
        "left_shoulder_pitch_joint",
        "left_shoulder_roll_joint",
        "left_shoulder_yaw_joint",
        "left_elbow_joint",
        "left_wrist_roll_joint",
        "left_wrist_pitch_joint",
        "left_wrist_yaw_joint",
    ],
    "right": [
        "right_shoulder_pitch_joint",
        "right_shoulder_roll_joint",
        "right_shoulder_yaw_joint",
        "right_elbow_joint",
        "right_wrist_roll_joint",
        "right_wrist_pitch_joint",
        "right_wrist_yaw_joint",
    ],
}

ARM_END_LINKS = {
    "left": "left_hand_palm_link",
    "right": "right_hand_palm_link",
}

ARM_LINKS = {
    "left": [
        "torso_link",
        "head_link",
        "left_shoulder_pitch_link",
        "left_shoulder_roll_link",
        "left_shoulder_yaw_link",
        "left_elbow_link",
        "left_wrist_roll_link",
        "left_wrist_pitch_link",
        "left_wrist_yaw_link",
        "left_hand_palm_link",
    ],
    "right": [
        "torso_link",
        "head_link",
        "right_shoulder_pitch_link",
        "right_shoulder_roll_link",
        "right_shoulder_yaw_link",
        "right_elbow_link",
        "right_wrist_roll_link",
        "right_wrist_pitch_link",
        "right_wrist_yaw_link",
        "right_hand_palm_link",
    ],
}

DEFAULT_TCP_OFFSET = [0.08, 0.0, 0.0]
DEFAULT_CURRENT_JOINTS = [0.0, 0.25, 0.0, 0.85, 0.0, -0.35, 0.0]
DEFAULT_DURATION = 4.0
DEFAULT_STEPS = 80

IK_SUCCESS_TOLERANCE_M = 0.025
IK_MAX_EVALUATIONS = 250

COLLISION_NEAR_MARGIN_M = 0.035


def validate_arm(arm: str) -> str:
    arm = (arm or "left").lower()
    if arm not in ARM_JOINTS:
        raise ValueError(f"arm must be one of {sorted(ARM_JOINTS)}")
    return arm

