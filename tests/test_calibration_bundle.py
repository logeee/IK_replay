import hashlib
import importlib
import json
from pathlib import Path

import pytest
import numpy as np

from core import capability_registry as reg
from core.calibration_bundle import compose_bound_calibration


def _write_artifact(
    root: Path,
    artifact_id: str,
    artifact_type: str,
    subject: dict,
    subject_key: str,
    primary_name: str,
    payload: dict,
    dependencies: list[dict] | None = None,
) -> dict:
    subject = {**subject, "unit_code": "H2-1336"}
    directory = root / artifact_type / subject_key / "run-1"
    directory.mkdir(parents=True)
    raw = (json.dumps(payload, ensure_ascii=False) + "\n").encode()
    (directory / primary_name).write_bytes(raw)
    manifest = {
        "schema": "calib-manifest/2",
        "artifact_id": artifact_id,
        "unit_code": "H2-1336",
        "vendor": "unitree",
        "robot_model": "h2",
        "type": artifact_type,
        "subject": subject,
        "subject_key": subject_key,
        "run_id": "run-1",
        "status": "active",
        "primary_file": primary_name,
        "dependencies": dependencies or [],
        "files": [{
            "name": primary_name,
            "bytes": len(raw),
            "sha256": hashlib.sha256(raw).hexdigest(),
        }],
    }
    (directory / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    return {
        "artifact_id": artifact_id,
        "unit_code": "H2-1336",
        "vendor": "unitree",
        "robot_model": "h2",
        "type": artifact_type,
        "subject": subject,
        "subject_key": subject_key,
        "run_id": "run-1",
        "status": "active",
        "local_path": str(directory),
    }


def _registry(tmp_path: Path) -> dict:
    registry = reg.seed_registry()
    registry["robot"] = {"unit_code": "H2-1336", "vendor": "unitree", "model": "h2"}
    hand_id = registry["active"]["hand_id"]
    hand_key = f"right_arm__{hand_id}"
    artifacts = [
        _write_artifact(
            tmp_path, "extrinsic-1", "extrinsic",
            {"kind": "camera", "camera_role": "head", "camera_serial": "CAM-1"},
            "head", "handeye_result_left.json",
            {"T_cam2base": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0], [0, 0, 0, 1]],
             "base_link": "torso_link", "tip_link": "right_wrist_yaw_link"},
        ),
        _write_artifact(
            tmp_path, "mount-1", "hand_mount",
            {"kind": "hand", "arm": "right_arm", "hand_id": hand_id},
            hand_key, "mount_result.json",
            {"T_wrist2hand": [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, 0.1], [0, 0, 0, 1]],
             "wrist_link": "right_wrist_yaw_link", "hand_base_link": "base_link",
             "solved_at": "2026-09-12T12:00:00", "residual_mm": {"rms": 1.2}},
            [{"relation": "solved_with", "artifact_id": "extrinsic-1", "type": "extrinsic"}],
        ),
        _write_artifact(
            tmp_path, "tcp-1", "tcp_profile",
            {"kind": "hand", "arm": "right_arm", "hand_id": hand_id},
            hand_key, "tcp_profile.json",
            {"default_tcp_point_id": "tip:index", "tcp_points_wrist_m": [
                {"id": "tip:index", "p_wrist_m": [0.2, 0.01, 0.03]},
                {"id": "tip:middle", "p_wrist_m": [0.21, 0.0, 0.0]},
            ]},
            [{"relation": "derived_from", "artifact_id": "mount-1", "type": "hand_mount"}],
        ),
    ]
    registry["calibration_artifacts"] = artifacts
    registry["calibration_bindings"] = [{
        "arm": "right_arm",
        "hand_id": hand_id,
        "camera_role": "head",
        "artifacts": {
            "extrinsic": "extrinsic-1",
            "hand_mount": "mount-1",
            "tcp_profile": "tcp-1",
        },
    }]
    return reg.validate_registry(registry)


def test_bound_artifacts_compose_legacy_runtime_view(tmp_path: Path):
    registry = _registry(tmp_path)
    hand_id = registry["active"]["hand_id"]

    calibration, provenance = compose_bound_calibration(
        registry, "right_arm", hand_id, "head")

    assert calibration["T_cam2base"][0] == [1, 0, 0, 0]
    assert calibration["T_wrist2hand"][2][3] == 0.1
    assert calibration["p_tool_wrist_m"] == [0.2, 0.01, 0.03]
    assert calibration["camera"]["serial"] == "CAM-1"
    assert calibration["arm"] == "right_arm"
    assert calibration["hand_id"] == hand_id
    assert provenance["artifact_ids"]["tcp_profile"] == "tcp-1"


def test_bound_artifact_hash_mismatch_is_rejected(tmp_path: Path):
    registry = _registry(tmp_path)
    hand_id = registry["active"]["hand_id"]
    tcp_dir = Path(registry["calibration_artifacts"][2]["local_path"])
    (tcp_dir / "tcp_profile.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="校验失败"):
        compose_bound_calibration(registry, "right_arm", hand_id, "head")


def test_unbound_combo_returns_none(tmp_path: Path):
    registry = _registry(tmp_path)
    hand_id = registry["active"]["hand_id"]
    assert compose_bound_calibration(
        registry, "right_arm", hand_id, "waist") is None


def test_binding_rejects_mount_solved_with_another_extrinsic(tmp_path: Path):
    registry = _registry(tmp_path)
    hand_id = registry["active"]["hand_id"]
    mount_dir = Path(registry["calibration_artifacts"][1]["local_path"])
    manifest_path = mount_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["dependencies"][0]["artifact_id"] = "another-extrinsic"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    with pytest.raises(ValueError, match="不是使用当前绑定 extrinsic"):
        compose_bound_calibration(registry, "right_arm", hand_id, "head")


def test_reach_state_does_not_double_apply_legacy_tool_offset():
    reach_state = importlib.import_module("adapters.reach.state")

    class Robot:
        @staticmethod
        def forward_kinematics(_joints):
            return {"torso_link": np.eye(4)}

        @staticmethod
        def joint_names(_chain):
            return []

    calibration = {
        "T_cam2base": np.eye(4).tolist(),
        "T_wrist2hand": np.eye(4).tolist(),
        "p_tool_wrist_m": [0.2, 0.01, 0.03],
        "tcp_points_wrist_m": [{"id": "tip:index", "p_wrist_m": [0.2, 0.01, 0.03]}],
        "base_link": "torso_link",
        "wrist_link": "right_wrist_yaw_link",
        "calibration_bundle": {"mode": "manifest_binding", "artifact_ids": {}},
    }
    reach_state.configure(
        camera=None, robot_model=Robot(), robot_id="h2", chain_id="right_arm",
        calib_path=None, calibration=calibration, tool_out_mm=15.0,
    )

    assert reach_state.state.p_tool == [0.2, 0.01, 0.03]
    assert reach_state.state.calib_meta["tool_out_mm"] == 0.0
