from __future__ import annotations

import hashlib
from random import Random

from releasepilot.providers.base import ProviderRequest, ProviderResponse, ReleasePilotProvider


class MockProvider(ReleasePilotProvider):
    """
    Deterministic local provider for tests. Outputs are derived from prompt hash.
    """

    def summarize(self, request: ProviderRequest) -> ProviderResponse:
        seed = self._seed(request)
        rng = Random(seed)
        confidence = round(0.6 + (rng.random() * 0.35), 2)
        return ProviderResponse(
            content=f"[mock-summary] {request.prompt[:60]}",
            confidence=confidence,
            model="mock-local",
        )

    def classify(self, request: ProviderRequest) -> ProviderResponse:
        seed = self._seed(request)
        rng = Random(seed)
        classes = [
            "added/api/external/minor",
            "fixed/backend/external/patch",
            "internal/docs/internal/none",
            "changed/general/external/patch",
        ]
        choice = classes[rng.randrange(len(classes))]
        confidence = round(0.55 + (rng.random() * 0.4), 2)
        return ProviderResponse(content=f"[mock-classification] {choice}", confidence=confidence, model="mock-local")

    @staticmethod
    def _seed(request: ProviderRequest) -> int:
        digest = hashlib.sha256(f"{request.task}:{request.prompt}".encode()).hexdigest()[:8]
        return int(digest, 16)
