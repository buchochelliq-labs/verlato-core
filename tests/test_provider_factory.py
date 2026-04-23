from releasepilot.providers.factory import build_provider
from releasepilot.providers.github_copilot import GitHubCopilotProvider
from releasepilot.providers.mock import MockProvider
from releasepilot.providers.stub import StubProvider


def test_provider_factory_defaults_to_stub(monkeypatch):
    monkeypatch.delenv("RELEASEPILOT_MODEL_PROVIDER", raising=False)
    assert isinstance(build_provider(), StubProvider)


def test_provider_factory_builds_mock(monkeypatch):
    monkeypatch.setenv("RELEASEPILOT_MODEL_PROVIDER", "mock")
    assert isinstance(build_provider(), MockProvider)


def test_provider_factory_builds_github_copilot(monkeypatch):
    monkeypatch.setenv("RELEASEPILOT_MODEL_PROVIDER", "github-copilot")
    assert isinstance(build_provider(), GitHubCopilotProvider)
