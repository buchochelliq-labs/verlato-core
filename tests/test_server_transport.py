from __future__ import annotations

import io

from releasepilot.cli.main import main
from releasepilot.server import ReleasePilotServer


class _FakeService:
    def __init__(self) -> None:
        self.applied: list[tuple[dict, str, str | None]] = []
        self.reviewed: list[dict] = []

    def get_draft_document(self, request):
        return {
            "version": request.version,
            "rendered": {"github-release": f"## {request.version}\n"},
            "sections": [],
            "targets": [],
            "base_ref": request.base,
            "head_ref": request.head,
            "generated_on": "2026-04-24",
            "item_count": 0,
        }

    def preview_release_notes(self, request):
        return f"preview:{request.base}->{request.head}"

    def apply_draft_document(self, *, document, target_name: str, target_path: str | None = None):
        self.applied.append((document, target_name, target_path))
        return {"target": target_name, "path": target_path or "CHANGELOG.md", "format": "changelog", "action": "replace"}

    def update_review_state(self, **kwargs):
        self.reviewed.append(kwargs)
        return {"id": kwargs["item_id"], "status": kwargs.get("status") or "edited"}

    def list_unpublished_changes(self, base: str, head: str = "HEAD"):
        return [{"id": f"{base}->{head}"}]


def test_server_handles_draft_get_document():
    server = ReleasePilotServer(service=_FakeService())

    response = server.handle_request({"id": "1", "method": "draft.getDocument", "params": {"base": "v1.0.0"}})

    assert response.error is None
    assert response.result["base_ref"] == "v1.0.0"


def test_server_handles_draft_apply():
    service = _FakeService()
    server = ReleasePilotServer(service=service)

    response = server.handle_request(
        {
            "id": "2",
            "method": "draft.apply",
            "params": {"base": "v1.0.0", "head": "HEAD", "version": "v1.1.0", "target": "changelog"},
        }
    )

    assert response.error is None
    assert response.result["target"] == "changelog"
    assert service.applied[0][1] == "changelog"


def test_server_serve_stdio_writes_json_responses():
    server = ReleasePilotServer(service=_FakeService())
    input_stream = io.StringIO(
        '{"id":"1","method":"scan.unpublished","params":{"base":"v1.0.0","head":"HEAD"}}\nnot-json\n'
    )
    output_stream = io.StringIO()

    rc = server.serve_stdio(input_stream=input_stream, output_stream=output_stream)

    assert rc == 0
    lines = [line for line in output_stream.getvalue().splitlines() if line.strip()]
    assert '"result"' in lines[0]
    assert '"error"' in lines[1]


def test_server_reports_unknown_method():
    server = ReleasePilotServer(service=_FakeService())

    response = server.handle_request({"id": "3", "method": "unknown.method", "params": {}})

    assert response.error is not None
    assert response.error["code"] == "unknown-method"


def test_cli_serve_delegates_to_stdio_server(monkeypatch):
    monkeypatch.setattr("releasepilot.server.ReleasePilotServer.serve_stdio", lambda self: 0)

    rc = main(["serve"])

    assert rc == 0
