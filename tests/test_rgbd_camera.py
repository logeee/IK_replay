from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path

import cv2
import numpy as np

from camera_sources.alignment import RGBDCalibration, SoftwareDepthAligner
from camera_sources.zmq_rgbd import ZmqRGBDCamera, decode_rgbd_parts


def calibration(
    *,
    color_shape=(3, 4),
    depth_shape=(3, 4),
    color_fx=1.0,
    color_fy=1.0,
    color_cx=0.0,
    color_cy=0.0,
    depth_fx=1.0,
    depth_fy=1.0,
    depth_cx=0.0,
    depth_cy=0.0,
    rotation=None,
    translation=None,
    depth_scale_mm=1.0,
    color_distortion=None,
    depth_distortion=None,
):
    return RGBDCalibration(
        path=Path("/tmp/test-calibration.json"),
        serial="TEST",
        color_shape=color_shape,
        depth_shape=depth_shape,
        color_matrix=np.array(
            [[color_fx, 0.0, color_cx], [0.0, color_fy, color_cy], [0.0, 0.0, 1.0]],
            dtype=np.float64,
        ),
        depth_matrix=np.array(
            [[depth_fx, 0.0, depth_cx], [0.0, depth_fy, depth_cy], [0.0, 0.0, 1.0]],
            dtype=np.float64,
        ),
        color_distortion=np.zeros(8, dtype=np.float64) if color_distortion is None else np.asarray(color_distortion, dtype=np.float64),
        depth_distortion=np.zeros(8, dtype=np.float64) if depth_distortion is None else np.asarray(depth_distortion, dtype=np.float64),
        depth_to_color_rotation=np.eye(3, dtype=np.float64) if rotation is None else np.asarray(rotation, dtype=np.float64),
        depth_to_color_translation_mm=np.zeros(3, dtype=np.float64) if translation is None else np.asarray(translation, dtype=np.float64),
        depth_scale_mm=depth_scale_mm,
    )


def _stream_payload(width: int, height: int) -> dict:
    return {
        "width": width,
        "height": height,
        "fps": 30,
        "format": "Y16",
        "intrinsics": {
            "width": width,
            "height": height,
            "fx": 100.0,
            "fy": 101.0,
            "cx": width / 2,
            "cy": height / 2,
        },
        "distortion": {
            "model": "brown_conrady",
            "coefficient_order": ["k1", "k2", "p1", "p2", "k3", "k4", "k5", "k6"],
            "coefficients": [0.0] * 8,
        },
    }


def write_calibration(path: Path, *, color_shape=(3, 4), depth_shape=(3, 4)) -> Path:
    color_h, color_w = color_shape
    depth_h, depth_w = depth_shape
    payload = {
        "schema_version": 1,
        "device": {"serial": "TEST"},
        "color": _stream_payload(color_w, color_h),
        "depth": _stream_payload(depth_w, depth_h),
        "depth_to_color": {
            "rotation_row_major": np.eye(3).tolist(),
            "translation": [0.0, 0.0, 0.0],
            "translation_unit": "mm",
        },
        "depth_scale": {"value": 1.0, "unit": "mm_per_raw_unit"},
    }
    path.write_text(json.dumps(payload), encoding="utf-8")
    return path


class SoftwareDepthAlignerTest(unittest.TestCase):
    def test_identity_calibration_preserves_depth_pixels(self):
        raw = np.array(
            [[100, 200, 300, 400], [500, 0, 700, 800], [900, 1000, 1100, 1200]],
            dtype=np.uint16,
        )
        aligned = SoftwareDepthAligner(calibration()).align(raw)
        np.testing.assert_array_equal(aligned, raw.astype(np.float32))

    def test_z_buffer_keeps_nearest_depth(self):
        calib = calibration(color_shape=(1, 1), depth_shape=(1, 2), color_fx=0.1)
        raw = np.array([[500, 100]], dtype=np.uint16)
        aligned = SoftwareDepthAligner(calib).align(raw)
        self.assertEqual(float(aligned[0, 0]), 100.0)

    def test_rejects_wrong_shape_and_dtype(self):
        aligner = SoftwareDepthAligner(calibration())
        with self.assertRaisesRegex(ValueError, "shape"):
            aligner.align(np.zeros((2, 2), dtype=np.uint16))
        with self.assertRaisesRegex(ValueError, "dtype"):
            aligner.align(np.zeros((3, 4), dtype=np.float32))

    def test_depth_scale_converts_raw_units_to_mm(self):
        raw = np.array([[0, 10]], dtype=np.uint16)
        aligned = SoftwareDepthAligner(
            calibration(color_shape=(1, 2), depth_shape=(1, 2), depth_scale_mm=2.5)
        ).align(raw)
        self.assertEqual(float(aligned[0, 1]), 25.0)

    def test_unproject_depth_samples_raw_geometry_without_color_alignment(self):
        raw = np.array(
            [[1000, 2000, 3000, 4000], [5000, 6000, 7000, 8000]],
            dtype=np.uint16,
        )
        aligner = SoftwareDepthAligner(
            calibration(
                color_shape=(4, 8),
                depth_shape=(2, 4),
                depth_scale_mm=1.0,
                translation=np.array([500.0, 0.0, 0.0]),
            )
        )
        points, measured = aligner.unproject_depth(raw, stride=2)
        self.assertEqual(measured, 2)
        np.testing.assert_allclose(
            points,
            np.array([[0.0, 0.0, 1.0], [6.0, 0.0, 3.0]], dtype=np.float32),
        )

    def test_known_translation_shifts_projection(self):
        raw = np.array([[1000]], dtype=np.uint16)
        aligned = SoftwareDepthAligner(
            calibration(
                color_shape=(1, 3),
                depth_shape=(1, 1),
                color_cx=0.0,
                translation=np.array([1000.0, 0.0, 0.0]),
            )
        ).align(raw)
        self.assertEqual(float(aligned[0, 1]), 1000.0)
        self.assertEqual(float(aligned[0, 0]), 0.0)

    def test_known_rotation_moves_point_out_of_view(self):
        raw = np.array([[1000]], dtype=np.uint16)
        rotation = np.array(
            [[0.0, 0.0, 1.0], [0.0, 1.0, 0.0], [-1.0, 0.0, 0.0]],
            dtype=np.float64,
        )
        aligned = SoftwareDepthAligner(
            calibration(color_shape=(1, 1), depth_shape=(1, 1), rotation=rotation)
        ).align(raw)
        self.assertEqual(float(aligned[0, 0]), 0.0)

    def test_out_of_bounds_points_are_dropped(self):
        raw = np.array([[1000]], dtype=np.uint16)
        aligned = SoftwareDepthAligner(
            calibration(
                color_shape=(1, 1),
                depth_shape=(1, 1),
                translation=np.array([5000.0, 0.0, 0.0]),
            )
        ).align(raw)
        self.assertEqual(float(aligned[0, 0]), 0.0)

    def test_color_distortion_shifts_pixel(self):
        raw = np.array([[0, 1000]], dtype=np.uint16)
        undistorted = SoftwareDepthAligner(
            calibration(color_shape=(1, 5), depth_shape=(1, 2), color_cx=0.0)
        ).align(raw)
        distorted = SoftwareDepthAligner(
            calibration(
                color_shape=(1, 5),
                depth_shape=(1, 2),
                color_cx=0.0,
                color_distortion=[1.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0, 0.0],
            )
        ).align(raw)
        self.assertEqual(float(undistorted[0, 1]), 1000.0)
        self.assertEqual(float(distorted[0, 1]), 0.0)
        self.assertEqual(float(distorted[0, 2]), 1000.0)


class CalibrationFileTest(unittest.TestCase):
    def test_loads_exported_schema(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibration.json"
            write_calibration(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["depth_to_color"]["translation"] = [1.0, 2.0, 3.0]
            path.write_text(json.dumps(payload), encoding="utf-8")
            loaded = RGBDCalibration.from_file(path)
        self.assertEqual(loaded.color_shape, (3, 4))
        self.assertEqual(loaded.depth_shape, (3, 4))
        self.assertEqual(loaded.serial, "TEST")
        np.testing.assert_array_equal(
            loaded.depth_to_color_translation_mm, np.array([1.0, 2.0, 3.0])
        )

    def test_rejects_missing_file_and_wrong_units(self):
        with self.assertRaisesRegex(ValueError, "不存在"):
            RGBDCalibration.from_file("/tmp/does-not-exist-orbbec.json")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "calibration.json"
            write_calibration(path)
            payload = json.loads(path.read_text(encoding="utf-8"))
            payload["depth_to_color"]["translation_unit"] = "m"
            path.write_text(json.dumps(payload), encoding="utf-8")
            with self.assertRaisesRegex(ValueError, "translation_unit"):
                RGBDCalibration.from_file(path)


class ZmqProtocolTest(unittest.TestCase):
    def test_decodes_existing_teleimager_protocol(self):
        calib = calibration()
        bgr = np.zeros((3, 4, 3), dtype=np.uint8)
        ok, encoded = cv2.imencode(".jpg", bgr)
        self.assertTrue(ok)
        depth = np.arange(12, dtype=np.uint16).reshape(3, 4)
        metadata = {
            "data_format": "rgbd",
            "color_format": "jpeg",
            "depth_format": "depth_z16",
            "depth_dtype": "uint16",
            "color_shape": [3, 4],
            "depth_shape": [3, 4],
        }
        decoded_metadata, jpeg, decoded_depth = decode_rgbd_parts(
            [json.dumps(metadata).encode(), encoded.tobytes(), depth.tobytes()],
            calib,
            verify_jpeg_shape=True,
        )
        self.assertEqual(decoded_metadata, metadata)
        self.assertEqual(jpeg, encoded.tobytes())
        np.testing.assert_array_equal(decoded_depth, depth)

    def test_rejects_profile_mismatch(self):
        metadata = {
            "data_format": "rgbd",
            "color_format": "jpeg",
            "depth_format": "depth_z16",
            "depth_dtype": "uint16",
            "color_shape": [1080, 1920],
            "depth_shape": [800, 1280],
        }
        with self.assertRaisesRegex(ValueError, "color shape"):
            decode_rgbd_parts(
                [json.dumps(metadata).encode(), b"jpeg", bytes(800 * 1280 * 2)],
                calibration(),
            )


class ProductionSafetyTest(unittest.TestCase):
    def test_zmq_package_does_not_import_orbbec_sdk(self):
        blocked = {name for name in sys.modules if "orbbec" in name.lower()}
        self.assertFalse(blocked)
        import camera_sources
        import camera_sources.alignment
        import camera_sources.zmq_rgbd
        self.assertFalse(any("orbbec" in name.lower() for name in sys.modules))
        self.assertIs(camera_sources.ZmqRGBDCamera, ZmqRGBDCamera)

    def test_exporter_help_does_not_need_sdk(self):
        import subprocess

        root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            [sys.executable, str(root / "tools" / "export_orbbec_rgbd_calibration.py"), "--help"],
            check=False,
            capture_output=True,
            text=True,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("Orbbec SDK", result.stdout)
        self.assertNotIn("pyorbbecsdk", sys.modules)


class CompareToolTest(unittest.TestCase):
    def test_offline_compare_reports_zero_error_on_identity(self):
        import subprocess

        root = Path(__file__).resolve().parents[1]
        raw = np.array([[100, 200], [300, 400]], dtype=np.uint16)
        with tempfile.TemporaryDirectory() as directory:
            directory_path = Path(directory)
            calib = write_calibration(
                directory_path / "calib.json",
                color_shape=(2, 2),
                depth_shape=(2, 2),
            )
            raw_path = directory_path / "raw.npy"
            sdk_path = directory_path / "sdk.npy"
            np.save(raw_path, raw, allow_pickle=False)
            np.save(sdk_path, raw.astype(np.float32), allow_pickle=False)
            result = subprocess.run(
                [
                    sys.executable,
                    str(root / "tools" / "compare_rgbd_alignment.py"),
                    "--raw-depth", str(raw_path),
                    "--sdk-aligned", str(sdk_path),
                    "--calibration", str(calib),
                ],
                check=False,
                capture_output=True,
                text=True,
            )
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report["depth_error_mm"]["max"], 0.0)
        self.assertEqual(report["overlap_pixels"], 4)


class MockTeleimagerTest(unittest.TestCase):
    def test_consumes_multipart_without_opening_a_camera(self):
        try:
            import zmq
        except ImportError:
            self.skipTest("pyzmq not installed")

        color_shape = (3, 4)
        depth_shape = (3, 4)
        bgr = np.full((*color_shape, 3), 80, dtype=np.uint8)
        ok, encoded = cv2.imencode(".jpg", bgr)
        self.assertTrue(ok)
        depth = np.full(depth_shape, 800, dtype=np.uint16)
        metadata = {
            "data_format": "rgbd",
            "color_format": "jpeg",
            "depth_format": "depth_z16",
            "depth_dtype": "uint16",
            "color_shape": list(color_shape),
            "depth_shape": list(depth_shape),
        }
        parts = [json.dumps(metadata).encode(), encoded.tobytes(), depth.tobytes()]

        context = zmq.Context()
        publisher = context.socket(zmq.PUB)
        publisher.setsockopt(zmq.LINGER, 0)
        port = publisher.bind_to_random_port("tcp://127.0.0.1")
        stop = threading.Event()

        def publish():
            while not stop.is_set():
                publisher.send_multipart(parts)
                time.sleep(0.05)

        worker = threading.Thread(target=publish, daemon=True)
        worker.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                calib_path = write_calibration(Path(directory) / "calib.json")
                camera = ZmqRGBDCamera(
                    host="127.0.0.1",
                    calibration_path=calib_path,
                    request_port=1,
                    stream_port=port,
                    stale_after_s=1.0,
                    startup_timeout_s=3.0,
                )
                camera.start()
                self.assertIsNone(camera.info()["aligned_generation"])
                time.sleep(0.15)
                self.assertIsNone(
                    camera.info()["aligned_generation"],
                    "仅接收 ZMQ 推流时不应执行深度对齐",
                )
                geometry = camera.depth_geometry_snapshot(max_dimension=2)
                self.assertIsNotNone(geometry)
                self.assertEqual(geometry["points_depth_m"].shape[1], 3)
                self.assertGreater(geometry["measured_pixels"], 0)
                self.assertIsNone(
                    camera.info()["aligned_generation"],
                    "原始深度几何采样不应触发 RGB 深度对齐",
                )
                picked = {"ok": False}
                jpeg = None
                for _ in range(40):
                    jpeg = camera.get_jpeg()
                    picked = camera.pick(1, 1)
                    if jpeg is not None and picked.get("ok"):
                        break
                    time.sleep(0.05)
                self.assertIsNotNone(jpeg)
                snapshot = camera.depth_snapshot()
                self.assertIsNotNone(snapshot)
                aligned, _intrinsics = snapshot
                self.assertEqual(aligned.shape, color_shape)
                self.assertGreater(float(np.median(aligned[aligned > 0])), 0.0)
                rgbd = camera.rgbd_snapshot()
                self.assertIsNotNone(rgbd)
                self.assertEqual(rgbd["jpeg"], encoded.tobytes())
                self.assertEqual(rgbd["depth_mm"].shape, color_shape)
                self.assertEqual(rgbd["metadata"], metadata)
                self.assertEqual(tuple(rgbd["intrinsics"]), camera.intrinsics)
                self.assertTrue(picked["ok"], picked)
                info = camera.info()
                self.assertEqual(info["source"], "zmq")
                self.assertIsNone(info["error"])
                camera.stop()
                camera.start()
                self.assertTrue(camera.info()["source"] == "zmq")
                camera.stop()
        finally:
            stop.set()
            worker.join(timeout=1.0)
            publisher.close()
            context.term()


if __name__ == "__main__":
    unittest.main()
