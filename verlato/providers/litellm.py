from __future__ import annotations

import json
import os
from urllib import error, request

from verlato.providers.base import (
    ProviderRequest,
    ProviderResponse,
    VerlatoProvider,
    default_system_prompt,
    provider_messages_for_request,
    provider_request_audit,
)

DEFAULT_LITELLM_ENDPOINT = "http://127.0.0.1:4000/v1/chat/completions"
DEFAULT_LITELLM_MODEL = "ollama/gemma4:latest"
DEFAULT_LITELLM_TIMEOUT_SECONDS = 30.0


class LiteLLMProvider(VerlatoProvider):
    """
    Recommended BYOM path backed by a LiteLLM OpenAI-compatible proxy.
    """

    def __init__(
        self,
        *,
        endpoint: str | None = None,
        model: str | None = None,
        suggest_model: str | None = None,
        api_key_env: str | None = None,
        timeout_seconds: float | None = DEFAULT_LITELLM_TIMEOUT_SECONDS,
    ) -> None:
        self.endpoint = endpoint or os.getenv("VERLATO_LITELLM_ENDPOINT") or DEFAULT_LITELLM_ENDPOINT
        self.model = model or os.getenv("VERLATO_LITELLM_MODEL") or DEFAULT_LITELLM_MODEL
        self.suggest_model = _normalize_optional(suggest_model or os.getenv("VERLATO_LITELLM_SUGGEST_MODEL"))
        self.api_key_env = api_key_env or os.getenv("VERLATO_LITELLM_API_KEY_ENV") or "LITELLM_API_KEY"
        self.timeout_seconds = timeout_seconds or DEFAULT_LITELLM_TIMEOUT_SECONDS

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
        model = self.model_for_task(task)
        system_prompt = default_system_prompt(task)
        messages = provider_messages_for_request(request_data, system_prompt=system_prompt)
        audit = provider_request_audit(request_data, provider="litellm", model=model, messages=messages)
        body: dict[str, object] = {
            "model": model,
            "temperature": 0.1,
            "messages": messages,
            "metadata": {
                "verlato_request_hash": audit["request_hash"],
                "verlato_prompt_hash": audit["prompt_hash"],
                "verlato_evidence_hash": audit["evidence_hash"],
                "verlato_schema_hash": audit["schema_hash"],
            },
        }
        if request_data.response_format is not None:
            body["response_format"] = _openai_response_format(request_data.response_format, task=task)
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        api_key = os.getenv("VERLATO_LITELLM_API_KEY") or os.getenv(self.api_key_env)
        if api_key:
            headers["Authorization"] = f"Bearer {api_key}"
        payload = json.dumps(body).encode("utf-8")
        req = request.Request(self.endpoint, data=payload, headers=headers, method="POST")
        try:
            with request.urlopen(req, timeout=self.timeout_seconds) as resp:
                data = json.loads(resp.read().decode("utf-8"))
        except error.HTTPError as exc:
            return ProviderResponse(
                content=f"[litellm-error] request failed ({exc.code}): {exc.reason}",
                confidence=0.0,
                model=f"litellm:{model}",
                metadata={"audit": audit, "http_status": exc.code},
            )
        except (error.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
            return ProviderResponse(
                content=f"[litellm-error] request failed: {exc}",
                confidence=0.0,
                model=f"litellm:{model}",
                metadata={"audit": audit},
            )

        content, reasoning = _message_content(data)
        metadata = _response_metadata(data)
        metadata["audit"] = audit
        if model != self.model:
            metadata["base_model"] = self.model
            metadata["suggest_model"] = self.suggest_model
            metadata["effective_model"] = model
        if not content:
            return ProviderResponse(
                content="[litellm-empty] no content",
                confidence=0.0,
                model=f"litellm:{model}",
                metadata=metadata,
                reasoning=reasoning,
            )
        return ProviderResponse(
            content=content,
            confidence=0.75,
            model=f"litellm:{model}",
            metadata=metadata,
            reasoning=reasoning,
        )


def _openai_response_format(response_format: str | dict[str, object], *, task: str) -> dict[str, object] | str:
    if isinstance(response_format, str):
        return response_format
    return {
        "type": "json_schema",
        "json_schema": {
            "name": f"verlato_{task}_response",
            "schema": response_format,
            "strict": True,
        },
    }


def _message_content(data: dict[str, object]) -> tuple[str, str]:
    raw_choices = data.get("choices")
    choices = raw_choices if isinstance(raw_choices, list) else []
    if not choices or not isinstance(choices[0], dict):
        return "", ""
    raw_message = choices[0].get("message")
    message = raw_message if isinstance(raw_message, dict) else {}
    content = str(message.get("content") or "").strip()
    reasoning = str(message.get("reasoning") or message.get("reasoning_content") or "").strip()
    return content, reasoning


def _response_metadata(data: dict[str, object]) -> dict[str, object]:
    metadata: dict[str, object] = {}
    usage = data.get("usage")
    if isinstance(usage, dict):
        prompt_tokens = usage.get("prompt_tokens")
        completion_tokens = usage.get("completion_tokens")
        total_tokens = usage.get("total_tokens")
        if isinstance(prompt_tokens, int):
            metadata["prompt_tokens"] = prompt_tokens
        if isinstance(completion_tokens, int):
            metadata["eval_tokens"] = completion_tokens
        if isinstance(total_tokens, int):
            metadata["total_tokens"] = total_tokens
    return metadata


def _normalize_optional(value: str | None) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None
