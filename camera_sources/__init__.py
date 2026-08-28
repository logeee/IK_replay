"""Camera inputs owned by IK_replay.

The production implementation consumes an external ZMQ stream. Direct Orbbec
SDK access is intentionally kept outside this package and is only selected
explicitly for debugging.
"""

from .alignment import RGBDCalibration, SoftwareDepthAligner
from .zmq_jpeg import ZmqJpegCamera
from .zmq_rgbd import ZmqRGBDCamera

__all__ = [
    "RGBDCalibration",
    "SoftwareDepthAligner",
    "ZmqJpegCamera",
    "ZmqRGBDCamera",
]
