from __future__ import annotations

import json
from pathlib import Path

from verlato.cli.main import main
from verlato.core.models import (
    ChangeItem,
    Classification,
    Content,
    Evidence,
    SourceRef,
)
from verlato.state.store import StateData, StateStore


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

    monkeypatch.setenv("VERLATO_STATE_PATH", str(state_path))
    rc = main(["state", "mark-published", "--version", "v1.2.0", "--all-unpublished"])
    assert rc == 0

    data = json.loads(state_path.read_text())
    assert data["items"][0]["published"] is True
    assert data["items"][0]["published_in"] == "v1.2.0"
    assert data["items"][1]["published_in"] is None


def test_state_clear_working_preserves_published_releases(monkeypatch, tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    store = StateStore(path=str(state_path))
    published = _mk_item("ci_published", published=True, published_in="v1.2.0")
    working = _mk_item("ci_working")
    working.status = "hidden"
    working.hidden = True
    store.save(StateData(
        items=[published, working],
        feedback_events=[
            {"event": "suggestion_applied", "item_id": "ci_working"},
            {"event": "release_note_kept", "item_id": "ci_published"},
        ],
    ))

    monkeypatch.setenv("VERLATO_STATE_PATH", str(state_path))
    rc = main(["state", "clear-working"])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "removed_working_items": 1,
        "removed_feedback_events": 1,
        "preserved_published_items": 1,
    }
    loaded = store.load()
    assert [item.id for item in loaded.items] == ["ci_published"]
    assert loaded.items[0].published_in == "v1.2.0"
    assert loaded.feedback_events == [{"event": "release_note_kept", "item_id": "ci_published"}]


def test_state_clear_suggestion_cache_deletes_cache_file(monkeypatch, tmp_path: Path, capsys):
    cache_path = tmp_path / "suggestions.json"
    cache_path.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "created_at": "2026-05-06T12:00:00Z",
                "updated_at": "2026-05-06T12:00:00Z",
                "entries": {"key-1": {"item": {"item_id": "ci_1", "suggestions": ["Improves release notes"]}}},
            }
        )
    )

    monkeypatch.setenv("VERLATO_SUGGESTION_CACHE_PATH", str(cache_path))
    rc = main(["state", "clear-suggestion-cache"])

    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out == {
        "removed_entries": 1,
        "path": str(cache_path),
    }
    assert not cache_path.exists()


def test_reconcile_command_outputs_count(monkeypatch, tmp_path: Path, capsys):
    monkeypatch.setenv("VERLATO_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setattr("verlato.cli.main.run_scan", lambda base, head, persist, **_kwargs: [{"id": "ci_1"}])

    rc = main(["reconcile", "--base", "v1.0.0", "--head", "HEAD"])
    assert rc == 0

    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 1
    assert out["unpublished"][0]["id"] == "ci_1"


def test_reconcile_from_latest_published_marker(monkeypatch, tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    store = StateStore(path=str(state_path))
    store.save(StateData(items=[_mk_item("ci_1", published=True, published_in="v1.0.0")]))
    monkeypatch.setenv("VERLATO_STATE_PATH", str(state_path))
    monkeypatch.setattr(
        "verlato.cli.main.run_scan",
        lambda base, head, persist, **_kwargs: [{"id": f"{base}->{head}"}],
    )

    rc = main(["reconcile", "--from-latest-published", "--head", "HEAD"])
    assert rc == 0
    out = json.loads(capsys.readouterr().out)
    assert out["count"] == 1
    assert out["unpublished"][0]["id"] == "v1.0.0->HEAD"


def test_scan_requires_base_or_marker(monkeypatch):
    monkeypatch.setenv("VERLATO_STATE_PATH", "unused.json")
    try:
        main(["scan"])
    except SystemExit as exc:
        assert "Provide --base or one marker mode" in str(exc)
    else:
        raise AssertionError("Expected SystemExit")


def test_build_adapter_prefers_github_when_enabled(monkeypatch):
    monkeypatch.setenv("VERLATO_PR_ADAPTER", "github")
    monkeypatch.setenv("VERLATO_GITHUB_REPO", "org/repo")
    monkeypatch.setenv("GITHUB_TOKEN", "token")

    from verlato.cli.main import _build_adapter
    from verlato.github.adapter import GitHubApiAdapter

    adapter = _build_adapter()
    assert isinstance(adapter, GitHubApiAdapter)


def test_render_supports_confidence_and_provenance(monkeypatch, tmp_path: Path, capsys):
    state_path = tmp_path / "state.json"
    StateStore(path=str(state_path)).save(StateData(items=[_mk_item("ci_1")]))
    monkeypatch.setenv("VERLATO_STATE_PATH", str(state_path))

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
