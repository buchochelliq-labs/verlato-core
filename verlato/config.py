from __future__ import annotations

import json
import os
import shlex
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from verlato.core.model_privacy import DEFAULT_MODEL_PRIVACY, normalize_model_privacy


class ConfigError(RuntimeError):
    pass


@dataclass
class OutputTargetConfig:
    path: str
    format: str


@dataclass
class ProviderConfig:
    name: str = "ollama"
    model: str | None = "gemma4:latest"
    suggest_model: str | None = None
    endpoint: str | None = None
    api_key_env: str | None = None
    max_items_per_scan: int = 25
    timeout_seconds: float | None = None
    keep_alive: str | None = "10m"
    num_ctx: int | None = None
    num_predict: int | None = None
    summary_num_predict: int | None = None
    classify_num_predict: int | None = None
    suggest_num_predict: int | None = None
    suggest_think: bool | None = False
    plugin_manifest: dict[str, Any] = field(default_factory=dict)
    plugin_command: list[str] = field(default_factory=list)
    plugin_config: dict[str, Any] = field(default_factory=dict)
    plugin_env: list[str] = field(default_factory=list)
    fallbacks: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class SuggestionStyleConfig:
    guidance: str = ""
    project_vocabulary: list[str] = field(default_factory=list)
    banned_phrases: list[str] = field(default_factory=list)
    specificity_floor: float = 0.0


@dataclass
class VerlatoConfig:
    state_path: str = ".verlato_state.json"
    commit_intent_inference: str = "off"
    model_privacy: str = DEFAULT_MODEL_PRIVACY
    suggestion_cache_backend: str = "json"
    suggestion_cache_path: str = ".verlato_suggestion_cache.json"
    suggestion_cache_ttl_days: int = 30
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    suggestion_count: int = 1
    suggestion_parallelism: int = 1
    suggestion_batch_size: int = 5
    suggestion_strategy: str = "auto"
    suggestion_prompt_profile: str = "conservative"
    suggestion_adaptive_recommendations: bool = False
    suggestion_tone: str = "release-note"
    suggestion_style: SuggestionStyleConfig = field(default_factory=SuggestionStyleConfig)
    feedback_include_text: bool = False
    targets: dict[str, OutputTargetConfig] = field(
        default_factory=lambda: {
            "changelog": OutputTargetConfig(path="CHANGELOG.md", format="changelog"),
            "github-release": OutputTargetConfig(path=".github/release.md", format="github-release"),
        }
    )
    raw: dict[str, Any] = field(default_factory=dict)


def load_config(cwd: str | None = None) -> VerlatoConfig:
    config_path = _resolve_config_path(cwd=cwd)
    if not config_path:
        return VerlatoConfig()

    try:
        raw = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Unable to load config from {config_path}: {exc}") from exc

    config = VerlatoConfig(raw=raw)
    if raw.get("state_path"):
        config.state_path = str(raw["state_path"])
    if raw.get("commit_intent_inference") is not None:
        configured_inference = str(raw["commit_intent_inference"]).strip().lower()
        config.commit_intent_inference = (
            configured_inference if configured_inference in {"off", "low-signal"} else "off"
        )
    if raw.get("model_privacy") is not None:
        config.model_privacy = normalize_model_privacy(raw["model_privacy"])
    if raw.get("suggestion_cache_backend"):
        config.suggestion_cache_backend = str(raw["suggestion_cache_backend"]).strip() or config.suggestion_cache_backend
    if raw.get("suggestion_cache_path"):
        config.suggestion_cache_path = str(raw["suggestion_cache_path"])
    if raw.get("suggestion_cache_ttl_days") is not None:
        config.suggestion_cache_ttl_days = int(raw["suggestion_cache_ttl_days"])
    if raw.get("suggestion_count") is not None:
        config.suggestion_count = int(raw["suggestion_count"])
    if raw.get("suggestion_parallelism") is not None:
        config.suggestion_parallelism = int(raw["suggestion_parallelism"])
    if raw.get("suggestion_batch_size") is not None:
        config.suggestion_batch_size = int(raw["suggestion_batch_size"])
    if raw.get("suggestion_strategy") is not None:
        config.suggestion_strategy = str(raw["suggestion_strategy"]).strip() or config.suggestion_strategy
    if raw.get("suggestion_prompt_profile") is not None:
        config.suggestion_prompt_profile = (
            str(raw["suggestion_prompt_profile"]).strip() or config.suggestion_prompt_profile
        )
    if raw.get("suggestion_adaptive_recommendations") is not None:
        config.suggestion_adaptive_recommendations = bool(raw["suggestion_adaptive_recommendations"])
    if raw.get("suggestion_tone") is not None:
        config.suggestion_tone = str(raw["suggestion_tone"]).strip() or config.suggestion_tone
    if raw.get("feedback_include_text") is not None:
        config.feedback_include_text = bool(raw["feedback_include_text"])
    style = raw.get("suggestion_style")
    if isinstance(style, dict):
        config.suggestion_style = SuggestionStyleConfig(
            guidance=str(style.get("guidance") or "").strip(),
            project_vocabulary=_string_list(style.get("project_vocabulary")),
            banned_phrases=_string_list(style.get("banned_phrases")),
            specificity_floor=float(style.get("specificity_floor") or 0.0),
        )

    provider = raw.get("provider")
    if isinstance(provider, dict):
        config.provider = _parse_provider_config(provider, defaults=config.provider)

    targets = raw.get("targets")
    if isinstance(targets, dict):
        parsed_targets: dict[str, OutputTargetConfig] = {}
        for name, target in targets.items():
            if not isinstance(target, dict):
                raise ConfigError(f"Invalid target config for {name!r}: expected object")
            path = str(target.get("path") or "").strip()
            fmt = str(target.get("format") or "").strip()
            if not path or not fmt:
                raise ConfigError(f"Invalid target config for {name!r}: require path and format")
            parsed_targets[name] = OutputTargetConfig(path=path, format=fmt)
        if parsed_targets:
            config.targets = parsed_targets

    return config


def _string_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item).strip() for item in value if str(item).strip()]


def _parse_provider_config(raw: dict[str, Any], *, defaults: ProviderConfig) -> ProviderConfig:
    name = str(raw.get("name") or defaults.name).strip() or "stub"
    model = raw.get("model")
    suggest_model = raw.get("suggest_model", defaults.suggest_model)
    endpoint = raw.get("endpoint")
    api_key_env = raw.get("api_key_env", defaults.api_key_env)
    max_items_per_scan = raw.get("max_items_per_scan", defaults.max_items_per_scan)
    timeout_seconds = raw.get("timeout_seconds", defaults.timeout_seconds)
    keep_alive = raw.get("keep_alive", defaults.keep_alive)
    num_ctx = raw.get("num_ctx", defaults.num_ctx)
    num_predict = raw.get("num_predict", defaults.num_predict)
    summary_num_predict = raw.get("summary_num_predict", defaults.summary_num_predict)
    classify_num_predict = raw.get("classify_num_predict", defaults.classify_num_predict)
    suggest_num_predict = raw.get("suggest_num_predict", defaults.suggest_num_predict)
    suggest_think = raw.get("suggest_think", defaults.suggest_think)
    raw_plugin_manifest = raw.get("plugin_manifest")
    plugin_manifest: dict[str, Any] = dict(raw_plugin_manifest) if isinstance(raw_plugin_manifest, dict) else {}
    plugin_command = raw.get("plugin_command")
    if plugin_command is None:
        plugin_command = raw.get("command")
    raw_plugin_config = raw.get("plugin_config")
    plugin_config: dict[str, Any] = dict(raw_plugin_config) if isinstance(raw_plugin_config, dict) else {}
    plugin_env = raw.get("plugin_env")
    raw_fallbacks = raw.get("fallbacks")
    fallbacks: list[Any] = list(raw_fallbacks) if isinstance(raw_fallbacks, list) else []
    return ProviderConfig(
        name=name,
        model=str(model).strip() if model else None,
        suggest_model=str(suggest_model).strip() if suggest_model else None,
        endpoint=str(endpoint).strip() if endpoint else None,
        api_key_env=str(api_key_env).strip() if api_key_env else None,
        max_items_per_scan=int(max_items_per_scan),
        timeout_seconds=float(timeout_seconds) if timeout_seconds is not None else None,
        keep_alive=str(keep_alive).strip() if keep_alive is not None else None,
        num_ctx=int(num_ctx) if num_ctx is not None else None,
        num_predict=int(num_predict) if num_predict is not None else None,
        summary_num_predict=int(summary_num_predict) if summary_num_predict is not None else None,
        classify_num_predict=int(classify_num_predict) if classify_num_predict is not None else None,
        suggest_num_predict=int(suggest_num_predict) if suggest_num_predict is not None else None,
        suggest_think=bool(suggest_think) if suggest_think is not None else None,
        plugin_manifest=dict(plugin_manifest),
        plugin_command=_command_list(plugin_command),
        plugin_config=dict(plugin_config),
        plugin_env=_string_list(plugin_env),
        fallbacks=[fallback for fallback in fallbacks if isinstance(fallback, dict)],
    )


def resolve_target_path(path: str, *, cwd: str | None = None) -> Path:
    root = Path(cwd or os.getcwd())
    target = Path(path)
    if target.is_absolute():
        return target
    return root / target


def _command_list(value: object) -> list[str]:
    if isinstance(value, list):
        return [str(part) for part in value if str(part).strip()]
    if isinstance(value, str):
        return [part for part in shlex.split(value, posix=os.name != "nt") if part]
    return []


def _resolve_config_path(cwd: str | None = None) -> Path | None:
    env_path = os.getenv("VERLATO_CONFIG_PATH")
    if env_path:
        return Path(env_path)

    root = Path(cwd or os.getcwd())
    candidate = root / "verlato.json"
    if candidate.exists():
        return candidate
    return None
