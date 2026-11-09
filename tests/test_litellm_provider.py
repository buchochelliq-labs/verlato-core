from __future__ import annotations

import json

from verlato.providers.base import ProviderRequest
from verlato.providers.litellm import LiteLLMProvider


class _Response:
    def __init__(self, payload: dict[str, object]) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


def test_litellm_provider_sends_schema_and_records_audit(monkeypatch):
    captured: dict[str, object] = {}

    def fake_urlopen(req, timeout):
        captured["timeout"] = timeout
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Response(
            {
                "choices": [
                    {"message": {"content": '{"items":[{"item_id":"ci_1","suggestions":["Clarifies diff-backed BYOM suggestions"]}]}'}}
                ],
                "usage": {"prompt_tokens": 12, "completion_tokens": 8, "total_tokens": 20},
            }
        )

    monkeypatch.setattr("verlato.providers.litellm.request.urlopen", fake_urlopen)
    provider = LiteLLMProvider(endpoint="http://litellm.test/v1/chat/completions", model="ollama/test", timeout_seconds=4)

    response = provider.summarize(
        ProviderRequest(
            task="suggest",
            prompt="Prompt",
            evidence={"item_ids": ["ci_1"]},
            response_format={"type": "object", "properties": {"items": {"type": "array"}}},
        )
    )

    body = captured["body"]
    assert captured["timeout"] == 4
    assert body["model"] == "ollama/test"
    assert body["response_format"]["type"] == "json_schema"
    assert body["metadata"]["verlato_request_hash"] == response.metadata["audit"]["request_hash"]
    assert response.content.startswith('{"items"')
    assert response.metadata["prompt_tokens"] == 12
    assert response.metadata["eval_tokens"] == 8


def test_litellm_provider_caps_output_tokens(monkeypatch):
    bodies: list[dict[str, object]] = []

    def fake_urlopen(req, timeout):
        bodies.append(json.loads(req.data.decode("utf-8")))
        return _Response({"choices": [{"message": {"content": "Fixes pagination."}}]})

    monkeypatch.setattr("verlato.providers.litellm.request.urlopen", fake_urlopen)
    monkeypatch.delenv("VERLATO_LITELLM_MAX_TOKENS", raising=False)
    provider = LiteLLMProvider(endpoint="http://litellm.test/v1/chat/completions", model="m")
    provider.summarize(ProviderRequest(task="summarize", prompt="p", evidence={}))
    provider.summarize(ProviderRequest(task="suggest", prompt="p", evidence={"suggestion_count": 3}))
    provider.summarize(ProviderRequest(task="suggest", prompt="p", evidence={"suggestion_count": 9}))
    assert [body["max_tokens"] for body in bodies] == [1024, 3072, 4096]

    monkeypatch.setenv("VERLATO_LITELLM_MAX_TOKENS", "0")
    LiteLLMProvider(endpoint="http://litellm.test/v1/chat/completions", model="m").summarize(
        ProviderRequest(task="summarize", prompt="p", evidence={})
    )
    assert "max_tokens" not in bodies[-1]
    assert LiteLLMProvider(max_tokens=200).max_tokens == 0  # the environment wins over config
    monkeypatch.delenv("VERLATO_LITELLM_MAX_TOKENS")
    assert LiteLLMProvider(max_tokens=200).max_tokens == 200
