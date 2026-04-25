from __future__ import annotations

import json
import os
import subprocess
from urllib import error, request

from releasepilot.providers.base import ProviderRequest, ProviderResponse, ReleasePilotProvider


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
    ) -> None:
        self.endpoint = endpoint or os.getenv(
            "RELEASEPILOT_GITHUB_MODELS_ENDPOINT",
            "https://models.inference.ai.azure.com/chat/completions",
        )
        self.token = token or os.getenv("GITHUB_TOKEN") or os.getenv("GH_TOKEN") or self._load_gh_cli_token()
        self.model = model or os.getenv("RELEASEPILOT_GITHUB_MODEL", "gpt-4o-mini")
        self.timeout_seconds = timeout_seconds

    def summarize(self, request_data: ProviderRequest) -> ProviderResponse:
        return self._invoke(task="summarize", request_data=request_data)

    def classify(self, request_data: ProviderRequest) -> ProviderResponse:
        return self._invoke(task="classify", request_data=request_data)

    def _invoke(self, *, task: str, request_data: ProviderRequest) -> ProviderResponse:
        if not self.token:
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
        body = {
            "model": self.model,
            "temperature": 0.1,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": request_data.prompt},
            ],
        }

        payload = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.token}",
        }
        req = request.Request(self.endpoint, data=payload, headers=headers, method="POST")

        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except (error.URLError, TimeoutError, json.JSONDecodeError):
            return ProviderResponse(
                content="[github-copilot-error] request failed",
                confidence=0.0,
                model=f"github-copilot:{self.model}",
            )

        content = ""
        confidence = 0.75
        choices = data.get("choices") or []
        if choices:
            message = choices[0].get("message") or {}
            content = str(message.get("content") or "").strip()
        if not content:
            content = "[github-copilot-empty] no content"
            confidence = 0.0

        return ProviderResponse(content=content, confidence=confidence, model=f"github-copilot:{self.model}")

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
