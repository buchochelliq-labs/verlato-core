from __future__ import annotations

from verlato.providers.base import ProviderRequest, ProviderResponse, VerlatoProvider


class StubProvider(VerlatoProvider):
    def summarize(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            content="",
            confidence=0.4,
            model="stub",
        )

    def classify(self, request: ProviderRequest) -> ProviderResponse:
        return ProviderResponse(
            content="[stub-classification] changed/general/external/patch",
            confidence=0.4,
            model="stub",
        )
