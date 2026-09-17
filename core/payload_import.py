"""Import named payload-identification files as immutable gravity profiles."""
from __future__ import annotations

import json
import math
import threading
from datetime import datetime
from pathlib import Path

from . import gravity_profiles as profiles

MAX_JSON_BYTES = 1024 * 1024
_import_lock = threading.Lock()


def parameters_from_payload(raw: dict, arm: str) -> dict:
    if not isinstance(raw, dict):
        raise ValueError("负载文件必须是 JSON object")
    if arm not in ("left_arm", "right_arm"):
        raise ValueError("请选择左臂或右臂")
    if raw.get("arm") not in (arm, arm.removesuffix("_arm")):
        raise ValueError("补偿文件的 arm 与所选手臂不一致")
    for key in ("mass_kg", "com_m", "alpha"):
        if key not in raw:
            raise ValueError(f"负载文件缺少 {key}")
    try:
        alpha = float(raw["alpha"])
        mass = float(raw["mass_kg"])
    except (TypeError, ValueError) as exc:
        raise ValueError("alpha 和 mass_kg 必须是数字") from exc
    if not math.isfinite(alpha) or alpha <= 0:
        raise ValueError("负载辨识文件的 alpha 必须大于 0")
    if not isinstance(raw["com_m"], list) or len(raw["com_m"]) != 3:
        raise ValueError("com_m 必须是 3 个数字")
    include_hand = raw.get("with_hand", True)
    if not isinstance(include_hand, bool):
        raise ValueError("负载辨识文件的 with_hand 必须是 boolean")
    # Identification: alpha * arm + payload. The controller scales the entire
    # model, so normalize payload mass and match the original hand subtree.
    return profiles.validate_parameters(dict(
        profiles.BASELINE_PROFILE["parameters"], grav_alpha=alpha,
        payload_kg=mass / alpha, payload_com_m=raw["com_m"],
        payload_link=arm.replace("_arm", "_wrist_yaw_link"),
        excluded_subtree_link=None if include_hand else arm.replace("_arm", "_hand_link"),
    ))


def import_payload_profile(body: dict, registry_path: Path) -> tuple[dict, bool]:
    """Persist a snapshot; importing never changes any active arm or version."""
    arm, hand_id = body.get("arm"), str(body.get("hand_id") or "").strip()
    if not hand_id:
        raise ValueError("请先选择手型号")
    source_path = str(body.get("source_path") or "").strip()
    content = body.get("content")
    if source_path and content is not None:
        raise ValueError("选择文件和服务器路径只能使用一种导入方式")
    if source_path:
        path = Path(source_path).expanduser()
        if not path.is_absolute():
            raise ValueError("服务器路径必须是绝对路径")
        path = path.resolve()
        filename, source = path.name, str(path)
        if path.suffix.lower() != ".json":
            raise ValueError("请选择 JSON 文件")
        with path.open("rb") as stream:
            data = stream.read(MAX_JSON_BYTES + 1)
        if len(data) > MAX_JSON_BYTES:
            raise ValueError("补偿文件不能超过 1 MiB")
        try:
            raw = json.loads(data.decode("utf-8-sig"))
        except (ValueError, UnicodeError) as exc:
            raise ValueError("补偿文件不是有效的 UTF-8 JSON") from exc
    else:
        filename = str(body.get("filename") or "").replace("\\", "/").rsplit("/", 1)[-1]
        if not filename or Path(filename).suffix.lower() != ".json":
            raise ValueError("请选择 JSON 文件或填写服务器路径")
        raw, source = content, f"upload:{filename}"
        if len(json.dumps(raw, ensure_ascii=False).encode("utf-8")) > MAX_JSON_BYTES:
            raise ValueError("补偿文件不能超过 1 MiB")
    parameters = parameters_from_payload(raw, arm)
    label = str(body.get("label") or "").strip() or filename
    session = str(raw.get("source_session") or "").strip()
    if session:
        source += f"#{session}"
    compatibility = {"arm": arm, "hand_id": hand_id}
    with _import_lock:
        registry = profiles.load_registry(registry_path)
        for old in registry["versions"]:
            if all(old.get(key) == value for key, value in {
                "label": label, "source": source, "parameters": parameters,
                "compatibility": compatibility,
            }.items()):
                return old, False
        major, minor, patch = max(tuple(map(int, p["version"].split("."))) for p in registry["versions"])
        profile = profiles.validate_profile({
            "version": f"{major}.{minor}.{patch + 1}", "label": label,
            "description": f"从 {filename} 导入负载辨识参数。"[:500],
            "created_at": datetime.now().astimezone().isoformat(timespec="seconds"),
            "parent_version": registry["active_version"], "source": source,
            "compatibility": compatibility, "parameters": parameters,
        }, known_versions={p["version"] for p in registry["versions"]})
        registry["versions"].append(profile)
        profiles.save_registry(registry, registry_path)
        return profile, True
