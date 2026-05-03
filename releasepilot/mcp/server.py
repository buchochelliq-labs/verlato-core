from __future__ import annotations

import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Annotated, Any, Literal

from releasepilot import __version__
from releasepilot.mcp.service import MCPService, ReleasePreviewRequest


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
        name="releasepilot_service_health",
        description="Return basic health and version information for the ReleasePilot backend.",
    ),
    MCPToolMetadata(
        name="releasepilot_service_info",
        description="Return ReleasePilot service details including state stats and configured output targets.",
    ),
    MCPToolMetadata(
        name="releasepilot_scan_unpublished_changes",
        description="List unpublished change items for a git ref range without mutating state.",
    ),
    MCPToolMetadata(
        name="releasepilot_get_draft_document",
        description="Build a structured draft release document for a git ref range.",
    ),
    MCPToolMetadata(
        name="releasepilot_preview_release_notes",
        description="Render GitHub-style release notes for a git ref range.",
    ),
    MCPToolMetadata(
        name="releasepilot_apply_draft",
        description="Generate and apply a draft release document to a configured target file.",
    ),
    MCPToolMetadata(
        name="releasepilot_update_review_state",
        description="Update review status, summary, or reviewer metadata for a draft item.",
    ),
    MCPToolMetadata(
        name="releasepilot_request_model_input",
        description="Ask the configured model provider for a summary suggestion on one draft item and persist it as an edit.",
    ),
    MCPToolMetadata(
        name="releasepilot_reconcile_preview",
        description="Return a lightweight unpublished-change preview for a git ref range.",
    ),
)

_RESOURCE_METADATA: tuple[MCPResourceMetadata, ...] = (
    MCPResourceMetadata(
        uri="releasepilot://overview",
        description="Structured overview of the ReleasePilot MCP server and its exposed capabilities.",
    ),
    MCPResourceMetadata(
        uri="releasepilot://guidance/{name}",
        description="Workspace guidance documents such as AGENT.md, README.md, ARCHITECTURE.md, and Copilot instructions.",
    ),
    MCPResourceMetadata(
        uri="releasepilot://sample-config",
        description="Sample ReleasePilot configuration file for repo setup and output targets.",
    ),
)

_PROMPT_METADATA: tuple[MCPPromptMetadata, ...] = (
    MCPPromptMetadata(
        name="releasepilot_review_request",
        description="Prompt template that guides an agent through reviewing and generating release notes for a ref range.",
    ),
)

_ROOT = Path(__file__).resolve().parents[2]
_GUIDANCE_FILES: dict[str, Path] = {
    "readme": _ROOT / "README.md",
    "agent": _ROOT / "AGENT.md",
    "architecture": _ROOT / "ARCHITECTURE.md",
    "copilot": _ROOT / ".github" / "copilot-instructions.md",
    "skill": _ROOT / ".codex" / "skills" / "releasepilot" / "SKILL.md",
}
_SAMPLE_CONFIG_PATH = _ROOT / "releasepilot.sample.json"

SERVER_INSTRUCTIONS = (
    "ReleasePilot helps agents inspect git ranges, generate structured draft release notes, "
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
ItemId = Annotated[str, "ReleasePilot draft item identifier."]
OptionalPath = Annotated[str | None, "Optional explicit file path override for the selected target."]
ReviewStatus = Annotated[
    Literal["needs-review", "ready", "accepted", "edited", "hidden"] | None,
    "Review status to persist for the selected draft item.",
]
OptionalSummary = Annotated[str | None, "Optional edited summary to persist for the draft item."]
OptionalReviewer = Annotated[str | None, "Optional reviewer identity to persist with the update."]
OptionalModelProvider = Annotated[
    Literal["stub", "mock", "ollama", "github-copilot"] | None,
    "Optional model provider override for LLM-assisted summaries and classifications.",
]
GuidanceName = Annotated[
    Literal["readme", "agent", "architecture", "copilot", "skill"],
    "Workspace guidance document to read.",
]


def _register_server_components(server, svc: MCPService) -> None:
    def _overview_payload() -> dict[str, Any]:
        return {
            "name": "releasepilot",
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
        "releasepilot://overview",
        name="ReleasePilot Overview",
        description=_RESOURCE_METADATA[0].description,
        mime_type="application/json",
        tags={"releasepilot", "docs"},
        annotations={"readOnlyHint": True, "idempotentHint": True},
        version=__version__,
    )
    def releasepilot_overview() -> str:
        """Structured overview of the ReleasePilot MCP server and its exposed capabilities."""

        return json.dumps(_overview_payload(), indent=2)

    @server.resource(
        "releasepilot://guidance/{name}",
        name="ReleasePilot Guidance",
        description=_RESOURCE_METADATA[1].description,
        mime_type="text/markdown",
        tags={"releasepilot", "docs", "guidance"},
        annotations={"readOnlyHint": True, "idempotentHint": True},
        version=__version__,
    )
    def releasepilot_guidance(name: GuidanceName) -> str:
        """Workspace guidance documents exposed through FastMCP resources."""

        return _read_guidance(name)

    @server.resource(
        "releasepilot://sample-config",
        name="ReleasePilot Sample Config",
        description=_RESOURCE_METADATA[2].description,
        mime_type="application/json",
        tags={"releasepilot", "config"},
        annotations={"readOnlyHint": True, "idempotentHint": True},
        version=__version__,
    )
    def releasepilot_sample_config() -> str:
        """Sample repository configuration for ReleasePilot."""

        if not _SAMPLE_CONFIG_PATH.exists():
            raise FileNotFoundError(f"Sample config not found: {_SAMPLE_CONFIG_PATH}")
        return _SAMPLE_CONFIG_PATH.read_text(encoding="utf-8")

    @server.prompt(
        title="ReleasePilot Review Request",
        description=_PROMPT_METADATA[0].description,
        tags={"releasepilot", "review", "release-notes"},
        version=__version__,
    )
    def releasepilot_review_request(
        base: GitRef,
        head: GitRef = "HEAD",
        version: ReleaseVersion = "Unreleased",
    ) -> str:
        """Prompt template for reviewing and generating release notes from a git ref range."""

        return (
            "Use ReleasePilot to review changes and generate release notes.\n"
            f"- Base ref: {base}\n"
            f"- Head ref: {head}\n"
            f"- Release version: {version}\n"
            "Recommended flow:\n"
            "1. Call releasepilot_scan_unpublished_changes to inspect the candidate changes.\n"
            "2. Call releasepilot_get_draft_document or releasepilot_preview_release_notes to build the draft.\n"
            "3. If needed, call releasepilot_update_review_state to refine item status or summary.\n"
            "4. Call releasepilot_apply_draft only when you are ready to write the chosen target file."
        )

    @server.tool(
        annotations={
            "title": "ReleasePilot Service Health",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        tags={"releasepilot", "service"},
        version=__version__,
    )
    def releasepilot_service_health() -> dict[str, Any]:
        """Return basic health and version information for the ReleasePilot backend."""

        return svc.health()

    @server.tool(
        annotations={
            "title": "ReleasePilot Service Info",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        tags={"releasepilot", "service"},
        version=__version__,
    )
    def releasepilot_service_info() -> dict[str, Any]:
        """Return service details including state stats and configured output targets."""

        return svc.info()

    @server.tool(
        annotations={
            "title": "Scan Unpublished Changes",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        tags={"releasepilot", "scan"},
        version=__version__,
    )
    def releasepilot_scan_unpublished_changes(base: GitRef, head: GitRef = "HEAD") -> list[dict[str, Any]]:
        """List unpublished change items for a git ref range without mutating state."""

        return svc.list_unpublished_changes(base=base, head=head)

    @server.tool(
        annotations={
            "title": "Get Draft Document",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        tags={"releasepilot", "draft"},
        version=__version__,
    )
    def releasepilot_get_draft_document(
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
        tags={"releasepilot", "draft", "preview"},
        version=__version__,
    )
    def releasepilot_preview_release_notes(
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
        tags={"releasepilot", "draft", "apply"},
        version=__version__,
    )
    def releasepilot_apply_draft(
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
        tags={"releasepilot", "review"},
        version=__version__,
    )
    def releasepilot_update_review_state(
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
        tags={"releasepilot", "review", "model"},
        version=__version__,
    )
    def releasepilot_request_model_input(
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
        tags={"releasepilot", "scan", "review"},
        version=__version__,
    )
    def releasepilot_reconcile_preview(base: GitRef, head: GitRef = "HEAD") -> dict[str, Any]:
        """Return a lightweight unpublished-change preview for a git ref range."""

        return svc.reconcile_preview(base=base, head=head)


def create_fastmcp_server(service: MCPService | None = None, fastmcp_cls: type | None = None):
    if fastmcp_cls is None:
        try:
            from fastmcp import FastMCP
        except ImportError as exc:  # pragma: no cover - depends on optional package
            raise RuntimeError(
                "MCP stdio transport requires the optional 'fastmcp' package. Install ReleasePilot with MCP support first."
            ) from exc
        fastmcp_cls = FastMCP

    svc = service or MCPService()
    server = fastmcp_cls(
        "ReleasePilot",
        instructions=SERVER_INSTRUCTIONS,
        version=__version__,
    )
    _register_server_components(server, svc)
    return server


def run_mcp_stdio(service: MCPService | None = None, fastmcp_cls: type | None = None) -> int:
    server = create_fastmcp_server(service=service, fastmcp_cls=fastmcp_cls)
    server.run()
    return 0
