from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from api.flow import SwitchFlow
from core.alignment_config import (
    DEFAULT_ALIGNMENT_CONFIG_PATH,
    load_alignment_config,
    save_alignment_config,
    validate_alignment_config,
)


class AlignmentConfigTests(unittest.TestCase):
    def test_repository_config_has_requested_asymmetric_acceptance_ranges(self):
        config = load_alignment_config(DEFAULT_ALIGNMENT_CONFIG_PATH)
        self.assertEqual(config["coarse"]["target_deg"], -7.0)
        self.assertEqual(config["coarse"]["accept_min_deg"], -8.5)
        self.assertEqual(config["coarse"]["accept_max_deg"], 0.0)
        self.assertEqual(config["fine"]["target_deg"], -3.0)
        self.assertEqual(config["fine"]["command_tolerance_deg"], 1.5)
        self.assertEqual(config["fine"]["accept_min_deg"], -5.0)
        self.assertEqual(config["fine"]["accept_max_deg"], 5.0)

    def test_save_and_load_round_trip_validated_config(self):
        config = load_alignment_config(DEFAULT_ALIGNMENT_CONFIG_PATH)
        config["coarse"]["target_deg"] = -6.0
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "waist.json"
            saved = save_alignment_config(config, path)
            loaded = load_alignment_config(path)
            raw = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(saved, loaded)
        self.assertEqual(raw, loaded)
        self.assertEqual(loaded["coarse"]["target_deg"], -6.0)

    def test_rejects_invalid_or_unsafe_ranges(self):
        config = load_alignment_config(DEFAULT_ALIGNMENT_CONFIG_PATH)
        config["fine"]["accept_min_deg"] = 8.0
        with self.assertRaisesRegex(ValueError, "最小值"):
            validate_alignment_config(config)

        config = load_alignment_config(DEFAULT_ALIGNMENT_CONFIG_PATH)
        config["coarse"]["target_deg"] = 5.0
        with self.assertRaisesRegex(ValueError, "目标角"):
            validate_alignment_config(config)


class _MeasureOnlyClient:
    def __init__(self, yaw: float):
        self.yaw = yaw
        self.align_started = False

    def perpendicular(self, _dmin: float, _dmax: float) -> dict:
        return {"ok": True, "yaw_err_deg": self.yaw}

    def align_yaw_start(self, *_args, **_kwargs):
        self.align_started = True
        raise AssertionError("yaw 已在非对称验收范围内，不应启动转身")


class FlowAcceptanceRangeTests(unittest.TestCase):
    def test_coarse_upper_zero_and_fine_upper_five_are_accepted(self):
        coarse_client = _MeasureOnlyClient(-1.0)
        coarse_flow = SwitchFlow(client=coarse_client)
        coarse_flow.waist_align(-7.0, -8.5, 0.0, 0.75)
        self.assertFalse(coarse_client.align_started)

        fine_client = _MeasureOnlyClient(4.0)
        fine_flow = SwitchFlow(client=fine_client)
        fine_flow.waist_align(-3.0, -5.0, 5.0, 1.0)
        self.assertFalse(fine_client.align_started)


if __name__ == "__main__":
    unittest.main()
