from __future__ import annotations

from dataclasses import dataclass

from releasepilot import __version__
from releasepilot.config import load_config
from releasepilot.cli.main import _update_draft_review, build_draft_document, run_scan
from releasepilot.core.drafts import DraftDocument, DraftItem, DraftSection, DraftService, DraftTarget
from releasepilot.state.store import StateStore


@dataclass
class ReleasePreviewRequest:
    base: str
    head: str = "HEAD"
    version: str = "Unreleased"
    include_confidence: bool = False
    include_provenance: bool = False
    model_provider: str | None = None


class MCPService:
    """
    Service interface suitable for MCP integration and local agent transports.
    """

    def list_unpublished_changes(self, base: str, head: str = "HEAD") -> list[dict]:
        return run_scan(base=base, head=head, persist=False)

    def health(self) -> dict:
        return {"status": "ok", "version": __version__}

    def info(self) -> dict:
        config = load_config()
        store = StateStore(path=config.state_path)
        state = store.load()
        from releasepilot.mcp.server import list_prompt_metadata, list_resource_metadata, list_tool_metadata

        return {
            "status": "ok",
            "version": __version__,
            "state_path": config.state_path,
            "schema_version": state.schema_version,
            "tracked_items": len(state.items),
            "targets": list(config.targets.keys()),
            "mcp_tools": [tool["name"] for tool in list_tool_metadata()],
            "mcp_resources": [resource["uri"] for resource in list_resource_metadata()],
            "mcp_prompts": [prompt["name"] for prompt in list_prompt_metadata()],
        }

    def get_draft_document(self, request: ReleasePreviewRequest) -> dict:
        document = build_draft_document(
            base=request.base,
            head=request.head,
            version=request.version,
            include_confidence=request.include_confidence,
            include_provenance=request.include_provenance,
            model_provider=request.model_provider,
        )
        if hasattr(document, "to_dict"):
            return document.to_dict()
        if hasattr(document, "__dict__"):
            data = dict(document.__dict__)
            if data:
                return data
        return {
            name: getattr(document, name)
            for name in dir(document)
            if not name.startswith("_") and not callable(getattr(document, name))
        }

    def build_draft_document(self, request: ReleasePreviewRequest) -> DraftDocument:
        return build_draft_document(
            base=request.base,
            head=request.head,
            version=request.version,
            include_confidence=request.include_confidence,
            include_provenance=request.include_provenance,
            model_provider=request.model_provider,
        )

    def preview_release_notes(self, request: ReleasePreviewRequest) -> str:
        document = self.get_draft_document(request)
        return str(document["rendered"]["github-release"])

    def apply_draft_document(self, *, document: dict | DraftDocument, target_name: str, target_path: str | None = None) -> dict:
        if isinstance(document, dict):
            draft_document = self._draft_document_from_dict(document)
        else:
            draft_document = document
        result = DraftService(config=load_config()).apply_document(
            document=draft_document,
            target_name=target_name,
            target_path=target_path,
        )
        return result.to_dict()

    def update_review_state(
        self,
        *,
        base: str,
        head: str = "HEAD",
        item_id: str,
        status: str | None = None,
        summary: str | None = None,
        reviewer: str | None = None,
    ) -> dict:
        _update_draft_review(
            base=base,
            head=head,
            item_id=item_id,
            status=status,
            summary=summary,
            reviewer=reviewer,
        )
        document = self.get_draft_document(ReleasePreviewRequest(base=base, head=head))
        for section in document["sections"]:
            for item in section["items"]:
                if item["id"] == item_id:
                    return item
        raise KeyError(f"Updated review item {item_id} not found in draft document")

    def request_model_input(
        self,
        *,
        base: str,
        head: str = "HEAD",
        item_id: str,
        reviewer: str | None = None,
        model_provider: str | None = None,
    ) -> dict:
        from releasepilot.cli.main import _request_draft_model_input

        _request_draft_model_input(
            base=base,
            head=head,
            item_id=item_id,
            reviewer=reviewer,
            model_provider=model_provider,
        )
        document = self.get_draft_document(ReleasePreviewRequest(base=base, head=head))
        for section in document["sections"]:
            for item in section["items"]:
                if item["id"] == item_id:
                    return item
        raise KeyError(f"Updated review item {item_id} not found in draft document")

    def reconcile_preview(self, base: str, head: str = "HEAD") -> dict:
        unpublished = self.list_unpublished_changes(base=base, head=head)
        return {"count": len(unpublished), "unpublished": unpublished}

    def _draft_document_from_dict(self, data: dict) -> DraftDocument:
        return DraftDocument(
            version=str(data["version"]),
            generated_on=str(data["generated_on"]),
            repository_url=data.get("repository_url"),
            base_ref=str(data["base_ref"]),
            head_ref=str(data["head_ref"]),
            targets=[
                self._draft_target_from_dict(target)
                for target in data.get("targets", [])
            ],
            sections=[
                self._draft_section_from_dict(section)
                for section in data.get("sections", [])
            ],
            rendered={str(key): str(value) for key, value in (data.get("rendered") or {}).items()},
            item_count=int(data.get("item_count", 0)),
        )

    @staticmethod
    def _draft_target_from_dict(data: dict):
        return DraftTarget(name=str(data["name"]), path=str(data["path"]), format=str(data["format"]))

    @staticmethod
    def _draft_section_from_dict(data: dict):
        return DraftSection(
            kind=str(data["kind"]),
            title=str(data["title"]),
            items=[
                DraftItem(
                    id=str(item["id"]),
                    title=str(item["title"]),
                    summary=str(item["summary"]),
                    original_summary=str(item.get("original_summary") or item["summary"]),
                    summary_source=str(item.get("summary_source") or ""),
                    classification_source=str(item.get("classification_source") or ""),
                    evidence_summary=str(item.get("evidence_summary") or ""),
                    kind=str(item["kind"]),
                    area=str(item["area"]),
                    audience=str(item["audience"]),
                    semver=str(item["semver"]),
                    confidence=float(item["confidence"]),
                    pr_number=item.get("pr_number"),
                    commit_shas=[str(sha) for sha in item.get("commit_shas", [])],
                    base_ref=str(item["base_ref"]),
                    head_ref=str(item["head_ref"]),
                    files=[str(path) for path in item.get("files", [])],
                    commit_signals=[str(signal) for signal in item.get("commit_signals", [])],
                    status=str(item["status"]),
                    reviewed_by=item.get("reviewed_by"),
                    reviewed_at=item.get("reviewed_at"),
                    edited_summary=item.get("edited_summary"),
                    hidden=bool(item.get("hidden", False)),
                )
                for item in data.get("items", [])
            ],
        )
