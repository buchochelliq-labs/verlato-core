from __future__ import annotations

from abc import ABC, abstractmethod


class SummarizationProvider(ABC):
    @abstractmethod
    def summarize(self, prompt: str) -> str:
        raise NotImplementedError
