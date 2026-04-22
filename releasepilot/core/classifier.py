from __future__ import annotations

from releasepilot.core.checksum import CHECKSUM_VERSION, build_checksum
from releasepilot.core.conventional import parse_conventional_commit
from releasepilot.core.diff_evidence import DiffEvidenceBuilder
from releasepilot.core.models import (
    ChangeGroup,
    ChangeItem,
    Classification,
    Content,
    Evidence,
    SourceRef,
)


class Classifier:
    LABEL_AREA_RULES = {
        "frontend": "frontend",
        "ui": "frontend",
        "backend": "backend",
        "server": "backend",
        "api": "api",
        "docs": "docs",
        "documentation": "docs",
        "infra": "infra",
        "ops": "infra",
        "test": "tests",
        "testing": "tests",
    }
    INTERNAL_LABELS = {"internal", "chore", "maintenance", "docs", "documentation", "test", "testing"}

    def __init__(self) -> None:
        self.diff_builder = DiffEvidenceBuilder()

    def classify(self, group: ChangeGroup, base_ref: str, head_ref: str) -> list[ChangeItem]:
        pr = group.pr
        labels = [label.lower() for label in (pr.labels if pr else [])]
        diff = self.diff_builder.build(pr.files if pr else [])

        kind = "changed"
        semver = "patch"
        area = self._classify_area(labels=labels, files_by_area=diff.files_by_area)
        audience = "external"
        confidence = 0.55
        commit_signals: list[str] = []

        parsed = []
        for commit in group.commits:
            parsed_commit = parse_conventional_commit(commit.message)
            if parsed_commit:
                parsed.append(parsed_commit)
        if parsed:
            best = parsed[0]
            commit_signals.append(f"conventional:{best.type}")
            if best.breaking:
                kind, semver = "removed", "major"
            elif best.type == "feat":
                kind, semver = "added", "minor"
            elif best.type == "fix":
                kind, semver = "fixed", "patch"
            elif best.type in {"docs", "test", "chore"}:
                kind, audience = "internal", "internal"
            confidence = 0.85

        general_files = diff.files_by_area.get("general", [])
        has_security_signal = any("security" in label for label in labels) or any(
            "security" in path.lower() for path in general_files
        )
        if has_security_signal:
            kind, semver, confidence = "security", "patch", max(confidence, 0.8)

        if diff.docs_only or diff.tests_only:
            kind, audience = "internal", "internal"
            semver = "none"
            area = "docs" if diff.docs_only else "tests"

        if any(lbl in self.INTERNAL_LABELS for lbl in labels):
            audience = "internal"
            confidence = max(confidence, 0.78)
            commit_signals.append("label:internal")

        if not parsed:
            confidence = 0.62
            commit_signals.append("diff-fallback")

        title = (pr.title if pr else group.commits[0].message.splitlines()[0]).strip()
        summary = diff.summary if pr else "Derived from commit history"

        source = SourceRef(
            pr_number=pr.number if pr else None,
            commit_shas=[c.sha for c in group.commits],
            base_ref=base_ref,
            head_ref=head_ref,
        )
        evidence = Evidence(
            labels=pr.labels if pr else [],
            files=[f.path for f in pr.files] if pr else [],
            commit_signals=commit_signals,
            diff_summary={
                "files_by_area": diff.files_by_area,
                "api_changes": diff.api_changes,
                "migrations": diff.migration_changes,
                "config_changes": diff.config_changes,
                "representative_hunks": diff.representative_hunks,
            },
        )

        checksum = build_checksum(
            pr_number=source.pr_number,
            commit_shas=source.commit_shas,
            title=title,
            area=area,
        )
        item = ChangeItem(
            id=f"ci_{checksum[:12]}",
            source=source,
            classification=Classification(kind=kind, area=area, audience=audience, semver=semver),
            content=Content(title=title, summary=summary),
            evidence=evidence,
            confidence=confidence,
            checksum=checksum,
            checksum_version=CHECKSUM_VERSION,
        )
        return [item]

    def _classify_area(self, labels: list[str], files_by_area: dict[str, list[str]]) -> str:
        for label in labels:
            normalized = label.strip().lower()
            if normalized in self.LABEL_AREA_RULES:
                return self.LABEL_AREA_RULES[normalized]

        if not files_by_area:
            return "general"

        ordered = sorted(files_by_area.items(), key=lambda item: (-len(item[1]), item[0]))
        return ordered[0][0]
