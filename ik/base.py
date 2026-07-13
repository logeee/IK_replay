from __future__ import annotations

from abc import ABC, abstractmethod

from core.types import IKRequest, IKResult


class BaseIKSolver(ABC):
    name = "base"

    @abstractmethod
    def solve(self, request: IKRequest) -> IKResult:
        raise NotImplementedError
