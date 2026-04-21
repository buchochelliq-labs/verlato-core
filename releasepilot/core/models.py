from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any


@dataclass
class Commit:
    sha: str
    message: str
    author: str = ""


@dataclass
class PullRequest:
    number: int
    title: str
    body: str = ""
    labels: list[str] = field(default_factory=list)
    files: list["ChangedFile"] = field(default_factory=list)
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

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @staticmethod
    def from_dict(data: dict[str, Any]) -> "ChangeItem":
        return ChangeItem(
            id=data["id"],
            source=SourceRef(**data["source"]),
            classification=Classification(**data["classification"]),
            content=Content(**data["content"]),
            evidence=Evidence(**data.get("evidence", {})),
            confidence=float(data.get("confidence", 0.5)),
            published=bool(data.get("published", False)),
            published_in=data.get("published_in"),
            checksum=data.get("checksum", ""),
        )


@dataclass
class DiffEvidence:
    files_by_area: dict[str, list[str]]
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
