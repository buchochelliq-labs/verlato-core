from __future__ import annotations

from collections import defaultdict
from datetime import date

from releasepilot.core.models import ChangeItem

SECTION_MAP = {
    "added": "Added",
    "changed": "Changed",
    "fixed": "Fixed",
    "removed": "Removed",
    "security": "Security",
    "internal": "Internal",
}


class MarkdownGenerator:
    def render_github_release(
        self,
        items: list[ChangeItem],
        version: str = "Unreleased",
        include_confidence: bool = False,
        include_provenance: bool = False,
    ) -> str:
        lines = [f"## {version} - {date.today().isoformat()}", ""]
        lines.extend(
            self._render_sections(
                items,
                include_confidence=include_confidence,
                include_provenance=include_provenance,
            )
        )
        return "\n".join(lines).strip() + "\n"

    def render_changelog_entry(
        self,
        items: list[ChangeItem],
        version: str = "Unreleased",
        include_confidence: bool = False,
        include_provenance: bool = False,
    ) -> str:
        lines = [f"### {version} ({date.today().isoformat()})", ""]
        lines.extend(
            self._render_sections(
                items,
                include_summary=True,
                include_confidence=include_confidence,
                include_provenance=include_provenance,
            )
        )
        return "\n".join(lines).strip() + "\n"

    def _render_sections(
        self,
        items: list[ChangeItem],
        include_summary: bool = False,
        include_confidence: bool = False,
        include_provenance: bool = False,
    ) -> list[str]:
        by_kind: dict[str, list[ChangeItem]] = defaultdict(list)
        for item in items:
            by_kind[item.classification.kind].append(item)

        lines: list[str] = []
        for key in ["added", "changed", "fixed", "removed", "security", "internal"]:
            section_items = by_kind.get(key, [])
            if not section_items:
                continue
            lines.append(f"### {SECTION_MAP[key]}")
            for item in section_items:
                source = f" (PR #{item.source.pr_number})" if item.source.pr_number else ""
                extras: list[str] = []
                if include_confidence:
                    extras.append(f"confidence {item.confidence:.2f}")
                if include_provenance:
                    extras.append(f"source {item.source.base_ref}..{item.source.head_ref}")
                    if item.lineage_releases:
                        extras.append(f"prior {', '.join(item.lineage_releases)}")
                suffix = f" [{'; '.join(extras)}]" if extras else ""
                if include_summary or item.content.summary:
                    lines.append(f"- {item.content.title}{source}: {item.content.summary}{suffix}")
                else:
                    lines.append(f"- {item.content.title}{source}{suffix}")
            lines.append("")
        return lines
