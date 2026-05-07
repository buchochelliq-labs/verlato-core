from __future__ import annotations

import io
import json
from pathlib import Path

from verlato.cli.main import main
from verlato.core.models import ChangeItem, Classification, Content, Evidence, SourceRef
from verlato.mcp.service import MCPService
from verlato.service_stdio import run_service_stdio
from verlato.state.store import StateData, StateStore


class _FakeService:
    def health(self):
        return {"status": "ok"}

    def info(self):
        return {"status": "ok", "targets": ["changelog"]}

    def get_draft_document(self, request):
        return {"base_ref": request.base, "head_ref": request.head, "item_count": 0, "sections": [], "rendered": {}}

    def preview_release_notes(self, request):
        return f"preview:{request.base}->{request.head}"

    def apply_draft_request(self, *, request, target_name: str, target_path: str | None = None):
        return {"target": target_name, "path": target_path or "CHANGELOG.md", "base_ref": request.base}

    def update_review_state(self, **kwargs):
        return {"id": kwargs["item_id"], "status": kwargs.get("status")}

    def update_review_change_item(self, **kwargs):
        return {"id": kwargs["item_id"], "status": kwargs.get("status")}

    def record_feedback(self, *, event: str, item_id: str, metadata: dict[str, object] | None = None):
        return {"recorded": True, "event": event, "item_id": item_id, "metadata": metadata or {}}

    def state_show(self):
        return {"items": []}

    def clear_working_state(self):
        return {"removed_working_items": 0, "removed_feedback_events": 0, "preserved_published_items": 0}

    def clear_suggestion_cache(self):
        return {"removed_entries": 0, "path": ".verlato_suggestion_cache.json"}


def _request(request_id: str, method: str, params: dict | None = None) -> str:
    return json.dumps({"id": request_id, "method": method, "params": params or {}}) + "\n"


def _responses(output: io.StringIO) -> list[dict]:
    return [json.loads(line) for line in output.getvalue().splitlines()]


def _mk_item(item_id: str, published: bool = False, published_in: str | None = None) -> ChangeItem:
    return ChangeItem(
        id=item_id,
        source=SourceRef(pr_number=1, commit_shas=["a"], base_ref="v1", head_ref="main"),
        classification=Classification(kind="changed", area="general", audience="external", semver="patch"),
        content=Content(title="title", summary="summary"),
        evidence=Evidence(),
        confidence=0.8,
        published=published,
        published_in=published_in,
        checksum=f"ck-{item_id}",
    )


def test_service_stdio_answers_health_info_and_shutdown():
    output = io.StringIO()
    rc = run_service_stdio(
        service=_FakeService(),
        input_stream=io.StringIO(
            _request("1", "health")
            + _request("2", "info")
            + _request("3", "shutdown")
            + _request("4", "health")
        ),
        output_stream=output,
    )

    assert rc == 0
    responses = _responses(output)
    assert [response["id"] for response in responses] == ["1", "2", "3"]
    assert responses[0]["result"] == {"status": "ok"}
    assert responses[1]["result"]["targets"] == ["changelog"]
    assert responses[2]["result"] == {"shutdown": True}


def test_service_stdio_dispatches_core_editor_methods():
    output = io.StringIO()
    input_stream = io.StringIO(
        _request("draft", "draft.get", {"request": {"base": "v1", "head": "main"}})
        + _request("preview", "draft.preview", {"request": {"base": "v1", "head": "main"}})
        + _request("apply", "draft.apply", {"request": {"base": "v1"}, "target": "github-release"})
        + _request("review", "draft.review", {"request": {"base": "v1"}, "item_id": "ci_1", "status": "ready"})
        + _request("feedback", "feedback.record", {"event": "useful", "item_id": "ci_1", "metadata": {"source": "test"}})
    )

    run_service_stdio(service=_FakeService(), input_stream=input_stream, output_stream=output)

    by_id = {response["id"]: response for response in _responses(output)}
    assert by_id["draft"]["result"]["base_ref"] == "v1"
    assert by_id["preview"]["result"] == "preview:v1->main"
    assert by_id["apply"]["result"]["target"] == "github-release"
    assert by_id["review"]["result"] == {"id": "ci_1", "status": "ready"}
    assert by_id["feedback"]["result"]["recorded"] is True


def test_service_stdio_errors_are_structured_and_nonfatal():
    output = io.StringIO()
    run_service_stdio(
        service=_FakeService(),
        input_stream=io.StringIO("{bad-json}\n" + _request("2", "missing.method") + _request("3", "health")),
        output_stream=output,
    )

    responses = _responses(output)
    assert responses[0]["ok"] is False
    assert responses[0]["error"]["code"] == "invalid_json"
    assert responses[1]["ok"] is False
    assert responses[1]["error"]["code"] == "invalid_request"
    assert responses[2]["result"] == {"status": "ok"}


def test_mcp_service_state_feedback_and_cache_methods(monkeypatch, tmp_path: Path):
    state_path = tmp_path / "state.json"
    cache_path = tmp_path / "suggestions.json"
    StateStore(path=str(state_path)).save(
        StateData(
            items=[
                _mk_item("ci_published", published=True, published_in="v1.2.0"),
                _mk_item("ci_working"),
            ],
            feedback_events=[
                {"event": "suggestion_applied", "item_id": "ci_working"},
                {"event": "release_note_kept", "item_id": "ci_published"},
            ],
        )
    )
    cache_path.write_text(
        json.dumps(
            {
                "schema_version": "v1",
                "created_at": "2026-05-06T12:00:00Z",
                "updated_at": "2026-05-06T12:00:00Z",
                "entries": {"key": {"item": {"item_id": "ci_1", "suggestions": ["Summary"]}}},
            }
        )
    )
    monkeypatch.setenv("VERLATO_STATE_PATH", str(state_path))
    monkeypatch.setenv("VERLATO_SUGGESTION_CACHE_PATH", str(cache_path))

    service = MCPService()
    assert len(service.state_show()["items"]) == 2
    assert service.record_feedback(event="suggestion_skipped", item_id="ci_working") == {
        "recorded": True,
        "event": "suggestion_skipped",
        "item_id": "ci_working",
    }
    assert service.clear_working_state() == {
        "removed_working_items": 1,
        "removed_feedback_events": 2,
        "preserved_published_items": 1,
    }
    assert service.clear_suggestion_cache() == {"removed_entries": 1, "path": str(cache_path)}


def test_cli_service_delegates_to_builtin_stdio(monkeypatch):
    monkeypatch.setattr("verlato.service_stdio.run_service_stdio", lambda: 0)

    assert main(["service"]) == 0
