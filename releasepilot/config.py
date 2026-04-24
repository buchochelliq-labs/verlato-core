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
class ReleasePilotConfig:
    state_path: str = ".releasepilot_state.json"
    targets: dict[str, OutputTargetConfig] = field(
        default_factory=lambda: {
            "changelog": OutputTargetConfig(path="CHANGELOG.md", format="changelog"),
            "github-release": OutputTargetConfig(path=".github/release.md", format="github-release"),
        }
    )
    raw: dict[str, Any] = field(default_factory=dict)


def load_config(cwd: str | None = None) -> ReleasePilotConfig:
    config_path = _resolve_config_path(cwd=cwd)
    if not config_path:
        return ReleasePilotConfig()

    try:
        raw = json.loads(config_path.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise ConfigError(f"Unable to load config from {config_path}: {exc}") from exc

    config = ReleasePilotConfig(raw=raw)
    if raw.get("state_path"):
        config.state_path = str(raw["state_path"])

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


def resolve_target_path(path: str, *, cwd: str | None = None) -> Path:
    root = Path(cwd or os.getcwd())
    target = Path(path)
    if target.is_absolute():
        return target
    return root / target


def _resolve_config_path(cwd: str | None = None) -> Path | None:
    env_path = os.getenv("RELEASEPILOT_CONFIG_PATH")
    if env_path:
        return Path(env_path)

    root = Path(cwd or os.getcwd())
    candidate = root / "releasepilot.json"
    if candidate.exists():
        return candidate
    return None
