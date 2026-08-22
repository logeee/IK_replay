from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from core.gravity_profiles import (
    DEFAULT_GRAVITY_PROFILES_PATH,
    activate_profile,
    active_profile,
    create_profile,
    load_registry,
    validate_parameters,
)


class GravityProfileTests(unittest.TestCase):
    def test_repository_keeps_baseline_and_valid_active_version(self):
        """出厂基线 0.0.0 必须原样保留；激活版本随标定推进（如 0.1.0），
        只要求指向注册表里真实存在的版本，不锁死具体值。"""
        registry = load_registry(DEFAULT_GRAVITY_PROFILES_PATH)
        by_version = {
            profile["version"]: profile for profile in registry["versions"]
        }
        baseline = by_version.get("0.0.0")
        self.assertIsNotNone(baseline, "出厂基线 0.0.0 不能被删除")
        self.assertEqual(baseline["label"], "未标定前的重力补偿版本")
        self.assertEqual(
            baseline["parameters"],
            {
                "grav_alpha": 1.0,
                "payload_kg": 0.0,
                "grav_in_float": True,
                "use_imu_gravity": False,
            },
        )
        self.assertIn(registry["active_version"], by_version)
        active = active_profile(registry)
        self.assertEqual(active["version"], registry["active_version"])
        validate_parameters(active["parameters"])

    def test_create_and_rollback_preserve_history(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "gravity.json"
            created = create_profile(
                version="0.1.0",
                label="标定一",
                description="第一批数据",
                parameters={
                    "grav_alpha": 0.92,
                    "payload_kg": 0.3,
                    "grav_in_float": True,
                    "use_imu_gravity": False,
                },
                path=path,
                activate=True,
            )
            self.assertEqual(created["parent_version"], "0.0.0")
            self.assertEqual(load_registry(path)["active_version"], "0.1.0")
            activate_profile("0.0.0", path)
            registry = load_registry(path)
            self.assertEqual(registry["active_version"], "0.0.0")
            self.assertEqual(
                [profile["version"] for profile in registry["versions"]],
                ["0.0.0", "0.1.0"],
            )
            with self.assertRaisesRegex(ValueError, "不可覆盖"):
                create_profile(
                    version="0.1.0",
                    label="试图覆盖",
                    description="",
                    parameters=created["parameters"],
                    path=path,
                )

    def test_rejects_unsafe_or_ambiguous_parameters(self):
        with self.assertRaisesRegex(ValueError, "0.0~1.2"):
            validate_parameters(
                {
                    "grav_alpha": 1.5,
                    "payload_kg": 0.0,
                    "grav_in_float": True,
                    "use_imu_gravity": False,
                }
            )
        with self.assertRaisesRegex(ValueError, "boolean"):
            validate_parameters(
                {
                    "grav_alpha": 1.0,
                    "payload_kg": 0.0,
                    "grav_in_float": "true",
                    "use_imu_gravity": False,
                }
            )


if __name__ == "__main__":
    unittest.main()
