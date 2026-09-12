from pathlib import Path
from unittest.mock import patch

from fastapi.testclient import TestClient

from core import capability_registry as reg
from tools import capability_server


def _manifest(artifact_id: str, artifact_type: str, subject: dict, subject_key: str) -> dict:
    return {
        "schema": "calib-manifest/2",
        "artifact_id": artifact_id,
        "unit_code": "H2-1336",
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
