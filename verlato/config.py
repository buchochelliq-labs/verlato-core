from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any


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
    endpoint: str | None = None
    max_items_per_scan: int = 25
    timeout_seconds: float | None = None
    keep_alive: str | None = "10m"
    num_ctx: int | None = None
    num_predict: int | None = None
    summary_num_predict: int | None = None
    classify_num_predict: int | None = None
    suggest_num_predict: int | None = None
    suggest_think: bool | None = False


@dataclass
class SuggestionStyleConfig:
    guidance: str = ""
    project_vocabulary: list[str] = field(default_factory=list)
    banned_phrases: list[str] = field(default_factory=list)
    specificity_floor: float = 0.0


@dataclass
class VerlatoConfig:
    state_path: str = ".verlato_state.json"
    suggestion_cache_backend: str = "json"
    suggestion_cache_path: str = ".verlato_suggestion_cache.json"
    suggestion_cache_ttl_days: int = 30
    provider: ProviderConfig = field(default_factory=ProviderConfig)
    suggestion_count: int = 1
    suggestion_parallelism: int = 1
    suggestion_batch_size: int = 2
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
        name = str(provider.get("name") or config.provider.name).strip() or "stub"
        model = provider.get("model")
        endpoint = provider.get("endpoint")
        max_items_per_scan = provider.get("max_items_per_scan", config.provider.max_items_per_scan)
        timeout_seconds = provider.get("timeout_seconds", config.provider.timeout_seconds)
        keep_alive = provider.get("keep_alive", config.provider.keep_alive)
        num_ctx = provider.get("num_ctx", config.provider.num_ctx)
        num_predict = provider.get("num_predict", config.provider.num_predict)
        summary_num_predict = provider.get("summary_num_predict", config.provider.summary_num_predict)
        classify_num_predict = provider.get("classify_num_predict", config.provider.classify_num_predict)
        suggest_num_predict = provider.get("suggest_num_predict", config.provider.suggest_num_predict)
        suggest_think = provider.get("suggest_think", config.provider.suggest_think)
        config.provider = ProviderConfig(
            name=name,
            model=str(model).strip() if model else None,
            endpoint=str(endpoint).strip() if endpoint else None,
            max_items_per_scan=int(max_items_per_scan),
            timeout_seconds=float(timeout_seconds) if timeout_seconds is not None else None,
            keep_alive=str(keep_alive).strip() if keep_alive is not None else None,
            num_ctx=int(num_ctx) if num_ctx is not None else None,
            num_predict=int(num_predict) if num_predict is not None else None,
            summary_num_predict=int(summary_num_predict) if summary_num_predict is not None else None,
            classify_num_predict=int(classify_num_predict) if classify_num_predict is not None else None,
            suggest_num_predict=int(suggest_num_predict) if suggest_num_predict is not None else None,
            suggest_think=bool(suggest_think) if suggest_think is not None else None,
        )

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


def resolve_target_path(path: str, *, cwd: str | None = None) -> Path:
    root = Path(cwd or os.getcwd())
    target = Path(path)
    if target.is_absolute():
        return target
    return root / target


def _resolve_config_path(cwd: str | None = None) -> Path | None:
    env_path = os.getenv("VERLATO_CONFIG_PATH")
    if env_path:
        return Path(env_path)

    root = Path(cwd or os.getcwd())
    candidate = root / "verlato.json"
    if candidate.exists():
        return candidate
    return None
