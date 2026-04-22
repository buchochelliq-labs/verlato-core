from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProviderRequest:
    task: str
    prompt: str
    evidence: dict[str, Any] = field(default_factory=dict)


@dataclass
class ProviderResponse:
    content: str
    confidence: float = 0.5
    model: str = "unknown"


class ReleasePilotProvider(ABC):
    @abstractmethod
    def summarize(self, request: ProviderRequest) -> ProviderResponse:
        raise NotImplementedError

    @abstractmethod
    def classify(self, request: ProviderRequest) -> ProviderResponse:
        raise NotImplementedError
