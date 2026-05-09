from __future__ import annotations

import re
from collections.abc import Iterable

SECTION_ORDER = (
    "breaking",
    "added",
    "changed",
    "deprecated",
    "removed",
    "fixed",
    "security",
    "documentation",
    "performance",
    "refactoring",
    "tests",
    "build",
    "internal",
    "other",
)

SECTION_MAP = {
    "breaking": "Breaking Changes",
    "added": "Added",
    "changed": "Changed",
    "deprecated": "Deprecated",
    "removed": "Removed",
    "fixed": "Fixed",
    "security": "Security",
    "documentation": "Documentation",
    "performance": "Performance",
    "refactoring": "Refactoring",
    "tests": "Tests",
    "build": "Build",
    "internal": "Internal",
    "other": "Other",
}

CHANGE_KIND_ALIASES = {
    "add": "added",
    "added": "added",
    "feat": "added",
    "feature": "added",
    "features": "added",
    "new": "added",
    "change": "changed",
    "changed": "changed",
    "breaking": "breaking",
    "breaking-change": "breaking",
    "breaking-changes": "breaking",
    "major": "breaking",
    "deprecate": "deprecated",
    "deprecated": "deprecated",
    "deprecation": "deprecated",
    "deprecations": "deprecated",
    "remove": "removed",
    "removed": "removed",
    "removal": "removed",
    "delete": "removed",
    "deleted": "removed",
    "fix": "fixed",
    "fixed": "fixed",
    "bugfix": "fixed",
    "bug-fix": "fixed",
    "security": "security",
    "sec": "security",
    "doc": "documentation",
    "docs": "documentation",
    "documentation": "documentation",
    "perf": "performance",
    "performance": "performance",
    "refactor": "refactoring",
    "refactoring": "refactoring",
    "test": "tests",
    "tests": "tests",
    "testing": "tests",
    "build": "build",
    "ci": "build",
    "deps": "build",
    "dependencies": "build",
    "internal": "internal",
    "chore": "internal",
    "maintenance": "internal",
    "style": "internal",
    "styles": "internal",
    "other": "other",
}

VALID_CHANGE_KINDS = set(SECTION_ORDER)


def normalize_change_kind(value: str | None) -> str | None:
    if not value:
        return None
    normalized = re.sub(r"[^a-z0-9]+", "-", value.strip().lower()).strip("-")
    return CHANGE_KIND_ALIASES.get(normalized)


def changelog_section_title(kind: str) -> str:
    normalized = normalize_change_kind(kind) or kind
    if normalized in SECTION_MAP:
        return SECTION_MAP[normalized]
    return re.sub(r"[-_]+", " ", kind).strip().title() or "Other"


def ordered_change_kinds(kinds: Iterable[str]) -> list[str]:
    unique = {kind for kind in kinds if kind}
    ordered = [kind for kind in SECTION_ORDER if kind in unique]
    ordered.extend(sorted(unique.difference(SECTION_ORDER)))
    return ordered
