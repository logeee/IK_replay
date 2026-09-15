import json
from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from core import capability_registry as reg
from core import gravity_profiles
from tools import capability_server


def test_active_combo_selects_only_compatible_gravity_profile(tmp_path: Path):
    registry_path = tmp_path / "capability_registry.json"
    seed = reg.seed_registry()
    seed["hands"].append({
        "id": "qiangnao-revo2-left",
        "name": "强脑-Revo2-左",
        "design_side": "left",
        "tool_out_mm": 0.0,
        "notes": "",
    })
    reg.save_registry(seed, registry_path)
    client = TestClient(capability_server.app)
    patches = (
        patch.object(capability_server, "REGISTRY_PATH", registry_path),
        patch.object(
            capability_server,
            "GRAVITY_PROFILES_PATH",
            gravity_profiles.DEFAULT_GRAVITY_PROFILES_PATH,
        ),
    )
    with patches[0], patches[1]:
        selected = client.post("/api/capability/active", json={
            "arm": "left_arm",
            "hand_id": "qiangnao-revo2-left",
            "gravity_profile_version": "0.2.0",
        })
        rejected = client.post("/api/capability/active", json={
            "arm": "right_arm",
            "hand_id": seed["hands"][0]["id"],
            "gravity_profile_version": "0.2.0",
        })

    assert selected.status_code == 200, selected.text
    assert selected.json()["registry"]["active"]["gravity_profile_version"] == "0.2.0"
    assert rejected.status_code == 400
    assert "仅适用于" in rejected.json()["error"]


def _manifest(artifact_id: str, artifact_type: str, subject: dict, subject_key: str) -> dict:
    subject = {**subject, "unit_code": "H2-1336"}
    return {
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
        "files": [],
        "cloud": {"remote_id": 17},
    }


def test_workstation_artifacts_can_be_bound_atomically(tmp_path: Path):
    registry_path = tmp_path / "capability_registry.json"
    reg.save_registry(reg.seed_registry(), registry_path)
    client = TestClient(capability_server.app)
    hand_id = "yinshi-1-right"
    manifests = [
        _manifest(
            "extrinsic-1", "extrinsic",
            {"kind": "camera", "camera_role": "head", "camera_serial": "CAM-1"},
            "head",
        ),
        _manifest(
            "mount-1", "hand_mount",
            {"kind": "hand", "arm": "right_arm", "hand_id": hand_id},
            f"right_arm__{hand_id}",
        ),
        _manifest(
            "tcp-1", "tcp_profile",
            {"kind": "hand", "arm": "right_arm", "hand_id": hand_id},
            f"right_arm__{hand_id}",
        ),
    ]

    with patch.object(capability_server, "REGISTRY_PATH", registry_path):
        identity = client.post("/api/capability/robot", json={
            "unit_code": "H2-1336", "vendor": "unitree", "model": "h2",
        })
        assert identity.status_code == 200, identity.text
        for manifest in manifests:
            response = client.post(
                "/api/capability/calibration-artifacts",
                json={"manifest": manifest, "local_path": f"/tmp/{manifest['artifact_id']}"},
            )
            assert response.status_code == 200, response.text
        response = client.post("/api/capability/calibration-bindings", json={
            "arm": "right_arm",
            "hand_id": hand_id,
            "camera_role": "head",
            "artifacts": {
                "extrinsic": "extrinsic-1",
                "hand_mount": "mount-1",
                "tcp_profile": "tcp-1",
            },
        })

    assert response.status_code == 200, response.text
    saved = reg.load_registry(registry_path)
    resolved = reg.calibration_binding(saved, "right_arm", hand_id, "head")
    assert resolved["resolved"]["tcp_profile"]["cloud_remote_id"] == 17
    assert resolved["resolved"]["tcp_profile"]["unit_code"] == "H2-1336"


def test_robot_identity_rejects_silent_unit_switch(tmp_path: Path):
    registry_path = tmp_path / "capability_registry.json"
    reg.save_registry(reg.seed_registry(), registry_path)
    client = TestClient(capability_server.app)
    with patch.object(capability_server, "REGISTRY_PATH", registry_path):
        first = client.post("/api/capability/robot", json={
            "unit_code": "H2-1336", "vendor": "unitree", "model": "h2",
        })
        second = client.post("/api/capability/robot", json={
            "unit_code": "H2-9999", "vendor": "unitree", "model": "h2",
        })
        vendor_switch = client.post("/api/capability/robot", json={
            "unit_code": "H2-1336", "vendor": "another-vendor", "model": "h2",
        })
    assert first.status_code == 200
    assert second.status_code == 409
    assert "拒绝静默切换" in second.text
    assert vendor_switch.status_code == 409


def test_robot_identity_hydrates_pre_identity_artifact(tmp_path: Path):
    registry_path = tmp_path / "capability_registry.json"
    artifact_dir = tmp_path / "artifact"
    artifact_dir.mkdir()
    manifest = _manifest(
        "extrinsic-old", "extrinsic",
        {"kind": "camera", "camera_role": "head", "camera_serial": "CAM-1"},
        "head",
    )
    (artifact_dir / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    registry = reg.seed_registry()
    registry["calibration_artifacts"] = [{
        "artifact_id": "extrinsic-old",
        "type": "extrinsic",
        "subject": {"kind": "camera", "camera_role": "head", "camera_serial": "CAM-1"},
        "subject_key": "head",
        "run_id": "run-1",
        "status": "active",
        "local_path": str(artifact_dir),
    }]
    reg.save_registry(registry, registry_path)
    client = TestClient(capability_server.app)

    with patch.object(capability_server, "REGISTRY_PATH", registry_path):
        response = client.post("/api/capability/robot", json={
            "unit_code": "H2-1336", "vendor": "unitree", "model": "h2",
        })

    assert response.status_code == 200, response.text
    saved = reg.load_registry(registry_path)
    assert saved["calibration_artifacts"][0]["unit_code"] == "H2-1336"
    assert saved["calibration_artifacts"][0]["subject"]["unit_code"] == "H2-1336"


def test_active_combo_can_select_mount_profile(tmp_path: Path):
    registry_path = tmp_path / "capability_registry.json"
    registry = reg.seed_registry()
    hand = registry["hands"][0]
    hand["mount_profiles"].append({
        "id": "cad_nominal",
        "name": "CAD 名义装配",
        "source": "fixed",
        "hand_base_link": "base_link",
        "model": {
            "source": "project",
            "root": "hands/revo2_left_cad",
            "urdf": "revo2_left_cad.urdf",
        },
        "T_wrist2hand": [
            [1, 0, 0, 0.1], [0, 1, 0, 0],
            [0, 0, 1, 0], [0, 0, 0, 1],
        ],
    })
    reg.save_registry(registry, registry_path)
    client = TestClient(capability_server.app)

    with patch.object(capability_server, "REGISTRY_PATH", registry_path):
        response = client.post("/api/capability/active", json={
            "arm": "right_arm",
            "hand_id": hand["id"],
            "camera_role": "head",
            "motion_backend": "legacy",
            "mount_profile_id": "cad_nominal",
        })

    assert response.status_code == 200, response.text
    assert response.json()["registry"]["active"]["mount_profile_id"] == "cad_nominal"
    assert reg.load_registry(registry_path)["active"]["mount_profile_id"] == "cad_nominal"
