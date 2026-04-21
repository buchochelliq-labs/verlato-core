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
    def render_github_release(self, items: list[ChangeItem], version: str = "Unreleased") -> str:
        lines = [f"## {version} - {date.today().isoformat()}", ""]
        lines.extend(self._render_sections(items))
        return "\n".join(lines).strip() + "\n"

    def render_changelog_entry(self, items: list[ChangeItem], version: str = "Unreleased") -> str:
        lines = [f"### {version} ({date.today().isoformat()})", ""]
        lines.extend(self._render_sections(items, include_summary=True))
        return "\n".join(lines).strip() + "\n"

    def _render_sections(self, items: list[ChangeItem], include_summary: bool = False) -> list[str]:
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
                if include_summary:
                    lines.append(f"- {item.content.title}{source}: {item.content.summary}")
                else:
                    lines.append(f"- {item.content.title}{source}")
            lines.append("")
        return lines
