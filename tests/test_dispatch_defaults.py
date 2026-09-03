"""外部调用默认配置（config/dispatch_defaults.json）的读/存/校验。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.dispatch_defaults import (
    DEFAULT_DISPATCH_DEFAULTS,
    DEFAULT_DISPATCH_DEFAULTS_PATH,
    DEFAULT_LIFT_MM,
    DEFAULT_PUSH_FORCE_N,
    find_offset_preset,
    load_dispatch_defaults,
    save_dispatch_defaults,
    validate_dispatch_defaults,
    validate_lift_mm,
    validate_offset_keyframes,
    validate_offset_mm,
    validate_push_force_n,
)


def _config(**overrides):
    payload = {
        "schema_version": 6,
        "defaults": {
            "site": "factory",
            "offset_preset_by_kind": {
                "close_to_remote": "",
                "remote_to_close": "",
            },
        },
        "offset_presets": [],
    }
    payload.update(overrides)
    return payload


class DispatchDefaultsTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "dispatch_defaults.json"

    def tearDown(self):
        self._tmp.cleanup()

    def test_missing_file_returns_factory_defaults(self):
        config = load_dispatch_defaults(self.path)
        self.assertEqual(config, DEFAULT_DISPATCH_DEFAULTS)
        self.assertEqual(config["defaults"]["site"], "factory")
        self.assertEqual(
            config["defaults"]["push_force_n_by_kind"],
            {
                "close_to_remote": DEFAULT_PUSH_FORCE_N,
                "remote_to_close": DEFAULT_PUSH_FORCE_N,
            },
        )

    def test_repository_config_has_independent_factory_direction_presets(self):
        config = load_dispatch_defaults(DEFAULT_DISPATCH_DEFAULTS_PATH)
        by_kind = config["defaults"]["offset_preset_by_kind"]
        self.assertIn("remote_to_close", by_kind)
        self.assertIn("close_to_remote", by_kind)
        first_by_kind = config["defaults"]["first_round_offset_wall_mm_by_kind"]
        self.assertIn("remote_to_close", first_by_kind)
        self.assertIn("close_to_remote", first_by_kind)

    def test_save_and_load_roundtrip_with_named_presets(self):
        payload = _config(
            defaults={
                "site": "lab",
                "offset_preset_by_kind": {
                    "close_to_remote": "右手偏移配置-1",
                    "remote_to_close": "备用",
                },
            },
            offset_presets=[
                {"name": "右手偏移配置-1",
                 "offset_mm": {"x": 6, "y": -2, "z": -4}},
                {"name": "备用", "offset_mm": {"x": 0}},
            ],
        )
        saved = save_dispatch_defaults(payload, self.path)
        loaded = load_dispatch_defaults(self.path)
        self.assertEqual(saved, loaded)
        self.assertEqual(
            loaded["defaults"]["offset_preset_by_kind"],
            {
                "close_to_remote": "右手偏移配置-1",
                "remote_to_close": "备用",
            },
        )
        preset = find_offset_preset(loaded, "右手偏移配置-1")
        self.assertEqual(preset["mode"], "static")
        self.assertEqual(preset["offset_mm"], {"x": 6.0, "y": -2.0, "z": -4.0})
        # 缺省轴按 0 补齐
        self.assertEqual(find_offset_preset(loaded, "备用")["offset_mm"],
                         {"x": 0.0, "y": 0.0, "z": 0.0})

    def test_rejects_bad_site(self):
        with self.assertRaisesRegex(ValueError, "site"):
            validate_dispatch_defaults(
                _config(defaults={
                    "site": "moon",
                    "offset_preset_by_kind": {
                        "close_to_remote": "",
                        "remote_to_close": "",
                    },
                }))

    def test_rejects_duplicate_preset_names(self):
        with self.assertRaisesRegex(ValueError, "重复"):
            validate_dispatch_defaults(_config(offset_presets=[
                {"name": "同名", "offset_mm": {}},
                {"name": "同名", "offset_mm": {}},
            ]))

    def test_rejects_offset_over_limit(self):
        self.assertEqual(
            validate_offset_mm({"x": 100, "y": -100}),
            {"x": 100.0, "y": -100.0, "z": 0.0},
        )
        with self.assertRaisesRegex(ValueError, "超范围"):
            validate_offset_mm({"x": 101})

    def test_rejects_default_pointing_to_missing_preset(self):
        with self.assertRaisesRegex(ValueError, "不存在的配置"):
            validate_dispatch_defaults(
                _config(defaults={
                    "site": "lab",
                    "offset_preset_by_kind": {
                        "close_to_remote": "不存在",
                        "remote_to_close": "",
                    },
                }))

    def test_v1_single_preset_migrates_to_both_directions(self):
        migrated = validate_dispatch_defaults({
            "schema_version": 1,
            "defaults": {"site": "factory", "offset_preset": "旧配置"},
            "offset_presets": [
                {"name": "旧配置", "offset_mm": {"x": 3}},
            ],
        })
        self.assertEqual(migrated["schema_version"], 6)
        self.assertEqual(
            migrated["defaults"]["offset_preset_by_kind"],
            {"close_to_remote": "旧配置", "remote_to_close": "旧配置"},
        )
        self.assertEqual(
            migrated["defaults"]["first_round_offset_wall_mm_by_kind"],
            {
                "close_to_remote": {"x": 0.0, "y": 0.0, "z": 0.0},
                "remote_to_close": {"x": 0.0, "y": 0.0, "z": 0.0},
            },
        )

    def test_first_round_offsets_are_direction_specific(self):
        payload = _config(defaults={
            "site": "factory",
            "offset_preset_by_kind": {
                "close_to_remote": "",
                "remote_to_close": "",
            },
            "first_round_offset_wall_mm_by_kind": {
                "close_to_remote": {"x": 1, "y": 12, "z": 3},
                "remote_to_close": {"x": -4, "y": 15, "z": -6},
            },
        })

        saved = save_dispatch_defaults(payload, self.path)

        self.assertEqual(
            saved["defaults"]["first_round_offset_wall_mm_by_kind"],
            {
                "close_to_remote": {"x": 1.0, "y": 12.0, "z": 3.0},
                "remote_to_close": {"x": -4.0, "y": 15.0, "z": -6.0},
            },
        )

    def test_offset_mm_accepts_missing_axes(self):
        self.assertEqual(validate_offset_mm(None),
                         {"x": 0.0, "y": 0.0, "z": 0.0})
        self.assertEqual(validate_offset_mm({"y": 3.5}),
                         {"x": 0.0, "y": 3.5, "z": 0.0})

    def test_keyframe_preset_roundtrip_sorts_and_normalizes(self):
        payload = _config(
            defaults={
                "site": "factory",
                "offset_preset_by_kind": {
                    "close_to_remote": "距离曲线",
                    "remote_to_close": "",
                },
            },
            offset_presets=[{
                "name": "距离曲线",
                "mode": "keyframes",
                "keyframes": [
                    {"distance_m": 0.60,
                     "offset_mm": {"x": 30, "y": 6, "z": -4}},
                    {"distance_m": 0.43,
                     "offset_mm": {"x": 10}},
                    {"distance_m": 0.50,
                     "offset_mm": {"x": 10}},
                ],
            }],
        )

        saved = save_dispatch_defaults(payload, self.path)
        curve = saved["offset_presets"][0]

        self.assertEqual(curve["mode"], "keyframes")
        self.assertEqual(
            [frame["distance_m"] for frame in curve["keyframes"]],
            [0.43, 0.50, 0.60],
        )
        self.assertEqual(
            curve["keyframes"][0]["offset_mm"],
            {"x": 10.0, "y": 0.0, "z": 0.0},
        )
        self.assertEqual(load_dispatch_defaults(self.path), saved)

    def test_keyframe_validation_rejects_bad_distance_and_duplicates(self):
        with self.assertRaisesRegex(ValueError, "0.01 m 对齐"):
            validate_offset_keyframes([
                {"distance_m": 0.435, "offset_mm": {}},
            ])
        with self.assertRaisesRegex(ValueError, "重复距离"):
            validate_offset_keyframes([
                {"distance_m": 0.50, "offset_mm": {}},
                {"distance_m": 0.50, "offset_mm": {"x": 1}},
            ])
        with self.assertRaisesRegex(ValueError, "超范围"):
            validate_offset_keyframes([
                {"distance_m": 0.61, "offset_mm": {}},
            ])
        with self.assertRaisesRegex(ValueError, "超范围"):
            validate_offset_keyframes([
                {"distance_m": 0.50, "offset_mm": {"z": 101}},
            ])

    def test_lift_mm_defaults_and_roundtrip(self):
        # 缺省 / 缺键都按出厂值补齐
        self.assertEqual(validate_lift_mm(None), DEFAULT_LIFT_MM)
        self.assertEqual(validate_lift_mm({"base": 5}),
                         {"base": 5.0, "step": DEFAULT_LIFT_MM["step"],
                          "max": DEFAULT_LIFT_MM["max"]})
        # 保存的默认配置里没有 lift_mm 的老文件，读出来自动补默认
        saved = save_dispatch_defaults(_config(), self.path)
        self.assertEqual(saved["defaults"]["lift_mm"], DEFAULT_LIFT_MM)
        # 显式配置能存取
        payload = _config(defaults={
            "site": "lab",
            "offset_preset_by_kind": {
                "close_to_remote": "",
                "remote_to_close": "",
            },
            "lift_mm": {"base": 0, "step": 5, "max": 20},
        })
        saved = save_dispatch_defaults(payload, self.path)
        self.assertEqual(load_dispatch_defaults(self.path)
                         ["defaults"]["lift_mm"],
                         {"base": 0.0, "step": 5.0, "max": 20.0})

    def test_lift_mm_rejects_out_of_range(self):
        with self.assertRaisesRegex(ValueError, "超范围"):
            validate_lift_mm({"base": 51})
        with self.assertRaisesRegex(ValueError, "超范围"):
            validate_lift_mm({"step": -1})
        with self.assertRaisesRegex(ValueError, "数字"):
            validate_lift_mm({"max": "很高"})

    def test_push_force_defaults_roundtrip_and_validation(self):
        self.assertEqual(validate_push_force_n(None), DEFAULT_PUSH_FORCE_N)
        self.assertEqual(validate_push_force_n(0), 0.0)
        self.assertEqual(validate_push_force_n(40), 40.0)
        payload = _config(defaults={
            "site": "factory",
            "offset_preset_by_kind": {
                "close_to_remote": "",
                "remote_to_close": "",
            },
            "push_force_n_by_kind": {
                "close_to_remote": 12.5,
                "remote_to_close": 18,
            },
        })
        saved = save_dispatch_defaults(payload, self.path)
        self.assertEqual(
            saved["defaults"]["push_force_n_by_kind"],
            {"close_to_remote": 12.5, "remote_to_close": 18.0},
        )
        self.assertEqual(
            load_dispatch_defaults(self.path)["defaults"]
            ["push_force_n_by_kind"],
            {"close_to_remote": 12.5, "remote_to_close": 18.0},
        )

    def test_v4_single_push_force_migrates_to_both_directions(self):
        migrated = validate_dispatch_defaults({
            "schema_version": 4,
            "defaults": {
                "site": "factory",
                "offset_preset_by_kind": {
                    "close_to_remote": "",
                    "remote_to_close": "",
                },
                "push_force_n": 11,
            },
            "offset_presets": [],
        })
        self.assertEqual(
            migrated["defaults"]["push_force_n_by_kind"],
            {"close_to_remote": 11.0, "remote_to_close": 11.0},
        )

    def test_push_force_rejects_out_of_range(self):
        with self.assertRaisesRegex(ValueError, "超范围"):
            validate_push_force_n(-1)
        with self.assertRaisesRegex(ValueError, "超范围"):
            validate_push_force_n(41)
        with self.assertRaisesRegex(ValueError, "数字"):
            validate_push_force_n("很大")


if __name__ == "__main__":
    unittest.main()
