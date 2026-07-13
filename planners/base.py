from __future__ import annotations

from abc import ABC, abstractmethod

from core.types import TrajectoryRequest, Waypoint


class BaseTrajectoryPlanner(ABC):
    name = "base"

    @abstractmethod
    def plan(self, request: TrajectoryRequest) -> list[Waypoint]:
        raise NotImplementedError
