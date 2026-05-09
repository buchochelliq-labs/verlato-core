from verlato.cli.main import run_scan
from verlato.core.models import ChangeItem, Classification, Commit, Content, Evidence, PullRequest, SourceRef
from verlato.mcp.service import MCPService, ReleasePreviewRequest
from verlato.providers.base import ProviderRequest
from verlato.providers.mock import MockProvider
from verlato.providers.stub import StubProvider
from verlato.state.store import StateData, StateStore


def _item(item_id: str, title: str, base_ref: str = "v1.0.0", head_ref: str = "HEAD") -> ChangeItem:
    return ChangeItem(
        id=item_id,
        source=SourceRef(pr_number=1, commit_shas=[item_id], base_ref=base_ref, head_ref=head_ref),
        classification=Classification(kind="changed", area="general", audience="external", semver="patch"),
        content=Content(title=title, summary=f"Summary for {title}"),
        evidence=Evidence(),
        confidence=0.8,
        checksum=f"sum-{item_id}",
    )


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
    monkeypatch.setattr(
        "verlato.mcp.service.run_scan",
        lambda base, head, persist, **_kwargs: [{"id": f"{base}->{head}"}],
    )
    monkeypatch.setattr(
        "verlato.mcp.service.build_draft_document",
        lambda **kwargs: type(
            "_DraftDocument",
            (),
            {
                "rendered": {"github-release": f"## {kwargs['version']}\n\n- {kwargs['base']}->{kwargs['head']}\n"},
                "version": kwargs["version"],
            },
        )(),
    )

    svc = MCPService()
    changes = svc.list_unpublished_changes("v1.0.0", "HEAD")
    assert changes[0]["id"] == "v1.0.0->HEAD"

    preview = svc.preview_release_notes(ReleasePreviewRequest(base="v1.0.0", head="HEAD", version="v1.1.0"))
    assert "## v1.1.0" in preview
    assert "v1.0.0->HEAD" in preview

    recon = svc.reconcile_preview("v1.0.0", "HEAD")
    assert recon["count"] == 1


def test_mcp_preview_uses_requested_range_and_excludes_unrelated_state(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    StateStore(path=str(state_path)).save(StateData(items=[_item("ci_old", "Old persisted release")]))

    monkeypatch.setenv("VERLATO_STATE_PATH", str(state_path))
    monkeypatch.setenv("VERLATO_PR_ADAPTER", "none")
    monkeypatch.setenv("VERLATO_MODEL_PROVIDER", "stub")
    monkeypatch.setattr(
        "verlato.cli.main.Collector.collect_commits",
        lambda self, base, head: [Commit(sha="a1", message="feat: add previewed change")],
    )
    monkeypatch.setattr(
        "verlato.cli.main.Collector.map_commits_to_prs",
        lambda self, commits: {
            "a1": PullRequest(number=7, title="Previewed change", files=[]),
        },
    )

    preview = MCPService().preview_release_notes(
        ReleasePreviewRequest(base="v2.0.0", head="HEAD", version="v2.1.0")
    )

    assert "Previewed change" in preview
    assert "Old persisted release" not in preview


def test_run_scan_uses_selected_provider(monkeypatch, tmp_path):
    monkeypatch.setenv("VERLATO_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("VERLATO_PR_ADAPTER", "none")
    monkeypatch.setattr(
        "verlato.cli.main.Collector.collect_commits",
        lambda self, base, head: [Commit(sha="a1", message="update service internals")],
    )
    monkeypatch.setattr("verlato.cli.main.Collector.map_commits_to_prs", lambda self, commits: {})

    monkeypatch.setenv("VERLATO_MODEL_PROVIDER", "stub")
    stub_items = run_scan("v1.0.0", "HEAD", persist=False)

    monkeypatch.setenv("VERLATO_MODEL_PROVIDER", "mock")
    mock_items = run_scan("v1.0.0", "HEAD", persist=False)

    assert stub_items[0]["content"]["summary"] == "No changed-file evidence was available from PR or commit metadata"
    assert mock_items[0]["content"]["summary"].startswith("Updates release scope from commit evidence:")


def test_run_scan_accepts_explicit_provider_override(monkeypatch, tmp_path):
    monkeypatch.setenv("VERLATO_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("VERLATO_PR_ADAPTER", "none")
    monkeypatch.setenv("VERLATO_MODEL_PROVIDER", "stub")
    monkeypatch.setattr(
        "verlato.cli.main.Collector.collect_commits",
        lambda self, base, head: [Commit(sha="a1", message="update service internals")],
    )
    monkeypatch.setattr("verlato.cli.main.Collector.map_commits_to_prs", lambda self, commits: {})

    mock_items = run_scan("v1.0.0", "HEAD", persist=False, model_provider="mock")

    assert mock_items[0]["content"]["summary"].startswith("Updates release scope from commit evidence:")
    assert any(
        signal.startswith("provider-summary:")
        for signal in mock_items[0]["evidence"]["commit_signals"]
    )


def test_mcp_service_can_update_review_state(monkeypatch, tmp_path):
    state_path = tmp_path / "state.json"
    monkeypatch.setenv("VERLATO_STATE_PATH", str(state_path))
    monkeypatch.setenv("VERLATO_PR_ADAPTER", "none")
    monkeypatch.setenv("VERLATO_MODEL_PROVIDER", "stub")
    monkeypatch.setattr(
        "verlato.cli.main._detect_review_candidates",
        lambda base, head, model_provider, **_kwargs: [_item("ci_review", "Review me", base_ref=base, head_ref=head)],
    )
    monkeypatch.setattr(
        "verlato.cli.main.detect_change_items",
        lambda base, head, **_kwargs: [_item("ci_review", "Review me", base_ref=base, head_ref=head)],
    )

    item = MCPService().update_review_state(
        base="v1.0.0",
        head="HEAD",
        item_id="ci_review",
        status="hidden",
        summary="Hidden by reviewer",
        reviewer="nick",
    )

    assert item["status"] == "hidden"
    assert item["summary"] == "Hidden by reviewer"


def test_mcp_service_info_includes_mcp_tools(monkeypatch, tmp_path):
    monkeypatch.setenv("VERLATO_STATE_PATH", str(tmp_path / "state.json"))

    info = MCPService().info()

    assert "mcp_tools" in info
    assert "verlato_preview_release_notes" in info["mcp_tools"]
    assert "mcp_resources" in info
    assert "verlato://overview" in info["mcp_resources"]
    assert "mcp_prompts" in info
    assert "verlato_review_request" in info["mcp_prompts"]
