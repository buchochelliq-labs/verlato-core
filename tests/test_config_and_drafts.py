from __future__ import annotations

import json
from pathlib import Path

from releasepilot.cli.main import main
from releasepilot.config import load_config
from releasepilot.core.drafts import normalize_remote_url
from releasepilot.core.models import (
    ChangeItem,
    Classification,
    Content,
    Evidence,
    SourceRef,
)


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
                    "name": "github-copilot",
                    "model": "gpt-4o-mini",
                    "endpoint": "https://example.test/chat/completions",
                }
            }
        )
    )
    monkeypatch.chdir(tmp_path)

    config = load_config()

    assert config.provider.name == "github-copilot"
    assert config.provider.model == "gpt-4o-mini"
    assert config.provider.endpoint == "https://example.test/chat/completions"


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

    def detect(base: str, head: str, model_provider: str | None = None):
        signals = ["diff-fallback"]
        summary = "Deterministic summary"
        if model_provider == "mock":
            signals.append("provider-summary:mock-local")
            summary = "Model-written summary"
        item = _mk_item("ci_1", base_ref=base, head_ref=head, summary=summary)
        item.evidence.commit_signals = signals
        return [item]

    monkeypatch.setattr("releasepilot.cli.main.detect_change_items", detect)

    rc = main(["draft", "suggest", "--base", "v1.0.0", "--id", "ci_1", "--model-provider", "mock"])

    assert rc == 0
    suggested = json.loads(capsys.readouterr().out)
    assert suggested["edited_summary"] == "Model-written summary"
    assert suggested["status"] == "edited"

    rc = main(["draft", "json", "--base", "v1.0.0"])
    assert rc == 0
    document = json.loads(capsys.readouterr().out)
    item = document["sections"][0]["items"][0]
    assert item["summary"] == "Model-written summary"
    assert item["original_summary"] == "Deterministic summary"
    assert item["summary_source"] == "Human edited summary, originally generated by LLM (mock-local)"


def test_draft_suggest_defaults_to_github_copilot_when_no_provider_configured(monkeypatch, tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(state_path))
    monkeypatch.delenv("RELEASEPILOT_MODEL_PROVIDER", raising=False)

    def detect(base: str, head: str, model_provider: str | None = None):
        assert model_provider == "github-copilot"
        item = _mk_item("ci_1", base_ref=base, head_ref=head, summary="GitHub model summary")
        item.evidence.commit_signals = ["diff-fallback", "provider-summary:github-copilot:gpt-4o-mini"]
        return [item]

    monkeypatch.setattr("releasepilot.cli.main.detect_change_items", detect)

    rc = main(["draft", "suggest", "--base", "v1.0.0", "--id", "ci_1"])

    assert rc == 0
    suggested = json.loads(capsys.readouterr().out)
    assert suggested["edited_summary"] == "GitHub model summary"


def test_draft_suggest_surfaces_provider_errors(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(tmp_path / "state.json"))

    def detect(base: str, head: str, model_provider: str | None = None):
        item = _mk_item("ci_1", base_ref=base, head_ref=head, summary="Deterministic summary")
        item.evidence.commit_signals = [
            "diff-fallback",
            "provider-error:github-copilot:gpt-4o-mini:[github-copilot-unavailable] missing GITHUB_TOKEN/GH_TOKEN",
        ]
        return [item]

    monkeypatch.setattr("releasepilot.cli.main.detect_change_items", detect)

    try:
        main(["draft", "suggest", "--base", "v1.0.0", "--id", "ci_1", "--model-provider", "github-copilot"])
    except SystemExit as exc:
        assert "missing GITHUB_TOKEN/GH_TOKEN" in str(exc)
    else:
        raise AssertionError("Expected draft suggest to fail with the provider error")


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
