from __future__ import annotations

import re

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
    default_review_status,
)
from releasepilot.providers.base import ProviderRequest, ReleasePilotProvider


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

    PROVIDER_CLASSIFICATION = re.compile(
        r"(?P<kind>[a-z-]+)/(?P<area>[a-z0-9_-]+)/(?P<audience>[a-z-]+)/(?P<semver>[a-z-]+)"
    )
    VALID_KINDS = {"added", "changed", "fixed", "removed", "security", "internal"}
    VALID_AUDIENCES = {"external", "internal"}
    VALID_SEMVER = {"major", "minor", "patch", "none"}

    def __init__(self, provider: ReleasePilotProvider | None = None) -> None:
        self.diff_builder = DiffEvidenceBuilder()
        self.provider = provider

    def classify(self, group: ChangeGroup, base_ref: str, head_ref: str, *, summary_only: bool = False) -> list[ChangeItem]:
        pr = group.pr
        labels = [label.lower() for label in (pr.labels if pr else [])]
        changed_files = pr.files if pr else group.files
        diff = self.diff_builder.build(changed_files)

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
        summary = diff.summary

        if self.provider:
            kind, area, audience, semver, summary, confidence = self._apply_provider_assists(
                group=group,
                labels=labels,
                diff=diff,
                title=title,
                summary=summary,
                kind=kind,
                area=area,
                audience=audience,
                semver=semver,
                confidence=confidence,
                commit_signals=commit_signals,
                allow_classification_override=(
                    not summary_only and not parsed and not has_security_signal and not (diff.docs_only or diff.tests_only)
                ),
            )

        source = SourceRef(
            pr_number=pr.number if pr else None,
            commit_shas=[c.sha for c in group.commits],
            base_ref=base_ref,
            head_ref=head_ref,
        )
        evidence = Evidence(
            labels=pr.labels if pr else [],
            files=[f.path for f in changed_files],
            commit_signals=commit_signals,
            diff_summary={
                "summary": diff.summary,
                "files_by_area": diff.files_by_area,
                "file_statuses": diff.file_statuses,
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
            status=default_review_status(confidence),
        )
        return [item]

    def _apply_provider_assists(
        self,
        *,
        group: ChangeGroup,
        labels: list[str],
        diff,
        title: str,
        summary: str,
        kind: str,
        area: str,
        audience: str,
        semver: str,
        confidence: float,
        commit_signals: list[str],
        allow_classification_override: bool,
    ) -> tuple[str, str, str, str, str, float]:
        evidence = {
            "title": title,
            "labels": labels,
            "pr_body": group.pr.body if group.pr else "",
            "commits": [commit.message for commit in group.commits],
            "commit_subjects": [commit.message.splitlines()[0] for commit in group.commits],
            "diff_summary": diff.summary,
            "files_by_area": diff.files_by_area,
            "file_statuses": diff.file_statuses,
            "api_changes": diff.api_changes,
            "migration_changes": diff.migration_changes,
            "config_changes": diff.config_changes,
            "representative_hunks": diff.representative_hunks,
        }
        summary_prompt = self._build_summary_prompt(
            title=title,
            labels=labels,
            pr_body=group.pr.body if group.pr else "",
            diff=diff,
            commits=group.commits,
        )
        summary_response = self.provider.summarize(
            ProviderRequest(task="summarize", prompt=summary_prompt, evidence=evidence)
        )
        if summary_response.confidence >= 0.5 and not self._is_low_value_summary(summary_response.content, title, group):
            summary = summary_response.content
            confidence = max(confidence, min(summary_response.confidence, 0.95))
            commit_signals.append(f"provider-summary:{summary_response.model}")
        elif self._is_provider_error(summary_response.content):
            commit_signals.append(f"provider-error:{summary_response.model}:{summary_response.content}")

        if allow_classification_override:
            classify_prompt = self._build_classification_prompt(
                title=title,
                labels=labels,
                diff_summary=diff.summary,
                commits=group.commits,
            )
            classify_response = self.provider.classify(
                ProviderRequest(task="classify", prompt=classify_prompt, evidence=evidence)
            )
            provider_classification = self._parse_provider_classification(classify_response.content)
            if provider_classification and classify_response.confidence >= 0.65:
                kind, area, audience, semver = provider_classification
                confidence = max(confidence, min(classify_response.confidence, 0.98))
                commit_signals.append(f"provider-classification:{classify_response.model}")

        return kind, area, audience, semver, summary, confidence

    def _build_summary_prompt(self, *, title: str, labels: list[str], pr_body: str, diff, commits: list) -> str:
        commit_lines = "; ".join(commit.message.splitlines()[0] for commit in commits)
        label_text = ", ".join(labels) if labels else "none"
        file_lines = "\n".join(
            f"- {status} {path}" for path, status in sorted(diff.file_statuses.items())
        ) or "- none"
        hunk_lines = []
        for path, hunks in diff.representative_hunks.items():
            hunk_lines.append(f"{path}:")
            hunk_lines.extend(f"  {hunk}" for hunk in hunks)
        hunk_text = "\n".join(hunk_lines) or "none"
        return (
            f"Write one concise release-note sentence based primarily on the file diffs.\n"
            f"Describe the user-facing behavior change when one is evident. Avoid generic filler and do not just repeat the title.\n"
            f"Title: {title}\n"
            f"Labels: {label_text}\n"
            f"Commits: {commit_lines}\n"
            f"PR body: {pr_body.strip() or 'none'}\n"
            f"Changed files:\n{file_lines}\n"
            f"Diff summary: {diff.summary}\n"
            f"Representative hunks:\n{hunk_text}"
        )

    def _build_classification_prompt(self, *, title: str, labels: list[str], diff_summary: str, commits: list) -> str:
        commit_lines = "; ".join(commit.message.splitlines()[0] for commit in commits)
        label_text = ", ".join(labels) if labels else "none"
        return (
            f"Classify this change as kind/area/audience/semver.\n"
            f"Title: {title}\n"
            f"Labels: {label_text}\n"
            f"Commits: {commit_lines}\n"
            f"Diff: {diff_summary}"
        )

    def _parse_provider_classification(self, content: str) -> tuple[str, str, str, str] | None:
        match = self.PROVIDER_CLASSIFICATION.search((content or "").lower())
        if not match:
            return None
        kind = match.group("kind")
        area = match.group("area")
        audience = match.group("audience")
        semver = match.group("semver")
        if kind not in self.VALID_KINDS or audience not in self.VALID_AUDIENCES or semver not in self.VALID_SEMVER:
            return None
        return kind, area, audience, semver

    def _is_low_value_summary(self, content: str, title: str, group: ChangeGroup) -> bool:
        normalized = (content or "").strip()
        if not normalized:
            return True
        lowered = normalized.lower()
        if lowered.startswith(("[github-copilot-", "[ollama-", "[stub-")):
            return True
        candidates = {title.strip().lower()}
        candidates.update(commit.message.splitlines()[0].strip().lower() for commit in group.commits)
        return lowered.rstrip(".") in {candidate.rstrip(".") for candidate in candidates if candidate}

    def _is_provider_error(self, content: str) -> bool:
        lowered = (content or "").strip().lower()
        return lowered.startswith(("[github-copilot-", "[ollama-"))

    def _classify_area(self, labels: list[str], files_by_area: dict[str, list[str]]) -> str:
        for label in labels:
            normalized = label.strip().lower()
            if normalized in self.LABEL_AREA_RULES:
                return self.LABEL_AREA_RULES[normalized]

        if not files_by_area:
            return "general"

        ordered = sorted(files_by_area.items(), key=lambda item: (-len(item[1]), item[0]))
        return ordered[0][0]
