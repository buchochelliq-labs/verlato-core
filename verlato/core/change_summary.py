from __future__ import annotations

import re
from collections import Counter
from typing import Any

from verlato.core.commit_intent import (
    commit_intent_payload,
    conventional_commit_message,
    effective_conventional_commit,
)
from verlato.core.model_privacy import OMITTED_PATCH_EVIDENCE, estimate_token_count
from verlato.core.models import ChangedFile, ChangeGroup, ChangeItem, DiffEvidence, changed_files_for_group

MAX_CHANGE_SUMMARY_COMMITS = 12
MAX_CHANGE_SUMMARY_FILES = 80
MAX_PUBLIC_SYMBOLS_PER_FILE = 8
MAX_PUBLIC_SYMBOL_FILES = 25

_SYMBOL_PATTERNS = (
    re.compile(r"^\+\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
    re.compile(r"^\+\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
    re.compile(
        r"^\+\s*export\s+(?:default\s+)?(?:async\s+)?"
        r"(?:function|class|interface|type|const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\b"
    ),
    re.compile(r"^\+\s*(?:public\s+)?(?:static\s+)?(?:async\s+)?[A-Za-z_$][A-Za-z0-9_$<>[\], ?]*\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\("),
)


def build_change_summary_payload(
    *,
    group: ChangeGroup,
    item: ChangeItem | None = None,
    diff: DiffEvidence,
    local_summary: str | None = None,
) -> dict[str, Any]:
    changed_files = changed_files_for_group(group)
    raw_patch_chars = raw_patch_chars_available(changed_files)
    status_counts = Counter(file.status or "modified" for file in changed_files)
    public_symbols = _public_symbols(changed_files)
    commits = [_commit_payload(commit) for commit in group.commits[:MAX_CHANGE_SUMMARY_COMMITS]]
    files = [
        _file_payload(file, diff=diff, public_symbols=public_symbols.get(file.path, []))
        for file in changed_files[:MAX_CHANGE_SUMMARY_FILES]
    ]
    omitted_fields = list(OMITTED_PATCH_EVIDENCE)
    payload: dict[str, Any] = {
        "schema": "verlato.change-summary.v1",
        "title": item.content.title if item else (group.pr.title if group.pr else _first_subject(group)),
        "current_summary": item.content.summary if item else "",
        "classification": item.classification.__dict__ if item else None,
        "pull_request": _pr_payload(group),
        "commit_boundaries": _commit_boundaries(group.commits),
        "commits": commits,
        "commits_total": len(group.commits),
        "commits_omitted": max(len(group.commits) - len(commits), 0),
        "files": files,
        "files_total": len(changed_files),
        "files_omitted": max(len(changed_files) - len(files), 0),
        "file_status_counts": dict(sorted(status_counts.items())),
        "areas": {area: sorted(paths) for area, paths in sorted(diff.files_by_area.items())},
        "deterministic_facts": deterministic_diff_facts(diff=diff, files=changed_files),
        "local_summary": (local_summary or "").strip() or None,
        "privacy": {
            "raw_patch_chars_available": raw_patch_chars,
            "raw_patch_chars_sent": 0,
            "omitted_fields": omitted_fields,
            "omission_reasons": [
                "metadata-only evidence is the default",
                "raw patch text is reserved for local or explicitly trusted patch modes",
            ],
        },
    }
    payload["size"] = {
        "json_chars": len(_jsonish(payload)),
        "estimated_tokens": estimate_token_count(len(_jsonish(payload))),
    }
    return payload


def deterministic_diff_facts(*, diff: DiffEvidence, files: list[ChangedFile]) -> dict[str, Any]:
    paths = [file.path for file in files]
    added = sorted(file.path for file in files if (file.status or "").lower() in {"added", "a"})
    removed = sorted(file.path for file in files if (file.status or "").lower() in {"removed", "deleted", "d"})
    renamed = sorted(file.path for file in files if (file.status or "").lower() in {"renamed", "r"})
    facts = {
        "docs_only": diff.docs_only,
        "tests_only": diff.tests_only,
        "api_changes": sorted(diff.api_changes),
        "migration_changes": sorted(diff.migration_changes),
        "config_changes": sorted(diff.config_changes),
        "added_files": added,
        "removed_files": removed,
        "renamed_files": renamed,
        "docs_changed": any(_looks_like_docs_path(path) for path in paths),
        "tests_changed": any(_looks_like_test_path(path) for path in paths),
        "build_changed": any(_looks_like_build_path(path) for path in paths),
        "config_changed": bool(diff.config_changes) or any(_looks_like_config_path(path) for path in paths),
        "migration_changed": bool(diff.migration_changes),
        "api_looking_changed": bool(diff.api_changes) or any(_looks_like_api_path(path) for path in paths),
        "raw_patch_chars_available": raw_patch_chars_available(files),
        "raw_patch_chars_sent": 0,
    }
    return facts


def raw_patch_chars_available(files: list[ChangedFile]) -> int:
    return sum(len(file.patch or "") for file in files)


def _commit_payload(commit) -> dict[str, Any]:
    parsed, inferred_used = effective_conventional_commit(commit)
    derived = commit_intent_payload(getattr(commit, "derived_intent", None))
    return {
        "sha": commit.sha,
        "subject": commit.message.splitlines()[0] if commit.message else "",
        "message": commit.message,
        "author": commit.author,
        "previous_sha": getattr(commit, "parent_sha", None),
        "initial_commit": getattr(commit, "parent_sha", None) is None,
        "authored_or_inferred_conventional": {
            "type": parsed.type,
            "scope": parsed.scope,
            "description": parsed.description,
            "breaking": parsed.breaking,
            "source": "inferred" if inferred_used else "authored",
        }
        if parsed
        else None,
        "inferred_intent": commit_intent_payload(commit.inferred_intent),
        "derived_conventional": derived,
        "files_total": len(commit.files),
    }


def _commit_boundaries(commits: list) -> dict[str, Any]:
    if not commits:
        return {"first": None, "last": None}
    return {
        "first": _commit_boundary_payload(commits[0]),
        "last": _commit_boundary_payload(commits[-1]),
    }


def _commit_boundary_payload(commit) -> dict[str, Any]:
    derived = getattr(commit, "derived_intent", None)
    return {
        "sha": commit.sha,
        "subject": commit.message.splitlines()[0] if commit.message else "",
        "previous_sha": getattr(commit, "parent_sha", None),
        "initial_commit": getattr(commit, "parent_sha", None) is None,
        "derived_conventional": conventional_commit_message(derived) if derived else None,
    }


def _file_payload(file: ChangedFile, *, diff: DiffEvidence, public_symbols: list[str]) -> dict[str, Any]:
    return {
        "path": file.path,
        "status": file.status,
        "area": _area_for_path(file.path, diff),
        "patch_chars_available": len(file.patch or ""),
        "public_symbols": public_symbols,
        "signals": {
            "docs": _looks_like_docs_path(file.path),
            "tests": _looks_like_test_path(file.path),
            "build": _looks_like_build_path(file.path),
            "config": _looks_like_config_path(file.path) or file.path in diff.config_changes,
            "migration": file.path in diff.migration_changes,
            "api": file.path in diff.api_changes or _looks_like_api_path(file.path),
        },
    }


def _pr_payload(group: ChangeGroup) -> dict[str, Any] | None:
    if group.pr is None:
        return None
    return {
        "number": group.pr.number,
        "title": group.pr.title,
        "body": group.pr.body,
        "labels": list(group.pr.labels),
    }


def _public_symbols(files: list[ChangedFile]) -> dict[str, list[str]]:
    result: dict[str, list[str]] = {}
    symbol_file_count = 0
    for file in files:
        if symbol_file_count >= MAX_PUBLIC_SYMBOL_FILES:
            break
        symbols: list[str] = []
        for line in (file.patch or "").splitlines():
            if not line.startswith("+") or line.startswith("+++"):
                continue
            for pattern in _SYMBOL_PATTERNS:
                match = pattern.match(line)
                if match:
                    symbol = match.group(1)
                    if symbol not in symbols:
                        symbols.append(symbol)
                    break
            if len(symbols) >= MAX_PUBLIC_SYMBOLS_PER_FILE:
                break
        if symbols:
            result[file.path] = symbols
            symbol_file_count += 1
    return result


def _area_for_path(path: str, diff: DiffEvidence) -> str | None:
    for area, paths in diff.files_by_area.items():
        if path in paths:
            return area
    return None


def _first_subject(group: ChangeGroup) -> str:
    if not group.commits:
        return "Untitled change"
    return group.commits[0].message.splitlines()[0] if group.commits[0].message else "Untitled change"


def _looks_like_docs_path(path: str) -> bool:
    normalized = path.lower()
    return normalized.startswith("docs/") or normalized.endswith(".md") or "readme" in normalized


def _looks_like_test_path(path: str) -> bool:
    normalized = path.lower()
    return "/test" in normalized or "\\test" in normalized or normalized.startswith("test") or "_test." in normalized


def _looks_like_build_path(path: str) -> bool:
    normalized = path.lower()
    names = ("package.json", "pyproject.toml", "cargo.toml", "go.mod", "pom.xml", "build.gradle")
    return normalized.endswith(names) or ".github/workflows/" in normalized or normalized.startswith(".github/workflows/")


def _looks_like_config_path(path: str) -> bool:
    normalized = path.lower()
    return normalized.endswith((".json", ".yaml", ".yml", ".toml", ".ini", ".cfg")) or normalized.startswith(".github/")


def _looks_like_api_path(path: str) -> bool:
    normalized = path.lower()
    return any(segment in normalized for segment in ("/api/", "\\api\\", "/sdk/", "\\sdk\\", "client", "provider"))


def _jsonish(value: Any) -> str:
    import json

    return json.dumps(value, sort_keys=True, default=str)
