from __future__ import annotations

import os

from releasepilot.providers.base import ReleasePilotProvider
from releasepilot.providers.github_copilot import GitHubCopilotProvider
from releasepilot.providers.mock import MockProvider
from releasepilot.providers.stub import StubProvider


def build_provider() -> ReleasePilotProvider:
    mode = os.getenv("RELEASEPILOT_MODEL_PROVIDER", "stub").lower()
    if mode == "mock":
        return MockProvider()
    if mode in {"github-copilot", "github", "copilot"}:
        return GitHubCopilotProvider()
    return StubProvider()
