from __future__ import annotations

import json
from pathlib import Path

from releasepilot.cli.main import main
from releasepilot.config import load_config
from releasepilot.core.classifier import Classifier
from releasepilot.core.drafts import normalize_remote_url
from releasepilot.core.models import (
    ChangeGroup,
    ChangedFile,
    ChangeItem,
    Classification,
    Commit,
    Content,
    Evidence,
    PullRequest,
    SourceRef,
)
from releasepilot.providers.base import ProviderRequest, ProviderResponse
from releasepilot.providers.stub import StubProvider


def _mk_item(
    item_id: str,
    *,
    kind: str = "changed",
    title: str = "Improve login",
    summary: str = "Improves login flows",
    base_ref: str = "v1.0.0",
    head_ref: str = "HEAD",
) -> ChangeItem:
    return ChangeItem(
        id=item_id,
        source=SourceRef(pr_number=12, commit_shas=["a1"], base_ref=base_ref, head_ref=head_ref),
        classification=Classification(kind=kind, area="general", audience="external", semver="patch"),
        content=Content(title=title, summary=summary),
        evidence=Evidence(files=["src/auth.py"], commit_signals=["diff-fallback"]),
        confidence=0.72,
        checksum=f"sum-{item_id}",
    )


def _mk_group(
    *,
    number: int,
    title: str,
    commit_sha: str,
    commit_message: str,
    path: str = "src/auth.py",
    patch: str = "+ change",
) -> ChangeGroup:
    return ChangeGroup(
        pr=PullRequest(
            number=number,
            title=title,
            files=[ChangedFile(path=path, patch=patch)],
        ),
        commits=[Commit(sha=commit_sha, message=commit_message)],
    )


def test_load_config_defaults_without_file(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.state_path == ".releasepilot_state.json"
    assert config.targets["changelog"].path == "CHANGELOG.md"
    assert config.targets["github-release"].path == ".github/release.md"


def test_load_config_reads_custom_targets(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "releasepilot.json"
    config_path.write_text(
        json.dumps(
            {
                "state_path": ".custom-state.json",
                "targets": {
                    "changelog": {"path": "docs/CHANGELOG.md", "format": "changelog"},
                    "github-release": {"path": ".github/release-notes.md", "format": "github-release"},
                },
            }
        )
    )
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.state_path == ".custom-state.json"
    assert config.targets["changelog"].path == "docs/CHANGELOG.md"
    assert config.targets["github-release"].path == ".github/release-notes.md"


def test_load_config_reads_provider_defaults(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "releasepilot.json"
    config_path.write_text(
        json.dumps(
            {
                "provider": {
                    "name": "ollama",
                    "model": "llama3.2:3b",
                    "endpoint": "http://127.0.0.1:11434/api/chat",
                }
            }
        )
    )
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.provider.name == "ollama"
    assert config.provider.model == "llama3.2:3b"
    assert config.provider.endpoint == "http://127.0.0.1:11434/api/chat"


def test_load_config_reads_provider_fanout_limit(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "releasepilot.json"
    config_path.write_text(json.dumps({"provider": {"max_items_per_scan": 7}}))
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.provider.max_items_per_scan == 7


def test_load_config_reads_suggestion_count(monkeypatch, tmp_path: Path):
    config_path = tmp_path / "releasepilot.json"
    config_path.write_text(json.dumps({"suggestion_count": 4}))
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.suggestion_count == 4


def test_draft_json_command_outputs_sections_and_targets(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "releasepilot.cli.main.detect_change_items",
        lambda base, head: [_mk_item("ci_1", base_ref=base, head_ref=head, kind="added")],
    )

    rc = main(["draft", "json", "--base", "v1.0.0", "--head", "HEAD", "--version", "v1.1.0"])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["version"] == "v1.1.0"
    assert "repository_url" in out
    assert out["base_ref"] == "v1.0.0"
    assert out["targets"][0]["name"] == "changelog"
    assert out["sections"][0]["kind"] == "added"
    item = out["sections"][0]["items"][0]
    assert item["commit_shas"] == ["a1"]
    assert item["summary_source"] == "Deterministic summary generated from changed-file evidence"
    assert item["classification_source"] == "Deterministic classification from changed-file evidence"
    assert item["evidence_summary"] == "Changed files: src/auth.py"


def test_draft_preview_command_renders_github_release(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setattr(
        "releasepilot.cli.main.detect_change_items",
        lambda base, head: [_mk_item("ci_1", base_ref=base, head_ref=head, kind="fixed")],
    )

    rc = main(["draft", "preview", "--base", "v1.0.0", "--version", "v1.0.1"])

    assert rc == 0
    out = capsys.readouterr().out
    assert "## v1.0.1" in out
    assert "### Fixed" in out


def test_draft_apply_updates_changelog_target(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    (tmp_path / "CHANGELOG.md").write_text("# Changelog\n\nExisting notes\n")
    monkeypatch.setattr(
        "releasepilot.cli.main.detect_change_items",
        lambda base, head: [_mk_item("ci_1", base_ref=base, head_ref=head)],
    )

    rc = main(["draft", "apply", "--base", "v1.0.0", "--version", "v1.1.0", "--target", "changelog"])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["target"] == "changelog"
    updated = (tmp_path / "CHANGELOG.md").read_text()
    assert updated.startswith("# Changelog\n\n### v1.1.0")
    assert "Existing notes" in updated


def test_draft_apply_uses_custom_file_for_github_release(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    custom_file = tmp_path / "notes" / "release.md"
    monkeypatch.setattr(
        "releasepilot.cli.main.detect_change_items",
        lambda base, head: [_mk_item("ci_1", base_ref=base, head_ref=head)],
    )

    rc = main(
        [
            "draft",
            "apply",
            "--base",
            "v1.0.0",
            "--version",
            "v1.1.0",
            "--target",
            "github-release",
            "--file",
            str(custom_file),
        ]
    )

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["format"] == "github-release"
    assert custom_file.read_text().startswith("## v1.1.0")


def test_draft_review_persists_and_overlays_review_state(monkeypatch, tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(state_path))
    monkeypatch.setattr(
        "releasepilot.cli.main.detect_change_items",
        lambda base, head: [_mk_item("ci_1", base_ref=base, head_ref=head)],
    )

    rc = main(
        [
            "draft",
            "review",
            "--base",
            "v1.0.0",
            "--id",
            "ci_1",
            "--status",
            "hidden",
            "--summary",
            "Edited hidden summary",
            "--reviewer",
            "nick",
        ]
    )

    assert rc == 0
    reviewed = json.loads(capsys.readouterr().out)
    assert reviewed["status"] == "hidden"
    assert reviewed["edited_summary"] == "Edited hidden summary"
    assert reviewed["hidden"] is True

    rc = main(["draft", "json", "--base", "v1.0.0", "--version", "v1.1.0"])
    assert rc == 0
    document = json.loads(capsys.readouterr().out)
    assert document["sections"][0]["items"][0]["status"] == "hidden"
    assert document["sections"][0]["items"][0]["summary"] == "Edited hidden summary"
    assert document["sections"][0]["items"][0]["original_summary"] == "Improves login flows"
    assert document["sections"][0]["items"][0]["summary_source"].startswith("Human edited summary")
    assert "Improve login" not in document["rendered"]["github-release"]


def test_draft_suggest_persists_model_summary(monkeypatch, tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(state_path))
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]

    class MockSummaryProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="Model-written summary", confidence=0.9, model="mock-local")

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="[mock-classification] changed/general/external/patch", confidence=0.8, model="mock-local")

    monkeypatch.setattr("releasepilot.cli.main._collect_groups", lambda base, head: [group])

    def fake_build_provider(config, mode=None):
        if mode == "mock":
            return MockSummaryProvider()
        return StubProvider()

    monkeypatch.setattr("releasepilot.cli.main.build_provider", fake_build_provider)

    rc = main(["draft", "suggest", "--base", "v1.0.0", "--id", target_item.id, "--model-provider", "mock"])

    assert rc == 0
    suggested = json.loads(capsys.readouterr().out)
    assert suggested["edited_summary"] == "Model-written summary"
    assert suggested["status"] == "edited"

    rc = main(["draft", "json", "--base", "v1.0.0"])
    assert rc == 0
    document = json.loads(capsys.readouterr().out)
    item = document["sections"][0]["items"][0]
    assert item["summary"] == "Model-written summary"
    assert item["original_summary"] != "Model-written summary"
    assert item["summary_source"] == "Human edited summary, originally generated by LLM (mock-local)"


def test_draft_suggest_preview_returns_variants_without_persisting(monkeypatch, tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(state_path))
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]

    class VariantProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            assert "Return exactly 3 distinct options as a numbered list" in request.prompt
            return ProviderResponse(
                content="1. Make login guidance easier to follow\n2. Clarify the login flow\n3. Improve login recovery wording",
                confidence=0.9,
                model="mock-local",
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="mock-local")

    monkeypatch.setattr("releasepilot.cli.main._collect_groups", lambda base, head: [group])
    monkeypatch.setattr("releasepilot.cli.main.build_provider", lambda config, mode=None: VariantProvider())

    rc = main(["draft", "suggest", "--base", "v1.0.0", "--id", target_item.id, "--preview", "--model-provider", "mock"])

    assert rc == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["count"] == 3
    assert preview["items"][0]["item_id"] == target_item.id
    assert preview["items"][0]["suggestions"] == [
        "Make login guidance easier to follow",
        "Clarify the login flow",
        "Improve login recovery wording",
    ]

    rc = main(["state", "show"])
    assert rc == 0
    state = json.loads(capsys.readouterr().out)
    assert state["items"] == []


def test_draft_suggest_batch_preview_excludes_hidden_items(monkeypatch, tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(state_path))
    groups = [
        _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow"),
        _mk_group(number=2, title="Tune dashboard", commit_sha="b2", commit_message="update dashboard"),
    ]
    first = Classifier().classify(groups[0], "v1.0.0", "HEAD")[0]
    second = Classifier().classify(groups[1], "v1.0.0", "HEAD")[0]

    class VariantProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="1. Suggested wording", confidence=0.9, model="mock-local")

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="", confidence=0.0, model="mock-local")

    monkeypatch.setattr("releasepilot.cli.main._collect_groups", lambda base, head: groups)
    monkeypatch.setattr("releasepilot.cli.main.build_provider", lambda config, mode=None: VariantProvider())

    rc = main(["draft", "review", "--base", "v1.0.0", "--id", second.id, "--status", "hidden"])
    assert rc == 0
    capsys.readouterr()

    rc = main(["draft", "suggest", "--base", "v1.0.0", "--all-visible", "--preview", "--count", "2", "--model-provider", "mock"])

    assert rc == 0
    preview = json.loads(capsys.readouterr().out)
    assert [item["item_id"] for item in preview["items"]] == [first.id]


def test_draft_suggest_defaults_to_ollama_when_no_provider_configured(monkeypatch, tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(state_path))
    monkeypatch.delenv("RELEASEPILOT_MODEL_PROVIDER", raising=False)
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]

    class LocalSummaryProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="Local model summary", confidence=0.9, model="ollama:llama3.2:3b")

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="[ollama-error] classify should not run", confidence=0.0, model="ollama:llama3.2:3b")

    def fake_build_provider(config, mode=None):
        assert mode == "ollama"
        return LocalSummaryProvider()

    monkeypatch.setattr("releasepilot.cli.main._collect_groups", lambda base, head: [group])
    monkeypatch.setattr("releasepilot.cli.main.build_provider", fake_build_provider)

    rc = main(["draft", "suggest", "--base", "v1.0.0", "--id", target_item.id])

    assert rc == 0
    suggested = json.loads(capsys.readouterr().out)
    assert suggested["edited_summary"] == "Local model summary"


def test_draft_suggest_surfaces_provider_errors(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(tmp_path / "state.json"))
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]

    class ErrorProvider:
        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(
                content="[github-copilot-unavailable] missing GITHUB_TOKEN/GH_TOKEN",
                confidence=0.0,
                model="github-copilot:gpt-4o-mini",
            )

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="[github-copilot-error] classify should not run", confidence=0.0, model="github-copilot:gpt-4o-mini")

    monkeypatch.setattr("releasepilot.cli.main._collect_groups", lambda base, head: [group])
    monkeypatch.setattr("releasepilot.cli.main.build_provider", lambda config, mode=None: ErrorProvider())

    try:
        main(["draft", "suggest", "--base", "v1.0.0", "--id", target_item.id, "--model-provider", "github-copilot"])
    except SystemExit as exc:
        assert "missing GITHUB_TOKEN/GH_TOKEN" in str(exc)
    else:
        raise AssertionError("Expected draft suggest to fail with the provider error")


def test_draft_suggest_only_requests_summary_for_selected_item(monkeypatch, tmp_path: Path, capsys):
    class CountingProvider:
        def __init__(self):
            self.summarize_calls = 0
            self.classify_calls = 0

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            self.summarize_calls += 1
            return ProviderResponse(content="Local targeted summary", confidence=0.9, model="ollama:llama3.2:3b")

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            self.classify_calls += 1
            return ProviderResponse(content="[ollama-error] classify should not be called", confidence=0.0, model="ollama:llama3.2:3b")

    provider = CountingProvider()
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(tmp_path / "state.json"))

    groups = [
        ChangeGroup(
            pr=PullRequest(
                number=1,
                title="Add login hints",
                files=[ChangedFile(path="src/auth.py", patch="+ help text")],
            ),
            commits=[Commit(sha="a1", message="update login flow")],
        ),
        ChangeGroup(
            pr=PullRequest(
                number=2,
                title="Tune dashboard spacing",
                files=[ChangedFile(path="src/ui.ts", patch="+ spacing")],
            ),
            commits=[Commit(sha="b2", message="update dashboard layout")],
        ),
    ]
    target_item = Classifier().classify(groups[0], "v1.0.0", "HEAD")[0]

    monkeypatch.setattr("releasepilot.cli.main._collect_groups", lambda base, head: groups)
    monkeypatch.setattr("releasepilot.cli.main.build_provider", lambda config, mode=None: provider)

    rc = main(["draft", "suggest", "--base", "v1.0.0", "--id", target_item.id])

    assert rc == 0
    suggested = json.loads(capsys.readouterr().out)
    assert suggested["edited_summary"] == "Local targeted summary"
    assert provider.summarize_calls == 1
    assert provider.classify_calls == 0


def test_scan_disables_provider_when_change_fanout_exceeds_limit(monkeypatch, tmp_path: Path):
    class CountingProvider:
        def __init__(self):
            self.summarize_calls = 0

        def summarize(self, request: ProviderRequest) -> ProviderResponse:
            self.summarize_calls += 1
            return ProviderResponse(content="Model summary", confidence=0.9, model="mock-local")

        def classify(self, request: ProviderRequest) -> ProviderResponse:
            return ProviderResponse(content="changed/general/external/patch", confidence=0.9, model="mock-local")

    provider = CountingProvider()
    groups = [
        _mk_group(number=1, title="First change", commit_sha="a1", commit_message="first"),
        _mk_group(number=2, title="Second change", commit_sha="b2", commit_message="second"),
    ]
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_MODEL_MAX_ITEMS", "1")
    monkeypatch.setattr("releasepilot.cli.main._collect_groups", lambda base, head: groups)
    monkeypatch.setattr("releasepilot.cli.main.build_provider", lambda config, mode=None: provider)

    from releasepilot.cli.main import detect_change_items

    items = detect_change_items("v1.0.0", "HEAD", model_provider="mock")

    assert len(items) == 2
    assert provider.summarize_calls == 0
    assert all("provider-summary" not in signal for item in items for signal in item.evidence.commit_signals)


def test_draft_prompt_outputs_prompt_without_calling_provider(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.chdir(tmp_path)
    group = _mk_group(number=1, title="Improve login", commit_sha="a1", commit_message="update login flow")
    target_item = Classifier().classify(group, "v1.0.0", "HEAD")[0]

    monkeypatch.setattr("releasepilot.cli.main._collect_groups", lambda base, head: [group])
    monkeypatch.setattr(
        "releasepilot.cli.main.build_provider",
        lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("draft prompt should not build a provider")),
    )

    rc = main(["draft", "prompt", "--base", "v1.0.0", "--id", target_item.id])

    assert rc == 0
    prompt_preview = json.loads(capsys.readouterr().out)
    assert prompt_preview["item_id"] == target_item.id
    assert prompt_preview["provider"] == "ollama"
    assert "Lead with observable impact, not file names." in prompt_preview["prompt"]


def test_render_uses_edited_summary_and_skips_hidden_items(monkeypatch, tmp_path: Path, capsys):
    from releasepilot.state.store import StateData, StateStore

    state_path = tmp_path / "state.json"
    hidden = _mk_item("ci_hidden", title="Hidden change")
    hidden.status = "hidden"
    hidden.hidden = True
    edited = _mk_item("ci_edited", title="Edited change", summary="Original summary")
    edited.status = "edited"
    edited.edited_summary = "Custom reviewer summary"
    StateStore(str(state_path)).save(StateData(items=[hidden, edited]))
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(state_path))

    rc = main(["render", "--format", "changelog", "--version", "v1.2.0"])

    assert rc == 0
    rendered = capsys.readouterr().out
    assert "Hidden change" not in rendered
    assert "Custom reviewer summary" in rendered


def test_normalize_remote_url_for_github_links():
    assert normalize_remote_url("git@github.com:owner/repo.git") == "https://github.com/owner/repo"
    assert normalize_remote_url("https://github.com/owner/repo.git") == "https://github.com/owner/repo"
