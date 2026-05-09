from __future__ import annotations

import hashlib
import json
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Callable


@dataclass
class ProviderRequest:
    task: str
    prompt: str
    evidence: dict[str, Any] = field(default_factory=dict)
    response_format: str | dict[str, Any] | None = None
    metadata: dict[str, Any] = field(default_factory=dict)
    tools: list[dict[str, Any]] = field(default_factory=list)
    tool_executor: Callable[[str, dict[str, Any]], Any] | None = None
    max_tool_rounds: int = 0


@dataclass
class ProviderResponse:
    content: str
    confidence: float = 0.5
    model: str = "unknown"
    metadata: dict[str, Any] = field(default_factory=dict)
    reasoning: str = ""


def canonical_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)


def sha256_hex(value: Any) -> str:
    return hashlib.sha256(canonical_json(value).encode("utf-8")).hexdigest()


def default_system_prompt(task: str) -> str:
    if task in {"summarize", "suggest"}:
        return (
            "You are Verlato. Write a useful release note with enough context for downstream consumers. "
            "Use paragraphs or bullet points when they make the change clearer, but stay focused and avoid padding."
        )
    if task == "infer-conventional":
        return (
            "You are Verlato. Infer a conventional commit from commit evidence. "
            "Return only strict JSON matching the requested schema."
        )
    return "You are Verlato. Return one line: kind/area/audience/semver."


def provider_messages_for_request(
    request: ProviderRequest,
    *,
    system_prompt: str | None = None,
) -> list[dict[str, str]]:
    return [
        {"role": "system", "content": system_prompt or default_system_prompt(request.task)},
        {"role": "user", "content": request.prompt},
    ]


def provider_request_audit(
    request: ProviderRequest,
    *,
    provider: str,
    model: str,
    messages: list[dict[str, str]] | None = None,
) -> dict[str, str]:
    prompt_payload = {
        "prompt": request.prompt,
        "messages": messages if messages is not None else provider_messages_for_request(request),
    }
    prompt_hash = sha256_hex(prompt_payload)
    evidence_hash = sha256_hex(request.evidence)
    schema_hash = sha256_hex(request.response_format)
    request_hash = sha256_hex(
        {
            "schema": "verlato.model_request.v1",
            "provider": provider,
            "model": model,
            "task": request.task,
            "prompt_hash": prompt_hash,
            "evidence_hash": evidence_hash,
            "schema_hash": schema_hash,
        }
    )
    return {
        "request_hash": request_hash,
        "prompt_hash": prompt_hash,
        "evidence_hash": evidence_hash,
        "schema_hash": schema_hash,
    }


class VerlatoProvider(ABC):
    @abstractmethod
    def summarize(self, request: ProviderRequest) -> ProviderResponse:
        raise NotImplementedError

    @abstractmethod
    def classify(self, request: ProviderRequest) -> ProviderResponse:
        raise NotImplementedError
