from datetime import date
from pathlib import Path

from verlato.core.generator import MarkdownGenerator
from verlato.core.models import ChangeItem, Classification, Content, Evidence, SourceRef


class _FixedDate(date):
    @classmethod
    def today(cls):
        return cls(2026, 4, 22)


def _item(
    item_id: str,
    pr_number: int,
    kind: str,
    title: str,
    summary: str,
    confidence: float,
    lineage: list[str] | None = None,
) -> ChangeItem:
    return ChangeItem(
        id=item_id,
        source=SourceRef(pr_number=pr_number, commit_shas=[item_id], base_ref="v1.9.0", head_ref="main"),
        classification=Classification(kind=kind, area="api", audience="external", semver="patch"),
        content=Content(title=title, summary=summary),
        evidence=Evidence(),
        confidence=confidence,
        checksum=f"sum-{item_id}",
        lineage_releases=lineage or [],
    )


def test_github_release_golden(monkeypatch):
    monkeypatch.setattr("verlato.core.generator.date", _FixedDate)
    items = [
        _item("ci_a", 10, "added", "Add public API", "Adds endpoint support", 0.91, ["v1.8.0"]),
        _item("ci_b", 11, "fixed", "Patch login bug", "Fixes timeout regression", 0.77),
    ]
    rendered = MarkdownGenerator().render_github_release(
        items,
        version="v2.0.0",
        include_confidence=True,
        include_provenance=True,
    )
    expected = Path("tests/fixtures/golden/github_release.md").read_text().strip() + "\n"
    assert rendered == expected


def test_changelog_golden(monkeypatch):
    monkeypatch.setattr("verlato.core.generator.date", _FixedDate)
    items = [
        _item("ci_a", 10, "added", "Add public API", "Adds endpoint support", 0.91, ["v1.8.0"]),
        _item("ci_b", 11, "fixed", "Patch login bug", "Fixes timeout regression", 0.77),
    ]
    rendered = MarkdownGenerator().render_changelog_entry(
        items,
        version="v2.0.0",
        include_confidence=True,
        include_provenance=True,
    )
    expected = Path("tests/fixtures/golden/changelog.md").read_text().strip() + "\n"
    assert rendered == expected


def test_github_release_empty_document_has_markdown_subheading(monkeypatch):
    monkeypatch.setattr("verlato.core.generator.date", _FixedDate)

    rendered = MarkdownGenerator().render_github_release([], version="v2.0.0")

    assert rendered == (
        "## v2.0.0 - 2026-04-22\n"
        "\n"
        "### No Changes\n"
        "- No notable changes were detected for this range.\n"
    )


def test_changelog_renders_supported_sections_in_canonical_order(monkeypatch):
    monkeypatch.setattr("verlato.core.generator.date", _FixedDate)
    items = [
        _item("ci_internal", 18, "internal", "Update repo chores", "Updates maintenance tasks", 0.71),
        _item("ci_removed", 14, "removed", "Remove old API", "Removes the old API", 0.91),
        _item("ci_tests", 17, "tests", "Cover auth", "Adds auth coverage", 0.81),
        _item("ci_breaking", 10, "breaking", "Replace auth API", "Changes the auth contract", 0.95),
        _item("ci_docs", 15, "documentation", "Document auth", "Documents auth setup", 0.82),
        _item("ci_perf", 16, "performance", "Speed up search", "Improves search latency", 0.88),
        _item("ci_deprecated", 13, "deprecated", "Deprecate tokens", "Deprecates legacy tokens", 0.86),
        _item("ci_build", 19, "build", "Update packaging", "Updates package metadata", 0.73),
        _item("ci_fixed", 12, "fixed", "Fix login", "Fixes login timeout", 0.89),
        _item("ci_added", 11, "added", "Add login", "Adds passwordless login", 0.9),
        _item("ci_security", 20, "security", "Patch token leak", "Tightens token handling", 0.92),
        _item("ci_refactor", 21, "refactoring", "Split renderer", "Splits release rendering", 0.78),
    ]

    rendered = MarkdownGenerator().render_changelog_entry(items, version="v2.0.0")

    headings = [line for line in rendered.splitlines() if line.startswith("### ")]
    assert headings == [
        "### v2.0.0 (2026-04-22)",
        "### Breaking Changes",
        "### Added",
        "### Deprecated",
        "### Removed",
        "### Fixed",
        "### Security",
        "### Documentation",
        "### Performance",
        "### Refactoring",
        "### Tests",
        "### Build",
        "### Internal",
    ]
