from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, replace
from datetime import date

from releasepilot.config import OutputTargetConfig, ReleasePilotConfig, resolve_target_path
from releasepilot.core.generator import MarkdownGenerator, SECTION_MAP
from releasepilot.core.models import ChangeItem


@dataclass
class DraftTarget:
    name: str
    path: str
    format: str


@dataclass
class DraftItem:
    id: str
    title: str
    summary: str
    kind: str
    area: str
    audience: str
    semver: str
    confidence: float
    pr_number: int | None
    base_ref: str
    head_ref: str
    files: list[str]
    commit_signals: list[str]
    status: str
    reviewed_by: str | None
    reviewed_at: str | None
    edited_summary: str | None
    hidden: bool


@dataclass
class DraftSection:
    kind: str
    title: str
    items: list[DraftItem] = field(default_factory=list)


@dataclass
class DraftDocument:
    version: str
    generated_on: str
    base_ref: str
    head_ref: str
    targets: list[DraftTarget]
    sections: list[DraftSection]
    rendered: dict[str, str]
    item_count: int

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ApplyResult:
    target: str
    path: str
    format: str
    action: str
    bytes_written: int

    def to_dict(self) -> dict:
        return asdict(self)


class DraftService:
    def __init__(self, config: ReleasePilotConfig | None = None) -> None:
        self.config = config or ReleasePilotConfig()
        self.generator = MarkdownGenerator()

    def build_document(
        self,
        *,
        items: list[ChangeItem],
        base_ref: str,
        head_ref: str,
        version: str,
        include_confidence: bool = False,
        include_provenance: bool = False,
    ) -> DraftDocument:
        targets = [
            DraftTarget(name=name, path=target.path, format=target.format)
            for name, target in sorted(self.config.targets.items())
        ]
        sections = self._build_sections(items)
        visible_items = [item for item in items if not item.hidden]
        render_items = [self._apply_review_content(item) for item in visible_items]
        rendered = {
            "github-release": self.generator.render_github_release(
                render_items,
                version=version,
                include_confidence=include_confidence,
                include_provenance=include_provenance,
            ),
            "changelog": self.generator.render_changelog_entry(
                render_items,
                version=version,
                include_confidence=include_confidence,
                include_provenance=include_provenance,
            ),
        }
        return DraftDocument(
            version=version,
            generated_on=date.today().isoformat(),
            base_ref=base_ref,
            head_ref=head_ref,
            targets=targets,
            sections=sections,
            rendered=rendered,
            item_count=len(items),
        )

    def apply_document(
        self,
        *,
        document: DraftDocument,
        target_name: str,
        target_path: str | None = None,
        cwd: str | None = None,
    ) -> ApplyResult:
        target = self._resolve_target(target_name, target_path=target_path)
        path = resolve_target_path(target.path, cwd=cwd)
        path.parent.mkdir(parents=True, exist_ok=True)

        rendered = document.rendered[target.format]
        existing = path.read_text() if path.exists() else ""
        if target.format == "changelog":
            updated, action = self._apply_changelog(existing, rendered, document.version)
        else:
            updated, action = rendered, "replace"

        path.write_text(updated)
        return ApplyResult(
            target=target_name,
            path=str(path),
            format=target.format,
            action=action,
            bytes_written=len(updated.encode("utf-8")),
        )

    def _build_sections(self, items: list[ChangeItem]) -> list[DraftSection]:
        grouped: dict[str, list[DraftItem]] = {}
        for item in items:
            grouped.setdefault(item.classification.kind, []).append(
                DraftItem(
                    id=item.id,
                    title=item.content.title,
                    summary=item.edited_summary or item.content.summary,
                    kind=item.classification.kind,
                    area=item.classification.area,
                    audience=item.classification.audience,
                    semver=item.classification.semver,
                    confidence=item.confidence,
                    pr_number=item.source.pr_number,
                    base_ref=item.source.base_ref,
                    head_ref=item.source.head_ref,
                    files=item.evidence.files,
                    commit_signals=item.evidence.commit_signals,
                    status="hidden" if item.hidden else item.status,
                    reviewed_by=item.reviewed_by,
                    reviewed_at=item.reviewed_at,
                    edited_summary=item.edited_summary,
                    hidden=item.hidden,
                )
            )

        sections: list[DraftSection] = []
        for key in ["added", "changed", "fixed", "removed", "security", "internal"]:
            if key in grouped:
                sections.append(DraftSection(kind=key, title=SECTION_MAP[key], items=grouped[key]))
        return sections

    def _resolve_target(self, target_name: str, *, target_path: str | None = None) -> OutputTargetConfig:
        if target_path:
            if target_name == "github-release":
                return OutputTargetConfig(path=target_path, format="github-release")
            return OutputTargetConfig(path=target_path, format="changelog")

        target = self.config.targets.get(target_name)
        if not target:
            raise ValueError(f"Unknown draft target: {target_name}")
        return target

    def _apply_changelog(self, existing: str, rendered: str, version: str) -> tuple[str, str]:
        if re.search(rf"^### {re.escape(version)} \([^)]+\)$", existing, flags=re.MULTILINE):
            pattern = re.compile(rf"^### {re.escape(version)} \([^)]+\)\n.*?(?=^### |\Z)", re.MULTILINE | re.DOTALL)
            return pattern.sub(rendered.rstrip() + "\n\n", existing, count=1), "replace"

        if not existing.strip():
            return rendered, "create"

        lines = existing.splitlines()
        if lines and lines[0].startswith("#"):
            prefix = "\n".join(lines[:1]) + "\n\n"
            suffix = "\n".join(lines[1:]).lstrip("\n")
            body = rendered.rstrip() + "\n"
            updated = prefix + body
            if suffix:
                updated += "\n" + suffix
            return updated, "insert"

        return rendered.rstrip() + "\n\n" + existing.lstrip("\n"), "prepend"

    def _apply_review_content(self, item: ChangeItem) -> ChangeItem:
        if not item.edited_summary:
            return item
        return replace(item, content=replace(item.content, summary=item.edited_summary))
