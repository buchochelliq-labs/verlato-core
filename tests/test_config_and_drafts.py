from __future__ import annotations

import json
from pathlib import Path

from releasepilot.cli.main import main
from releasepilot.config import load_config
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
    assert out["base_ref"] == "v1.0.0"
    assert out["targets"][0]["name"] == "changelog"
    assert out["sections"][0]["kind"] == "added"


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
    assert "Improve login" not in document["rendered"]["github-release"]


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
