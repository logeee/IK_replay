"""Read-only consumer for teleimager's RGB-D ZMQ multipart stream."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from .alignment import RGBDCalibration, SoftwareDepthAligner


PICK_SAMPLES = 3


def _shape(value: Any, name: str) -> tuple[int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 2:
        raise ValueError(f"{name} 必须是 [height, width]，实际为 {value!r}")
    height, width = int(value[0]), int(value[1])
    if height <= 0 or width <= 0:
        raise ValueError(f"{name} 必须为正数，实际为 {value!r}")
    return height, width


def decode_rgbd_parts(
    parts: list[bytes] | tuple[bytes, ...],
    calibration: RGBDCalibration,
    *,
    verify_jpeg_shape: bool = False,
) -> tuple[dict[str, Any], bytes, np.ndarray]:
    """Decode one immutable teleimager message and enforce the local profile."""
    if len(parts) != 3:
        raise ValueError(f"RGB-D multipart 应有 3 段，实际为 {len(parts)}")
    try:
        metadata = json.loads(parts[0].decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"RGB-D metadata 不是合法 JSON: {exc}") from exc
    if not isinstance(metadata, dict) or metadata.get("data_format") != "rgbd":
        raise ValueError(f"不支持 RGB-D metadata: {metadata!r}")
    if metadata.get("color_format") != "jpeg":
        raise ValueError(f"不支持 color_format={metadata.get('color_format')!r}")
    if metadata.get("depth_format") != "depth_z16":
        raise ValueError(f"不支持 depth_format={metadata.get('depth_format')!r}")
    if metadata.get("depth_dtype", "uint16") != "uint16":
        raise ValueError(f"不支持 depth_dtype={metadata.get('depth_dtype')!r}")

    color_shape = _shape(metadata.get("color_shape"), "metadata.color_shape")
    depth_shape = _shape(metadata.get("depth_shape"), "metadata.depth_shape")
    if color_shape != calibration.color_shape:
        raise ValueError(
            f"ZMQ color shape {color_shape} 与标定 {calibration.color_shape} 不一致"
        )
    if depth_shape != calibration.depth_shape:
        raise ValueError(
            f"ZMQ depth shape {depth_shape} 与标定 {calibration.depth_shape} 不一致"
        )
    expected_bytes = depth_shape[0] * depth_shape[1] * np.dtype(np.uint16).itemsize
    if len(parts[2]) != expected_bytes:
        raise ValueError(
            f"ZMQ depth payload 为 {len(parts[2])} bytes，期望 {expected_bytes}"
        )
    depth = np.frombuffer(parts[2], dtype=np.uint16).reshape(depth_shape).copy()

    color_jpeg = bytes(parts[1])
    if verify_jpeg_shape:
        bgr = cv2.imdecode(np.frombuffer(color_jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
        if bgr is None:
            raise ValueError("ZMQ color JPEG 解码失败")
        if bgr.shape[:2] != color_shape:
            raise ValueError(
                f"JPEG shape {bgr.shape[:2]} 与 metadata {color_shape} 不一致"
            )
    return metadata, color_jpeg, depth


class ZmqRGBDCamera:
    """CameraBase-compatible adapter that never opens a local camera device."""

    source = "zmq"

    def __init__(
        self,
        *,
        host: str,
        calibration_path: str | Path,
        camera_name: str = "head_rgbd_camera",
        request_port: int = 60000,
        stream_port: int | None = None,
        config_cache_path: str | Path | None = None,
        stale_after_s: float = 2.0,
        startup_timeout_s: float = 15.0,
    ):
        self.host = host
        self.camera_name = camera_name
        self.request_port = int(request_port)
        self.stream_port = None if stream_port is None else int(stream_port)
        self.config_cache_path = (
            None if config_cache_path is None else Path(config_cache_path).expanduser()
        )
        self.stale_after_s = float(stale_after_s)
        self.startup_timeout_s = float(startup_timeout_s)
        self.calibration = RGBDCalibration.from_file(calibration_path)
        self.aligner = SoftwareDepthAligner(self.calibration)
        self.width = self.calibration.color_shape[1]
        self.height = self.calibration.color_shape[0]
        self.intrinsics = self.calibration.color_intrinsics
        self.serial = self.calibration.serial
        self.name = camera_name

        self._lock = threading.Lock()
        self._condition = threading.Condition(self._lock)
        self._align_lock = threading.Lock()
        self._color_jpeg: bytes | None = None
        self._raw_depth: np.ndarray | None = None
        self._frame_generation = 0
        self._aligned_depth: np.ndarray | None = None
        self._aligned_generation = -1
        self._aligned_color_jpeg: bytes | None = None
        self._aligned_metadata: dict[str, Any] | None = None
        self._aligned_frame_at: float | None = None
        self._metadata: dict[str, Any] | None = None
        self._last_frame_at: float | None = None
        self._stop_evt = threading.Event()
        self._ready = threading.Event()
        self._thread: threading.Thread | None = None
        self._context = None
        self.error: str | None = None
        self._verified_jpeg = False

    def _save_config_cache(self, config: dict[str, Any]) -> None:
        if self.config_cache_path is None:
            return
        path = self.config_cache_path
        path.parent.mkdir(parents=True, exist_ok=True)
        temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
        try:
            temporary.write_text(
                json.dumps(config, indent=2, ensure_ascii=False) + "\n",
                encoding="utf-8",
            )
            os.replace(temporary, path)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass

    def _load_config_cache(self) -> dict[str, Any] | None:
        if self.config_cache_path is None or not self.config_cache_path.exists():
            return None
        try:
            value = json.loads(self.config_cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"无法读取 ZMQ 配置缓存 {self.config_cache_path}: {exc}") from exc
        if not isinstance(value, dict):
            raise RuntimeError(f"ZMQ 配置缓存 {self.config_cache_path} 不是 JSON object")
        return value

    def _request_config(self, zmq) -> dict[str, Any] | None:
        socket = self._context.socket(zmq.REQ)
        socket.setsockopt(zmq.LINGER, 0)
        socket.connect(f"tcp://{self.host}:{self.request_port}")
        try:
            socket.send(b"GET_DATA")
            if socket.poll(1000, zmq.POLLIN):
                config = socket.recv_json()
                if not isinstance(config, dict):
                    raise RuntimeError("teleimager 配置响应不是 object")
                self._save_config_cache(config)
                return config
        finally:
            socket.close()
        return self._load_config_cache()

    def _resolve_stream_port(self, zmq) -> int:
        config = self._request_config(zmq)
        if config is not None:
            camera = config.get(self.camera_name)
            if not isinstance(camera, dict):
                raise RuntimeError(f"teleimager 配置中没有 {self.camera_name!r}")
            if not camera.get("enable_zmq"):
                raise RuntimeError(f"teleimager 的 {self.camera_name!r} 未启用 ZMQ")
            if camera.get("data_format") != "rgbd":
                raise RuntimeError(
                    f"teleimager 的 {self.camera_name!r} 不是 rgbd stream"
                )
            configured_port = int(camera["zmq_port"])
            if self.stream_port is not None and self.stream_port != configured_port:
                raise RuntimeError(
                    f"命令行 stream port {self.stream_port} 与服务配置 {configured_port} 不一致"
                )
            return configured_port
        if self.stream_port is None:
            raise RuntimeError(
                f"无法从 {self.host}:{self.request_port} 获取 teleimager 配置，"
                "且没有提供 --camera-port"
            )
        return self.stream_port

    def start(self) -> None:
        if self._thread is not None:
            return
        try:
            import zmq
        except ImportError as exc:
            raise RuntimeError("缺少 pyzmq，请安装 requirements.txt") from exc
        self._stop_evt.clear()
        self._ready.clear()
        self._context = zmq.Context()
        try:
            self.stream_port = self._resolve_stream_port(zmq)
            self._thread = threading.Thread(target=self._run, args=(zmq,), daemon=True)
            self._thread.start()
            if not self._ready.wait(self.startup_timeout_s):
                detail = self.error or "尚未收到合法 RGB-D 帧"
                raise RuntimeError(
                    f"ZMQ 相机 {self.host}:{self.stream_port} "
                    f"{self.startup_timeout_s:.0f}s 内未就绪: {detail}"
                )
        except Exception:
            self.stop()
            raise

    def _open_subscriber(self, zmq):
        socket = self._context.socket(zmq.SUB)
        socket.setsockopt(zmq.RCVHWM, 1)
        socket.setsockopt(zmq.LINGER, 0)
        socket.setsockopt_string(zmq.SUBSCRIBE, "")
        socket.connect(f"tcp://{self.host}:{self.stream_port}")
        poller = zmq.Poller()
        poller.register(socket, zmq.POLLIN)
        return socket, poller

    def _run(self, zmq) -> None:
        socket = None
        poller = None
        try:
            while not self._stop_evt.is_set():
                if socket is None:
                    try:
                        socket, poller = self._open_subscriber(zmq)
                    except Exception as exc:
                        with self._lock:
                            self.error = f"ZMQ 订阅失败: {exc}"
                        time.sleep(0.2)
                        continue
                events = dict(poller.poll(200))
                if socket not in events:
                    with self._lock:
                        last = self._last_frame_at
                        if last is not None and time.monotonic() - last > self.stale_after_s:
                            self.error = f"RGB-D 数据超过 {self.stale_after_s:.1f}s 未更新"
                    continue
                try:
                    parts = socket.recv_multipart()
                    metadata, color_jpeg, raw_depth = decode_rgbd_parts(
                        parts,
                        self.calibration,
                        verify_jpeg_shape=not self._verified_jpeg,
                    )
                    now = time.monotonic()
                    with self._condition:
                        self._color_jpeg = color_jpeg
                        self._raw_depth = raw_depth
                        self._frame_generation += 1
                        self._metadata = metadata
                        self._last_frame_at = now
                        self.error = None
                        self._verified_jpeg = True
                        self._condition.notify_all()
                    self._ready.set()
                except zmq.ZMQError as exc:
                    with self._lock:
                        self.error = f"ZMQ 连接中断: {exc}"
                    try:
                        poller.unregister(socket)
                    except Exception:
                        pass
                    socket.close()
                    socket = None
                    poller = None
                except Exception as exc:
                    with self._lock:
                        self.error = str(exc)
        finally:
            if socket is not None:
                try:
                    if poller is not None:
                        poller.unregister(socket)
                except Exception:
                    pass
                socket.close()

    def stop(self) -> None:
        self._stop_evt.set()
        with self._condition:
            self._condition.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        if self._context is not None:
            self._context.term()
            self._context = None

    def _fresh(self) -> bool:
        return (
            self._last_frame_at is not None
            and time.monotonic() - self._last_frame_at <= self.stale_after_s
        )

    def get_jpeg(self) -> bytes | None:
        with self._lock:
            if not self._fresh():
                return None
            return self._color_jpeg

    def _raw_snapshot(
        self,
        *,
        after_generation: int | None = None,
        timeout_s: float = 0.0,
    ) -> tuple[int, bytes, np.ndarray, dict[str, Any], float] | None:
        """Copy references to one immutable source message, optionally waiting."""
        deadline = time.monotonic() + max(0.0, timeout_s)
        with self._condition:
            while True:
                fresh = (
                    self._fresh()
                    and self._color_jpeg is not None
                    and self._raw_depth is not None
                    and (
                        after_generation is None
                        or self._frame_generation != after_generation
                    )
                )
                if fresh:
                    return (
                        self._frame_generation,
                        self._color_jpeg,
                        self._raw_depth,
                        {} if self._metadata is None else dict(self._metadata),
                        self._last_frame_at,
                    )
                remaining = deadline - time.monotonic()
                if remaining <= 0.0 or self._stop_evt.is_set():
                    return None
                self._condition.wait(remaining)

    def _align_on_demand(
        self,
        *,
        after_generation: int | None = None,
        timeout_s: float = 0.0,
    ) -> tuple[int, bytes, np.ndarray, dict[str, Any]] | None:
        """Align one source message, coalescing concurrent requests by frame."""
        source = self._raw_snapshot(
            after_generation=after_generation,
            timeout_s=timeout_s,
        )
        if source is None:
            return None
        generation, jpeg, raw_depth, metadata, frame_at = source
        with self._align_lock:
            with self._lock:
                if (
                    self._aligned_generation == generation
                    and self._aligned_depth is not None
                    and self._aligned_color_jpeg is not None
                ):
                    return (
                        generation,
                        self._aligned_color_jpeg,
                        self._aligned_depth,
                        {} if self._aligned_metadata is None
                        else dict(self._aligned_metadata),
                    )

            aligned_depth = self.aligner.align(raw_depth)
            with self._lock:
                if generation >= self._aligned_generation:
                    self._aligned_generation = generation
                    self._aligned_depth = aligned_depth
                    self._aligned_color_jpeg = jpeg
                    self._aligned_metadata = dict(metadata)
                    self._aligned_frame_at = frame_at
            return generation, jpeg, aligned_depth, metadata

    def pick(self, u: int, v: int, win: int = 5) -> dict[str, Any]:
        del win
        if not (0 <= u < self.width and 0 <= v < self.height):
            return {
                "ok": False,
                "error": (
                    f"像素越界 ({u},{v})，深度图 "
                    f"{self.width}x{self.height}"
                ),
            }
        frames: list[np.ndarray] = []
        generation = None
        for index in range(PICK_SAMPLES):
            aligned = self._align_on_demand(
                after_generation=generation,
                timeout_s=0.0 if index == 0 else 1.0,
            )
            if aligned is None:
                break
            generation, _jpeg, depth, _metadata = aligned
            frames.append(depth)
        if len(frames) < PICK_SAMPLES:
            return {
                "ok": False,
                "error": (
                    f"无法取得 {PICK_SAMPLES} 帧新鲜深度"
                    f"（实际 {len(frames)} 帧）"
                ),
            }
        values = np.asarray([frame[v, u] for frame in frames], dtype=np.float64)
        valid = values[(values > 60.0) & (values < 15000.0)]
        if valid.size < PICK_SAMPLES:
            return {
                "ok": False,
                "error": f"该像素没有稳定深度（{valid.size}/{PICK_SAMPLES} 帧有效）",
            }
        spread = float(np.max(valid) - np.min(valid))
        if spread > 80.0:
            return {
                "ok": False,
                "error": f"该像素深度在多帧间跳动 {spread:.0f}mm（边缘闪烁）",
            }
        z_mm = float(np.median(valid))
        z_m = z_mm / 1000.0
        fx, fy, cx, cy = self.intrinsics
        return {
            "ok": True,
            "p_camera": [(u - cx) * z_m / fx, (v - cy) * z_m / fy, z_m],
            "depth_mm": z_mm,
            "valid_ratio": float(valid.size / len(values)),
            "pixel": [u, v],
        }

    def depth_snapshot(self):
        aligned = self._align_on_demand()
        if aligned is None:
            return None
        _generation, _jpeg, depth, _metadata = aligned
        return depth.copy(), self.intrinsics

    def rgbd_snapshot(self) -> dict[str, Any] | None:
        """Align and return one strict same-message color/depth pair."""
        aligned = self._align_on_demand()
        if aligned is None:
            return None
        _generation, jpeg, depth, metadata = aligned
        return {
            "jpeg": bytes(jpeg),
            "depth_mm": depth.copy(),
            "intrinsics": tuple(float(v) for v in self.intrinsics),
            "metadata": dict(metadata),
        }

    def info(self) -> dict[str, Any]:
        with self._lock:
            metadata = None if self._metadata is None else dict(self._metadata)
            last_age = (
                None
                if self._last_frame_at is None
                else max(0.0, time.monotonic() - self._last_frame_at)
            )
            error = self.error
            aligned_age = (
                None
                if self._aligned_frame_at is None
                else max(0.0, time.monotonic() - self._aligned_frame_at)
            )
            aligned_generation = (
                None if self._aligned_generation < 0
                else self._aligned_generation
            )
        return {
            "source": self.source,
            "host": self.host,
            "request_port": self.request_port,
            "stream_port": self.stream_port,
            "camera_name": self.camera_name,
            "serial": self.serial,
            "width": self.width,
            "height": self.height,
            "intrinsics": {
                "fx": self.intrinsics[0],
                "fy": self.intrinsics[1],
                "cx": self.intrinsics[2],
                "cy": self.intrinsics[3],
            },
            "calibration_path": str(self.calibration.path),
            "last_frame_age_s": last_age,
            "alignment_mode": "on_demand",
            "aligned_frame_age_s": aligned_age,
            "aligned_generation": aligned_generation,
            "metadata": metadata,
            "error": error,
        }
