from __future__ import annotations

import os
from dataclasses import asdict, dataclass

from verlato import __version__
from verlato.cli.main import (
    _annotate_semantics,
    _build_adapter,
    _diff_engine_info,
    _can_send_raw_patches_for_config,
    _append_feedback_event,
    _build_suggestion_cache_store,
    _collect_groups,
    _update_draft_review,
    build_draft_document,
    run_scan,
)
from verlato.config import load_config
from verlato.core.classifier import Classifier
from verlato.core.collector import Collector
from verlato.core.commit_intent import draft_conventional_commit
from verlato.core.drafts import DraftDocument, DraftItem, DraftSection, DraftService, DraftTarget
from verlato.core.suggestion_tools import SuggestionEvidenceToolProvider
from verlato.core.model_privacy import resolve_model_privacy
from verlato.providers.factory import build_provider
from verlato.state.store import StateStore


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
        from verlato.mcp.server import list_prompt_metadata, list_resource_metadata, list_tool_metadata

        return {
            "status": "ok",
            "version": __version__,
            "diff_engine": _diff_engine_info(),
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

    def apply_draft_request(
        self,
        *,
        request: ReleasePreviewRequest,
        target_name: str,
        target_path: str | None = None,
    ) -> dict:
        return self.apply_draft_document(
            document=self.build_draft_document(request),
            target_name=target_name,
            target_path=target_path,
        )

    def update_review_state(
        self,
        *,
        base: str,
        head: str = "HEAD",
        item_id: str,
        status: str | None = None,
        summary: str | None = None,
        reviewer: str | None = None,
        model_provider: str | None = None,
        summary_provenance: dict[str, object] | None = None,
    ) -> dict:
        self.update_review_change_item(
            base=base,
            head=head,
            item_id=item_id,
            status=status,
            summary=summary,
            reviewer=reviewer,
            model_provider=model_provider,
            summary_provenance=summary_provenance,
        )
        document = self.get_draft_document(ReleasePreviewRequest(base=base, head=head))
        for section in document["sections"]:
            for item in section["items"]:
                if item["id"] == item_id:
                    return item
        raise KeyError(f"Updated review item {item_id} not found in draft document")

    def update_review_change_item(
        self,
        *,
        base: str,
        head: str = "HEAD",
        item_id: str,
        status: str | None = None,
        summary: str | None = None,
        reviewer: str | None = None,
        model_provider: str | None = None,
        summary_provenance: dict[str, object] | None = None,
    ) -> dict:
        updated = _update_draft_review(
            base=base,
            head=head,
            item_id=item_id,
            status=status,
            summary=summary,
            reviewer=reviewer,
            model_provider=model_provider,
            summary_provenance=summary_provenance,
        )
        return updated.to_dict()

    def request_model_input(
        self,
        *,
        base: str,
        head: str = "HEAD",
        item_id: str,
        reviewer: str | None = None,
        model_provider: str | None = None,
    ) -> dict:
        from verlato.cli.main import _request_draft_model_input

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

    def list_suggestion_items(
        self,
        *,
        base: str,
        head: str = "HEAD",
        include_files: bool = False,
    ) -> dict:
        return self._suggestion_tool_provider(base=base, head=head).list_suggestion_items(include_files=include_files)

    def get_suggestion_item(self, *, base: str, item_id: str, head: str = "HEAD") -> dict:
        return self._suggestion_tool_provider(base=base, head=head).get_suggestion_item(item_id=item_id)

    def get_file_patch(
        self,
        *,
        base: str,
        item_id: str,
        path: str,
        head: str = "HEAD",
        offset: int = 0,
        limit: int = 12_000,
    ) -> dict:
        return self._suggestion_tool_provider(base=base, head=head).get_file_patch(
            item_id=item_id,
            path=path,
            offset=offset,
            limit=limit,
        )

    def search_item_diff(
        self,
        *,
        base: str,
        item_id: str,
        query: str,
        head: str = "HEAD",
        limit: int = 5,
    ) -> dict:
        return self._suggestion_tool_provider(base=base, head=head).search_item_diff(
            item_id=item_id,
            query=query,
            limit=limit,
        )

    def draft_conventional_commit(
        self,
        *,
        base: str,
        sha: str,
        head: str = "HEAD",
        model_provider: str | None = None,
        model_privacy: str | None = None,
        allow_remote_patches: bool = False,
        evidence_mode: str = "diff-only",
    ) -> dict:
        commit = self._find_commit(base=base, head=head, sha=sha)
        _annotate_semantics([commit])
        context = self._conventional_draft_context(
            model_provider=model_provider,
            model_privacy=model_privacy,
            allow_remote_patches=allow_remote_patches,
        )
        draft = draft_conventional_commit(
            commit,
            context["provider"],
            privacy_mode=str(context["privacy_mode"]),
            provider_mode=str(context["provider_mode"]),
            evidence_mode=evidence_mode,
        )
        if draft is None:
            raise RuntimeError(f"No conventional commit draft could be generated for {sha}")
        return draft.to_dict()

    def draft_conventional_commits(
        self,
        *,
        base: str,
        head: str = "HEAD",
        shas: list[str] | None = None,
        model_provider: str | None = None,
        model_privacy: str | None = None,
        allow_remote_patches: bool = False,
        evidence_mode: str = "diff-only",
    ) -> dict:
        commits = Collector(pr_adapter=_build_adapter()).collect_commits(base, head)
        selected_commits, lookup_errors = self._select_commits(commits, shas)
        _annotate_semantics(selected_commits)
        context = self._conventional_draft_context(
            model_provider=model_provider,
            model_privacy=model_privacy,
            allow_remote_patches=allow_remote_patches,
        )
        results: list[dict] = []
        errors: list[dict[str, str]] = list(lookup_errors)
        for commit in selected_commits:
            try:
                draft = draft_conventional_commit(
                    commit,
                    context["provider"],
                    privacy_mode=str(context["privacy_mode"]),
                    provider_mode=str(context["provider_mode"]),
                    evidence_mode=evidence_mode,
                )
                if draft is None:
                    raise RuntimeError("No conventional commit draft could be generated")
                results.append(draft.to_dict())
            except Exception as exc:
                errors.append({"sha": commit.sha, "error": str(exc)})
        return {
            "base_ref": base,
            "head_ref": head,
            "count": len(selected_commits) + len(lookup_errors),
            "completed": len(results),
            "failed": len(errors),
            "results": results,
            "errors": errors,
        }

    def state_show(self) -> dict:
        return {"items": [asdict(item) for item in self._store().load().items]}

    def clear_working_state(self) -> dict:
        store = self._store()
        state = store.load()
        kept_items = [item for item in state.items if item.published]
        removed_ids = {item.id for item in state.items if not item.published}
        removed_feedback_count = 0
        if removed_ids:
            filtered_feedback = []
            for event in state.feedback_events:
                if event.get("item_id") in removed_ids:
                    removed_feedback_count += 1
                    continue
                filtered_feedback.append(event)
            state.feedback_events = filtered_feedback
        removed_count = len(state.items) - len(kept_items)
        state.items = kept_items
        store.save(state)
        return {
            "removed_working_items": removed_count,
            "removed_feedback_events": removed_feedback_count,
            "preserved_published_items": len(kept_items),
        }

    def clear_suggestion_cache(self) -> dict:
        cache = _build_suggestion_cache_store(load_config())
        removed_count = cache.clear()
        return {
            "removed_entries": removed_count,
            "path": str(cache.path),
        }

    def record_feedback(self, *, event: str, item_id: str, metadata: dict[str, object] | None = None) -> dict:
        _append_feedback_event(event=event, item_id=item_id, metadata=metadata or {})
        return {"recorded": True, "event": event, "item_id": item_id}

    def _store(self) -> StateStore:
        config = load_config()
        return StateStore(path=os.getenv("VERLATO_STATE_PATH", config.state_path))

    def _find_commit(self, *, base: str, head: str, sha: str):
        commits = Collector(pr_adapter=_build_adapter()).collect_commits(base, head)
        normalized = sha.strip().lower()
        for commit in commits:
            candidate = commit.sha.lower()
            if candidate == normalized or candidate.startswith(normalized) or normalized.startswith(candidate):
                return commit
        raise KeyError(f"Commit {sha} was not found in range {base}..{head}")

    def _conventional_draft_context(
        self,
        *,
        model_provider: str | None,
        model_privacy: str | None,
        allow_remote_patches: bool,
    ) -> dict[str, object]:
        config = load_config()
        privacy_mode = resolve_model_privacy(
            config.model_privacy,
            requested=model_privacy,
            allow_remote_patches=allow_remote_patches or os.getenv("VERLATO_ALLOW_REMOTE_PATCHES") == "1",
        )
        selected_model_provider = None if model_provider in {None, "configured"} else model_provider
        provider_mode = selected_model_provider or os.getenv("VERLATO_MODEL_PROVIDER") or config.provider.name
        dispatch_privacy_mode = (
            privacy_mode
            if _can_send_raw_patches_for_config(config.provider, provider_mode, privacy_mode)
            else "metadata-only"
        )
        return {
            "provider": build_provider(config.provider, mode=selected_model_provider),
            "provider_mode": provider_mode,
            "privacy_mode": dispatch_privacy_mode,
        }

    @staticmethod
    def _select_commits(commits, shas: list[str] | None):
        if not shas:
            return list(commits), []
        selected = []
        errors: list[dict[str, str]] = []
        for sha in shas:
            normalized = str(sha).strip().lower()
            match = None
            for commit in commits:
                candidate = commit.sha.lower()
                if candidate == normalized or candidate.startswith(normalized) or normalized.startswith(candidate):
                    match = commit
                    break
            if match is None:
                errors.append({"sha": str(sha), "error": f"Commit {sha} was not found in range"})
            else:
                selected.append(match)
        return selected, errors

    def _suggestion_tool_provider(self, *, base: str, head: str) -> SuggestionEvidenceToolProvider:
        groups = _collect_groups(base, head)
        classifier = Classifier()
        items = [classifier.classify(group, base_ref=base, head_ref=head)[0] for group in groups]
        return SuggestionEvidenceToolProvider.from_groups(groups, items)

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
                    derived_conventional_commits=[
                        entry for entry in item.get("derived_conventional_commits", []) if isinstance(entry, dict)
                    ],
                    grouping_reason=str(item.get("grouping_reason") or ""),
                    status=str(item["status"]),
                    reviewed_by=item.get("reviewed_by"),
                    reviewed_at=item.get("reviewed_at"),
                    edited_summary=item.get("edited_summary"),
                    summary_provenance=item.get("summary_provenance")
                    if isinstance(item.get("summary_provenance"), dict)
                    else None,
                    hidden=bool(item.get("hidden", False)),
                )
                for item in data.get("items", [])
            ],
        )
