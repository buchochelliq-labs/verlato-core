from __future__ import annotations

import json
import os
import sys
from http import client
from urllib import error, request

from releasepilot.providers.base import ProviderRequest, ProviderResponse, ReleasePilotProvider

DEFAULT_OLLAMA_ENDPOINT = "http://127.0.0.1:11434/api/chat"
DEFAULT_OLLAMA_MODEL = "gemma4:latest"
DEFAULT_OLLAMA_TIMEOUT_SECONDS = 120.0


class OllamaProvider(ReleasePilotProvider):
    """
    Local provider backed by a local Ollama server.
    """

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        model: str | None = None,
        timeout_seconds: float = DEFAULT_OLLAMA_TIMEOUT_SECONDS,
    ) -> None:
        self.endpoint = endpoint or os.getenv("RELEASEPILOT_OLLAMA_ENDPOINT", DEFAULT_OLLAMA_ENDPOINT)
        self.model = model or os.getenv("RELEASEPILOT_OLLAMA_MODEL", DEFAULT_OLLAMA_MODEL)
        self.timeout_seconds = timeout_seconds

    def summarize(self, request_data: ProviderRequest) -> ProviderResponse:
        return self._invoke(task="summarize", request_data=request_data)

    def classify(self, request_data: ProviderRequest) -> ProviderResponse:
        return self._invoke(task="classify", request_data=request_data)

    def _invoke(self, *, task: str, request_data: ProviderRequest) -> ProviderResponse:
        system_prompt = (
            "You are ReleasePilot. Return concise output for the requested task."
            if task == "summarize"
            else "You are ReleasePilot. Return one line: kind/area/audience/semver."
        )
        self._trace(
            f"provider=ollama task={task} model={self.model} endpoint={self.endpoint} "
            f"prompt_chars={len(request_data.prompt)} evidence_keys={len(request_data.evidence)}"
        )
        body = {
            "model": self.model,
            "stream": False,
            "options": {"temperature": 0.1},
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": request_data.prompt},
            ],
        }

        try:
            self._trace(f"post provider=ollama task={task} endpoint={self.endpoint} mode=chat")
            data = self._post_json(self.endpoint, body)
        except client.RemoteDisconnected:
            self._trace(
                f"fallback provider=ollama task={task} from=chat to=generate reason=remote-disconnected"
            )
            try:
                data = self._invoke_generate(system_prompt=system_prompt, user_prompt=request_data.prompt)
            except (client.RemoteDisconnected, error.URLError, TimeoutError, json.JSONDecodeError) as exc:
                self._trace(f"error provider=ollama task={task} stage=generate reason={type(exc).__name__}")
                return self._unavailable_response(exc)
        except error.HTTPError as exc:
            self._trace(f"error provider=ollama task={task} stage=chat http_status={exc.code}")
            return ProviderResponse(
                content=f"[ollama-error] local model request failed ({exc.code}): {exc.reason}",
                confidence=0.0,
                model=f"ollama:{self.model}",
            )
        except (error.URLError, TimeoutError, json.JSONDecodeError) as exc:
            self._trace(f"error provider=ollama task={task} stage=chat reason={type(exc).__name__}")
            return self._unavailable_response(exc)

        message = data.get("message") or {}
        content = str(message.get("content") or data.get("response") or "").strip()
        self._trace(f"response provider=ollama task={task} content_chars={len(content)} empty={not bool(content)}")
        if not content:
            return ProviderResponse(
                content="[ollama-empty] no content",
                confidence=0.0,
                model=f"ollama:{self.model}",
            )
        return ProviderResponse(content=content, confidence=0.75, model=f"ollama:{self.model}")

    def _invoke_generate(self, *, system_prompt: str, user_prompt: str) -> dict[str, object]:
        body = {
            "model": self.model,
            "stream": False,
            "options": {"temperature": 0.1},
            "system": system_prompt,
            "prompt": user_prompt,
        }
        self._trace(f"post provider=ollama task=generate endpoint={self._generate_endpoint()} mode=generate")
        return self._post_json(self._generate_endpoint(), body)

    def _post_json(self, endpoint: str, body: dict[str, object]) -> dict[str, object]:
        payload = json.dumps(body).encode("utf-8")
        headers = {
            "Content-Type": "application/json",
        }
        req = request.Request(endpoint, data=payload, headers=headers, method="POST")
        with request.urlopen(req, timeout=self.timeout_seconds) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        if not isinstance(data, dict):
            raise json.JSONDecodeError("expected JSON object", str(data), 0)
        return data

    def _generate_endpoint(self) -> str:
        if self.endpoint.rstrip("/").endswith("/api/chat"):
            return f"{self.endpoint.rstrip('/')[:-len('/api/chat')]}/api/generate"
        return self.endpoint

    def _unavailable_response(self, exc: BaseException) -> ProviderResponse:
        return ProviderResponse(
            content=f"[ollama-unavailable] unable to reach local Ollama at {self.endpoint}: {exc}",
            confidence=0.0,
            model=f"ollama:{self.model}",
        )

    def _trace(self, message: str) -> None:
        if os.getenv("RELEASEPILOT_TRACE_MODEL_CALLS") != "1":
            return
        print(f"[releasepilot:model] {message}", file=sys.stderr, flush=True)
