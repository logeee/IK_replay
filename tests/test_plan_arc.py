"""/api/reach/plan_arc 定点圆弧扭转的离线几何验证（H2 模型，无真机）。

核心验收（用户的硬指标）：
- wrist_only 模式肩×3+肘全程纹丝不动（冻结），旋转量全在腕三关节；
- 捏合点始终贴在"绕过中心 C、沿轴 axis 的圆弧"上（半径/轴向坐标不漂）；
- 同参数重复规划输出逐位一致（轨迹可复现）。
"""

from __future__ import annotations

import unittest

import numpy as np

from adapters.reach import planning
from adapters.reach.state import state
from core.robot_config import load_app_config
from core.robot_model import RobotModel
from ik.numerical_solver import NumericalIKSolver

START_JOINTS = {
    "right_shoulder_pitch_joint": 0.2,
    "right_shoulder_roll_joint": -0.25,
    "right_shoulder_yaw_joint": 0.0,
    "right_elbow_joint": 0.9,
    "right_wrist_roll_joint": 0.0,
    "right_wrist_pitch_joint": -0.1,
    "right_wrist_yaw_joint": 0.0,
}
PROXIMAL = [name for name in START_JOINTS if "wrist" not in name]


def _base_body(**over):
    body = {"start_joints": dict(START_JOINTS), "twist": "cw",
            "angle_deg": 30.0, "step_deg": 3.0, "mode": "wrist_only",
            "co_rotate": True, "center_offset_deg": 135.0,
            "center_offset_cm": 3.0, "check_collision": False}
    body.update(over)
    return body


class PlanArcTests(unittest.TestCase):
    _FIELDS = ("robot_model", "ik_solver", "chain_id", "joint_names",
               "p_tool", "plane", "collision_checker")

    @classmethod
    def setUpClass(cls):
        config = load_app_config()
        robot_config = config.robots["h2"]
        cls.model = RobotModel(robot_config)
        cls.solver = NumericalIKSolver(cls.model, config.ik)

    def setUp(self):
        self._backup = {f: getattr(state, f) for f in self._FIELDS}
        self.addCleanup(self._restore)
        state.robot_model = self.model
        state.ik_solver = self.solver
        state.chain_id = "right_arm"
        state.joint_names = self.model.joint_names("right_arm")
        state.p_tool = [0.12, 0.0, -0.02]   # 捏合点：腕系 +x 指尖方向 12cm
        state.plane = None                  # 无平面 → 轴退化为根系 -X
        state.collision_checker = None

    def _restore(self):
        for field, value in self._backup.items():
            setattr(state, field, value)

    def _plan(self, **over):
        res = planning.reach_plan_arc(_base_body(**over))
        self.assertIsInstance(
            res, dict, getattr(res, "body", b"").decode("utf-8", "ignore")
            if not isinstance(res, dict) else "")
        self.assertTrue(res["ok"])
        return res

    def _arc_deviation_mm(self, res) -> tuple[float, float]:
        """实际 tcp 相对理想弧的最大半径漂移 / 轴向漂移（mm）。"""
        center = np.asarray(res["center_root"])
        axis = np.asarray(res["axis_root"])
        radius = float(res["radius_m"])
        p0 = np.asarray(res["waypoints"][0]["tcp_pose"]["xyz"])
        axial0 = float(np.dot(p0 - center, axis))
        dr_max = dz_max = 0.0
        for wp in res["waypoints"]:
            p = np.asarray(wp["tcp_pose"]["xyz"])
            axial = float(np.dot(p - center, axis))
            r = float(np.linalg.norm((p - center) - axis * axial))
            dr_max = max(dr_max, abs(r - radius))
            dz_max = max(dz_max, abs(axial - axial0))
        return dr_max * 1000.0, dz_max * 1000.0

    def test_wrist_only_freezes_proximal_and_keeps_arc(self):
        res = self._plan(mode="wrist_only")
        travel = res["joint_travel_deg"]
        for wp in res["waypoints"]:
            for name in PROXIMAL:
                self.assertAlmostEqual(
                    wp["named_joints"][name], START_JOINTS[name], places=9,
                    msg=f"{name} 应被冻结")
        self.assertEqual(res["proximal_travel_deg"], 0.0)
        dr, dz = self._arc_deviation_mm(res)
        self.assertLess(dr, 6.0, "半径漂移超过 6mm")
        self.assertLess(dz, 6.0, "轴向漂移超过 6mm")
        print(f"\n[wrist_only] 30° cw 半径3cm: 行程={travel} "
              f"弧偏差 径向{dr:.1f}mm 轴向{dz:.1f}mm "
              f"IK误差{res['max_ik_error_mm']:.1f}mm "
              f"朝向差{res['max_rot_error_deg']:.1f}°")

    def test_weighted_prefers_wrist(self):
        res = self._plan(mode="weighted")
        travel = res["joint_travel_deg"]
        wrist_max = max(v for k, v in travel.items() if "wrist" in k)
        self.assertGreater(wrist_max, 10.0, "30° 扭转腕部行程应显著")
        self.assertLess(res["proximal_travel_deg"], 5.0,
                        f"肩肘行程过大: {travel}")
        self.assertLess(res["proximal_travel_deg"], wrist_max / 3.0)
        # 大臂被正则按住后，弧线精度让步到模式容差 6mm（指垫/旋钮间隙可吸收）
        dr, dz = self._arc_deviation_mm(res)
        self.assertLess(dr, 6.5)
        self.assertLess(dz, 6.5)
        print(f"\n[weighted] 30° cw 半径3cm: 行程={travel} "
              f"弧偏差 径向{dr:.1f}mm 轴向{dz:.1f}mm "
              f"IK误差{res['max_ik_error_mm']:.1f}mm "
              f"朝向差{res['max_rot_error_deg']:.1f}°")

    def test_free_mode_baseline(self):
        res = self._plan(mode="free")
        dr, dz = self._arc_deviation_mm(res)
        self.assertLess(dr, 5.0)
        self.assertLess(dz, 5.0)
        print(f"\n[free] 30° cw 半径3cm: 行程={res['joint_travel_deg']} "
              f"弧偏差 径向{dr:.1f}mm 轴向{dz:.1f}mm "
              f"朝向差{res['max_rot_error_deg']:.1f}°")

    def test_deterministic_replan(self):
        a = self._plan(mode="wrist_only")
        b = self._plan(mode="wrist_only")
        for wa, wb in zip(a["waypoints"], b["waypoints"], strict=True):
            for name, value in wa["named_joints"].items():
                self.assertEqual(value, wb["named_joints"][name],
                                 "同参数重复规划应逐位一致（轨迹可复现）")

    def test_cw_ccw_are_mirrored_directions(self):
        cw = self._plan(mode="wrist_only", angle_deg=15.0, twist="cw")
        ccw = self._plan(mode="wrist_only", angle_deg=15.0, twist="ccw")
        p0 = np.asarray(cw["waypoints"][0]["tcp_pose"]["xyz"])
        d_cw = np.asarray(cw["waypoints"][-1]["tcp_pose"]["xyz"]) - p0
        d_ccw = np.asarray(ccw["waypoints"][-1]["tcp_pose"]["xyz"]) - p0
        # 中心在左上(135°)、顺时针 → 终点应向左上偏；逆时针反之
        self.assertLess(float(np.dot(d_cw, d_ccw)), 0.0,
                        "顺/逆时针终点位移应大体相反")

    def test_center_root_direct(self):
        p0 = self.model.tcp_pose(START_JOINTS, "right_arm",
                                 tcp_offset=None)   # 仅取个参考点验证接口
        res = self._plan(mode="wrist_only", angle_deg=10.0,
                         center_root=[p0.xyz[0], p0.xyz[1] + 0.03,
                                      p0.xyz[2] + 0.03])
        self.assertGreater(res["radius_m"], 0.01)

    def test_zero_angle_rejected(self):
        res = planning.reach_plan_arc(_base_body(angle_deg=0.0))
        self.assertNotIsInstance(res, dict)
        self.assertEqual(res.status_code, 400)


if __name__ == "__main__":
    unittest.main()
