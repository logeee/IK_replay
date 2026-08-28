"""Read-only consumer for one teleimager JPEG ZMQ stream."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

import cv2
import numpy as np


class ZmqJpegCamera:
    """Keep the latest JPEG from a named teleimager camera."""

    source = "zmq"

    def __init__(
        self,
        *,
        host: str,
        camera_name: str,
        request_port: int = 60000,
        stream_port: int | None = None,
        config_cache_path: str | Path | None = None,
        stale_after_s: float = 2.0,
        startup_timeout_s: float = 5.0,
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
        self.width: int | None = None
        self.height: int | None = None

        self._lock = threading.Lock()
        self._jpeg: bytes | None = None
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
            raise RuntimeError(
                f"无法读取 ZMQ 配置缓存 {self.config_cache_path}: {exc}"
            ) from exc
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

    def _resolve_stream(self, zmq) -> int:
        config = self._request_config(zmq)
        if config is not None:
            camera = config.get(self.camera_name)
            if not isinstance(camera, dict):
                raise RuntimeError(f"teleimager 配置中没有 {self.camera_name!r}")
            if not camera.get("enable_zmq"):
                raise RuntimeError(f"teleimager 的 {self.camera_name!r} 未启用 ZMQ")
            if camera.get("data_format", "jpeg") != "jpeg":
                raise RuntimeError(
                    f"teleimager 的 {self.camera_name!r} 不是 JPEG stream"
                )
            configured_port = int(camera["zmq_port"])
            if self.stream_port is not None and self.stream_port != configured_port:
                raise RuntimeError(
                    f"命令行 wrist port {self.stream_port} "
                    f"与服务配置 {configured_port} 不一致"
                )
            shape = camera.get("image_shape")
            if isinstance(shape, (list, tuple)) and len(shape) == 2:
                self.height, self.width = int(shape[0]), int(shape[1])
            return configured_port
        if self.stream_port is None:
            raise RuntimeError(
                f"无法从 {self.host}:{self.request_port} 获取 teleimager 配置，"
                "且没有提供 --wrist-camera-port"
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
            self.stream_port = self._resolve_stream(zmq)
            self._thread = threading.Thread(target=self._run, args=(zmq,), daemon=True)
            self._thread.start()
            if not self._ready.wait(self.startup_timeout_s):
                detail = self.error or "尚未收到合法 JPEG 帧"
                raise RuntimeError(
                    f"ZMQ 腕部相机 {self.host}:{self.stream_port} "
                    f"{self.startup_timeout_s:.0f}s 内未就绪: {detail}"
                )
        except Exception:
            self.stop()
            raise

    def _run(self, zmq) -> None:
        socket = self._context.socket(zmq.SUB)
        socket.setsockopt(zmq.RCVHWM, 1)
        socket.setsockopt(zmq.LINGER, 0)
        socket.setsockopt_string(zmq.SUBSCRIBE, "")
        socket.connect(f"tcp://{self.host}:{self.stream_port}")
        poller = zmq.Poller()
        poller.register(socket, zmq.POLLIN)
        try:
            while not self._stop_evt.is_set():
                if socket not in dict(poller.poll(200)):
                    with self._lock:
                        last = self._last_frame_at
                        if last is not None and time.monotonic() - last > self.stale_after_s:
                            self.error = (
                                f"JPEG 数据超过 {self.stale_after_s:.1f}s 未更新"
                            )
                    continue
                try:
                    jpeg = bytes(socket.recv())
                    if not self._verified_jpeg:
                        bgr = cv2.imdecode(
                            np.frombuffer(jpeg, dtype=np.uint8), cv2.IMREAD_COLOR
                        )
                        if bgr is None:
                            raise ValueError("ZMQ 腕部相机 JPEG 解码失败")
                        if (
                            self.height is not None
                            and self.width is not None
                            and bgr.shape[:2] != (self.height, self.width)
                        ):
                            raise ValueError(
                                f"腕部 JPEG shape {bgr.shape[:2]} 与 teleimager 配置 "
                                f"{(self.height, self.width)} 不一致"
                            )
                    with self._lock:
                        self._jpeg = jpeg
                        self._last_frame_at = time.monotonic()
                        self.error = None
                        self._verified_jpeg = True
                    self._ready.set()
                except Exception as exc:
                    with self._lock:
                        self.error = str(exc)
        finally:
            poller.unregister(socket)
            socket.close()

    def stop(self) -> None:
        self._stop_evt.set()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
            self._thread = None
        if self._context is not None:
            self._context.term()
            self._context = None

    def get_jpeg(self) -> bytes | None:
        with self._lock:
            if (
                self._last_frame_at is None
                or time.monotonic() - self._last_frame_at > self.stale_after_s
            ):
                return None
            return self._jpeg

    def info(self) -> dict[str, Any]:
        with self._lock:
            age = (
                None
                if self._last_frame_at is None
                else max(0.0, time.monotonic() - self._last_frame_at)
            )
            error = self.error
        return {
            "source": self.source,
            "host": self.host,
            "request_port": self.request_port,
            "stream_port": self.stream_port,
            "camera_name": self.camera_name,
            "width": self.width,
            "height": self.height,
            "last_frame_age_s": age,
            "error": error,
        }
