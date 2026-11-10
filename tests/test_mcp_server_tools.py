from __future__ import annotations

from typing import get_type_hints

from verlato.cli.main import main
from verlato.mcp.server import (
    create_fastmcp_server,
    list_prompt_metadata,
    list_resource_metadata,
    list_tool_metadata,
    run_mcp_stdio,
)


class _FakeFastMCP:
    def __init__(self, name: str, **kwargs) -> None:
        self.name = name
        self.kwargs = kwargs
        self.registered: dict[str, object] = {}
        self.tool_annotations: dict[str, dict[str, object]] = {}
        self.resources: dict[str, object] = {}
        self.prompts: dict[str, object] = {}
        self.ran = False

    def tool(self, fn=None, **kwargs):
        def decorator(inner):
            self.registered[inner.__name__] = inner
            self.tool_annotations[inner.__name__] = dict(kwargs.get("annotations") or {})
            return inner

        if fn is None:
            return decorator
        return decorator(fn)

    def resource(self, uri, **kwargs):
        def decorator(inner):
            self.resources[uri] = {"fn": inner, "kwargs": kwargs}
            return inner

        return decorator

    def prompt(self, fn=None, **kwargs):
        def decorator(inner):
            self.prompts[inner.__name__] = {"fn": inner, "kwargs": kwargs}
            return inner

        if fn is None:
            return decorator
        return decorator(fn)

    def run(self) -> None:
        self.ran = True


class _FakeService:
    def health(self):
        return {"status": "ok"}

    def info(self):
        return {"targets": ["changelog"], "mcp_tools": ["verlato_service_health"]}

    def list_unpublished_changes(self, base: str, head: str = "HEAD"):
        return [{"id": f"{base}->{head}"}]

    def get_draft_document(self, request):
        return {
            "version": request.version,
            "base_ref": request.base,
            "head_ref": request.head,
            "rendered": {"github-release": f"## {request.version}\n"},
            "sections": [],
            "targets": [],
            "generated_on": "2026-04-24",
            "item_count": 0,
        }

    def preview_release_notes(self, request):
        return f"preview:{request.base}->{request.head}"

    def apply_draft_document(self, *, document, target_name: str, target_path: str | None = None):
        return {
            "target": target_name,
            "path": target_path or "CHANGELOG.md",
            "base_ref": document["base_ref"],
        }

    def update_review_state(self, **kwargs):
        return {"id": kwargs["item_id"], "status": kwargs.get("status") or "ready"}

    def request_model_input(self, **kwargs):
        return {"id": kwargs["item_id"], "summary": "Model summary"}

    def reconcile_preview(self, base: str, head: str = "HEAD"):
        return {"count": 1, "unpublished": [{"id": f"{base}->{head}"}]}

    def list_suggestion_items(self, *, base: str, head: str = "HEAD", include_files: bool = False):
        return {"items": [{"item_id": f"{base}->{head}", "include_files": include_files}]}

    def get_suggestion_item(self, *, base: str, item_id: str, head: str = "HEAD"):
        return {"item_id": item_id, "base": base, "head": head}

    def get_file_semantic_diff(self, *, base: str, item_id: str, path: str, head: str = "HEAD"):
        return {"item_id": item_id, "path": path, "records": [], "text": ""}

    def search_semantic_diff(self, *, base: str, item_id: str, query: str, head: str = "HEAD", limit: int = 5):
        return {"item_id": item_id, "query": query, "results": []}

    def get_file_patch(
        self,
        *,
        base: str,
        item_id: str,
        path: str,
        head: str = "HEAD",
        offset: int = 0,
        limit: int = 12000,
    ):
        return {"item_id": item_id, "path": path, "offset": offset, "limit": limit}

    def search_item_diff(self, *, base: str, item_id: str, query: str, head: str = "HEAD", limit: int = 5):
        return {"item_id": item_id, "query": query, "limit": limit}

    def draft_conventional_commit(self, **kwargs):
        return {
            "sha": kwargs["sha"],
            "header": "refactor(vscode): update settings",
            "evidence_mode": kwargs.get("evidence_mode"),
        }

    def draft_conventional_commits(self, **kwargs):
        return {
            "base_ref": kwargs["base"],
            "head_ref": kwargs.get("head", "HEAD"),
            "count": len(kwargs.get("shas") or []),
            "completed": len(kwargs.get("shas") or []),
            "failed": 0,
            "results": [{"sha": sha, "evidence_mode": kwargs.get("evidence_mode")} for sha in (kwargs.get("shas") or [])],
            "errors": [],
        }


def test_mcp_tool_metadata_lists_agent_facing_tools():
    names = [tool["name"] for tool in list_tool_metadata()]

    assert names == [
        "verlato_service_health",
        "verlato_service_info",
        "verlato_scan_unpublished_changes",
        "verlato_get_draft_document",
        "verlato_preview_release_notes",
        "verlato_apply_draft",
        "verlato_update_review_state",
        "verlato_request_model_input",
        "verlato_reconcile_preview",
        "verlato_list_suggestion_items",
        "verlato_get_suggestion_item",
        "verlato_get_file_semantic_diff",
        "verlato_search_semantic_diff",
        "verlato_draft_conventional_commit",
        "verlato_draft_conventional_commits",
    ]


def test_legacy_git_engine_exposes_patch_tools_instead(monkeypatch):
    monkeypatch.setenv("VERLATO_DIFF_ENGINE", "git")
    names = [tool["name"] for tool in list_tool_metadata()]
    assert "verlato_get_file_patch" in names and "verlato_search_item_diff" in names
    server = create_fastmcp_server(service=_FakeService(), fastmcp_cls=_FakeFastMCP)
    patch = server.registered["verlato_get_file_patch"](base="v1.0.0", item_id="ci_1", path="src/a.py")
    assert patch["path"] == "src/a.py"
    assert "verlato_get_file_semantic_diff" in server.registered


def test_semantic_engine_hides_raw_patch_tools(monkeypatch):
    monkeypatch.delenv("VERLATO_DIFF_ENGINE", raising=False)
    server = create_fastmcp_server(service=_FakeService(), fastmcp_cls=_FakeFastMCP)
    assert "verlato_get_file_patch" not in server.registered
    assert "verlato_search_item_diff" not in server.registered


def test_mcp_resource_and_prompt_metadata_lists_capabilities():
    resource_uris = [resource["uri"] for resource in list_resource_metadata()]
    prompt_names = [prompt["name"] for prompt in list_prompt_metadata()]

    assert "verlato://overview" in resource_uris
    assert "verlato://guidance/{name}" in resource_uris
    assert "verlato_review_request" in prompt_names


def test_create_fastmcp_server_registers_verlato_tools():
    server = create_fastmcp_server(service=_FakeService(), fastmcp_cls=_FakeFastMCP)

    assert server.name == "Verlato"
    assert server.kwargs["version"]
    assert "instructions" in server.kwargs
    assert set(server.registered) == {
        "verlato_service_health",
        "verlato_service_info",
        "verlato_scan_unpublished_changes",
        "verlato_get_draft_document",
        "verlato_preview_release_notes",
        "verlato_apply_draft",
        "verlato_update_review_state",
        "verlato_request_model_input",
        "verlato_reconcile_preview",
        "verlato_list_suggestion_items",
        "verlato_get_suggestion_item",
        "verlato_get_file_semantic_diff",
        "verlato_search_semantic_diff",
        "verlato_draft_conventional_commit",
        "verlato_draft_conventional_commits",
    }
    assert "verlato://overview" in server.resources
    assert "verlato://guidance/{name}" in server.resources
    assert "verlato_sample_config" not in server.prompts
    assert "verlato_review_request" in server.prompts
    assert server.tool_annotations["verlato_preview_release_notes"]["readOnlyHint"] is True
    assert server.tool_annotations["verlato_apply_draft"]["destructiveHint"] is True
    hints = get_type_hints(server.registered["verlato_apply_draft"], include_extras=True)
    assert "target" in hints
    assert "file" in hints
    assert server.registered["verlato_preview_release_notes"](base="v1.0.0") == "preview:v1.0.0->HEAD"
    assert server.registered["verlato_apply_draft"](base="v1.0.0", target="changelog")["target"] == "changelog"
    semantic = server.registered["verlato_get_file_semantic_diff"](base="v1.0.0", item_id="ci_1", path="src/a.py")
    assert semantic["path"] == "src/a.py"
    assert "IntentumDiff semantic diff" in server.kwargs["instructions"]
    assert server.registered["verlato_draft_conventional_commit"](base="v1.0.0", sha="a1")["sha"] == "a1"
    batch = server.registered["verlato_draft_conventional_commits"](base="v1.0.0", shas=["a1", "b2"])
    assert batch["completed"] == 2
    assert batch["results"][0]["evidence_mode"] == "diff-only"


def test_run_mcp_stdio_uses_stdio_transport():
    server = _FakeFastMCP("Verlato")

    class _Factory:
        def __call__(self, name: str, **kwargs):
            assert name == "Verlato"
            assert "instructions" in kwargs
            return server

    rc = run_mcp_stdio(service=_FakeService(), fastmcp_cls=_Factory())

    assert rc == 0
    assert server.ran is True


def test_cli_serve_can_delegate_to_mcp_stdio(monkeypatch):
    monkeypatch.setattr("verlato.mcp.server.run_mcp_stdio", lambda: 0)

    rc = main(["serve"])

    assert rc == 0


def test_review_request_prompt_renders_through_real_fastmcp():
    # Regression: FastMCP resolved the prompt's string annotations ("GitRef") lazily and failed
    # with "TypeAdapter[GitRef] is not fully defined" on every prompts/get.
    import asyncio

    import pytest

    fastmcp = pytest.importorskip("fastmcp")
    server = create_fastmcp_server(service=_FakeService())

    async def render():
        async with fastmcp.Client(server) as client:
            return await client.get_prompt("verlato_review_request", {"base": "v1.0.0", "head": "main"})

    result = asyncio.run(render())
    text = result.messages[0].content.text
    assert "- Base ref: v1.0.0\n- Head ref: main\n- Release version: Unreleased\n" in text


def test_system_exit_from_service_becomes_a_tool_error():
    # Regression: CLI helpers raise SystemExit (e.g. an unknown draft item), which FastMCP does
    # not catch, so the stdio server never answered the call.
    import asyncio

    import pytest

    fastmcp = pytest.importorskip("fastmcp")

    class _ExitingService(_FakeService):
        def update_review_state(self, **kwargs):
            raise SystemExit("Could not find draft item ci_x for range v1..HEAD")

    server = create_fastmcp_server(service=_ExitingService())

    async def call():
        async with fastmcp.Client(server) as client:
            return await client.call_tool(
                "verlato_update_review_state",
                {"base": "v1", "item_id": "ci_x", "status": "ready"},
                raise_on_error=False,
            )

    result = asyncio.run(call())
    assert result.is_error
    assert result.content[0].text == (
        "Error calling tool 'verlato_update_review_state': Could not find draft item ci_x for range v1..HEAD"
    )
