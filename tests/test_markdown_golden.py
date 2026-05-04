from datetime import date
from pathlib import Path

from releasepilot.core.generator import MarkdownGenerator
from releasepilot.core.models import ChangeItem, Classification, Content, Evidence, SourceRef


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
    monkeypatch.setattr("releasepilot.core.generator.date", _FixedDate)
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
    monkeypatch.setattr("releasepilot.core.generator.date", _FixedDate)
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
    monkeypatch.setattr("releasepilot.core.generator.date", _FixedDate)

    rendered = MarkdownGenerator().render_github_release([], version="v2.0.0")

    assert rendered == (
        "## v2.0.0 - 2026-04-22\n"
        "\n"
        "### No Changes\n"
        "- No notable changes were detected for this range.\n"
    )
