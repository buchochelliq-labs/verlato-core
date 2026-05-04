from __future__ import annotations

import os

from releasepilot.config import ProviderConfig
from releasepilot.providers.base import ReleasePilotProvider
from releasepilot.providers.github_copilot import GitHubCopilotProvider
from releasepilot.providers.mock import MockProvider
from releasepilot.providers.ollama import OllamaProvider
from releasepilot.providers.stub import StubProvider


def build_provider(config: ProviderConfig | None = None, *, mode: str | None = None) -> ReleasePilotProvider:
    config = config or ProviderConfig()
    selected_mode = (mode or os.getenv("RELEASEPILOT_MODEL_PROVIDER") or config.name or "stub").lower()
    if selected_mode == "mock":
        return MockProvider()
    if selected_mode in {"ollama", "local"}:
        return OllamaProvider(
            model=os.getenv("RELEASEPILOT_OLLAMA_MODEL", config.model or ""),
            endpoint=os.getenv("RELEASEPILOT_OLLAMA_ENDPOINT", config.endpoint or ""),
            stream=os.getenv("RELEASEPILOT_OLLAMA_STREAM", "").strip().lower() in {"1", "true", "yes", "on"},
            timeout_seconds=_resolve_float_env("RELEASEPILOT_OLLAMA_TIMEOUT_SECONDS", config.timeout_seconds),
        )
    if selected_mode in {"github-copilot", "github", "copilot"}:
        return GitHubCopilotProvider(
            model=os.getenv("RELEASEPILOT_GITHUB_MODEL", config.model or ""),
            endpoint=os.getenv("RELEASEPILOT_GITHUB_MODELS_ENDPOINT", config.endpoint or ""),
        )
    return StubProvider()


def _resolve_float_env(name: str, fallback: float | None) -> float | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return fallback
    return float(raw)
