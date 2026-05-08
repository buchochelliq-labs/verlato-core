from __future__ import annotations

import os
from dataclasses import replace
from typing import Any

from verlato.config import ProviderConfig
from verlato.providers.base import VerlatoProvider
from verlato.providers.cli_agents import ClaudeCodeProvider, CodexCliProvider
from verlato.providers.fallback import FallbackProvider
from verlato.providers.github_copilot import GitHubCopilotProvider
from verlato.providers.litellm import LiteLLMProvider
from verlato.providers.mock import MockProvider
from verlato.providers.ollama import OllamaProvider
from verlato.providers.plugin import ExternalModelPluginProvider
from verlato.providers.stub import StubProvider


def build_provider(
    config: ProviderConfig | None = None,
    *,
    mode: str | None = None,
    suggest_model: str | None = None,
) -> VerlatoProvider:
    config = config or ProviderConfig()
    primary = _build_single_provider(config, mode=mode, suggest_model=suggest_model)
    fallback_configs = [_provider_config_from_mapping(raw, defaults=config) for raw in config.fallbacks]
    if not fallback_configs:
        return primary
    return FallbackProvider([primary, *[_build_single_provider(fallback, mode=None, suggest_model=suggest_model) for fallback in fallback_configs]])


def _build_single_provider(
    config: ProviderConfig,
    *,
    mode: str | None = None,
    suggest_model: str | None = None,
) -> VerlatoProvider:
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
            timeout_seconds=_resolve_float_env("VERLATO_OLLAMA_TIMEOUT_SECONDS", config.timeout_seconds) or 15.0,
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
    if selected_mode in {"litellm", "lite-llm"}:
        return LiteLLMProvider(
            model=os.getenv("VERLATO_LITELLM_MODEL") or (config.model if (config.name or "").lower() in {"litellm", "lite-llm"} else ""),
            suggest_model=(
                _normalize_optional_string(suggest_model)
                if suggest_model is not None
                else _resolve_optional_string_env("VERLATO_LITELLM_SUGGEST_MODEL", config.suggest_model)
            ),
            endpoint=os.getenv("VERLATO_LITELLM_ENDPOINT", config.endpoint or ""),
            api_key_env=os.getenv("VERLATO_LITELLM_API_KEY_ENV", config.api_key_env or ""),
            timeout_seconds=_resolve_float_env("VERLATO_LITELLM_TIMEOUT_SECONDS", config.timeout_seconds),
        )
    if selected_mode in {"plugin", "external-plugin", "model-plugin"}:
        return ExternalModelPluginProvider(
            manifest=config.plugin_manifest,
            command=_resolve_plugin_command(config) or None,
            plugin_config=config.plugin_config,
            env_names=_resolve_plugin_env(config),
            model=os.getenv("VERLATO_PLUGIN_MODEL") or (config.model if (config.name or "").lower() in {"plugin", "external-plugin", "model-plugin"} else ""),
            suggest_model=(
                _normalize_optional_string(suggest_model)
                if suggest_model is not None
                else _resolve_optional_string_env("VERLATO_PLUGIN_SUGGEST_MODEL", config.suggest_model)
            ),
            timeout_seconds=_resolve_float_env("VERLATO_PLUGIN_TIMEOUT_SECONDS", config.timeout_seconds),
        )
    if selected_mode in {"codex-cli", "codex"}:
        return CodexCliProvider(
            model=os.getenv("VERLATO_CODEX_MODEL", config.model or ""),
            timeout_seconds=_resolve_float_env("VERLATO_CODEX_TIMEOUT_SECONDS", config.timeout_seconds),
        )
    if selected_mode in {"claude-code", "claude"}:
        return ClaudeCodeProvider(
            model=os.getenv("VERLATO_CLAUDE_MODEL", config.model or ""),
            timeout_seconds=_resolve_float_env("VERLATO_CLAUDE_TIMEOUT_SECONDS", config.timeout_seconds),
        )
    return StubProvider()


def _resolve_plugin_command(config: ProviderConfig) -> list[str]:
    raw = os.getenv("VERLATO_PLUGIN_COMMAND")
    if raw and raw.strip():
        import shlex

        return [part for part in shlex.split(raw, posix=os.name != "nt") if part]
    return config.plugin_command


def _resolve_plugin_env(config: ProviderConfig) -> list[str]:
    raw = os.getenv("VERLATO_PLUGIN_ENV")
    if raw is None:
        return list(config.plugin_env)
    return [part.strip() for part in raw.split(",") if part.strip()]


def _provider_config_from_mapping(raw: dict[str, Any], *, defaults: ProviderConfig) -> ProviderConfig:
    return replace(
        defaults,
        name=str(raw.get("name") or defaults.name).strip() or defaults.name,
        model=_string_or_none(raw.get("model", defaults.model)),
        suggest_model=_string_or_none(raw.get("suggest_model", defaults.suggest_model)),
        endpoint=_string_or_none(raw.get("endpoint", defaults.endpoint)),
        api_key_env=_string_or_none(raw.get("api_key_env", defaults.api_key_env)),
        max_items_per_scan=int(raw.get("max_items_per_scan", defaults.max_items_per_scan)),
        timeout_seconds=(
            float(raw["timeout_seconds"])
            if raw.get("timeout_seconds") is not None
            else defaults.timeout_seconds
        ),
        plugin_manifest=dict(raw.get("plugin_manifest") or defaults.plugin_manifest),
        plugin_command=_command_list(raw.get("plugin_command", raw.get("command", defaults.plugin_command))),
        plugin_config=dict(raw.get("plugin_config") or defaults.plugin_config),
        plugin_env=list(raw.get("plugin_env") or defaults.plugin_env),
        fallbacks=[],
    )


def _string_or_none(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _command_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(part) for part in value if str(part).strip()]
    if isinstance(value, str):
        import shlex

        return [part for part in shlex.split(value, posix=os.name != "nt") if part]
    return []


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
