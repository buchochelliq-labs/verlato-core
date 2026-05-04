import json
from http import client
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


def test_ollama_provider_falls_back_to_generate_when_chat_disconnects(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        def read():
            return json.dumps({"response": "Generated summary"}).encode("utf-8")

    def fake_urlopen(req, timeout):
        captured.setdefault("urls", []).append(req.full_url)
        captured["timeout"] = timeout
        if req.full_url.endswith("/api/chat"):
            raise client.RemoteDisconnected("remote end closed connection without response")
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr("releasepilot.providers.ollama.request.urlopen", fake_urlopen)

    provider = OllamaProvider()
    response = provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change"))

    assert response.content == "Generated summary"
    assert captured["urls"] == [
        DEFAULT_OLLAMA_ENDPOINT,
        "http://127.0.0.1:11434/api/generate",
    ]
    assert captured["body"]["model"] == DEFAULT_OLLAMA_MODEL
    assert captured["body"]["stream"] is False
    assert captured["body"]["prompt"] == "Summarize this change"


def test_ollama_provider_reports_unavailable_server(monkeypatch):
    def fake_urlopen(req, timeout):
        raise error.URLError("connection refused")

    monkeypatch.setattr("releasepilot.providers.ollama.request.urlopen", fake_urlopen)

    provider = OllamaProvider()
    response = provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change"))

    assert response.confidence == 0.0
    assert response.content.startswith("[ollama-unavailable]")


def test_ollama_provider_traces_model_boundary(monkeypatch, capsys):
    monkeypatch.setenv("RELEASEPILOT_TRACE_MODEL_CALLS", "1")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        def read():
            return json.dumps({"message": {"content": "Local summary"}}).encode("utf-8")

    monkeypatch.setattr("releasepilot.providers.ollama.request.urlopen", lambda req, timeout: FakeResponse())

    provider = OllamaProvider(model="test-model")
    provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change", evidence={"files": []}))

    stderr = capsys.readouterr().err
    assert "[releasepilot:model] provider=ollama task=summarize model=test-model" in stderr
    assert "prompt_chars=21 evidence_keys=1" in stderr
    assert "[releasepilot:model] post provider=ollama task=summarize" in stderr
    assert "[releasepilot:model] response provider=ollama task=summarize content_chars=13 empty=False" in stderr
