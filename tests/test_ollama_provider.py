import json
from http import client
from urllib import error

from verlato.providers.base import ProviderRequest
from verlato.providers.model_logging import ModelLogRecord, ModelLogSink, redact_model_payload
from verlato.providers.ollama import (
    DEFAULT_OLLAMA_ENDPOINT,
    DEFAULT_OLLAMA_MAX_SUGGEST_NUM_PREDICT,
    DEFAULT_OLLAMA_MIN_SUGGEST_NUM_PREDICT,
    DEFAULT_OLLAMA_MODEL,
    OllamaProvider,
)


def _clear_ollama_tuning_env(monkeypatch):
    for name in (
        "VERLATO_OLLAMA_KEEP_ALIVE",
        "VERLATO_OLLAMA_NUM_CTX",
        "VERLATO_OLLAMA_NUM_PREDICT",
        "VERLATO_OLLAMA_SUMMARY_NUM_PREDICT",
        "VERLATO_OLLAMA_CLASSIFY_NUM_PREDICT",
        "VERLATO_OLLAMA_SUGGEST_NUM_PREDICT",
        "VERLATO_OLLAMA_SUGGEST_THINK",
        "VERLATO_OLLAMA_SUGGEST_MODEL",
    ):
        monkeypatch.delenv(name, raising=False)


def test_ollama_provider_uses_local_defaults(monkeypatch):
    monkeypatch.delenv("VERLATO_OLLAMA_ENDPOINT", raising=False)
    monkeypatch.delenv("VERLATO_OLLAMA_MODEL", raising=False)
    monkeypatch.delenv("VERLATO_OLLAMA_TIMEOUT_SECONDS", raising=False)
    _clear_ollama_tuning_env(monkeypatch)

    provider = OllamaProvider()

    assert provider.endpoint == DEFAULT_OLLAMA_ENDPOINT
    assert provider.model == DEFAULT_OLLAMA_MODEL
    assert provider.timeout_seconds == 15.0
    assert provider.keep_alive == "10m"
    assert provider.summary_num_predict == 180
    assert provider.classify_num_predict == 24
    assert provider.suggest_model is None
    assert provider.effective_suggest_model == DEFAULT_OLLAMA_MODEL
    assert provider.suggest_num_predict is None
    assert provider.suggest_think is False


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
    monkeypatch.setenv("VERLATO_OLLAMA_TIMEOUT_SECONDS", "42.5")

    provider = OllamaProvider()

    assert provider.timeout_seconds == 42.5


def test_ollama_provider_sends_local_chat_request(monkeypatch):
    _clear_ollama_tuning_env(monkeypatch)
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

    monkeypatch.setattr("verlato.providers.ollama.request.urlopen", fake_urlopen)

    provider = OllamaProvider(timeout_seconds=5.0)
    response = provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change"))

    assert response.content == "Local summary"
    assert response.model == f"ollama:{DEFAULT_OLLAMA_MODEL}"
    assert captured["url"] == DEFAULT_OLLAMA_ENDPOINT
    assert captured["timeout"] == 5.0
    assert captured["body"]["model"] == DEFAULT_OLLAMA_MODEL
    assert captured["body"]["stream"] is False
    assert captured["body"]["keep_alive"] == "10m"
    assert captured["body"]["options"] == {"temperature": 0.1, "num_predict": 180}
    assert "think" not in captured["body"]
    messages = captured["body"]["messages"]
    assert messages[0]["role"] == "system"
    assert "enough context for downstream consumers" in messages[0]["content"]
    assert "avoid padding" in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "Summarize this change"}


def test_ollama_provider_sends_suggest_request_with_thinking_disabled_and_adaptive_caps(monkeypatch):
    _clear_ollama_tuning_env(monkeypatch)
    captured_bodies: list[dict[str, object]] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        def read():
            return json.dumps({"message": {"content": "Local suggestion"}}).encode("utf-8")

    def fake_urlopen(req, timeout):
        captured_bodies.append(json.loads(req.data.decode("utf-8")))
        return FakeResponse()

    monkeypatch.setattr("verlato.providers.ollama.request.urlopen", fake_urlopen)

    provider = OllamaProvider(model="test-model")
    for count in (1, 3, 5):
        response = provider.summarize(
            ProviderRequest(task="suggest", prompt="Suggest wording", evidence={"suggestion_count": count})
        )
        assert response.content == "Local suggestion"

    caps = [body["options"]["num_predict"] for body in captured_bodies]
    assert caps == [
        DEFAULT_OLLAMA_MIN_SUGGEST_NUM_PREDICT,
        180,
        DEFAULT_OLLAMA_MAX_SUGGEST_NUM_PREDICT,
    ]
    assert all(body["think"] is False for body in captured_bodies)
    assert all("enough context for downstream consumers" in body["messages"][0]["content"] for body in captured_bodies)


def test_ollama_provider_disables_thinking_for_conventional_draft(monkeypatch):
    _clear_ollama_tuning_env(monkeypatch)
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        def read():
            return json.dumps({"message": {"content": "{}"}}).encode("utf-8")

    def fake_urlopen(req, timeout):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr("verlato.providers.ollama.request.urlopen", fake_urlopen)

    provider = OllamaProvider(model="test-model")
    provider.summarize(ProviderRequest(task="draft-conventional", prompt="Draft commit"))

    assert captured["body"]["think"] is False


def test_ollama_provider_routes_only_suggestions_to_suggest_model(monkeypatch):
    _clear_ollama_tuning_env(monkeypatch)
    captured_bodies: list[dict[str, object]] = []

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        def read():
            return json.dumps({"message": {"content": "Local response"}}).encode("utf-8")

    def fake_urlopen(req, timeout):
        captured_bodies.append(json.loads(req.data.decode("utf-8")))
        return FakeResponse()

    monkeypatch.setattr("verlato.providers.ollama.request.urlopen", fake_urlopen)

    provider = OllamaProvider(model="base-model", suggest_model="fast-model")
    summary = provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change"))
    suggestion = provider.summarize(ProviderRequest(task="suggest", prompt="Suggest wording"))
    classification = provider.classify(ProviderRequest(task="classify", prompt="Classify"))

    assert [body["model"] for body in captured_bodies] == ["base-model", "fast-model", "base-model"]
    assert summary.model == "ollama:base-model"
    assert suggestion.model == "ollama:fast-model"
    assert suggestion.metadata["base_model"] == "base-model"
    assert suggestion.metadata["suggest_model"] == "fast-model"
    assert suggestion.metadata["effective_model"] == "fast-model"
    assert classification.model == "ollama:base-model"


def test_ollama_provider_allows_suggest_thinking_and_cap_overrides(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        def read():
            return json.dumps({"message": {"content": "Local suggestion"}}).encode("utf-8")

    def fake_urlopen(req, timeout):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setenv("VERLATO_OLLAMA_SUGGEST_THINK", "1")
    monkeypatch.setenv("VERLATO_OLLAMA_SUGGEST_NUM_PREDICT", "72")
    monkeypatch.setattr("verlato.providers.ollama.request.urlopen", fake_urlopen)

    provider = OllamaProvider(model="test-model")
    provider.summarize(ProviderRequest(task="suggest", prompt="Suggest wording", evidence={"suggestion_count": 3}))

    assert captured["body"]["think"] is True
    assert captured["body"]["options"]["num_predict"] == 72


def test_ollama_provider_sends_response_format_for_structured_suggestions(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        def read():
            return json.dumps({"message": {"content": '{"items":[]}'}}).encode("utf-8")

    def fake_urlopen(req, timeout):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr("verlato.providers.ollama.request.urlopen", fake_urlopen)

    provider = OllamaProvider(model="test-model")
    provider.summarize(
        ProviderRequest(
            task="suggest",
            prompt="Suggest wording",
            evidence={"suggestion_count": 1},
            response_format={"type": "object", "required": ["items"]},
        )
    )

    assert captured["body"]["format"] == {"type": "object", "required": ["items"]}


def test_ollama_provider_runs_tool_call_loop(monkeypatch):
    captured_bodies: list[dict[str, object]] = []
    responses = [
        {
            "message": {
                "content": "",
                "tool_calls": [
                    {
                        "function": {
                            "name": "verlato_get_file_patch",
                            "arguments": {"item_id": "ci_1", "path": "src/auth.py"},
                        }
                    }
                ],
            }
        },
        {"message": {"content": "Uses the retrieved auth patch."}},
    ]

    class FakeResponse:
        def __init__(self, payload):
            self.payload = payload

        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        def read(self):
            return json.dumps(self.payload).encode("utf-8")

    def fake_urlopen(req, timeout):
        captured_bodies.append(json.loads(req.data.decode("utf-8")))
        return FakeResponse(responses.pop(0))

    tool_calls: list[tuple[str, dict[str, object]]] = []

    def tool_executor(name: str, arguments: dict[str, object]) -> dict[str, object]:
        tool_calls.append((name, arguments))
        return {"patch": "@@ -1 +1 @@\n-old\n+new"}

    monkeypatch.setattr("verlato.providers.ollama.request.urlopen", fake_urlopen)

    provider = OllamaProvider(model="test-model")
    response = provider.summarize(
        ProviderRequest(
            task="suggest",
            prompt="Use tools before suggesting.",
            tools=[
                {
                    "type": "function",
                    "function": {
                        "name": "verlato_get_file_patch",
                        "description": "Get a patch.",
                        "parameters": {"type": "object", "properties": {}},
                    },
                }
            ],
            tool_executor=tool_executor,
            max_tool_rounds=2,
        )
    )

    assert response.content == "Uses the retrieved auth patch."
    assert response.metadata["tool_rounds"] == 1
    assert response.metadata["tool_call_count"] == 1
    assert response.metadata["tool_names"] == ["verlato_get_file_patch"]
    assert tool_calls == [("verlato_get_file_patch", {"item_id": "ci_1", "path": "src/auth.py"})]
    assert captured_bodies[0]["tools"]
    assert captured_bodies[1]["messages"][-1]["role"] == "tool"
    assert "@@ -1 +1 @@" in captured_bodies[1]["messages"][-1]["content"]


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

    monkeypatch.setattr("verlato.providers.ollama.request.urlopen", fake_urlopen)

    provider = OllamaProvider(model="test-model", stream=True, log_sink=CapturingSink())
    response = provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change"))

    assert captured["body"]["stream"] is True
    assert response.content == "Local summary"
    assert response.reasoning == "Used diffs."
    assert response.metadata == {"eval_count": 2, "done_reason": "stop"}
    bodies = [record.body for record in records]
    stream_records = [json.loads(body.removeprefix("json ")) for body in bodies if body.startswith("json ")]
    assert any(record["event"] == "stream_chunk" and record["attributes"]["kind"] == "reasoning" for record in stream_records)
    assert any(record["event"] == "stream_chunk" and record["attributes"]["kind"] == "content" for record in stream_records)
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

    monkeypatch.delenv("VERLATO_TRACE_MODEL_REASONING", raising=False)
    monkeypatch.setattr("verlato.providers.ollama.request.urlopen", lambda req, timeout: FakeResponse())

    provider = OllamaProvider(model="test-model", stream=True, log_sink=CapturingSink())
    response = provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change"))

    assert response.reasoning == "Private reasoning"
    stream_records = [json.loads(record.body.removeprefix("json ")) for record in records if record.body.startswith("json ")]
    reasoning_records = [
        record
        for record in stream_records
        if record["event"] == "stream_chunk" and record["attributes"]["kind"] == "reasoning"
    ]
    assert reasoning_records
    assert reasoning_records[0]["attributes"]["chars"] == 17
    assert reasoning_records[0]["payload"] == ""


def test_ollama_provider_falls_back_to_generate_when_chat_disconnects(monkeypatch):
    _clear_ollama_tuning_env(monkeypatch)
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

    monkeypatch.setattr("verlato.providers.ollama.request.urlopen", fake_urlopen)

    provider = OllamaProvider()
    response = provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change"))

    assert response.content == "Generated summary"
    assert captured["urls"] == [
        DEFAULT_OLLAMA_ENDPOINT,
        "http://127.0.0.1:11434/api/generate",
    ]
    assert captured["body"]["model"] == DEFAULT_OLLAMA_MODEL
    assert captured["body"]["stream"] is False
    assert captured["body"]["keep_alive"] == "10m"
    assert captured["body"]["options"] == {"temperature": 0.1, "num_predict": 180}
    assert "think" not in captured["body"]
    assert captured["body"]["prompt"] == "Summarize this change"


def test_ollama_provider_fallback_generate_keeps_suggest_tuning(monkeypatch):
    _clear_ollama_tuning_env(monkeypatch)
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        def read():
            return json.dumps({"response": "Generated suggestion"}).encode("utf-8")

    def fake_urlopen(req, timeout):
        if req.full_url.endswith("/api/chat"):
            raise client.RemoteDisconnected("remote end closed connection without response")
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr("verlato.providers.ollama.request.urlopen", fake_urlopen)

    provider = OllamaProvider()
    response = provider.summarize(
        ProviderRequest(task="suggest", prompt="Suggest this change", evidence={"suggestion_count": 1})
    )

    assert response.content == "Generated suggestion"
    assert captured["body"]["think"] is False
    assert captured["body"]["options"] == {"temperature": 0.1, "num_predict": 96}
    assert captured["body"]["prompt"] == "Suggest this change"


def test_ollama_provider_applies_task_specific_generation_caps(monkeypatch):
    _clear_ollama_tuning_env(monkeypatch)
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        def read():
            return json.dumps({"message": {"content": "changed/general/internal/none"}}).encode("utf-8")

    def fake_urlopen(req, timeout):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr("verlato.providers.ollama.request.urlopen", fake_urlopen)

    provider = OllamaProvider(model="test-model", num_ctx=2048)
    response = provider.classify(ProviderRequest(task="classify", prompt="Classify this change"))

    assert response.content == "changed/general/internal/none"
    assert captured["body"]["options"] == {"temperature": 0.1, "num_ctx": 2048, "num_predict": 24}


def test_ollama_provider_env_overrides_generation_options(monkeypatch):
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
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setenv("VERLATO_OLLAMA_KEEP_ALIVE", "30m")
    monkeypatch.setenv("VERLATO_OLLAMA_NUM_CTX", "4096")
    monkeypatch.setenv("VERLATO_OLLAMA_NUM_PREDICT", "64")
    monkeypatch.setattr("verlato.providers.ollama.request.urlopen", fake_urlopen)

    provider = OllamaProvider(model="test-model")
    provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change"))

    assert captured["body"]["keep_alive"] == "30m"
    assert captured["body"]["options"] == {"temperature": 0.1, "num_ctx": 4096, "num_predict": 64}


def test_ollama_provider_reports_unavailable_server(monkeypatch):
    def fake_urlopen(req, timeout):
        raise error.URLError("connection refused")

    monkeypatch.setattr("verlato.providers.ollama.request.urlopen", fake_urlopen)

    provider = OllamaProvider()
    response = provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change"))

    assert response.confidence == 0.0
    assert response.content.startswith("[ollama-unavailable]")


def test_ollama_provider_traces_model_boundary(monkeypatch, capsys):
    monkeypatch.setenv("VERLATO_TRACE_MODEL_CALLS", "1")

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        def read():
            return json.dumps({"message": {"content": "Local summary"}}).encode("utf-8")

    monkeypatch.setattr("verlato.providers.ollama.request.urlopen", lambda req, timeout: FakeResponse())

    provider = OllamaProvider(model="test-model")
    provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change", evidence={"files": []}))

    stderr = capsys.readouterr().err
    assert "[verlato:model] provider=ollama task=summarize model=test-model" in stderr
    assert "prompt_chars=21 evidence_keys=1" in stderr
    assert "timeout_seconds=15" in stderr
    assert "[verlato:model] post provider=ollama task=summarize" in stderr
    assert "[verlato:model] response provider=ollama task=summarize content_chars=13 empty=False" in stderr


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

    monkeypatch.setattr("verlato.providers.ollama.request.urlopen", lambda req, timeout: FakeResponse())

    provider = OllamaProvider(model="test-model", log_sink=CapturingSink())
    response = provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change"))

    assert response.metadata == {"prompt_eval_count": 7, "eval_count": 3}
    assert records
    assert records[0].event_name == "verlato.model.ollama.log"
    assert records[0].to_otel_dict()["Body"].startswith("provider=ollama")
    assert records[0].to_otel_dict()["Attributes"] == {"provider": "ollama", "model": "test-model"}


def test_ollama_provider_can_persist_jsonl_model_logs(monkeypatch, tmp_path):
    monkeypatch.delenv("VERLATO_TRACE_MODEL_CALLS", raising=False)
    monkeypatch.setenv("VERLATO_MODEL_LOG_PATH", str(tmp_path / "model.jsonl"))

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"message":{"content":"Model summary"},"prompt_eval_count":7}'

    monkeypatch.setattr("verlato.providers.ollama.request.urlopen", lambda req, timeout: FakeResponse())

    provider = OllamaProvider(model="test-model")
    provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change"))

    records = [json.loads(line) for line in (tmp_path / "model.jsonl").read_text().splitlines()]
    assert records
    assert records[0]["EventName"] == "verlato.model.ollama.log"
    assert records[0]["Attributes"] == {"provider": "ollama", "model": "test-model"}


def test_ollama_provider_preserves_model_reasoning(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return b'{"message":{"content":"Model summary","thinking":"Used auth hunk as primary evidence."}}'

    monkeypatch.setattr("verlato.providers.ollama.request.urlopen", lambda req, timeout: FakeResponse())

    provider = OllamaProvider(model="test-model")
    response = provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change"))

    assert response.content == "Model summary"
    assert response.reasoning == "Used auth hunk as primary evidence."
