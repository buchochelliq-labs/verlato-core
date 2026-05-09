import io
import json
from urllib import error

from verlato.providers.base import ProviderRequest
from verlato.providers.github_copilot import (
    DEFAULT_GITHUB_MODELS_ENDPOINT,
    DEFAULT_GITHUB_MODELS_MODEL,
    GITHUB_MODELS_API_VERSION,
    GitHubCopilotProvider,
)
from verlato.providers.model_logging import ModelLogRecord, ModelLogSink


def test_github_copilot_provider_uses_github_models_defaults(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.delenv("VERLATO_GITHUB_MODEL", raising=False)
    monkeypatch.delenv("VERLATO_GITHUB_MODELS_ENDPOINT", raising=False)
    monkeypatch.setattr(GitHubCopilotProvider, "_load_gh_cli_token", staticmethod(lambda: None))

    provider = GitHubCopilotProvider(token=None)

    assert provider.endpoint == DEFAULT_GITHUB_MODELS_ENDPOINT
    assert provider.model == DEFAULT_GITHUB_MODELS_MODEL


def test_github_copilot_provider_normalizes_legacy_openai_model_name(monkeypatch):
    monkeypatch.setattr(GitHubCopilotProvider, "_load_gh_cli_token", staticmethod(lambda: None))

    provider = GitHubCopilotProvider(token="token", model="gpt-4o-mini")

    assert provider.model == "openai/gpt-4o-mini"


def test_github_copilot_provider_preserves_provider_qualified_model_name(monkeypatch):
    monkeypatch.setattr(GitHubCopilotProvider, "_load_gh_cli_token", staticmethod(lambda: None))

    provider = GitHubCopilotProvider(token="token", model="meta/Llama-3.1-405B-Instruct")

    assert provider.model == "meta/Llama-3.1-405B-Instruct"


def test_github_copilot_provider_handles_missing_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(GitHubCopilotProvider, "_load_gh_cli_token", staticmethod(lambda: None))

    provider = GitHubCopilotProvider(token=None)
    response = provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change"))

    assert "missing GITHUB_TOKEN/GH_TOKEN" in response.content
    assert response.confidence == 0.0
    assert response.model.startswith("github-copilot:")


def test_github_copilot_provider_uses_logged_in_gh_user(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)
    monkeypatch.setattr(GitHubCopilotProvider, "_load_gh_cli_token", staticmethod(lambda: "gh-user-token"))

    provider = GitHubCopilotProvider(token=None)

    assert provider.token == "gh-user-token"


def test_github_copilot_provider_sends_github_models_headers(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        def read():
            return json.dumps({"choices": [{"message": {"content": "Generated summary"}}]}).encode("utf-8")

    def fake_urlopen(req, timeout):
        captured["url"] = req.full_url
        captured["timeout"] = timeout
        captured["headers"] = dict(req.header_items())
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr("verlato.providers.github_copilot.request.urlopen", fake_urlopen)

    provider = GitHubCopilotProvider(token="token", model="gpt-4o-mini", timeout_seconds=3.0)
    response = provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change"))

    assert response.content == "Generated summary"
    assert captured["url"] == DEFAULT_GITHUB_MODELS_ENDPOINT
    assert captured["timeout"] == 3.0
    assert captured["body"]["model"] == "openai/gpt-4o-mini"
    messages = captured["body"]["messages"]
    assert messages[0]["role"] == "system"
    assert "enough context for downstream consumers" in messages[0]["content"]
    assert "avoid padding" in messages[0]["content"]
    assert messages[1] == {"role": "user", "content": "Summarize this change"}
    headers = captured["headers"]
    assert headers["Accept"] == "application/vnd.github+json"
    assert headers["Authorization"] == "Bearer token"
    assert headers["Content-type"] == "application/json"
    assert headers["X-github-api-version"] == GITHUB_MODELS_API_VERSION


def test_github_copilot_provider_preserves_draft_conventional_task(monkeypatch):
    captured: dict[str, object] = {}

    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, exc_type, exc, tb):
            return False

        @staticmethod
        def read():
            return json.dumps({"choices": [{"message": {"content": "{}"}}]}).encode("utf-8")

    def fake_urlopen(req, timeout):
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return FakeResponse()

    monkeypatch.setattr("verlato.providers.github_copilot.request.urlopen", fake_urlopen)

    provider = GitHubCopilotProvider(token="token")
    provider.summarize(ProviderRequest(task="draft-conventional", prompt="Draft it"))

    system = captured["body"]["messages"][0]["content"]
    assert "Infer or draft a conventional commit" in system


def test_github_copilot_provider_reports_auth_http_errors(monkeypatch):
    def fake_urlopen(req, timeout):
        raise error.HTTPError(
            req.full_url,
            403,
            "Forbidden",
            {},
            io.BytesIO(json.dumps({"message": "Resource not accessible by personal access token"}).encode("utf-8")),
        )

    monkeypatch.setattr("verlato.providers.github_copilot.request.urlopen", fake_urlopen)

    provider = GitHubCopilotProvider(token="token")
    response = provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change"))

    assert response.confidence == 0.0
    assert "check that the token has access to GitHub Models" in response.content
    assert "Resource not accessible by personal access token" in response.content


def test_github_copilot_provider_reports_rate_limit_http_errors(monkeypatch):
    def fake_urlopen(req, timeout):
        raise error.HTTPError(req.full_url, 429, "Too Many Requests", {}, io.BytesIO(b""))

    monkeypatch.setattr("verlato.providers.github_copilot.request.urlopen", fake_urlopen)

    provider = GitHubCopilotProvider(token="token")
    response = provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change"))

    assert response.confidence == 0.0
    assert "GitHub Models rate limit exceeded" in response.content


def test_github_copilot_provider_accepts_pluggable_model_log_sink(monkeypatch):
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
            return (
                b'{"id":"chatcmpl_1","choices":[{"message":{"content":"Model summary"}}],'
                b'"usage":{"prompt_tokens":11,"completion_tokens":4,"total_tokens":15}}'
            )

    monkeypatch.setattr("verlato.providers.github_copilot.request.urlopen", lambda req, timeout: FakeResponse())

    provider = GitHubCopilotProvider(token="token", model="gpt-4o-mini", log_sink=CapturingSink())
    response = provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change"))

    assert response.metadata == {
        "prompt_tokens": 11,
        "completion_tokens": 4,
        "total_tokens": 15,
        "response_id": "chatcmpl_1",
    }
    assert records
    record = records[0].to_otel_dict()
    assert record["EventName"] == "verlato.model.github_copilot"
    assert record["Attributes"] == {"provider": "github-copilot", "model": "openai/gpt-4o-mini"}


def test_github_copilot_provider_preserves_model_reasoning(monkeypatch):
    class FakeResponse:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def read(self):
            return (
                b'{"choices":[{"message":{"content":"Model summary",'
                b'"reasoning_content":"Focused on the changed API behavior."}}]}'
            )

    monkeypatch.setattr("verlato.providers.github_copilot.request.urlopen", lambda req, timeout: FakeResponse())

    provider = GitHubCopilotProvider(token="token", model="gpt-4o-mini")
    response = provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change"))

    assert response.content == "Model summary"
    assert response.reasoning == "Focused on the changed API behavior."
