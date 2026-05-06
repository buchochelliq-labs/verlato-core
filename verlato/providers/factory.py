from __future__ import annotations

import os

from verlato.config import ProviderConfig
from verlato.providers.base import VerlatoProvider
from verlato.providers.github_copilot import GitHubCopilotProvider
from verlato.providers.mock import MockProvider
from verlato.providers.ollama import OllamaProvider
from verlato.providers.stub import StubProvider


def build_provider(
    config: ProviderConfig | None = None,
    *,
    mode: str | None = None,
    suggest_model: str | None = None,
) -> VerlatoProvider:
    config = config or ProviderConfig()
    selected_mode = (mode or os.getenv("VERLATO_MODEL_PROVIDER") or config.name or "stub").lower()
    if selected_mode == "mock":
        return MockProvider()
    if selected_mode in {"ollama", "local"}:
        return OllamaProvider(
            model=os.getenv("VERLATO_OLLAMA_MODEL", config.model or ""),
            suggest_model=(
                _normalize_optional_string(suggest_model)
                if suggest_model is not None
                else _resolve_optional_string_env("VERLATO_OLLAMA_SUGGEST_MODEL", config.suggest_model)
            ),
            endpoint=os.getenv("VERLATO_OLLAMA_ENDPOINT", config.endpoint or ""),
            stream=os.getenv("VERLATO_OLLAMA_STREAM", "").strip().lower() in {"1", "true", "yes", "on"},
            timeout_seconds=_resolve_float_env("VERLATO_OLLAMA_TIMEOUT_SECONDS", config.timeout_seconds),
            keep_alive=_resolve_string_env("VERLATO_OLLAMA_KEEP_ALIVE", config.keep_alive),
            num_ctx=_resolve_int_env("VERLATO_OLLAMA_NUM_CTX", config.num_ctx),
            num_predict=_resolve_int_env("VERLATO_OLLAMA_NUM_PREDICT", config.num_predict),
            summary_num_predict=_resolve_int_env("VERLATO_OLLAMA_SUMMARY_NUM_PREDICT", config.summary_num_predict),
            classify_num_predict=_resolve_int_env("VERLATO_OLLAMA_CLASSIFY_NUM_PREDICT", config.classify_num_predict),
            suggest_num_predict=_resolve_int_env("VERLATO_OLLAMA_SUGGEST_NUM_PREDICT", config.suggest_num_predict),
            suggest_think=_resolve_bool_env("VERLATO_OLLAMA_SUGGEST_THINK", config.suggest_think),
        )
    if selected_mode in {"github-copilot", "github", "copilot"}:
        return GitHubCopilotProvider(
            model=os.getenv("VERLATO_GITHUB_MODEL", config.model or ""),
            endpoint=os.getenv("VERLATO_GITHUB_MODELS_ENDPOINT", config.endpoint or ""),
        )
    return StubProvider()


def _resolve_float_env(name: str, fallback: float | None) -> float | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return fallback
    return float(raw)


def _resolve_string_env(name: str, fallback: str | None) -> str | None:
    raw = os.getenv(name)
    if raw is None:
        return fallback
    return raw


def _resolve_optional_string_env(name: str, fallback: str | None) -> str | None:
    raw = os.getenv(name)
    if raw is None:
        return _normalize_optional_string(fallback)
    return _normalize_optional_string(raw)


def _normalize_optional_string(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _resolve_int_env(name: str, fallback: int | None) -> int | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return fallback
    return int(raw)


def _resolve_bool_env(name: str, fallback: bool | None) -> bool | None:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return fallback
    return raw.strip().lower() in {"1", "true", "yes", "on"}
