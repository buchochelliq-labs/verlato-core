from __future__ import annotations

import hashlib

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
    def __init__(self) -> None:
        self.diff_builder = DiffEvidenceBuilder()

    def classify(self, group: ChangeGroup, base_ref: str, head_ref: str) -> list[ChangeItem]:
        pr = group.pr
        labels = [l.lower() for l in (pr.labels if pr else [])]
        diff = self.diff_builder.build(pr.files if pr else [])

        kind = "changed"
        semver = "patch"
        area = next(iter(diff.files_by_area.keys()), "general")
        audience = "external"
        confidence = 0.55
        commit_signals: list[str] = []

        parsed = [parse_conventional_commit(c.message) for c in group.commits]
        parsed = [p for p in parsed if p]
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

        if any("security" in l for l in labels) or any("security" in p.lower() for p in diff.files_by_area.get("general", [])):
            kind, semver, confidence = "security", "patch", max(confidence, 0.8)

        if diff.docs_only or diff.tests_only:
            kind, audience = "internal", "internal"
            semver = "none"

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

        raw_id = f"{source.pr_number}:{','.join(source.commit_shas)}:{title}"
        checksum = hashlib.sha256(raw_id.encode()).hexdigest()
        item = ChangeItem(
            id=f"ci_{checksum[:12]}",
            source=source,
            classification=Classification(kind=kind, area=area, audience=audience, semver=semver),
            content=Content(title=title, summary=summary),
            evidence=evidence,
            confidence=confidence,
            checksum=checksum,
        )
        return [item]
