"""外部调用默认配置（config/dispatch_defaults.json）的读/存/校验。"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.dispatch_defaults import (
    DEFAULT_DISPATCH_DEFAULTS,
    DEFAULT_LIFT_MM,
    find_offset_preset,
    load_dispatch_defaults,
    save_dispatch_defaults,
    validate_dispatch_defaults,
    validate_lift_mm,
    validate_offset_mm,
)


def _config(**overrides):
    payload = {
        "schema_version": 1,
        "defaults": {"site": "factory", "offset_preset": ""},
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

    def test_save_and_load_roundtrip_with_named_presets(self):
        payload = _config(
            defaults={"site": "lab", "offset_preset": "右手偏移配置-1"},
            offset_presets=[
                {"name": "右手偏移配置-1",
                 "offset_mm": {"x": 6, "y": -2, "z": -4}},
                {"name": "备用", "offset_mm": {"x": 0}},
            ],
        )
        saved = save_dispatch_defaults(payload, self.path)
        loaded = load_dispatch_defaults(self.path)
        self.assertEqual(saved, loaded)
        preset = find_offset_preset(loaded, "右手偏移配置-1")
        self.assertEqual(preset["offset_mm"], {"x": 6.0, "y": -2.0, "z": -4.0})
        # 缺省轴按 0 补齐
        self.assertEqual(find_offset_preset(loaded, "备用")["offset_mm"],
                         {"x": 0.0, "y": 0.0, "z": 0.0})

    def test_rejects_bad_site(self):
        with self.assertRaisesRegex(ValueError, "site"):
            validate_dispatch_defaults(
                _config(defaults={"site": "moon", "offset_preset": ""}))

    def test_rejects_duplicate_preset_names(self):
        with self.assertRaisesRegex(ValueError, "重复"):
            validate_dispatch_defaults(_config(offset_presets=[
                {"name": "同名", "offset_mm": {}},
                {"name": "同名", "offset_mm": {}},
            ]))

    def test_rejects_offset_over_limit(self):
        with self.assertRaisesRegex(ValueError, "超范围"):
            validate_offset_mm({"x": 51})

    def test_rejects_default_pointing_to_missing_preset(self):
        with self.assertRaisesRegex(ValueError, "不存在的配置"):
            validate_dispatch_defaults(
                _config(defaults={"site": "lab", "offset_preset": "不存在"}))

    def test_offset_mm_accepts_missing_axes(self):
        self.assertEqual(validate_offset_mm(None),
                         {"x": 0.0, "y": 0.0, "z": 0.0})
        self.assertEqual(validate_offset_mm({"y": 3.5}),
                         {"x": 0.0, "y": 3.5, "z": 0.0})

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
        payload = _config(defaults={"site": "lab", "offset_preset": "",
                                    "lift_mm": {"base": 0, "step": 5,
                                                "max": 20}})
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


if __name__ == "__main__":
    unittest.main()
