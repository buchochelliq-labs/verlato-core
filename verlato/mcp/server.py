from __future__ import annotations

import json
import typing
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
        description=(
            "Return commits, PR metadata, file manifest, diff summary, and the IntentumDiff semantic review "
            "for one suggestion item."
        ),
    ),
    MCPToolMetadata(
        name="verlato_get_file_semantic_diff",
        description=(
            "Return the IntentumDiff semantic diff for one file in one suggestion item: meaningful changes with "
            "their enclosing symbols, refactorings, moved code, and ignored formatting."
        ),
    ),
    MCPToolMetadata(
        name="verlato_search_semantic_diff",
        description="Search the IntentumDiff semantic diff of one suggestion item for a symbol or term.",
    ),
    MCPToolMetadata(
        name="verlato_get_file_patch",
        description="Return a bounded patch chunk for one file in one suggestion item.",
    ),
    MCPToolMetadata(
        name="verlato_search_item_diff",
        description="Search patch text for one suggestion item and return compact snippets.",
    ),
    MCPToolMetadata(
        name="verlato_draft_conventional_commit",
        description="Infer or draft a full Conventional Commits message for one commit in a git range.",
    ),
    MCPToolMetadata(
        name="verlato_draft_conventional_commits",
        description="Infer or draft Conventional Commits messages for commits in a git range sequentially.",
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
    "review draft items, and apply changelog or GitHub release-note updates. "
    "Change evidence comes from the IntentumDiff semantic diff: use verlato_get_file_semantic_diff and "
    "verlato_search_semantic_diff to see meaningful changes, refactorings, moved code and ignored formatting, "
    "and never describe moved code, refactorings or formatting as new behavior. Raw patches are not exposed."
)

# Raw-patch tools exist only for the legacy `git` diff engine, which has no semantic diff.
LEGACY_PATCH_TOOLS = frozenset({"verlato_get_file_patch", "verlato_search_item_diff"})


def legacy_patch_tools_enabled() -> bool:
    from verlato.config import load_config
    from verlato.core.semantic import resolve_diff_engine

    return resolve_diff_engine(load_config().diff_engine) == "git"


def _active_tool_metadata() -> tuple[MCPToolMetadata, ...]:
    if legacy_patch_tools_enabled():
        return _TOOL_METADATA
    return tuple(tool for tool in _TOOL_METADATA if tool.name not in LEGACY_PATCH_TOOLS)


def list_tool_metadata() -> list[dict[str, str]]:
    return [asdict(tool) for tool in _active_tool_metadata()]


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
SearchLimit = Annotated[int, "Maximum number of search results to return."]
CommitSha = Annotated[str, "Commit SHA or unique prefix to draft a conventional commit for."]
CommitShas = Annotated[list[str] | None, "Optional ordered commit SHAs or unique prefixes to infer. Defaults to every commit in the range."]
EvidenceMode = Annotated[
    Literal["diff-only", "subject-hint"],
    "Use diff-only to omit authored commit text, or subject-hint to include the authored subject/message as model evidence.",
]
GuidanceName = Annotated[
    Literal["readme", "agent", "architecture", "copilot", "skill"],
    "Workspace guidance document to read.",
]


class _ExitSafeService:
    """Proxy that turns `SystemExit` from CLI helpers into ordinary errors.

    The CLI helpers behind the service report user errors with `SystemExit`, which FastMCP does
    not catch: it escaped the tool call and left the stdio server without an answer.
    """

    def __init__(self, service: Any) -> None:
        self._service = service

    def __getattr__(self, name: str) -> Any:
        attribute = getattr(self._service, name)
        if not callable(attribute):
            return attribute

        def call(*args: Any, **kwargs: Any) -> Any:
            try:
                return attribute(*args, **kwargs)
            except SystemExit as exc:
                raise RuntimeError(str(exc.code if exc.code is not None else exc)) from None

        return call


def _register_server_components(server, svc: MCPService) -> None:
    svc = _ExitSafeService(svc)  # type: ignore[assignment]
    def _overview_payload() -> dict[str, Any]:
        return {
            "name": "verlato",
            "version": __version__,
            "instructions": SERVER_INSTRUCTIONS,
            "tools": [tool.name for tool in _active_tool_metadata()],
            "change_evidence": "git-patch" if legacy_patch_tools_enabled() else "intentumdiff-semantic-diff",
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
            "3. To understand or rewrite an item, call verlato_get_suggestion_item and "
            "verlato_get_file_semantic_diff. Base summaries on the IntentumDiff semantic diff: describe meaningful "
            "changes and never present moved code, refactorings or formatting as new behavior.\n"
            "4. If needed, call verlato_update_review_state to refine item status or summary.\n"
            "5. Call verlato_apply_draft only when you are ready to write the chosen target file."
        )

    # FastMCP converts prompt arguments with the raw signature annotations, which are unresolved
    # strings ("GitRef") under `from __future__ import annotations`; resolve them first.
    verlato_review_request.__annotations__ = typing.get_type_hints(verlato_review_request, include_extras=True)
    server.prompt(
        title="Verlato Review Request",
        description=_PROMPT_METADATA[0].description,
        tags={"verlato", "review", "release-notes"},
        version=__version__,
    )(verlato_review_request)

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
            "title": "Get File Semantic Diff",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        tags={"verlato", "suggestions", "diff", "semantic"},
        version=__version__,
    )
    def verlato_get_file_semantic_diff(
        base: GitRef,
        item_id: ItemId,
        path: Annotated[str, "Changed file path."],
        head: GitRef = "HEAD",
    ) -> dict[str, Any]:
        """Return the IntentumDiff semantic diff for one file in one suggestion item."""

        return svc.get_file_semantic_diff(base=base, head=head, item_id=item_id, path=path)

    @server.tool(
        annotations={
            "title": "Search Semantic Diff",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": False,
        },
        tags={"verlato", "suggestions", "diff", "semantic"},
        version=__version__,
    )
    def verlato_search_semantic_diff(
        base: GitRef,
        item_id: ItemId,
        query: Annotated[str, "Case-insensitive symbol or term to find in the semantic diff."],
        head: GitRef = "HEAD",
        limit: SearchLimit = 5,
    ) -> dict[str, Any]:
        """Search the IntentumDiff semantic diff of one suggestion item."""

        return svc.search_semantic_diff(base=base, head=head, item_id=item_id, query=query, limit=limit)

    if legacy_patch_tools_enabled():
        # Legacy `git` diff engine only: there is no semantic diff, so agents get bounded patches.
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

    @server.tool(
        annotations={
            "title": "Draft Conventional Commit",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        tags={"verlato", "commit", "model"},
        version=__version__,
    )
    def verlato_draft_conventional_commit(
        base: GitRef,
        sha: CommitSha,
        head: GitRef = "HEAD",
        model_provider: OptionalModelProvider = None,
        model_privacy: Annotated[
            Literal["metadata-only", "local-summary-then-ai", "trusted-provider-patch"] | None,
            "Optional privacy mode override for commit evidence.",
        ] = None,
        allow_remote_patches: Annotated[
            bool,
            "Allow raw patch excerpts to be sent to a configured remote provider for this request.",
        ] = False,
        evidence_mode: EvidenceMode = "diff-only",
    ) -> dict[str, Any]:
        """Infer or draft a full Conventional Commits message for one commit in a git range."""

        return svc.draft_conventional_commit(
            base=base,
            head=head,
            sha=sha,
            model_provider=model_provider,
            model_privacy=model_privacy,
            allow_remote_patches=allow_remote_patches,
            evidence_mode=evidence_mode,
        )

    @server.tool(
        annotations={
            "title": "Draft Conventional Commits",
            "readOnlyHint": True,
            "idempotentHint": True,
            "openWorldHint": True,
        },
        tags={"verlato", "commit", "model"},
        version=__version__,
    )
    def verlato_draft_conventional_commits(
        base: GitRef,
        shas: CommitShas = None,
        head: GitRef = "HEAD",
        model_provider: OptionalModelProvider = None,
        model_privacy: Annotated[
            Literal["metadata-only", "local-summary-then-ai", "trusted-provider-patch"] | None,
            "Optional privacy mode override for commit evidence.",
        ] = None,
        allow_remote_patches: Annotated[
            bool,
            "Allow raw patch excerpts to be sent to a configured remote provider for this request.",
        ] = False,
        evidence_mode: EvidenceMode = "diff-only",
    ) -> dict[str, Any]:
        """Infer or draft Conventional Commits messages for commits in a git range sequentially."""

        return svc.draft_conventional_commits(
            base=base,
            head=head,
            shas=shas,
            model_provider=model_provider,
            model_privacy=model_privacy,
            allow_remote_patches=allow_remote_patches,
            evidence_mode=evidence_mode,
        )


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


if __name__ == "__main__":  # pragma: no cover - exercised by MCP clients
    raise SystemExit(main())
