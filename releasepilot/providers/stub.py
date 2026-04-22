from __future__ import annotations

from releasepilot.providers.base import ProviderRequest, ProviderResponse, ReleasePilotProvider


class StubProvider(ReleasePilotProvider):
    def summarize(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            content=f"[stub-summary] {request.prompt[:120]}",
            confidence=0.4,
            model="stub",
        )

    def classify(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            content="[stub-classification] changed/general/external/patch",
            confidence=0.4,
            model="stub",
        )
