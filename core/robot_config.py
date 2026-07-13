from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .types import Pose
from .utils import resolve_project_path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_CONFIG_PATH = PROJECT_ROOT / "config" / "default.yaml"


@dataclass(frozen=True)
class ChainConfig:
    name: str
    display_name: str
    subtitle: str
    panel_side: str
    base_link: str
    end_link: str
    joints: list[str]
    target_visual_link: str | None = None
    display_links: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class RobotConfig:
    name: str
    display_name: str
    urdf_path: Path
    mesh_root: Path
    preview_links: list[str]
    chains: dict[str, ChainConfig]
    tcp_offsets: dict[str, Pose]
    initial_joints: dict[str, dict[str, float]]
    collision: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AppConfig:
    active_robot: str
    robots: dict[str, RobotConfig]
    ik: dict[str, Any]
    trajectory: dict[str, Any]
    viewer: dict[str, Any]

    @property
    def robot(self) -> RobotConfig:
        return self.robots[self.active_robot]


def load_yaml(path: Path) -> dict[str, Any]:
    with path.open("r", encoding="utf-8") as file:
        data = yaml.safe_load(file) or {}
    if not isinstance(data, dict):
        raise ValueError(f"YAML root must be a mapping: {path}")
    return data


def load_app_config(config_path: str | Path = DEFAULT_CONFIG_PATH) -> AppConfig:
    config_path = resolve_project_path(PROJECT_ROOT, config_path)
    default_cfg = load_yaml(config_path)
    active_robot = str(default_cfg.get("active_robot") or "")
    robots = default_cfg.get("robots") or {}
    if active_robot not in robots:
        raise ValueError(f"active_robot {active_robot!r} is not defined in {config_path}")

    robot_configs: dict[str, RobotConfig] = {}
    for robot_id, robot_entry in robots.items():
        robot_cfg_path = robot_entry.get("config_path")
        if not robot_cfg_path:
            raise ValueError(f"robot {robot_id!r} is missing config_path")
        robot_configs[str(robot_id)] = load_robot_config(resolve_project_path(PROJECT_ROOT, robot_cfg_path))
    return AppConfig(
        active_robot=active_robot,
        robots=robot_configs,
        ik=dict(default_cfg.get("ik") or {}),
        trajectory=dict(default_cfg.get("trajectory") or {}),
        viewer=dict(default_cfg.get("viewer") or {}),
    )


def load_robot_config(path: str | Path) -> RobotConfig:
    path = resolve_project_path(PROJECT_ROOT, path)
    cfg = load_yaml(path)
    robot = cfg.get("robot") or {}
    chains_raw = cfg.get("chains")
    if chains_raw is None and cfg.get("chain"):
        legacy_chain = dict(cfg.get("chain") or {})
        chains_raw = {str(legacy_chain.get("name") or "left_arm"): legacy_chain}
    chains_raw = chains_raw or {}
    tcp = cfg.get("tcp") or {}
    name = str(robot.get("name") or path.stem)
    chains: dict[str, ChainConfig] = {}
    for chain_id, chain in chains_raw.items():
        joints = [str(item) for item in chain.get("joints") or []]
        if not joints:
            raise ValueError(f"robot config {path} chain {chain_id!r} must define joints")
        chain_id = str(chain_id)
        chains[chain_id] = ChainConfig(
            name=chain_id,
            display_name=str(chain.get("display_name") or chain_id),
            subtitle=str(chain.get("subtitle") or chain_id),
            panel_side=str(chain.get("panel_side") or chain_id),
            base_link=str(chain["base_link"]),
            end_link=str(chain["end_link"]),
            target_visual_link=str(chain.get("target_visual_link") or chain.get("target_hand_link") or chain["end_link"]),
            joints=joints,
            display_links=[str(item) for item in chain.get("display_links") or []],
        )
    if not chains:
        raise ValueError(f"robot config {path} must define chains")
    initial_raw = cfg.get("initial_joints") or {}
    tcp_offsets = _parse_tcp_offsets(tcp, chains)
    initial_joints = _parse_initial_joints(initial_raw, chains)
    return RobotConfig(
        name=name,
        display_name=str(robot.get("display_name") or name),
        urdf_path=resolve_project_path(PROJECT_ROOT, str(robot["urdf_path"])),
        mesh_root=resolve_project_path(PROJECT_ROOT, str(robot.get("mesh_root") or Path(robot["urdf_path"]).parent)),
        preview_links=[str(item) for item in cfg.get("preview_links") or robot.get("preview_links") or []],
        chains=chains,
        tcp_offsets=tcp_offsets,
        initial_joints=initial_joints,
        collision=dict(cfg.get("collision") or {}),
    )


def _parse_tcp_offsets(raw: dict[str, Any], chains: dict[str, ChainConfig]) -> dict[str, Pose]:
    default_raw = raw.get("default") if isinstance(raw.get("default"), dict) else raw
    default_pose = _parse_pose(default_raw or {})
    offsets: dict[str, Pose] = {}
    for chain_id in chains:
        chain_raw = raw.get(chain_id)
        offsets[chain_id] = _parse_pose(chain_raw) if isinstance(chain_raw, dict) else default_pose
    return offsets


def _parse_pose(raw: dict[str, Any]) -> Pose:
    return Pose(
        xyz=[float(v) for v in raw.get("offset_xyz", raw.get("xyz", [0.0, 0.0, 0.0]))],
        rpy=[float(v) for v in raw.get("offset_rpy", raw.get("rpy", [0.0, 0.0, 0.0]))],
    )


def _parse_initial_joints(raw: dict[str, Any], chains: dict[str, ChainConfig]) -> dict[str, dict[str, float]]:
    result: dict[str, dict[str, float]] = {}
    for chain_id, chain in chains.items():
        chain_raw = raw.get(chain_id) if isinstance(raw.get(chain_id), dict) else raw
        result[chain_id] = {name: float(chain_raw.get(name, 0.0)) for name in chain.joints}
    return result


def project_relative(path: Path) -> str:
    try:
        return path.resolve().relative_to(PROJECT_ROOT).as_posix()
    except ValueError:
        return path.as_posix()
