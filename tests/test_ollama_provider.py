import json
from http import client
from urllib import error

from releasepilot.providers.base import ProviderRequest
from releasepilot.providers.model_logging import ModelLogRecord, ModelLogSink, redact_model_payload
from releasepilot.providers.ollama import DEFAULT_OLLAMA_ENDPOINT, DEFAULT_OLLAMA_MODEL, OllamaProvider


def test_ollama_provider_uses_local_defaults(monkeypatch):
    monkeypatch.delenv("RELEASEPILOT_OLLAMA_ENDPOINT", raising=False)
    monkeypatch.delenv("RELEASEPILOT_OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("RELEASEPILOT_OLLAMA_TIMEOUT_SECONDS", raising=False)

    provider = OllamaProvider()

    assert provider.endpoint == DEFAULT_OLLAMA_ENDPOINT
    assert provider.model == DEFAULT_OLLAMA_MODEL
    assert provider.timeout_seconds == 120.0


def test_model_payload_redaction_masks_common_secret_shapes():
    redacted, changed = redact_model_payload(
        "Authorization: Bearer sk-testsecretvalue123456\n"
        "GITHUB_TOKEN=ghp_abcdefghijklmnopqrstuvwxyz123456\n"
        "api_key=plain-secret-value\n"
        "digest=0123456789abcdef0123456789abcdef01234567"
    )

    assert changed is True
    assert "sk-testsecretvalue" not in redacted
    assert "ghp_" not in redacted
    assert "plain-secret-value" not in redacted
    assert "0123456789abcdef0123456789abcdef01234567" not in redacted
    assert redacted.count("[REDACTED]") >= 4


def test_ollama_provider_uses_timeout_env(monkeypatch):
    monkeypatch.setenv("RELEASEPILOT_OLLAMA_TIMEOUT_SECONDS", "42.5")

    provider = OllamaProvider()

    assert provider.timeout_seconds == 42.5


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


def test_ollama_provider_streams_chat_response_chunks(monkeypatch):
    captured: dict[str, object] = {}
    records: list[ModelLogRecord] = []

    class CapturingSink(ModelLogSink):
        def emit(self, record: ModelLogRecord) -> None:
            records.append(record)

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            return iter(
                [
                    b'{"message":{"thinking":"Used "},"done":false}\n',
                    b'{"message":{"thinking":"diffs. "},"done":false}\n',
                    b'{"message":{"content":"Local "},"done":false}\n',
                    b'{"message":{"content":"summary"},"done":true,"done_reason":"stop","eval_count":2}\n',
                ]
            )

    def fake_urlopen(req, timeout):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr("releasepilot.providers.ollama.request.urlopen", fake_urlopen)

    provider = OllamaProvider(model="test-model", stream=True, log_sink=CapturingSink())
    response = provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change"))

    assert captured["body"]["stream"] is True
    assert response.content == "Local summary"
    assert response.reasoning == "Used diffs."
    assert response.metadata == {"eval_count": 2, "done_reason": "stop"}
    bodies = [record.body for record in records]
    assert any("stream-chunk provider=ollama task=summarize kind=reasoning" in body for body in bodies)
    assert any("stream-chunk provider=ollama task=summarize kind=content" in body for body in bodies)
    assert any("stream-complete provider=ollama task=summarize chunks=4" in body for body in bodies)


def test_ollama_provider_hides_streamed_reasoning_text_unless_enabled(monkeypatch):
    records: list[ModelLogRecord] = []

    class CapturingSink(ModelLogSink):
        def emit(self, record: ModelLogRecord) -> None:
            records.append(record)

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def __iter__(self):
            return iter(
                [
                    b'{"message":{"thinking":"Private reasoning"},"done":false}\n',
                    b'{"message":{"content":"Summary"},"done":true}\n',
                ]
            )

    monkeypatch.delenv("RELEASEPILOT_TRACE_MODEL_REASONING", raising=False)
    monkeypatch.setattr("releasepilot.providers.ollama.request.urlopen", lambda req, timeout: FakeResponse())

    provider = OllamaProvider(model="test-model", stream=True, log_sink=CapturingSink())
    response = provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change"))

    assert response.reasoning == "Private reasoning"
    bodies = [record.body for record in records]
    assert any("kind=reasoning chars=17 redacted=false text=" in body for body in bodies)
    assert not any("Private reasoning" in body for body in bodies)


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
    assert "timeout_seconds=120" in stderr
    assert "[releasepilot:model] post provider=ollama task=summarize" in stderr
    assert "[releasepilot:model] response provider=ollama task=summarize content_chars=13 empty=False" in stderr


def test_ollama_provider_accepts_pluggable_model_log_sink(monkeypatch):
    records: list[ModelLogRecord] = []

    class CapturingSink(ModelLogSink):
        def emit(self, record: ModelLogRecord) -> None:
            records.append(record)

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"message":{"content":"Model summary"},"prompt_eval_count":7,"eval_count":3}'

    monkeypatch.setattr("releasepilot.providers.ollama.request.urlopen", lambda req, timeout: FakeResponse())

    provider = OllamaProvider(model="test-model", log_sink=CapturingSink())
    response = provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change"))

    assert response.metadata == {"prompt_eval_count": 7, "eval_count": 3}
    assert records
    assert records[0].event_name == "releasepilot.model.ollama.log"
    assert records[0].to_otel_dict()["Body"].startswith("provider=ollama")
    assert records[0].to_otel_dict()["Attributes"] == {"provider": "ollama", "model": "test-model"}


def test_ollama_provider_can_persist_jsonl_model_logs(monkeypatch, tmp_path):
    monkeypatch.delenv("RELEASEPILOT_TRACE_MODEL_CALLS", raising=False)
    monkeypatch.setenv("RELEASEPILOT_MODEL_LOG_PATH", str(tmp_path / "model.jsonl"))

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"message":{"content":"Model summary"},"prompt_eval_count":7}'

    monkeypatch.setattr("releasepilot.providers.ollama.request.urlopen", lambda req, timeout: FakeResponse())

    provider = OllamaProvider(model="test-model")
    provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change"))

    records = [json.loads(line) for line in (tmp_path / "model.jsonl").read_text().splitlines()]
    assert records
    assert records[0]["EventName"] == "releasepilot.model.ollama.log"
    assert records[0]["Attributes"] == {"provider": "ollama", "model": "test-model"}


def test_ollama_provider_preserves_model_reasoning(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"message":{"content":"Model summary","thinking":"Used auth hunk as primary evidence."}}'

    monkeypatch.setattr("releasepilot.providers.ollama.request.urlopen", lambda req, timeout: FakeResponse())

    provider = OllamaProvider(model="test-model")
    response = provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change"))

    assert response.content == "Model summary"
    assert response.reasoning == "Used auth hunk as primary evidence."
