from releasepilot.mcp.service import MCPService, ReleasePreviewRequest
from releasepilot.providers.base import ProviderRequest
from releasepilot.providers.mock import MockProvider
from releasepilot.providers.stub import StubProvider


def test_mock_provider_is_deterministic():
    provider = MockProvider()
    req = ProviderRequest(task="summarize", prompt="Important API changes")

    r1 = provider.summarize(req)
    r2 = provider.summarize(req)
    assert r1.content == r2.content
    assert r1.confidence == r2.confidence


def test_stub_provider_contracts():
    provider = StubProvider()
    req = ProviderRequest(task="classify", prompt="fix bug")
    summary = provider.summarize(req)
    classified = provider.classify(req)
    assert summary.model == "stub"
    assert classified.model == "stub"


def test_mcp_service_read_only_endpoints(monkeypatch):
    monkeypatch.setattr("releasepilot.mcp.service.run_scan", lambda base, head, persist: [{"id": f"{base}->{head}"}])
    monkeypatch.setattr(
        "releasepilot.mcp.service._build_store",
        lambda: type("_Store", (), {"load": lambda self: type("_State", (), {"items": []})()})(),
    )

    svc = MCPService()
    changes = svc.list_unpublished_changes("v1.0.0", "HEAD")
    assert changes[0]["id"] == "v1.0.0->HEAD"

    preview = svc.preview_release_notes(ReleasePreviewRequest(base="v1.0.0", head="HEAD", version="v1.1.0"))
    assert "## v1.1.0" in preview

    recon = svc.reconcile_preview("v1.0.0", "HEAD")
    assert recon["count"] == 1
