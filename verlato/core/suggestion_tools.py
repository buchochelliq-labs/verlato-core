from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from verlato.core.commit_intent import commit_intent_payload, conventional_commit_message, effective_conventional_commit
from verlato.core.diff_evidence import DiffEvidenceBuilder
from verlato.core.models import ChangedFile, ChangeGroup, ChangeItem, changed_files_for_group
from verlato.core.tooling import ToolDefinition, ToolProvider

DEFAULT_PATCH_CHUNK_CHARS = 12_000
MAX_PATCH_CHUNK_CHARS = 24_000
MAX_SEARCH_RESULTS = 8


@dataclass
class SuggestionEvidenceItem:
    group: ChangeGroup
    item: ChangeItem
    files: list[ChangedFile]
    diff: Any
    patch_chars: int
    missing_patch_count: int

    @property
    def id(self) -> str:
        return self.item.id


class SuggestionEvidenceToolProvider(ToolProvider):
    def __init__(self, entries: list[SuggestionEvidenceItem]) -> None:
        self.entries = entries
        self._by_id = {entry.id: entry for entry in entries}

    @classmethod
    def from_groups(cls, groups: list[ChangeGroup], items: list[ChangeItem]) -> SuggestionEvidenceToolProvider:
        diff_builder = DiffEvidenceBuilder()
        entries: list[SuggestionEvidenceItem] = []
        for group, item in zip(groups, items, strict=False):
            files = changed_files_for_group(group)
            diff = diff_builder.build(files)
            patch_chars = sum(len(file.patch or "") for file in files)
            missing_patch_count = sum(1 for file in files if not file.patch)
            entries.append(
                SuggestionEvidenceItem(
                    group=group,
                    item=item,
                    files=files,
                    diff=diff,
                    patch_chars=patch_chars,
                    missing_patch_count=missing_patch_count,
                )
            )
        return cls(entries)

    def list_tools(self) -> list[ToolDefinition]:
        return [
            ToolDefinition(
                name="verlato_list_suggestion_items",
                description="List compact release-note suggestion items and changed-file manifests for the active git range.",
                input_schema={
                    "type": "object",
                    "properties": {
                        "include_files": {
                            "type": "boolean",
                            "description": "Include per-file path, status, and patch-size metadata.",
                        }
                    },
                },
            ),
            ToolDefinition(
                name="verlato_get_suggestion_item",
                description="Return metadata, commits, file manifest, and deterministic diff summary for one suggestion item.",
                input_schema={
                    "type": "object",
                    "required": ["item_id"],
                    "properties": {
                        "item_id": {"type": "string", "description": "Suggestion item id."},
                    },
                },
            ),
            ToolDefinition(
                name="verlato_get_file_patch",
                description="Return a bounded patch chunk for one file in one suggestion item.",
                input_schema={
                    "type": "object",
                    "required": ["item_id", "path"],
                    "properties": {
                        "item_id": {"type": "string", "description": "Suggestion item id."},
                        "path": {"type": "string", "description": "Changed file path."},
                        "offset": {"type": "integer", "minimum": 0, "description": "Character offset into the patch."},
                        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_PATCH_CHUNK_CHARS},
                    },
                },
            ),
            ToolDefinition(
                name="verlato_search_item_diff",
                description="Search patch text for one suggestion item and return small matching snippets.",
                input_schema={
                    "type": "object",
                    "required": ["item_id", "query"],
                    "properties": {
                        "item_id": {"type": "string", "description": "Suggestion item id."},
                        "query": {"type": "string", "description": "Case-insensitive text to search in patches."},
                        "limit": {"type": "integer", "minimum": 1, "maximum": MAX_SEARCH_RESULTS},
                    },
                },
            ),
        ]

    def call_tool(self, name: str, arguments: dict[str, Any]) -> Any:
        if name == "verlato_list_suggestion_items":
            return self.list_suggestion_items(include_files=bool(arguments.get("include_files", False)))
        if name == "verlato_get_suggestion_item":
            return self.get_suggestion_item(item_id=_required_string(arguments, "item_id"))
        if name == "verlato_get_file_patch":
            return self.get_file_patch(
                item_id=_required_string(arguments, "item_id"),
                path=_required_string(arguments, "path"),
                offset=_coerce_int(arguments.get("offset"), default=0, minimum=0),
                limit=_coerce_int(
                    arguments.get("limit"),
                    default=DEFAULT_PATCH_CHUNK_CHARS,
                    minimum=1,
                    maximum=MAX_PATCH_CHUNK_CHARS,
                ),
            )
        if name == "verlato_search_item_diff":
            return self.search_item_diff(
                item_id=_required_string(arguments, "item_id"),
                query=_required_string(arguments, "query"),
                limit=_coerce_int(arguments.get("limit"), default=5, minimum=1, maximum=MAX_SEARCH_RESULTS),
            )
        raise KeyError(f"Unknown suggestion evidence tool: {name}")

    def list_suggestion_items(self, *, include_files: bool = False) -> dict[str, Any]:
        return {
            "items": [
                self._item_manifest(entry, include_files=include_files)
                for entry in self.entries
            ]
        }

    def get_suggestion_item(self, *, item_id: str) -> dict[str, Any]:
        entry = self._entry(item_id)
        return {
            **self._item_manifest(entry, include_files=True),
            "labels": list(entry.group.pr.labels if entry.group.pr else []),
            "pr": _pr_payload(entry.group),
            "commit_boundaries": _commit_boundaries_payload(entry.group),
            "commits": _commit_payloads(entry.group),
            "diff_summary": _diff_summary_payload(entry.diff),
        }

    def get_file_patch(self, *, item_id: str, path: str, offset: int = 0, limit: int = DEFAULT_PATCH_CHUNK_CHARS) -> dict[str, Any]:
        entry = self._entry(item_id)
        changed_file = self._file(entry, path)
        patch = changed_file.patch or ""
        offset = min(max(offset, 0), len(patch))
        limit = min(max(limit, 1), MAX_PATCH_CHUNK_CHARS)
        chunk = patch[offset: offset + limit]
        return {
            "item_id": item_id,
            "path": changed_file.path,
            "status": changed_file.status,
            "patch_available": bool(patch),
            "patch_chars": len(patch),
            "offset": offset,
            "limit": limit,
            "next_offset": offset + len(chunk) if offset + len(chunk) < len(patch) else None,
            "truncated": offset + len(chunk) < len(patch),
            "patch": chunk,
        }

    def search_item_diff(self, *, item_id: str, query: str, limit: int = 5) -> dict[str, Any]:
        entry = self._entry(item_id)
        needle = query.strip().lower()
        results: list[dict[str, Any]] = []
        if not needle:
            return {"item_id": item_id, "query": query, "results": results}
        for changed_file in entry.files:
            lines = (changed_file.patch or "").splitlines()
            for index, line in enumerate(lines):
                if needle not in line.lower():
                    continue
                start = max(index - 2, 0)
                end = min(index + 3, len(lines))
                results.append(
                    {
                        "path": changed_file.path,
                        "line": index + 1,
                        "snippet": "\n".join(lines[start:end])[:1200],
                    }
                )
                if len(results) >= limit:
                    return {"item_id": item_id, "query": query, "results": results}
        return {"item_id": item_id, "query": query, "results": results}

    def full_diff_payload_item(self, item_id: str) -> dict[str, Any]:
        entry = self._entry(item_id)
        return {
            **self.get_suggestion_item(item_id=item_id),
            "files": [
                {
                    "path": changed_file.path,
                    "status": changed_file.status,
                    "patch": changed_file.patch or "",
                    "patch_chars": len(changed_file.patch or ""),
                    "patch_available": bool(changed_file.patch),
                    "patch_unavailable_reason": "" if changed_file.patch else "missing-or-unavailable",
                }
                for changed_file in entry.files
            ],
            "file_count": len(entry.files),
            "patch_chars": entry.patch_chars,
            "missing_patch_count": entry.missing_patch_count,
        }

    def _entry(self, item_id: str) -> SuggestionEvidenceItem:
        try:
            return self._by_id[item_id]
        except KeyError as exc:
            raise KeyError(f"Unknown suggestion item id: {item_id}") from exc

    @staticmethod
    def _file(entry: SuggestionEvidenceItem, path: str) -> ChangedFile:
        normalized = path.replace("\\", "/")
        for changed_file in entry.files:
            if changed_file.path.replace("\\", "/") == normalized:
                return changed_file
        raise KeyError(f"Unknown file path for suggestion item {entry.id}: {path}")

    @staticmethod
    def _item_manifest(entry: SuggestionEvidenceItem, *, include_files: bool) -> dict[str, Any]:
        item = entry.item
        payload: dict[str, Any] = {
            "item_id": item.id,
            "title": item.content.title,
            "current_summary": item.edited_summary or item.content.summary,
            "classification": {
                "kind": item.classification.kind,
                "area": item.classification.area,
                "audience": item.classification.audience,
                "semver": item.classification.semver,
            },
            "file_count": len(entry.files),
            "patch_chars": entry.patch_chars,
            "missing_patch_count": entry.missing_patch_count,
            "diff_summary": entry.diff.summary,
        }
        if include_files:
            payload["files"] = [
                {
                    "path": changed_file.path,
                    "status": changed_file.status,
                    "patch_chars": len(changed_file.patch or ""),
                    "patch_available": bool(changed_file.patch),
                }
                for changed_file in entry.files
            ]
        return payload


def _pr_payload(group: ChangeGroup) -> dict[str, Any] | None:
    if group.pr is None:
        return None
    return {
        "number": group.pr.number,
        "title": group.pr.title,
        "body": group.pr.body,
        "base_ref": group.pr.base_ref,
        "head_ref": group.pr.head_ref,
    }


def _commit_payloads(group: ChangeGroup) -> list[dict[str, Any]]:
    payloads: list[dict[str, Any]] = []
    for commit in group.commits:
        effective, used_inferred = effective_conventional_commit(commit)
        payload: dict[str, Any] = {
            "sha": commit.sha,
            "subject": commit.message.splitlines()[0],
            "message": commit.message,
            "author": commit.author,
            "previous_sha": commit.parent_sha,
            "initial_commit": commit.parent_sha is None,
        }
        if commit.inferred_intent:
            payload["inferred_conventional"] = commit_intent_payload(commit.inferred_intent)
        if commit.derived_intent:
            payload["derived_conventional"] = commit_intent_payload(commit.derived_intent)
        if effective and used_inferred:
            payload["effective_conventional"] = {
                "type": effective.type,
                "scope": effective.scope,
                "description": effective.description,
                "breaking": effective.breaking,
                "source": "inferred",
            }
        payloads.append(payload)
    return payloads


def _commit_boundaries_payload(group: ChangeGroup) -> dict[str, Any]:
    if not group.commits:
        return {"first": None, "last": None}
    return {
        "first": _commit_boundary_payload(group.commits[0]),
        "last": _commit_boundary_payload(group.commits[-1]),
    }


def _commit_boundary_payload(commit: Any) -> dict[str, Any]:
    return {
        "sha": commit.sha,
        "subject": commit.message.splitlines()[0] if commit.message else "",
        "previous_sha": commit.parent_sha,
        "initial_commit": commit.parent_sha is None,
        "derived_conventional": conventional_commit_message(commit.derived_intent) if commit.derived_intent else None,
    }


def _diff_summary_payload(diff: Any) -> dict[str, Any]:
    return {
        "summary": diff.summary,
        "files_by_area": diff.files_by_area,
        "file_statuses": diff.file_statuses,
        "api_changes": diff.api_changes,
        "migration_changes": diff.migration_changes,
        "config_changes": diff.config_changes,
    }


def _required_string(arguments: dict[str, Any], key: str) -> str:
    value = arguments.get(key)
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"Missing required string argument: {key}")
    return value


def _coerce_int(value: object, *, default: int, minimum: int, maximum: int | None = None) -> int:
    try:
        result = int(value) if value is not None else default
    except (TypeError, ValueError):
        result = default
    result = max(result, minimum)
    if maximum is not None:
        result = min(result, maximum)
    return result
