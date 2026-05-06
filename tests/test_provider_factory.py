from verlato.config import ProviderConfig
from verlato.providers.factory import build_provider
from verlato.providers.github_copilot import GitHubCopilotProvider
from verlato.providers.mock import MockProvider
from verlato.providers.ollama import OllamaProvider
from verlato.providers.stub import StubProvider


def test_provider_factory_defaults_to_ollama(monkeypatch):
    monkeypatch.delenv("VERLATO_MODEL_PROVIDER", raising=False)
    assert isinstance(build_provider(), OllamaProvider)


def test_provider_factory_builds_mock(monkeypatch):
    monkeypatch.setenv("VERLATO_MODEL_PROVIDER", "mock")
    assert isinstance(build_provider(), MockProvider)


def test_provider_factory_builds_github_copilot(monkeypatch):
    monkeypatch.setenv("VERLATO_MODEL_PROVIDER", "github-copilot")
    assert isinstance(build_provider(), GitHubCopilotProvider)


def test_provider_factory_builds_ollama(monkeypatch):
    monkeypatch.setenv("VERLATO_MODEL_PROVIDER", "ollama")
    assert isinstance(build_provider(), OllamaProvider)


def test_provider_factory_passes_ollama_timeout(monkeypatch):
    monkeypatch.setenv("VERLATO_MODEL_PROVIDER", "ollama")
    provider = build_provider(ProviderConfig(timeout_seconds=44))
    assert isinstance(provider, OllamaProvider)
    assert provider.timeout_seconds == 44


def test_provider_factory_timeout_env_overrides_config(monkeypatch):
    monkeypatch.setenv("VERLATO_MODEL_PROVIDER", "ollama")
    monkeypatch.setenv("VERLATO_OLLAMA_TIMEOUT_SECONDS", "55")
    provider = build_provider(ProviderConfig(timeout_seconds=44))
    assert isinstance(provider, OllamaProvider)
    assert provider.timeout_seconds == 55


def test_provider_factory_passes_ollama_tuning(monkeypatch):
    monkeypatch.setenv("VERLATO_MODEL_PROVIDER", "ollama")
    provider = build_provider(
        ProviderConfig(
            keep_alive="20m",
            num_ctx=4096,
            summary_num_predict=96,
            classify_num_predict=12,
        )
    )

    assert isinstance(provider, OllamaProvider)
    assert provider.keep_alive == "20m"
    assert provider.num_ctx == 4096
    assert provider.summary_num_predict == 96
    assert provider.classify_num_predict == 12


def test_provider_factory_ollama_tuning_env_overrides_config(monkeypatch):
    monkeypatch.setenv("VERLATO_MODEL_PROVIDER", "ollama")
    monkeypatch.setenv("VERLATO_OLLAMA_KEEP_ALIVE", "45m")
    monkeypatch.setenv("VERLATO_OLLAMA_NUM_PREDICT", "64")
    provider = build_provider(ProviderConfig(keep_alive="20m", summary_num_predict=96))

    assert isinstance(provider, OllamaProvider)
    assert provider.keep_alive == "45m"
    assert provider.num_predict == 64


def test_provider_factory_uses_config_default(monkeypatch):
    monkeypatch.delenv("VERLATO_MODEL_PROVIDER", raising=False)
    provider = build_provider(ProviderConfig(name="mock"))
    assert isinstance(provider, MockProvider)


def test_provider_factory_env_overrides_config(monkeypatch):
    monkeypatch.setenv("VERLATO_MODEL_PROVIDER", "mock")
    provider = build_provider(ProviderConfig(name="github-copilot"))
    assert isinstance(provider, MockProvider)


def test_provider_factory_explicit_mode_overrides_env(monkeypatch):
    monkeypatch.setenv("VERLATO_MODEL_PROVIDER", "stub")
    provider = build_provider(ProviderConfig(name="stub"), mode="mock")
    assert isinstance(provider, MockProvider)
