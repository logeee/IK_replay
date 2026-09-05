"""Transport-neutral state and command boundaries for the H2 left arm."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import numpy as np


@dataclass(frozen=True)
class ArmState:
    """Measured state consumed by the standalone controller."""

    q_actual: np.ndarray
    dq_actual: np.ndarray
    world_T_root: np.ndarray
    world_T_tcp: np.ndarray


class StateProvider(Protocol):
    """Replace Isaac with H2 lowstate by implementing this protocol."""

    def read(self) -> ArmState:
        """Return one timestamp-consistent measured arm/root/TCP state."""


class CommandSink(Protocol):
    """Replace Isaac with the H2 arm SDK by implementing this protocol."""

    def send(self, q_target: np.ndarray) -> None:
        """Send one seven-joint position target."""
