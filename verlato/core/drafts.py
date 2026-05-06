from __future__ import annotations

import re
import subprocess
from dataclasses import asdict, dataclass, field, replace
from datetime import date
from pathlib import Path

from verlato.config import OutputTargetConfig, VerlatoConfig, resolve_target_path
from verlato.core.generator import MarkdownGenerator, SECTION_MAP
from verlato.core.models import ChangeItem


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
    original_summary: str
    summary_source: str
    classification_source: str
    evidence_summary: str
    kind: str
    area: str
    audience: str
    semver: str
    confidence: float
    pr_number: int | None
    commit_shas: list[str]
    base_ref: str
    head_ref: str
    files: list[str]
    commit_signals: list[str]
    grouping_reason: str
    status: str
    reviewed_by: str | None
    reviewed_at: str | None
    edited_summary: str | None
    summary_provenance: dict | None
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
    repository_url: str | None
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
    def __init__(self, config: VerlatoConfig | None = None) -> None:
        self.config = config or VerlatoConfig()
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
            repository_url=self._repository_url(),
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
                    original_summary=item.content.summary,
                    summary_source=self._summary_source(item),
                    classification_source=self._classification_source(item),
                    evidence_summary=self._evidence_summary(item),
                    kind=item.classification.kind,
                    area=item.classification.area,
                    audience=item.classification.audience,
                    semver=item.classification.semver,
                    confidence=item.confidence,
                    pr_number=item.source.pr_number,
                    commit_shas=item.source.commit_shas,
                    base_ref=item.source.base_ref,
                    head_ref=item.source.head_ref,
                    files=item.evidence.files,
                    commit_signals=item.evidence.commit_signals,
                    grouping_reason=str(item.evidence.diff_summary.get("grouping_reason") or ""),
                    status="hidden" if item.hidden else item.status,
                    reviewed_by=item.reviewed_by,
                    reviewed_at=item.reviewed_at,
                    edited_summary=item.edited_summary,
                    summary_provenance=item.summary_provenance,
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

    def _summary_source(self, item: ChangeItem) -> str:
        if item.summary_provenance:
            source = str(item.summary_provenance.get("source") or "")
            model = str(item.summary_provenance.get("model") or item.summary_provenance.get("provider") or "unknown")
            if source == "human-edited-model":
                return f"Human edited summary, originally generated by LLM ({model})"
            if source in {"model-generated", "model-suggestion"}:
                return f"LLM summary generated from diff evidence ({model})"
            if source == "human-edited":
                return "Human edited summary, originally generated from deterministic diff evidence"
        provider_signal = next(
            (signal for signal in item.evidence.commit_signals if signal.startswith("provider-summary:")),
            None,
        )
        if item.edited_summary:
            if provider_signal:
                return f"Human edited summary, originally generated by LLM ({provider_signal.split(':', 1)[1]})"
            return "Human edited summary, originally generated from deterministic diff evidence"
        if provider_signal:
            return f"LLM summary generated from diff evidence ({provider_signal.split(':', 1)[1]})"
        return "Deterministic summary generated from changed-file evidence"

    def _classification_source(self, item: ChangeItem) -> str:
        provider_signal = next(
            (signal for signal in item.evidence.commit_signals if signal.startswith("provider-classification:")),
            None,
        )
        if provider_signal:
            return f"LLM classification assist ({provider_signal.split(':', 1)[1]})"
        if any(signal.startswith("conventional:") for signal in item.evidence.commit_signals):
            return "Deterministic classification from conventional commit signal"
        if any(signal.startswith("label:") for signal in item.evidence.commit_signals):
            return "Deterministic classification from labels and changed files"
        return "Deterministic classification from changed-file evidence"

    def _evidence_summary(self, item: ChangeItem) -> str:
        summary = item.evidence.diff_summary.get("summary")
        if isinstance(summary, str) and summary:
            return summary
        if item.evidence.files:
            return "Changed files: " + ", ".join(item.evidence.files[:5])
        return "No changed-file evidence recorded"

    def _repository_url(self) -> str | None:
        if not _inside_git_worktree(Path.cwd()):
            return None
        try:
            result = subprocess.run(
                ["git", "config", "--get", "remote.origin.url"],
                check=False,
                capture_output=True,
                text=True,
            )
        except OSError:
            return None
        if result.returncode != 0:
            return None
        return normalize_remote_url(result.stdout.strip())


def normalize_remote_url(remote_url: str) -> str | None:
    if not remote_url:
        return None
    if remote_url.startswith("git@github.com:"):
        path = remote_url.removeprefix("git@github.com:")
        return f"https://github.com/{path.removesuffix('.git')}"
    if remote_url.startswith("https://github.com/"):
        return remote_url.removesuffix(".git")
    if remote_url.startswith("http://github.com/"):
        return "https://" + remote_url.removeprefix("http://").removesuffix(".git")
    return remote_url.removesuffix(".git")


def _inside_git_worktree(path: Path) -> bool:
    current = path.resolve()
    for candidate in (current, *current.parents):
        if (candidate / ".git").exists():
            return True
    return False
