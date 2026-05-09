from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from verlato import __version__
from verlato.mcp.service import MCPService, ReleasePreviewRequest


@dataclass(frozen=True)
class MCPToolMetadata:
    name: str
    description: str


@dataclass(frozen=True)
class MCPResourceMetadata:
    uri: str
    description: str


@dataclass(frozen=True)
class MCPPromptMetadata:
    name: str
    description: str


_TOOL_METADATA: tuple[MCPToolMetadata, ...] = (
    MCPToolMetadata(
        name="verlato_service_health",
        description="Return basic health and version information for the Verlato backend.",
    ),
    MCPToolMetadata(
        name="verlato_service_info",
        description="Return Verlato service details including state stats and configured output targets.",
    ),
    MCPToolMetadata(
        name="verlato_scan_unpublished_changes",
        description="List unpublished change items for a git ref range without mutating state.",
    ),
    MCPToolMetadata(
        name="verlato_get_draft_document",
        description="Build a structured draft release document for a git ref range.",
    ),
    MCPToolMetadata(
        name="verlato_preview_release_notes",
        description="Render GitHub-style release notes for a git ref range.",
    ),
    MCPToolMetadata(
        name="verlato_apply_draft",
        description="Generate and apply a draft release document to a configured target file.",
    ),
    MCPToolMetadata(
        name="verlato_update_review_state",
        description="Update review status, summary, or reviewer metadata for a draft item.",
    ),
    MCPToolMetadata(
        name="verlato_request_model_input",
        description="Ask the configured model provider for a summary suggestion on one draft item and persist it as an edit.",
    ),
    MCPToolMetadata(
        name="verlato_reconcile_preview",
        description="Return a lightweight unpublished-change preview for a git ref range.",
    ),
    MCPToolMetadata(
        name="verlato_list_suggestion_items",
        description="List compact suggestion items and file manifests for a git ref range.",
    ),
    MCPToolMetadata(
        name="verlato_get_suggestion_item",
        description="Return commits, PR metadata, file manifest, and diff summary for one suggestion item.",
    ),
    MCPToolMetadata(
        name="verlato_get_file_patch",
        description="Return a bounded patch chunk for one file in one suggestion item.",
    ),
    MCPToolMetadata(
        name="verlato_search_item_diff",
        description="Search patch text for one suggestion item and return compact snippets.",
    ),
)

_RESOURCE_METADATA: tuple[MCPResourceMetadata, ...] = (
    MCPResourceMetadata(
        uri="verlato://overview",
        description="Structured overview of the Verlato MCP server and its exposed capabilities.",
    ),
    MCPResourceMetadata(
        uri="verlato://guidance/{name}",
        description="Workspace guidance documents such as AGENT.md, README.md, ARCHITECTURE.md, and Copilot instructions.",
    ),
    MCPResourceMetadata(
        uri="verlato://sample-config",
        description="Sample Verlato configuration file for repo setup and output targets.",
    ),
)

_PROMPT_METADATA: tuple[MCPPromptMetadata, ...] = (
    MCPPromptMetadata(
        name="verlato_review_request",
        description="Prompt template that guides an agent through reviewing and generating release notes for a ref range.",
    ),
)

_ROOT = Path(__file__).resolve().parents[2]
_GUIDANCE_FILES: dict[str, Path] = {
    "readme": _ROOT / "README.md",
    "agent": _ROOT / "AGENT.md",
    "architecture": _ROOT / "ARCHITECTURE.md",
    "copilot": _ROOT / ".github" / "copilot-instructions.md",
    "skill": _ROOT / ".codex" / "skills" / "verlato" / "SKILL.md",
}
_SAMPLE_CONFIG_PATH = _ROOT / "verlato.sample.json"

SERVER_INSTRUCTIONS = (
    "Verlato helps agents inspect git ranges, generate structured draft release notes, "
    "review draft items, and apply changelog or GitHub release-note updates."
)


def list_tool_metadata() -> list[dict[str, str]]:
    return [asdict(tool) for tool in _TOOL_METADATA]


def list_resource_metadata() -> list[dict[str, str]]:
    return [asdict(resource) for resource in _RESOURCE_METADATA]


def list_prompt_metadata() -> list[dict[str, str]]:
    return [asdict(prompt) for prompt in _PROMPT_METADATA]


GitRef = Annotated[str, "Git ref, tag, or commit to use for the requested range."]
ReleaseVersion = Annotated[str, "Release version label to use in rendered notes."]
TargetName = Annotated[Literal["changelog", "github-release"], "Configured output target to update."]
ItemId = Annotated[str, "Verlato draft item identifier."]
OptionalPath = Annotated[str | None, "Optional explicit file path override for the selected target."]
ReviewStatus = Annotated[
    Literal["needs-review", "ready", "accepted", "edited", "hidden"] | None,
    "Review status to persist for the selected draft item.",
]
OptionalSummary = Annotated[str | None, "Optional edited summary to persist for the draft item."]
OptionalReviewer = Annotated[str | None, "Optional reviewer identity to persist with the update."]
OptionalModelProvider = Annotated[
    Literal["stub", "mock", "ollama", "litellm", "plugin", "github-copilot", "codex-cli", "claude-code"] | None,
    "Optional model provider override for LLM-assisted summaries and classifications.",
]
PatchOffset = Annotated[int, "Character offset into the requested patch."]
PatchLimit = Annotated[int, "Maximum patch characters to return."]
SearchQuery = Annotated[str, "Case-insensitive query to search inside patch text."]
SearchLimit = Annotated[int, "Maximum number of patch search snippets to return."]
GuidanceName = Annotated[
    Literal["readme", "agent", "architecture", "copilot", "skill"],
    "Workspace guidance document to read.",
]


def _register_server_components(server, svc: MCPService) -> None:
    def _overview_payload() -> dict[str, Any]:
        return {
            "name": "verlato",
            "version": __version__,
            "instructions": SERVER_INSTRUCTIONS,
            "tools": [tool.name for tool in _TOOL_METADATA],
            "resources": [resource.uri for resource in _RESOURCE_METADATA],
            "prompts": [prompt.name for prompt in _PROMPT_METADATA],
        }

    def _read_guidance(name: str) -> str:
        path = _GUIDANCE_FILES.get(name)
        if path is None:
            raise ValueError(f"Unknown guidance document: {name}")
        if not path.exists():
            raise FileNotFoundError(f"Guidance document not found: {path}")
        return path.read_text(encoding="utf-8")

    @server.resource(
        "verlato://overview",
        name="Verlato Overview",
        description=_RESOURCE_METADATA[0].description,
        mime_type="application/json",
        tags={"verlato", "docs"},
        annotations={"readOnlyHint": True, "idempotentHint": True},
        version=__version__,
    )
    def verlato_overview() -> str:
        """Structured overview of the Verlato MCP server and its exposed capabilities."""

        return json.dumps(_overview_payload(), indent=2)

    @server.resource(
        "verlato://guidance/{name}",
        name="Verlato Guidance",
        description=_RESOURCE_METADATA[1].description,
        mime_type="text/markdown",
        tags={"verlato", "docs", "guidance"},
        annotations={"readOnlyHint": True, "idempotentHint": True},
        version=__version__,
    )
    def verlato_guidance(name: GuidanceName) -> str:
        """Workspace guidance documents exposed through FastMCP resources."""

        return _read_guidance(name)

    @server.resource(
        "verlato://sample-config",
        name="Verlato Sample Config",
        description=_RESOURCE_METADATA[2].description,
        mime_type="application/json",
        tags={"verlato", "config"},
        annotations={"readOnlyHint": True, "idempotentHint": True},
        version=__version__,
    )
    def verlato_sample_config() -> str:
        """Sample repository configuration for Verlato."""

        if not _SAMPLE_CONFIG_PATH.exists():
            raise FileNotFoundError(f"Sample config not found: {_SAMPLE_CONFIG_PATH}")
        return _SAMPLE_CONFIG_PATH.read_text(encoding="utf-8")

    @server.prompt(
        title="Verlato Review Request",
        description=_PROMPT_METADATA[0].description,
        tags={"verlato", "review", "release-notes"},
        version=__version__,
    )
    def verlato_review_request(
        base: GitRef,
        head: GitRef = "HEAD",
        version: ReleaseVersion = "Unreleased",
    ) -> str:
        """Prompt template for reviewing and generating release notes from a git ref range."""

        return (
            "Use Verlato to review changes and generate release notes.\n"
            f"- Base ref: {base}\n"
            f"- Head ref: {head}\n"
            f"- Release version: {version}\n"
            "Recommended flow:\n"
            "1. Call verlato_scan_unpublished_changes to inspect the candidate changes.\n"
            "2. Call verlato_get_draft_document or verlato_preview_release_notes to build the draft.\n"
            "3. If needed, call verlato_update_review_state to refine item status or summary.\n"
            "4. Call verlato_apply_draft only when you are ready to write the chosen target file."
        )

    @server.tool(
        annotations={
            "title": "Verlato Service Health",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        tags={"verlato", "service"},
        version=__version__,
    )
    def verlato_service_health() -> dict[str, Any]:
        """Return basic health and version information for the Verlato backend."""

        return svc.health()

    @server.tool(
        annotations={
            "title": "Verlato Service Info",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        tags={"verlato", "service"},
        version=__version__,
    )
    def verlato_service_info() -> dict[str, Any]:
        """Return service details including state stats and configured output targets."""

        return svc.info()

    @server.tool(
        annotations={
            "title": "Scan Unpublished Changes",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        tags={"verlato", "scan"},
        version=__version__,
    )
    def verlato_scan_unpublished_changes(base: GitRef, head: GitRef = "HEAD") -> list[dict[str, Any]]:
        """List unpublished change items for a git ref range without mutating state."""

        return svc.list_unpublished_changes(base=base, head=head)

    @server.tool(
        annotations={
            "title": "Get Draft Document",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        tags={"verlato", "draft"},
        version=__version__,
    )
    def verlato_get_draft_document(
        base: GitRef,
        head: GitRef = "HEAD",
        version: ReleaseVersion = "Unreleased",
        include_confidence: Annotated[bool, "Whether to include confidence values in the returned draft."] = False,
        include_provenance: Annotated[bool, "Whether to include provenance and evidence details in the draft."] = False,
        model_provider: OptionalModelProvider = None,
    ) -> dict[str, Any]:
        """Build a structured draft release document for a git ref range."""

        return svc.get_draft_document(
            ReleasePreviewRequest(
                base=base,
                head=head,
                version=version,
                include_confidence=include_confidence,
                include_provenance=include_provenance,
                model_provider=model_provider,
            )
        )

    @server.tool(
        annotations={
            "title": "Preview Release Notes",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        tags={"verlato", "draft", "preview"},
        version=__version__,
    )
    def verlato_preview_release_notes(
        base: GitRef,
        head: GitRef = "HEAD",
        version: ReleaseVersion = "Unreleased",
        include_confidence: Annotated[bool, "Whether to include confidence values in the rendered preview."] = False,
        include_provenance: Annotated[bool, "Whether to include provenance and evidence details in the rendered preview."] = False,
        model_provider: OptionalModelProvider = None,
    ) -> str:
        """Render GitHub-style release notes for a git ref range."""

        return svc.preview_release_notes(
            ReleasePreviewRequest(
                base=base,
                head=head,
                version=version,
                include_confidence=include_confidence,
                include_provenance=include_provenance,
                model_provider=model_provider,
            )
        )

    @server.tool(
        annotations={
            "title": "Apply Draft",
            "destructiveHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        tags={"verlato", "draft", "apply"},
        version=__version__,
    )
    def verlato_apply_draft(
        base: GitRef,
        head: GitRef = "HEAD",
        version: ReleaseVersion = "Unreleased",
        target: TargetName = "changelog",
        file: OptionalPath = None,
        include_confidence: Annotated[bool, "Whether to include confidence values while generating the draft."] = False,
        include_provenance: Annotated[bool, "Whether to include provenance while generating the draft."] = False,
        model_provider: OptionalModelProvider = None,
    ) -> dict[str, Any]:
        """Generate and apply a draft release document to a configured target file."""

        document = svc.get_draft_document(
            ReleasePreviewRequest(
                base=base,
                head=head,
                version=version,
                include_confidence=include_confidence,
                include_provenance=include_provenance,
                model_provider=model_provider,
            )
        )
        return svc.apply_draft_document(document=document, target_name=target, target_path=file)

    @server.tool(
        annotations={
            "title": "Update Review State",
            "idempotentHint": True,
            "openWorldHint": False,
        },
        tags={"verlato", "review"},
        version=__version__,
    )
    def verlato_update_review_state(
        base: GitRef,
        item_id: ItemId,
        head: GitRef = "HEAD",
        status: ReviewStatus = None,
        summary: OptionalSummary = None,
        reviewer: OptionalReviewer = None,
    ) -> dict[str, Any]:
        """Update review status, summary, or reviewer metadata for a draft item."""

        return svc.update_review_state(
            base=base,
            head=head,
            item_id=item_id,
            status=status,
            summary=summary,
            reviewer=reviewer,
        )

    @server.tool(
        annotations={
            "title": "Request Model Input",
            "idempotentHint": True,
            "openWorldHint": True,
        },
        tags={"verlato", "review", "model"},
        version=__version__,
    )
    def verlato_request_model_input(
        base: GitRef,
        item_id: ItemId,
        head: GitRef = "HEAD",
        reviewer: OptionalReviewer = None,
        model_provider: OptionalModelProvider = None,
    ) -> dict[str, Any]:
        """Ask the configured model provider for a summary suggestion on one draft item."""

        return svc.request_model_input(
            base=base,
            head=head,
            item_id=item_id,
            reviewer=reviewer,
            model_provider=model_provider,
        )

    @server.tool(
        annotations={
            "title": "Reconcile Preview",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        tags={"verlato", "scan", "review"},
        version=__version__,
    )
    def verlato_reconcile_preview(base: GitRef, head: GitRef = "HEAD") -> dict[str, Any]:
        """Return a lightweight unpublished-change preview for a git ref range."""

        return svc.reconcile_preview(base=base, head=head)

    @server.tool(
        annotations={
            "title": "List Suggestion Items",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        tags={"verlato", "suggestions", "diff"},
        version=__version__,
    )
    def verlato_list_suggestion_items(
        base: GitRef,
        head: GitRef = "HEAD",
        include_files: Annotated[bool, "Include changed-file path, status, and patch-size metadata."] = False,
    ) -> dict[str, Any]:
        """List compact suggestion items and changed-file manifests for a git ref range."""

        return svc.list_suggestion_items(base=base, head=head, include_files=include_files)

    @server.tool(
        annotations={
            "title": "Get Suggestion Item",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        tags={"verlato", "suggestions", "diff"},
        version=__version__,
    )
    def verlato_get_suggestion_item(base: GitRef, item_id: ItemId, head: GitRef = "HEAD") -> dict[str, Any]:
        """Return metadata, commits, file manifest, and diff summary for one suggestion item."""

        return svc.get_suggestion_item(base=base, head=head, item_id=item_id)

    @server.tool(
        annotations={
            "title": "Get File Patch",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        tags={"verlato", "suggestions", "diff"},
        version=__version__,
    )
    def verlato_get_file_patch(
        base: GitRef,
        item_id: ItemId,
        path: Annotated[str, "Changed file path."],
        head: GitRef = "HEAD",
        offset: PatchOffset = 0,
        limit: PatchLimit = 12000,
    ) -> dict[str, Any]:
        """Return a bounded patch chunk for one file in one suggestion item."""

        return svc.get_file_patch(
            base=base,
            head=head,
            item_id=item_id,
            path=path,
            offset=offset,
            limit=limit,
        )

    @server.tool(
        annotations={
            "title": "Search Item Diff",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        tags={"verlato", "suggestions", "diff"},
        version=__version__,
    )
    def verlato_search_item_diff(
        base: GitRef,
        item_id: ItemId,
        query: SearchQuery,
        head: GitRef = "HEAD",
        limit: SearchLimit = 5,
    ) -> dict[str, Any]:
        """Search patch text for one suggestion item and return compact snippets."""

        return svc.search_item_diff(base=base, head=head, item_id=item_id, query=query, limit=limit)


def create_fastmcp_server(service: MCPService | None = None, fastmcp_cls: type | None = None):
    if fastmcp_cls is None:
        try:
            from fastmcp import FastMCP
        except ImportError as exc:  # pragma: no cover - depends on optional package
            raise RuntimeError(
                "MCP stdio transport requires the optional 'fastmcp' package. Install Verlato with MCP support first."
            ) from exc
        fastmcp_cls = FastMCP

    svc = service or MCPService()
    server = fastmcp_cls(
        "Verlato",
        instructions=SERVER_INSTRUCTIONS,
        version=__version__,
    )
    _register_server_components(server, svc)
    return server


def run_mcp_stdio(service: MCPService | None = None, fastmcp_cls: type | None = None) -> int:
    server = create_fastmcp_server(service=service, fastmcp_cls=fastmcp_cls)
    server.run()
    return 0


def main() -> int:
    return run_mcp_stdio()
