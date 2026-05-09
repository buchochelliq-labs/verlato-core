from __future__ import annotations

import json
import os
import shlex
import subprocess
import tempfile
from dataclasses import dataclass, field
from typing import Any

from verlato.providers.base import (
    ProviderRequest,
    ProviderResponse,
    VerlatoProvider,
    default_system_prompt,
    provider_messages_for_request,
    provider_request_audit,
)

PLUGIN_REQUEST_SCHEMA = "verlato.model_plugin.request.v1"
PLUGIN_RESPONSE_SCHEMA = "verlato.model_plugin.response.v1"
DEFAULT_PLUGIN_TIMEOUT_SECONDS = 30.0


@dataclass
class ModelPluginManifest:
    id: str
    display_name: str
    command: list[str]
    config_schema: dict[str, Any] = field(default_factory=dict)
    capabilities: dict[str, Any] = field(default_factory=dict)


class PluginManifestError(ValueError):
    pass


class ExternalModelPluginProvider(VerlatoProvider):
    """
    Audited external-command BYOM adapter.

    Verlato owns prompts, evidence, response schema, and validation. The plugin
    receives one canonical JSON request over stdin and returns JSON over stdout.
    """

    def __init__(
        self,
        *,
        manifest: dict[str, Any] | None = None,
        command: list[str] | str | None = None,
        plugin_config: dict[str, Any] | None = None,
        env_names: list[str] | None = None,
        model: str | None = None,
        suggest_model: str | None = None,
        timeout_seconds: float | None = DEFAULT_PLUGIN_TIMEOUT_SECONDS,
    ) -> None:
        raw_manifest = manifest or {}
        self.manifest = parse_plugin_manifest(raw_manifest, command=command)
        self.command = self.manifest.command
        self.plugin_config = plugin_config or {}
        self.env_names = env_names or []
        self.model = model or os.getenv("VERLATO_PLUGIN_MODEL") or self.manifest.id
        self.suggest_model = _normalize_optional(suggest_model or os.getenv("VERLATO_PLUGIN_SUGGEST_MODEL"))
        self.timeout_seconds = timeout_seconds or DEFAULT_PLUGIN_TIMEOUT_SECONDS

    def summarize(self, request_data: ProviderRequest) -> ProviderResponse:
        task = (
            request_data.task
            if request_data.task in {"suggest", "infer-conventional", "derive-conventional", "draft-conventional"}
            else "summarize"
        )
        return self._invoke(task=task, request_data=request_data)

    def classify(self, request_data: ProviderRequest) -> ProviderResponse:
        return self._invoke(task="classify", request_data=request_data)

    def model_for_task(self, task: str) -> str:
        return self.suggest_model if task == "suggest" and self.suggest_model else self.model

    def _invoke(self, *, task: str, request_data: ProviderRequest) -> ProviderResponse:
        if not self.command:
            return ProviderResponse(
                content="[plugin-unavailable] no command configured",
                confidence=0.0,
                model=f"plugin:{self.manifest.id}:{self.model_for_task(task)}",
            )
        model = self.model_for_task(task)
        system_prompt = default_system_prompt(task)
        messages = provider_messages_for_request(request_data, system_prompt=system_prompt)
        audit = provider_request_audit(
            request_data,
            provider=f"plugin:{self.manifest.id}",
            model=model,
            messages=messages,
        )
        payload = {
            "schema": PLUGIN_REQUEST_SCHEMA,
            "plugin": {
                "id": self.manifest.id,
                "display_name": self.manifest.display_name,
                "capabilities": self.manifest.capabilities,
            },
            "request_hash": audit["request_hash"],
            "prompt_hash": audit["prompt_hash"],
            "evidence_hash": audit["evidence_hash"],
            "schema_hash": audit["schema_hash"],
            "task": request_data.task,
            "model": model,
            "messages": messages,
            "prompt": request_data.prompt,
            "evidence": request_data.evidence,
            "response_format": request_data.response_format,
            "config": self.plugin_config,
            "metadata": dict(request_data.metadata),
        }
        try:
            completed = subprocess.run(
                self.command,
                input=json.dumps(payload, sort_keys=True),
                capture_output=True,
                text=True,
                check=False,
                timeout=self.timeout_seconds,
                cwd=tempfile.gettempdir(),
                env=self._minimal_env(),
            )
        except subprocess.TimeoutExpired as exc:
            return ProviderResponse(
                content=f"[plugin-timeout] command timed out after {self.timeout_seconds:g}s",
                confidence=0.0,
                model=f"plugin:{self.manifest.id}:{model}",
                metadata={"audit": audit, "stderr": _trim_text(exc.stderr)},
            )
        except OSError as exc:
            return ProviderResponse(
                content=f"[plugin-error] command failed to start: {exc}",
                confidence=0.0,
                model=f"plugin:{self.manifest.id}:{model}",
                metadata={"audit": audit},
            )
        stderr = _trim_text(completed.stderr)
        if completed.returncode != 0:
            return ProviderResponse(
                content=f"[plugin-error] command exited {completed.returncode}",
                confidence=0.0,
                model=f"plugin:{self.manifest.id}:{model}",
                metadata={"audit": audit, "stderr": stderr, "returncode": completed.returncode},
            )
        try:
            raw_response = json.loads(completed.stdout or "{}")
        except json.JSONDecodeError as exc:
            return ProviderResponse(
                content=f"[plugin-invalid-json] stdout was not valid JSON: {exc}",
                confidence=0.0,
                model=f"plugin:{self.manifest.id}:{model}",
                metadata={"audit": audit, "stderr": stderr},
            )
        if not isinstance(raw_response, dict):
            return ProviderResponse(
                content="[plugin-invalid-json] stdout JSON must be an object",
                confidence=0.0,
                model=f"plugin:{self.manifest.id}:{model}",
                metadata={"audit": audit, "stderr": stderr},
            )
        returned_hash = raw_response.get("request_hash")
        if returned_hash != audit["request_hash"]:
            return ProviderResponse(
                content="[plugin-tamper] request_hash missing or mismatched",
                confidence=0.0,
                model=f"plugin:{self.manifest.id}:{model}",
                metadata={
                    "audit": audit,
                    "stderr": stderr,
                    "returned_request_hash": returned_hash,
                },
            )
        content = str(raw_response.get("content") or "").strip()
        if not content:
            return ProviderResponse(
                content="[plugin-empty] no content",
                confidence=0.0,
                model=f"plugin:{self.manifest.id}:{model}",
                metadata={"audit": audit, "stderr": stderr},
            )
        metadata = raw_response.get("metadata")
        response_metadata = dict(metadata) if isinstance(metadata, dict) else {}
        response_metadata["audit"] = audit
        if stderr:
            response_metadata["stderr"] = stderr
        response_schema = raw_response.get("schema")
        if response_schema:
            response_metadata["schema"] = response_schema
        return ProviderResponse(
            content=content,
            confidence=_confidence(raw_response.get("confidence")),
            model=str(raw_response.get("model") or f"plugin:{self.manifest.id}:{model}"),
            metadata=response_metadata,
            reasoning=str(raw_response.get("reasoning") or ""),
        )

    def _minimal_env(self) -> dict[str, str]:
        allowed = {
            "PATH",
            "PATHEXT",
            "SYSTEMROOT",
            "WINDIR",
            "COMSPEC",
            "TEMP",
            "TMP",
        }
        env = {key: value for key in allowed if (value := os.environ.get(key))}
        for name in self.env_names:
            value = os.environ.get(name)
            if value is not None:
                env[name] = value
        return env


def parse_plugin_manifest(raw: dict[str, Any], *, command: list[str] | str | None = None) -> ModelPluginManifest:
    plugin_id = str(raw.get("id") or "external").strip()
    display_name = str(raw.get("display_name") or raw.get("name") or plugin_id).strip()
    raw_command = command if command is not None else raw.get("command")
    parsed_command = _parse_command(raw_command)
    raw_config_schema = raw.get("config_schema")
    raw_capabilities = raw.get("capabilities")
    config_schema: dict[str, Any] = dict(raw_config_schema) if isinstance(raw_config_schema, dict) else {}
    capabilities: dict[str, Any] = dict(raw_capabilities) if isinstance(raw_capabilities, dict) else {}
    if not plugin_id:
        raise PluginManifestError("Plugin manifest id is required.")
    if not display_name:
        raise PluginManifestError("Plugin manifest display_name is required.")
    if not parsed_command:
        raise PluginManifestError("Plugin manifest command is required.")
    return ModelPluginManifest(
        id=plugin_id,
        display_name=display_name,
        command=parsed_command,
        config_schema=config_schema,
        capabilities=capabilities,
    )


def _parse_command(value: list[str] | str | object | None) -> list[str]:
    if isinstance(value, list):
        return [str(part) for part in value if str(part).strip()]
    if isinstance(value, str):
        return [part for part in shlex.split(value, posix=os.name != "nt") if part]
    return []


def _confidence(value: object) -> float:
    if not isinstance(value, (int, float, str)):
        return 0.75
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return 0.75
    return max(0.0, min(parsed, 1.0))


def _trim_text(value: object, limit: int = 4000) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    return text[:limit]


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None
