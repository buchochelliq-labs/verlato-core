from __future__ import annotations

from releasepilot.providers.base import SummarizationProvider


class StubProvider(SummarizationProvider):
    def summarize(self, prompt: str) -> str:
        return f"[stub-summary] {prompt[:120]}"
