from __future__ import annotations

from dataclasses import dataclass

from releasepilot.cli.main import run_scan, scan_change_items
from releasepilot.core.generator import MarkdownGenerator


@dataclass
class ReleasePreviewRequest:
    base: str
    head: str = "HEAD"
    version: str = "Unreleased"
    include_confidence: bool = False
    include_provenance: bool = False


class MCPService:
    """
    Read-only service interface suitable for MCP integration.
    """

    def list_unpublished_changes(self, base: str, head: str = "HEAD") -> list[dict]:
        return run_scan(base=base, head=head, persist=False)

    def preview_release_notes(self, request: ReleasePreviewRequest) -> str:
        state_items = scan_change_items(request.base, request.head, persist=False)
        return MarkdownGenerator().render_github_release(
            state_items,
            version=request.version,
            include_confidence=request.include_confidence,
            include_provenance=request.include_provenance,
        )

    def reconcile_preview(self, base: str, head: str = "HEAD") -> dict:
        unpublished = self.list_unpublished_changes(base=base, head=head)
        return {"count": len(unpublished), "unpublished": unpublished}
