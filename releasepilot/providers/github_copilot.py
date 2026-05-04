from __future__ import annotations

import json
import os
import subprocess
from http import HTTPStatus
from urllib import error, request

from releasepilot.providers.base import ProviderRequest, ProviderResponse, ReleasePilotProvider
from releasepilot.providers.model_logging import ModelLogSink, default_model_log_sink, model_log_record

DEFAULT_GITHUB_MODELS_ENDPOINT = "https://models.github.ai/inference/chat/completions"
DEFAULT_GITHUB_MODELS_MODEL = "openai/gpt-4o-mini"
GITHUB_MODELS_API_VERSION = "2022-11-28"


class GitHubCopilotProvider(ReleasePilotProvider):
    """
    Provider that can call a GitHub-hosted chat-completions compatible endpoint.

    This is intended for teams that want to run ReleasePilot against organization-
    managed GitHub models / Copilot-connected infrastructure rather than a local mock.
    """

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        token: str | None = None,
        model: str | None = None,
        timeout_seconds: float = 20.0,
        log_sink: ModelLogSink | None = None,
    ) -> None:
        self.endpoint: str = endpoint or os.getenv("RELEASEPILOT_GITHUB_MODELS_ENDPOINT") or DEFAULT_GITHUB_MODELS_ENDPOINT
        self.token = token or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or self._load_gh_cli_token()
        self.model = self._normalize_model_id(model or os.getenv("RELEASEPILOT_GITHUB_MODEL") or DEFAULT_GITHUB_MODELS_MODEL)
        self.timeout_seconds = timeout_seconds
        self.log_sink = log_sink or default_model_log_sink()

    def summarize(self, request_data: ProviderRequest) -> ProviderResponse:
        return self._invoke(task="summarize", request_data=request_data)

    def classify(self, request_data: ProviderRequest) -> ProviderResponse:
        return self._invoke(task="classify", request_data=request_data)

    def _invoke(self, *, task: str, request_data: ProviderRequest) -> ProviderResponse:
        if not self.token:
            self._trace(f"unavailable provider=github-copilot task={task} reason=missing-token", error=True)
            return ProviderResponse(
                content="[github-copilot-unavailable] missing GITHUB_TOKEN/GH_TOKEN and no logged-in gh user",
                confidence=0.0,
                model=f"github-copilot:{self.model}",
            )

        system_prompt = (
            "You are ReleasePilot. Return concise output for the requested task."
            if task == "summarize"
            else "You are ReleasePilot. Return one line: kind/area/audience/semver."
        )
        body: dict[str, object] = {
            "model": self.model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": request_data.prompt},
            ],
        }
        self._trace(
            f"provider=github-copilot task={task} model={self.model} endpoint={self.endpoint} "
            f"prompt_chars={len(request_data.prompt)} evidence_keys={len(request_data.evidence)}"
        )

        payload = json.dumps(body).encode("utf-8")
        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {self.token}",
            "Content-Type": "application/json",
            "X-GitHub-Api-Version": GITHUB_MODELS_API_VERSION,
        }
        req = request.Request(self.endpoint, data=payload, headers=headers, method="POST")

        try:
            self._trace(f"post provider=github-copilot task={task} endpoint={self.endpoint}")
            with request.urlopen(req, timeout=self.timeout_seconds) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as exc:
            self._trace(f"error provider=github-copilot task={task} http_status={exc.code}", error=True)
            return ProviderResponse(
                content=f"[github-copilot-error] {self._format_http_error(exc)}",
                confidence=0.0,
                model=f"github-copilot:{self.model}",
            )
        except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            self._trace(f"error provider=github-copilot task={task} reason={type(exc).__name__}", error=True)
            return ProviderResponse(
                content=f"[github-copilot-error] request failed: {exc}",
                confidence=0.0,
                model=f"github-copilot:{self.model}",
            )

        content = ""
        confidence = 0.75
        raw_choices = data.get("choices")
        choices = raw_choices if isinstance(raw_choices, list) else []
        if choices and isinstance(choices[0], dict):
            raw_message = choices[0].get("message")
            message = raw_message if isinstance(raw_message, dict) else {}
            content = str(message.get("content") or "").strip()
            reasoning = self._message_reasoning(message)
        else:
            reasoning = ""
        if not content:
            content = "[github-copilot-empty] no content"
            confidence = 0.0
        metadata = self._response_metadata(data)
        metadata_text = " ".join(f"{key}={value}" for key, value in sorted(metadata.items()))
        metadata_suffix = f" {metadata_text}" if metadata_text else ""
        self._trace(
            f"response provider=github-copilot task={task} choices={len(choices)} "
            f"content_chars={len(content)} empty={content.startswith('[github-copilot-empty]')}"
            f" reasoning_chars={len(reasoning)}{metadata_suffix}"
        )

        return ProviderResponse(
            content=content,
            confidence=confidence,
            model=f"github-copilot:{self.model}",
            metadata=metadata,
            reasoning=reasoning,
        )

    @staticmethod
    def _normalize_model_id(model: str) -> str:
        normalized = model.strip()
        if not normalized:
            return DEFAULT_GITHUB_MODELS_MODEL
        if "/" in normalized:
            return normalized
        if normalized.startswith(("gpt-", "o1", "o3", "o4")):
            return f"openai/{normalized}"
        return normalized

    @staticmethod
    def _format_http_error(exc: error.HTTPError) -> str:
        try:
            reason = HTTPStatus(exc.code).phrase
        except ValueError:
            reason = "HTTP error"
        details = ""
        try:
            body = exc.read().decode("utf-8").strip()
        except (OSError, UnicodeDecodeError):
            body = ""
        if body:
            try:
                parsed = json.loads(body)
            except json.JSONDecodeError:
                details = body
            else:
                message = parsed.get("message") if isinstance(parsed, dict) else None
                details = str(message or body)
        if exc.code in {401, 403}:
            hint = "check that the token has access to GitHub Models"
        elif exc.code == 429:
            hint = "GitHub Models rate limit exceeded"
        else:
            hint = "GitHub Models request failed"
        suffix = f": {details}" if details else ""
        return f"{hint} ({exc.code} {reason}){suffix}"

    @staticmethod
    def _response_metadata(data: dict[str, object]) -> dict[str, object]:
        metadata: dict[str, object] = {}
        usage = data.get("usage")
        if isinstance(usage, dict):
            for key in ("prompt_tokens", "completion_tokens", "total_tokens"):
                if key in usage:
                    metadata[key] = usage[key]
        if "id" in data:
            metadata["response_id"] = data["id"]
        return metadata

    @staticmethod
    def _message_reasoning(message: dict[str, object]) -> str:
        candidates = [
            message.get(key)
            for key in ("reasoning", "reasoning_content", "thinking")
            if message.get(key)
        ]
        return "\n".join(str(candidate).strip() for candidate in candidates if str(candidate).strip())

    @staticmethod
    def _load_gh_cli_token() -> str | None:
        try:
            result = subprocess.run(
                ["gh", "auth", "token"],
                check=False,
                capture_output=True,
                text=True,
                timeout=5,
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        token = result.stdout.strip()
        return token or None

    def _trace(self, message: str, *, error: bool = False) -> None:
        self.log_sink.emit(
            model_log_record(
                message,
                event_name="releasepilot.model.github_copilot",
                attributes={"provider": "github-copilot", "model": self.model},
                error=error,
            )
        )
