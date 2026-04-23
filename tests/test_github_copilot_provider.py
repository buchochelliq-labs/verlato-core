from releasepilot.providers.base import ProviderRequest
from releasepilot.providers.github_copilot import GitHubCopilotProvider


def test_github_copilot_provider_handles_missing_token(monkeypatch):
    monkeypatch.delenv("GITHUB_TOKEN", raising=False)
    monkeypatch.delenv("GH_TOKEN", raising=False)

    provider = GitHubCopilotProvider(token=None)
    response = provider.summarize(ProviderRequest(task="summarize", prompt="Summarize this change"))

    assert "missing GITHUB_TOKEN/GH_TOKEN" in response.content
    assert response.confidence == 0.0
    assert response.model.startswith("github-copilot:")
