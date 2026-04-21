from __future__ import annotations

import json
from pathlib import Path

from releasepilot.cli.main import main
from releasepilot.core.models import (
    ChangeItem,
    Classification,
    Content,
    Evidence,
    SourceRef,
)
from releasepilot.state.store import StateData, StateStore


def _mk_item(item_id: str, published: bool = False, published_in: str | None = None) -> ChangeItem:
    return ChangeItem(
        id=item_id,
        source=SourceRef(pr_number=1, commit_shas=["a"], base_ref="v1", head_ref="main"),
        classification=Classification(kind="changed", area="general", audience="external", semver="patch"),
        content=Content(title="t", summary="s"),
        evidence=Evidence(),
        confidence=0.8,
        published=published,
        published_in=published_in,
        checksum=f"ck-{item_id}",
    )


def test_state_mark_published_all_unpublished(monkeypatch, tmp_path: Path):
    state_path = tmp_path / "state.json"
    store = StateStore(path=str(state_path))
    store.save(StateData(items=[_mk_item("ci_1"), _mk_item("ci_2", published=True)]))

    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(state_path))
    rc = main(["state", "mark-published", "--version", "v1.2.0", "--all-unpublished"])
    assert rc == 0

    data = json.loads(state_path.read_text())
    assert data["items"][0]["published"] is True
    assert data["items"][0]["published_in"] == "v1.2.0"
    assert data["items"][1]["published_in"] is None


def test_reconcile_command_outputs_count(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr("releasepilot.cli.main.run_scan", lambda base, head, persist: [{"id": "ci_1"}])

    rc = main(["reconcile", "--base", "v1.0.0", "--head", "HEAD"])
    assert rc == 0

    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 1
    assert out["unpublished"][0]["id"] == "ci_1"


def test_reconcile_from_latest_published_marker(monkeypatch, tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    store = StateStore(path=str(state_path))
    store.save(StateData(items=[_mk_item("ci_1", published=True, published_in="v1.0.0")]))
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(state_path))
    monkeypatch.setattr("releasepilot.cli.main.run_scan", lambda base, head, persist: [{"id": f"{base}->{head}"}])

    rc = main(["reconcile", "--from-latest-published", "--head", "HEAD"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 1
    assert out["unpublished"][0]["id"] == "v1.0.0->HEAD"


def test_scan_requires_base_or_marker(monkeypatch):
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", "unused.json")
    try:
        main(["scan"])
    except SystemExit as exc:
        assert "Provide --base or one marker mode" in str(exc)
    else:
        raise AssertionError("Expected SystemExit")


def test_build_adapter_prefers_github_when_enabled(monkeypatch):
    monkeypatch.setenv("RELEASEPILOT_PR_ADAPTER", "github")
    monkeypatch.setenv("RELEASEPILOT_GITHUB_REPO", "org/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    from releasepilot.cli.main import _build_adapter
    from releasepilot.github.adapter import GitHubApiAdapter

    adapter = _build_adapter()
    assert isinstance(adapter, GitHubApiAdapter)


def test_render_supports_confidence_and_provenance(monkeypatch, tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    StateStore(path=str(state_path)).save(StateData(items=[_mk_item("ci_1")]))
    monkeypatch.setenv("RELEASEPILOT_STATE_PATH", str(state_path))

    rc = main(
        [
            "render",
            "--format",
            "github-release",
            "--version",
            "v1.0.0",
            "--include-confidence",
            "--include-provenance",
        ]
    )
    assert rc == 0
    out = capsys.readouterr().out
    assert "confidence 0.80" in out
    assert "source v1..main" in out
