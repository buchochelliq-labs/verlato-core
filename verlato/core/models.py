from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any

REVIEW_STATUS_VALUES = {"needs-review", "ready", "accepted", "edited", "hidden"}


def default_review_status(confidence: float) -> str:
    return "ready" if confidence >= 0.8 else "needs-review"


@dataclass
class CommitIntent:
    type: str
    scope: str | None
    description: str
    breaking: bool = False
    confidence: float = 0.0
    model: str = ""
    source: str = "model-inferred"
    rationale: str = ""


@dataclass
class Commit:
    sha: str
    message: str
    author: str = ""
    parent_sha: str | None = None
    files: list[ChangedFile] = field(default_factory=list)
    inferred_intent: CommitIntent | None = None
    derived_intent: CommitIntent | None = None


@dataclass
class PullRequest:
    number: int
    title: str
    body: str = ""
    labels: list[str] = field(default_factory=list)
    files: list[ChangedFile] = field(default_factory=list)
    base_ref: str = ""
    head_ref: str = ""


@dataclass
class ChangedFile:
    path: str
    status: str = "modified"
    patch: str = ""


@dataclass
class SourceRef:
    pr_number: int | None
    commit_shas: list[str]
    base_ref: str
    head_ref: str


@dataclass
class Classification:
    kind: str
    area: str
    audience: str
    semver: str


@dataclass
class Content:
    title: str
    summary: str


@dataclass
class Evidence:
    labels: list[str] = field(default_factory=list)
    files: list[str] = field(default_factory=list)
    commit_signals: list[str] = field(default_factory=list)
    diff_summary: dict[str, Any] = field(default_factory=dict)
    derived_conventional_commits: list[dict[str, Any]] = field(default_factory=list)


@dataclass
class ChangeItem:
    id: str
    source: SourceRef
    classification: Classification
    content: Content
    evidence: Evidence
    confidence: float
    published: bool = False
    published_in: str | None = None
    checksum: str = ""
    checksum_version: str = "v1"
    lineage_releases: list[str] = field(default_factory=list)
    status: str = "needs-review"
    reviewed_by: str | None = None
    reviewed_at: str | None = None
    edited_summary: str | None = None
    summary_provenance: dict[str, Any] | None = None
    hidden: bool = False

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> ChangeItem:
        confidence = float(data.get("confidence", 0.5))
        status = str(data.get("status") or default_review_status(confidence))
        if status not in REVIEW_STATUS_VALUES:
            status = default_review_status(confidence)
        hidden = bool(data.get("hidden", False) or status == "hidden")
        return ChangeItem(
            id=data["id"],
            source=SourceRef(**data["source"]),
            classification=Classification(**data["classification"]),
            content=Content(**data["content"]),
            evidence=Evidence(**data.get("evidence", {})),
            confidence=confidence,
            published=bool(data.get("published", False)),
            published_in=data.get("published_in"),
            checksum=data.get("checksum", ""),
            checksum_version=data.get("checksum_version", "v1"),
            lineage_releases=list(data.get("lineage_releases", [])),
            status=status,
            reviewed_by=data.get("reviewed_by"),
            reviewed_at=data.get("reviewed_at"),
            edited_summary=data.get("edited_summary"),
            summary_provenance=data.get("summary_provenance") if isinstance(data.get("summary_provenance"), dict) else None,
            hidden=hidden,
        )


@dataclass
class DiffEvidence:
    files_by_area: dict[str, list[str]]
    file_statuses: dict[str, str]
    api_changes: list[str]
    migration_changes: list[str]
    config_changes: list[str]
    docs_only: bool
    tests_only: bool
    representative_hunks: dict[str, list[str]]
    summary: str


@dataclass
class ChangeGroup:
    pr: PullRequest | None
    commits: list[Commit]
    files: list[ChangedFile] = field(default_factory=list)
    grouping_reason: str = ""


def changed_files_for_group(group: ChangeGroup) -> list[ChangedFile]:
    if group.pr is None:
        return [ChangedFile(path=f.path, status=f.status, patch=f.patch) for f in group.files]
    return merge_changed_files(group.pr.files, group.files)


def merge_changed_files(primary: list[ChangedFile], fallback: list[ChangedFile]) -> list[ChangedFile]:
    if not primary:
        return [ChangedFile(path=f.path, status=f.status, patch=f.patch) for f in fallback]

    fallback_by_path = {file.path: file for file in fallback}
    merged: list[ChangedFile] = []
    seen_paths: set[str] = set()
    for file in primary:
        fallback_file = fallback_by_path.get(file.path)
        merged.append(
            ChangedFile(
                path=file.path,
                status=file.status or (fallback_file.status if fallback_file else "modified"),
                patch=file.patch or (fallback_file.patch if fallback_file else ""),
            )
        )
        seen_paths.add(file.path)

    for file in fallback:
        if file.path not in seen_paths:
            merged.append(ChangedFile(path=file.path, status=file.status, patch=file.patch))

    return merged
