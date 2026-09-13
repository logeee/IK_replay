#!/usr/bin/env python3
"""Import the left Revo2 + wrist adapter from a colleague's H2 assembly.

The browser viewers load the robot and dexterous hand separately, so the full H2
assembly cannot be used directly.  This importer extracts the left hand subtree,
adds the adapter as a second visual branch, and keeps the same canonical
``base_link`` frame used by the existing BrainCo preview.  Consequently 18003
``xyz_hand`` TCP points remain meaningful when the mount profile is switched.
"""
from __future__ import annotations

import argparse
import shutil
import xml.etree.ElementTree as ET
from copy import deepcopy
from pathlib import Path


DEFAULT_SOURCE = Path("/home/robot/yx/urdf_Revo2")
DEFAULT_OUTPUT = Path(__file__).resolve().parents[1] / "assets/hands/revo2_left_cad"


def _named(elements: list[ET.Element]) -> dict[str, ET.Element]:
    return {str(item.get("name")): item for item in elements if item.get("name")}


def import_model(source_root: Path, output_root: Path) -> Path:
    source_root = source_root.expanduser().resolve()
    source_urdf = source_root / "H2_handcart_tool_revo2_left.urdf"
    tree = ET.parse(source_urdf)
    source_robot = tree.getroot()
    links = _named(list(source_robot.findall("link")))
    joints = list(source_robot.findall("joint"))

    descendants = {"left_base_link"}
    selected_joints: list[ET.Element] = []
    changed = True
    while changed:
        changed = False
        for joint in joints:
            parent = joint.find("parent")
            child = joint.find("child")
            parent_name = parent.get("link") if parent is not None else None
            child_name = child.get("link") if child is not None else None
            if parent_name in descendants and child_name not in descendants:
                descendants.add(str(child_name))
                selected_joints.append(joint)
                changed = True

    missing_links = sorted(name for name in descendants if name not in links)
    if missing_links:
        raise ValueError(f"source URDF is missing links: {missing_links}")

    output_root.mkdir(parents=True, exist_ok=True)
    mesh_root = output_root / "meshes"
    mesh_root.mkdir(parents=True, exist_ok=True)
    robot = ET.Element("robot", {"name": "h2-revo2-left-cad-profile"})
    robot.append(ET.Comment(
        " Generated from H2_handcart_tool_revo2_left.urdf; canonical root "
        "matches the existing BrainCo base_link frame. "))
    ET.SubElement(robot, "link", {"name": "base_link"})

    # Keep the exact legacy BrainCo root convention so 18003 TCP points can be
    # shared between measured_3d and cad_nominal.
    base_joint = ET.SubElement(robot, "joint", {"name": "base", "type": "fixed"})
    ET.SubElement(base_joint, "parent", {"link": "base_link"})
    ET.SubElement(base_joint, "child", {"link": "left_base_link"})
    ET.SubElement(base_joint, "origin", {"rpy": "1.57 3.14 0", "xyz": "0 0 0"})

    adapter = ET.SubElement(robot, "link", {"name": "left_hand_adapter_visual_link"})
    for tag in ("visual", "collision"):
        shape = ET.SubElement(adapter, tag)
        ET.SubElement(shape, "origin", {"xyz": "0 0 0", "rpy": "0 0 0"})
        geometry = ET.SubElement(shape, "geometry")
        ET.SubElement(geometry, "mesh", {"filename": "meshes/revo2_left_hand_adapter.stl"})
        if tag == "visual":
            material = ET.SubElement(shape, "material", {"name": "adapter_gray"})
            ET.SubElement(material, "color", {"rgba": "0.30 0.34 0.38 1"})
    adapter_joint = ET.SubElement(
        robot, "joint", {"name": "left_hand_adapter_visual_joint", "type": "fixed"})
    ET.SubElement(adapter_joint, "parent", {"link": "base_link"})
    ET.SubElement(adapter_joint, "child", {"link": "left_hand_adapter_visual_link"})
    # T_base_link_adapter = T_base_link_left_base * inv(T_adapter_left_base).
    ET.SubElement(adapter_joint, "origin", {
        "xyz": "-0.000000059460 0.046882363658 0.000037333643",
        "rpy": "0.001592654095 0.000796325785 -1.570795058522",
    })

    copied: set[str] = set()
    for name in sorted(descendants):
        link = deepcopy(links[name])
        for mesh in link.findall(".//mesh"):
            filename = str(mesh.get("filename") or "")
            source_mesh = (source_root / filename).resolve()
            if not source_mesh.is_file():
                raise FileNotFoundError(source_mesh)
            destination = mesh_root / source_mesh.name
            if source_mesh.name not in copied:
                shutil.copy2(source_mesh, destination)
                copied.add(source_mesh.name)
            mesh.set("filename", f"meshes/{source_mesh.name}")
        robot.append(link)
    for joint in selected_joints:
        robot.append(deepcopy(joint))

    adapter_source = source_root / "meshes/revo2_left_hand_adapter.stl"
    shutil.copy2(adapter_source, mesh_root / adapter_source.name)
    ET.indent(robot, space="  ")
    output_urdf = output_root / "revo2_left_cad.urdf"
    ET.ElementTree(robot).write(output_urdf, encoding="utf-8", xml_declaration=True)
    return output_urdf


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--source", type=Path, default=DEFAULT_SOURCE)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    args = parser.parse_args()
    output = import_model(args.source, args.output)
    print(f"Imported CAD hand model: {output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
