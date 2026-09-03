from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from core import capability_registry as reg


class SequenceClaimTests(unittest.TestCase):
    """认领挂在能力条目上：拨/扭各认各的，seed 自带 cap-rtl-flick /
    cap-ltr-flick 两个条目。"""

    def _with_claims(self, claims):
        seed = reg.seed_registry()
        seed["sequence_claims"] = claims
        return reg.validate_registry(seed)

    def test_claims_normalized_deduped_sorted(self):
        registry = self._with_claims([{
            "capability_id": "cap-rtl-flick",
            "names": ["b-起手式", "a-起手式", "b-起手式", "", None],
            "waypoint_names": ["点2", "点1", "点2", ""],
        }])
        self.assertEqual(registry["sequence_claims"][0]["names"],
                         ["a-起手式", "b-起手式"])
        self.assertEqual(registry["sequence_claims"][0]["waypoint_names"],
                         ["点1", "点2"])

    def test_waypoint_names_defaults_to_empty(self):
        registry = self._with_claims([{
            "capability_id": "cap-rtl-flick", "names": ["a"],
        }])
        self.assertEqual(registry["sequence_claims"][0]["waypoint_names"], [])

    def test_claim_with_unknown_capability_rejected(self):
        with self.assertRaisesRegex(ValueError, "不存在的能力条目"):
            self._with_claims([{"capability_id": "cap-ghost", "names": []}])

    def test_duplicate_claim_capability_rejected(self):
        with self.assertRaisesRegex(ValueError, "认领条目重复"):
            self._with_claims([
                {"capability_id": "cap-rtl-flick", "names": []},
                {"capability_id": "cap-rtl-flick", "names": ["x"]},
            ])

    def test_claimed_names_helper_absent_capability_is_empty(self):
        registry = self._with_claims([{
            "capability_id": "cap-rtl-flick", "names": ["a-起手式"],
        }])
        self.assertEqual(
            reg.claimed_sequence_names(registry, "cap-rtl-flick"),
            ["a-起手式"])
        # 严格语义：没有认领记录 = 空（一个都不能用）
        self.assertEqual(
            reg.claimed_sequence_names(registry, "cap-ltr-flick"), [])

    def test_effective_pattern_prefers_capability_own(self):
        registry = reg.validate_registry(reg.seed_registry())
        rtl = next(c for c in registry["capabilities"]
                   if c["id"] == "cap-rtl-flick")
        # seed 自配了正则 → 用自己的
        self.assertEqual(reg.effective_pose_pattern(rtl),
                         rtl["assets"]["pose_pattern"])
        # 清掉自配 → 按方向落回内置
        rtl["assets"]["pose_pattern"] = ""
        self.assertEqual(reg.effective_pose_pattern(rtl),
                         reg.BUILTIN_POSE_PATTERNS["rtl"])
        # cw/ccw 没有内置 → None
        rtl["task"]["direction"] = "cw"
        self.assertIsNone(reg.effective_pose_pattern(rtl))

    def test_route_claim_separates_flick_families(self):
        registry = reg.validate_registry(reg.seed_registry())
        self.assertEqual(
            reg.route_sequence_claim(registry, "right_arm",
                                     "yinshi-1-right", "0.50-起手式新"),
            ["cap-rtl-flick"])
        self.assertEqual(
            reg.route_sequence_claim(registry, "right_arm",
                                     "yinshi-1-right", "0.50-左-起手式"),
            ["cap-ltr-flick"])
        # 扭命名不命中任何拨条目 → 留池
        self.assertEqual(
            reg.route_sequence_claim(registry, "right_arm",
                                     "yinshi-1-right", "0.50-扭-起手式"),
            [])
        # 别的组合下没有条目 → 不路由
        self.assertEqual(
            reg.route_sequence_claim(registry, "left_arm",
                                     "yinshi-1-right", "0.50-起手式新"),
            [])

    def test_route_claim_matches_twist_capability_pattern(self):
        seed = reg.seed_registry()
        seed["hands"].append({
            "id": "qiangnao-1-right", "name": "强脑-右-1",
            "design_side": "right", "tool_out_mm": 12.0, "notes": "",
        })
        seed["capabilities"].append({
            "id": "cap-cw-twist", "arm": "right_arm",
            "hand_id": "qiangnao-1-right",
            "task": {"name": "扭旋钮", "direction": "cw",
                     "sites": ["lab"]},
            "method": "twist", "method_params": {},
            "assets": {
                "pose_pattern": r"^\s*(\d+(?:\.\d+)?)-扭-起手式\s*$",
                "endpoint_pattern": "",
            },
            "enabled": False, "notes": "",
        })
        registry = reg.validate_registry(seed)
        # 扭命名只归扭条目（停用也算，录制时临时停用不丢认领）
        self.assertEqual(
            reg.route_sequence_claim(registry, "right_arm",
                                     "qiangnao-1-right", "0.50-扭-起手式"),
            ["cap-cw-twist"])
        # 拨命名在强脑组合下没有条目 → 不路由
        self.assertEqual(
            reg.route_sequence_claim(registry, "right_arm",
                                     "qiangnao-1-right", "0.50-起手式新"),
            [])


class EndpointDerivationTests(unittest.TestCase):
    """终点位点推导：与 api/flow.py 运行时规则一致。"""

    def test_from_last_waypoint_file_strips_stamp(self):
        self.assertEqual(
            reg.derive_endpoint_name(
                "0.49-起手式新", "0.49-起手式新终点_20260822_031632.json"),
            "0.49-起手式新终点")

    def test_naming_fallback_left_family(self):
        self.assertEqual(reg.derive_endpoint_name("0.49-左-起手式"),
                         "0.49-左-终点")

    def test_naming_fallback_default_family(self):
        self.assertEqual(reg.derive_endpoint_name("0.49-起手式新"),
                         "0.49-起手式新终点")


class SequencePoolTests(unittest.TestCase):
    @staticmethod
    def _write_sequence(root: Path, filename: str, payload: dict):
        directory = root / "data" / "sequences"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    @staticmethod
    def _write_waypoint(root: Path, filename: str, payload: dict):
        directory = root / "data" / "waypoints"
        directory.mkdir(parents=True, exist_ok=True)
        (directory / filename).write_text(
            json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def test_pool_groups_by_name_with_latest_metadata(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_sequence(root, "0.50-起手式新_20260822_031632.json", {
                "name": "0.50-起手式新", "chain_id": "right_arm",
                "created_at": "2026-08-22 03:16:32",
            })
            self._write_sequence(root, "0.50-起手式新_20260901_100000.json", {
                "name": "0.50-起手式新", "chain_id": "right_arm",
                "created_at": "2026-09-01 10:00:00",
                "recorded_combo": {"arm": "right_arm",
                                   "hand_id": "qiangnao-1-right"},
            })
            self._write_sequence(root, "扭旋钮-起手式_20260903_000000.json", {
                "name": "扭旋钮-起手式",
                "created_at": "2026-09-03 00:00:00",
            })
            (root / "data" / "sequences" / "bad.json").write_text(
                "{oops", encoding="utf-8")
            pool = reg.sequence_pool(root)
            self.assertEqual([entry["name"] for entry in pool],
                             sorted(["0.50-起手式新", "扭旋钮-起手式"]))
            merged = next(entry for entry in pool
                          if entry["name"] == "0.50-起手式新")
            self.assertEqual(merged["files"], 2)
            self.assertEqual(merged["latest_file"],
                             "0.50-起手式新_20260901_100000.json")
            self.assertEqual(merged["recorded_combo"],
                             {"arm": "right_arm",
                              "hand_id": "qiangnao-1-right"})

    def test_pool_name_falls_back_to_stem_without_stamp(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_sequence(root, "无名动作_20260101_000000.json", {})
            pool = reg.sequence_pool(root)
            self.assertEqual([entry["name"] for entry in pool], ["无名动作"])

    def test_pool_entry_carries_endpoint_from_last_waypoint(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_sequence(root, "0.50-起手式新_20260822_031632.json", {
                "name": "0.50-起手式新",
                "created_at": "2026-08-22 03:16:32",
                "waypoints": ["中间点_20260822_031000.json",
                              "0.50-起手式新终点_20260822_031632.json"],
            })
            entry = reg.sequence_pool(root)[0]
            self.assertEqual(entry["endpoint_name"], "0.50-起手式新终点")

    def test_waypoint_pool_groups_by_name(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            self._write_waypoint(root, "录制点位1_20260726_151627.json", {
                "name": "录制点位1", "chain_id": "right_arm",
                "created_at": "2026-07-26 15:16:27",
            })
            self._write_waypoint(root, "录制点位1_20260901_000000.json", {
                "name": "录制点位1", "chain_id": "right_arm",
                "created_at": "2026-09-01 00:00:00",
            })
            pool = reg.waypoint_pool(root)
            self.assertEqual(len(pool), 1)
            self.assertEqual(pool[0]["files"], 2)
            self.assertEqual(pool[0]["latest_file"],
                             "录制点位1_20260901_000000.json")

    def test_claimed_waypoints_union_manual_and_derived(self):
        seed = reg.seed_registry()
        seed["sequence_claims"] = [{
            "capability_id": "cap-rtl-flick",
            "names": ["0.50-起手式新", "孤儿动作"],
            "waypoint_names": ["录制点位1", "起手点测试"],
        }]
        registry = reg.validate_registry(seed)
        pool = [{"name": "0.50-起手式新",
                 "endpoint_name": "0.50-起手式新终点"}]
        effective = reg.claimed_waypoint_names(registry, "cap-rtl-flick",
                                               pool)
        # 手选 ∪ 池内推导终点 ∪ 池外起手式的命名兜底终点
        self.assertEqual(effective, sorted([
            "录制点位1", "起手点测试", "0.50-起手式新终点", "孤儿动作终点",
        ]))
        # 没有认领记录 → 空
        self.assertEqual(
            reg.claimed_waypoint_names(registry, "cap-ltr-flick", pool), [])

    def test_migration_from_missing_key_routes_pool_by_pattern(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "capability_registry.json"
            legacy = reg.seed_registry()
            legacy.pop("sequence_claims")   # 模拟最早期文件：还没有该键
            path.write_text(json.dumps(legacy, ensure_ascii=False),
                            encoding="utf-8")
            self._write_sequence(root, "0.50-起手式新_20260822_031632.json", {
                "name": "0.50-起手式新",
                "created_at": "2026-08-22 03:16:32",
            })
            self._write_sequence(root, "0.48-左-起手式_20260826_144000.json", {
                "name": "0.48-左-起手式",
                "created_at": "2026-08-26 14:40:00",
            })
            self._write_sequence(root, "0.44避障起手式_20260730_180703.json", {
                "name": "0.44避障起手式",
                "created_at": "2026-07-30 18:07:03",
            })
            self.assertTrue(reg.migrate_sequence_claims(path, root))
            registry = reg.load_registry(path)
            # 按正则拆到条目：起手式新→rtl、左-起手式→ltr、避障遗留→无人认领
            self.assertEqual(
                reg.claimed_sequence_names(registry, "cap-rtl-flick"),
                ["0.50-起手式新"])
            self.assertEqual(
                reg.claimed_sequence_names(registry, "cap-ltr-flick"),
                ["0.48-左-起手式"])
            all_claimed = {n for c in registry["sequence_claims"]
                           for n in c["names"]}
            self.assertNotIn("0.44避障起手式", all_claimed)
            # flick 条目预置流程必需公共位点
            for claim in registry["sequence_claims"]:
                self.assertEqual(claim["waypoint_names"],
                                 sorted(reg.FLOW_REQUIRED_WAYPOINTS))
            # 已是新格式 → 不再迁移（清空认领也不会被重新填回）
            self.assertFalse(reg.migrate_sequence_claims(path, root))

    def test_migration_converts_combo_format_to_capability(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "capability_registry.json"
            legacy = reg.seed_registry()
            legacy["sequence_claims"] = [{   # 组合级旧格式
                "arm": "right_arm", "hand_id": "yinshi-1-right",
                "names": ["0.50-起手式新", "0.48-左-起手式", "孤儿动作"],
            }]
            path.write_text(json.dumps(legacy, ensure_ascii=False),
                            encoding="utf-8")
            self.assertTrue(reg.migrate_sequence_claims(path, root))
            registry = reg.load_registry(path)
            self.assertEqual(
                reg.claimed_sequence_names(registry, "cap-rtl-flick"),
                ["0.50-起手式新"])
            self.assertEqual(
                reg.claimed_sequence_names(registry, "cap-ltr-flick"),
                ["0.48-左-起手式"])

    def test_migration_adds_waypoint_field_to_capability_format(self):
        """条目级但缺 waypoint_names 的中间格式 → 只补位点字段。"""
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "capability_registry.json"
            legacy = reg.seed_registry()
            legacy["sequence_claims"] = [
                {"capability_id": "cap-rtl-flick",
                 "names": ["0.50-起手式新"]},   # 无 waypoint_names 键
            ]
            path.write_text(json.dumps(legacy, ensure_ascii=False),
                            encoding="utf-8")
            self.assertTrue(reg.migrate_sequence_claims(path, root))
            registry = reg.load_registry(path)
            claim = registry["sequence_claims"][0]
            self.assertEqual(claim["names"], ["0.50-起手式新"])   # 认领保留
            self.assertEqual(claim["waypoint_names"],
                             sorted(reg.FLOW_REQUIRED_WAYPOINTS))
            self.assertFalse(reg.migrate_sequence_claims(path, root))

    def test_migration_skips_new_format(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            path = root / "capability_registry.json"
            reg.save_registry(reg.seed_registry(), path)   # 含空 claims 键
            self.assertFalse(reg.migrate_sequence_claims(path, root))
            registry = reg.load_registry(path)
            self.assertEqual(registry["sequence_claims"], [])


class SeedAndPersistenceTests(unittest.TestCase):
    def test_seed_contains_two_verified_capabilities(self):
        seed = reg.seed_registry()
        self.assertEqual(seed["active"],
                         {"arm": "right_arm", "hand_id": "yinshi-1-right"})
        directions = {cap["task"]["direction"]
                      for cap in seed["capabilities"]}
        self.assertEqual(directions, {"rtl", "ltr"})
        for cap in seed["capabilities"]:
            self.assertEqual(cap["method"], "flick")
            self.assertTrue(cap["enabled"])
            # 参数块补齐为现有代码默认值
            self.assertEqual(cap["method_params"], {
                "sidestep_cm": 10.0, "push_force_n": 15.0,
                "push_hold_s": 1.5, "down_deg": 15.0,
            })

    def test_seed_sites_reproduce_site_supported_kinds(self):
        seed = reg.seed_registry()
        rtl = next(c for c in seed["capabilities"]
                   if c["task"]["direction"] == "rtl")
        ltr = next(c for c in seed["capabilities"]
                   if c["task"]["direction"] == "ltr")
        self.assertEqual(rtl["task"]["sites"], ["lab", "factory"])
        self.assertEqual(ltr["task"]["sites"], ["factory"])

    def test_ensure_registry_writes_seed_on_first_run(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "capability_registry.json"
            registry = reg.ensure_registry(path, root=Path(temporary))
            self.assertTrue(path.is_file())
            on_disk = json.loads(path.read_text(encoding="utf-8"))
            self.assertEqual(on_disk["schema_version"], 1)
            self.assertEqual(len(on_disk["capabilities"]), 2)
            # 再次加载与首次一致
            self.assertEqual(reg.load_registry(path), registry)

    def test_save_and_reload_roundtrip(self):
        with tempfile.TemporaryDirectory() as temporary:
            path = Path(temporary) / "capability_registry.json"
            seed = reg.seed_registry()
            seed["hands"].append({
                "id": "qiangnao-1-left", "name": "强脑-左-1",
                "design_side": "left", "tool_out_mm": 12.0, "notes": "",
            })
            saved = reg.save_registry(seed, path)
            self.assertEqual(reg.load_registry(path), saved)
            self.assertEqual(len(saved["hands"]), 2)


class ValidationTests(unittest.TestCase):
    def _seed(self):
        return reg.seed_registry()

    def test_rejects_duplicate_hand_name(self):
        seed = self._seed()
        seed["hands"].append({
            "id": "another", "name": "因时-右-1", "design_side": "right",
        })
        with self.assertRaises(ValueError):
            reg.validate_registry(seed)

    def test_rejects_capability_with_unknown_hand(self):
        seed = self._seed()
        seed["capabilities"][0]["hand_id"] = "no-such-hand"
        with self.assertRaises(ValueError):
            reg.validate_registry(seed)

    def test_rejects_unknown_method_param(self):
        seed = self._seed()
        seed["capabilities"][0]["method_params"] = {"bogus": 1.0}
        with self.assertRaises(ValueError):
            reg.validate_registry(seed)

    def test_rejects_out_of_range_push_hold(self):
        seed = self._seed()
        seed["capabilities"][0]["method_params"] = {"push_hold_s": 9.0}
        with self.assertRaises(ValueError):
            reg.validate_registry(seed)

    def test_rejects_pose_pattern_without_group(self):
        seed = self._seed()
        seed["capabilities"][0]["assets"]["pose_pattern"] = r"^起手式$"
        with self.assertRaises(ValueError):
            reg.validate_registry(seed)

    def test_rejects_active_pointing_to_missing_hand(self):
        seed = self._seed()
        seed["active"] = {"arm": "right_arm", "hand_id": "ghost"}
        with self.assertRaises(ValueError):
            reg.validate_registry(seed)

    def test_twist_method_has_empty_params(self):
        seed = self._seed()
        seed["capabilities"][0]["method"] = "twist"
        seed["capabilities"][0]["method_params"] = {}
        validated = reg.validate_registry(seed)
        self.assertEqual(validated["capabilities"][0]["method_params"], {})

    def test_capability_id_autogenerated_when_missing(self):
        seed = self._seed()
        seed["capabilities"][0]["id"] = ""
        validated = reg.validate_registry(seed)
        self.assertTrue(validated["capabilities"][0]["id"].startswith("cap-"))


class CalibrationTests(unittest.TestCase):
    def test_calib_rel_path_is_combo_unique(self):
        self.assertEqual(
            reg.calib_rel_path("right_arm", "yinshi-1-right"),
            "config/hand_eye/right_arm__yinshi-1-right/handeye3d_result.json",
        )

    def test_import_copies_from_source_path(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            source = root / "external" / "handeye3d_result.json"
            source.parent.mkdir(parents=True)
            source.write_text(json.dumps({
                "T_cam2base": [[1, 0, 0, 0]], "solved_at": "2026-08-01",
                "residual_mm": 2.5, "num_samples": 30,
            }), encoding="utf-8")
            registry = reg.seed_registry()
            calib = registry["calibrations"][0]
            calib["source_path"] = str(source)
            self.assertTrue(reg.try_import_calibration(calib, root))
            archived = root / calib["path"]
            self.assertTrue(archived.is_file())
            info = reg.calibration_info(
                registry, "right_arm", "yinshi-1-right", root)
            self.assertEqual(info["status"], "ready")
            self.assertEqual(info["residual_mm"], 2.5)
            self.assertEqual(info["num_samples"], 30)

    def test_mount_fields_exposed_with_suggested_tool_out(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = reg.seed_registry()
            calib = registry["calibrations"][0]
            archived = root / calib["path"]
            archived.parent.mkdir(parents=True)
            archived.write_text(json.dumps({
                "T_cam2base": [[1, 0, 0, 0]], "solved_at": "2026-09-01",
                "residual_mm": {"rms": 3.1}, "num_samples": 24,
                "p_tool_wrist_m": [0.155, 0.01, -0.02],
                "T_wrist2hand": [[1, 0, 0, 0], [0, 1, 0, 0],
                                 [0, 0, 1, 0], [0, 0, 0, 1]],
                "mount_solved_at": "2026-09-01T21:00:00",
                "mount_residual_mm": {"rms": 2.2},
                "tcp_points_wrist_m": [
                    {"id": "tip:R_thumb_tip", "p_wrist_m": [0.12, -0.04, 0.01]},
                    {"id": "tip:R_index_tip", "p_wrist_m": [0.172, 0.012, -0.018]},
                ],
            }), encoding="utf-8")
            info = reg.calibration_info(
                registry, "right_arm", "yinshi-1-right", root)
            self.assertEqual(info["status"], "ready")
            self.assertTrue(info["has_mount"])
            self.assertEqual(info["mount_solved_at"], "2026-09-01T21:00:00")
            self.assertEqual(info["mount_residual_mm"], {"rms": 2.2})
            self.assertAlmostEqual(info["suggested_tool_out_mm"], 17.0)

    def test_mount_absent_keeps_fields_null(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = reg.seed_registry()
            calib = registry["calibrations"][0]
            archived = root / calib["path"]
            archived.parent.mkdir(parents=True)
            archived.write_text(json.dumps({
                "T_cam2base": [[1, 0, 0, 0]], "num_samples": 12,
            }), encoding="utf-8")
            info = reg.calibration_info(
                registry, "right_arm", "yinshi-1-right", root)
            self.assertEqual(info["status"], "ready")
            self.assertFalse(info["has_mount"])
            self.assertIsNone(info["suggested_tool_out_mm"])

    def test_missing_source_reports_pending(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            registry = reg.seed_registry()
            calib = registry["calibrations"][0]
            calib["source_path"] = str(root / "nowhere.json")
            self.assertFalse(reg.try_import_calibration(calib, root))
            info = reg.calibration_info(
                registry, "right_arm", "yinshi-1-right", root)
            self.assertEqual(info["status"], "pending")

    def test_unregistered_combo_reports_missing(self):
        registry = reg.seed_registry()
        info = reg.calibration_info(
            registry, "left_arm", "yinshi-1-right",
            Path(tempfile.gettempdir()) / "no-such-root")
        self.assertEqual(info["status"], "missing")


class QueryTests(unittest.TestCase):
    def test_capability_for_matches_direction_and_site(self):
        registry = reg.seed_registry()
        rtl = reg.capability_for(
            registry, "right_arm", "yinshi-1-right", "rtl", "lab")
        self.assertIsNotNone(rtl)
        self.assertEqual(rtl["task"]["name"], "旋钮右到左")
        # 向右拨（ltr）只在 factory 验证过 → lab 不匹配
        self.assertIsNone(reg.capability_for(
            registry, "right_arm", "yinshi-1-right", "ltr", "lab"))
        self.assertIsNotNone(reg.capability_for(
            registry, "right_arm", "yinshi-1-right", "ltr", "factory"))

    def test_disabled_capability_not_matched(self):
        registry = reg.seed_registry()
        for cap in registry["capabilities"]:
            cap["enabled"] = False
        self.assertIsNone(reg.capability_for(
            registry, "right_arm", "yinshi-1-right", "rtl", "factory"))


if __name__ == "__main__":
    unittest.main()
