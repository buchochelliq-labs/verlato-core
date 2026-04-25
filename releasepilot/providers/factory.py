from __future__ import annotations

import os

from releasepilot.config import ProviderConfig
from releasepilot.providers.base import ReleasePilotProvider
from releasepilot.providers.github_copilot import GitHubCopilotProvider
from releasepilot.providers.mock import MockProvider
from releasepilot.providers.stub import StubProvider


def build_provider(config: ProviderConfig | None = None) -> ReleasePilotProvider:
    config = config or ProviderConfig()
    mode = os.getenv("RELEASEPILOT_MODEL_PROVIDER", config.name).lower()
    if mode == "mock":
        return MockProvider()
    if mode in {"github-copilot", "github", "copilot"}:
        return GitHubCopilotProvider(
            model=os.getenv("RELEASEPILOT_GITHUB_MODEL", config.model or ""),
            endpoint=os.getenv("RELEASEPILOT_GITHUB_MODELS_ENDPOINT", config.endpoint or ""),
        )
    return StubProvider()
