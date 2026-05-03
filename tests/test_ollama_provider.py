import json
from urllib import error

from releasepilot.providers.base import ProviderRequest
from releasepilot.providers.ollama import DEFAULT_OLLAMA_ENDPOINT, DEFAULT_OLLAMA_MODEL, OllamaProvider


def test_ollama_provider_uses_local_defaults(monkeypatch):
    monkeypatch.delenv("RELEASEPILOT_OLLAMA_ENDPOINT", raising=False)
    monkeypatch.delenv("RELEASEPILOT_OLLAMA_MODEL", raising=False)

    provider = OllamaProvider()

    assert provider.endpoint == DEFAULT_OLLAMA_ENDPOINT
    assert provider.model == DEFAULT_OLLAMA_MODEL


def test_ollama_provider_sends_local_chat_request(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        def read():
            return json.dumps({"message": {"content": "Local summary"}}).encode("utf-8")

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr("releasepilot.providers.ollama.request.urlopen", fake_urlopen)

    provider = OllamaProvider(timeout_seconds=5.0)
    response = provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change"))

    assert response.content == "Local summary"
    assert response.model == f"ollama:{DEFAULT_OLLAMA_MODEL}"
    assert captured["url"] == DEFAULT_OLLAMA_ENDPOINT
    assert captured["timeout"] == 5.0
    assert captured["body"]["model"] == DEFAULT_OLLAMA_MODEL
    assert captured["body"]["stream"] is False


def test_ollama_provider_reports_unavailable_server(monkeypatch):
    def fake_urlopen(req, timeout):
        raise error.URLError("connection refused")

    monkeypatch.setattr("releasepilot.providers.ollama.request.urlopen", fake_urlopen)

    provider = OllamaProvider()
    response = provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change"))

    assert response.confidence == 0.0
    assert response.content.startswith("[ollama-unavailable]")
