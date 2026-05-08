from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass

from verlato.core.checksum import CHECKSUM_VERSION, build_checksum
from verlato.core.conventional import parse_conventional_commit
from verlato.core.diff_evidence import DiffEvidenceBuilder
from verlato.core.models import (
    ChangeGroup,
    ChangeItem,
    Classification,
    Content,
    Evidence,
    SourceRef,
    default_review_status,
)
from verlato.providers.base import ProviderRequest, VerlatoProvider

MAX_PROMPT_CHANGED_FILES = 12
MAX_PROMPT_COMMITS = 3
MAX_PROMPT_PR_BODY_CHARS = 500
MAX_PROMPT_DIFF_SUMMARY_CHARS = 650
MAX_PROMPT_HUNK_FILES = 3
MAX_PROMPT_HUNKS_PER_FILE = 2
MAX_PROMPT_HUNK_CHARS = 220
SUGGEST_PROMPT_CHANGED_FILES = 8
SUGGEST_PROMPT_COMMITS = 2
SUGGEST_PROMPT_PR_BODY_CHARS = 240
SUGGEST_PROMPT_DIFF_SUMMARY_CHARS = 420
SUGGEST_PROMPT_HUNK_FILES = 2
SUGGEST_PROMPT_HUNKS_PER_FILE = 1
SUGGEST_PROMPT_HUNK_CHARS = 160
LEAN_SUGGEST_PROMPT_CHANGED_FILES = 5
LEAN_SUGGEST_PROMPT_COMMITS = 1
LEAN_SUGGEST_PROMPT_PR_BODY_CHARS = 160
LEAN_SUGGEST_PROMPT_DIFF_SUMMARY_CHARS = 280
LEAN_SUGGEST_PROMPT_HUNK_FILES = 1
LEAN_SUGGEST_PROMPT_HUNKS_PER_FILE = 1
LEAN_SUGGEST_PROMPT_HUNK_CHARS = 120
MINIMAL_SUGGEST_PROMPT_CHANGED_FILES = 3
MINIMAL_SUGGEST_PROMPT_COMMITS = 1
MINIMAL_SUGGEST_PROMPT_PR_BODY_CHARS = 80
MINIMAL_SUGGEST_PROMPT_DIFF_SUMMARY_CHARS = 180
MINIMAL_SUGGEST_PROMPT_HUNK_FILES = 1
MINIMAL_SUGGEST_PROMPT_HUNKS_PER_FILE = 1
MINIMAL_SUGGEST_PROMPT_HUNK_CHARS = 80


@dataclass(frozen=True)
class PromptBudgetProfile:
    name: str
    changed_files: int
    commits: int
    pr_body_chars: int
    diff_summary_chars: int
    hunk_files: int
    hunks_per_file: int
    hunk_chars: int


@dataclass(frozen=True)
class SummaryPrompt:
    prompt: str
    budget: dict[str, int | str]


SUMMARY_PROMPT_PROFILE = PromptBudgetProfile(
    name="summary",
    changed_files=MAX_PROMPT_CHANGED_FILES,
    commits=MAX_PROMPT_COMMITS,
    pr_body_chars=MAX_PROMPT_PR_BODY_CHARS,
    diff_summary_chars=MAX_PROMPT_DIFF_SUMMARY_CHARS,
    hunk_files=MAX_PROMPT_HUNK_FILES,
    hunks_per_file=MAX_PROMPT_HUNKS_PER_FILE,
    hunk_chars=MAX_PROMPT_HUNK_CHARS,
)
SUGGESTION_PROMPT_PROFILE = PromptBudgetProfile(
    name="suggestion-conservative",
    changed_files=SUGGEST_PROMPT_CHANGED_FILES,
    commits=SUGGEST_PROMPT_COMMITS,
    pr_body_chars=SUGGEST_PROMPT_PR_BODY_CHARS,
    diff_summary_chars=SUGGEST_PROMPT_DIFF_SUMMARY_CHARS,
    hunk_files=SUGGEST_PROMPT_HUNK_FILES,
    hunks_per_file=SUGGEST_PROMPT_HUNKS_PER_FILE,
    hunk_chars=SUGGEST_PROMPT_HUNK_CHARS,
)
LEAN_SUGGESTION_PROMPT_PROFILE = PromptBudgetProfile(
    name="suggestion-lean",
    changed_files=LEAN_SUGGEST_PROMPT_CHANGED_FILES,
    commits=LEAN_SUGGEST_PROMPT_COMMITS,
    pr_body_chars=LEAN_SUGGEST_PROMPT_PR_BODY_CHARS,
    diff_summary_chars=LEAN_SUGGEST_PROMPT_DIFF_SUMMARY_CHARS,
    hunk_files=LEAN_SUGGEST_PROMPT_HUNK_FILES,
    hunks_per_file=LEAN_SUGGEST_PROMPT_HUNKS_PER_FILE,
    hunk_chars=LEAN_SUGGEST_PROMPT_HUNK_CHARS,
)
MINIMAL_SUGGESTION_PROMPT_PROFILE = PromptBudgetProfile(
    name="suggestion-minimal",
    changed_files=MINIMAL_SUGGEST_PROMPT_CHANGED_FILES,
    commits=MINIMAL_SUGGEST_PROMPT_COMMITS,
    pr_body_chars=MINIMAL_SUGGEST_PROMPT_PR_BODY_CHARS,
    diff_summary_chars=MINIMAL_SUGGEST_PROMPT_DIFF_SUMMARY_CHARS,
    hunk_files=MINIMAL_SUGGEST_PROMPT_HUNK_FILES,
    hunks_per_file=MINIMAL_SUGGEST_PROMPT_HUNKS_PER_FILE,
    hunk_chars=MINIMAL_SUGGEST_PROMPT_HUNK_CHARS,
)


def _summary_tone_instruction(tone: str) -> str:
    normalized = (tone or "release-note").strip().lower()
    instructions = {
        "release-note": "polished release-note wording for a broad product audience.",
        "concise": "short, direct wording with no extra qualifiers.",
        "technical": "precise engineering wording that names APIs, files, protocols, or internal behavior when useful.",
        "customer-facing": "plain customer-facing wording that emphasizes user-visible value and avoids internal jargon.",
    }
    return instructions.get(normalized, instructions["release-note"])


def _prompt_budget_profile(name: str) -> PromptBudgetProfile:
    normalized = (name or "summary").strip().lower()
    if normalized in {"suggest", "suggestion", "conservative", "suggestion-conservative"}:
        return SUGGESTION_PROMPT_PROFILE
    if normalized in {"lean", "suggestion-lean"}:
        return LEAN_SUGGESTION_PROMPT_PROFILE
    if normalized in {"minimal", "suggestion-minimal"}:
        return MINIMAL_SUGGESTION_PROMPT_PROFILE
    return SUMMARY_PROMPT_PROFILE


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
    LOW_DETAIL_SUGGESTIONS = {
        "change",
        "changes",
        "update",
        "updates",
        "updated wording",
        "improve",
        "improvements",
        "suggested wording",
    }

    def __init__(
        self,
        provider: VerlatoProvider | None = None,
        *,
        summary_tone: str = "release-note",
        style_guidance: str = "",
        project_vocabulary: list[str] | None = None,
        banned_phrases: list[str] | None = None,
    ) -> None:
        self.diff_builder = DiffEvidenceBuilder()
        self.provider = provider
        self.summary_tone = summary_tone
        self.style_guidance = style_guidance
        self.project_vocabulary = project_vocabulary or []
        self.banned_phrases = banned_phrases or []

    def classify(self, group: ChangeGroup, base_ref: str, head_ref: str, *, summary_only: bool = False) -> list[ChangeItem]:
        pr = group.pr
        labels = [label.lower() for label in (pr.labels if pr else [])]
        changed_files = (pr.files or group.files) if pr else group.files
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
                "grouping_reason": group.grouping_reason,
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
        provider = self.provider
        if provider is None:
            return kind, area, audience, semver, summary, confidence

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
            kind=kind,
            audience=audience,
            tone=self.summary_tone,
            style_guidance=self.style_guidance,
            project_vocabulary=self.project_vocabulary,
            banned_phrases=self.banned_phrases,
        )
        self._trace_model_call_start("summarize", title)
        summary_response = provider.summarize(
            ProviderRequest(task="summarize", prompt=summary_prompt, evidence=evidence)
        )
        self._trace_model_call("summarize", title, summary_response.model, summary_response.confidence)
        summary_rejection = self._summary_rejection_reason(summary_response.content, title, group)
        if summary_response.confidence >= 0.5 and summary_rejection is None:
            summary = summary_response.content
            confidence = max(confidence, min(summary_response.confidence, 0.95))
            commit_signals.append(f"provider-summary:{summary_response.model}")
            self._trace_model_decision("summary accepted", title, summary_response.model)
        elif self._is_provider_error(summary_response.content):
            commit_signals.append(f"provider-error:{summary_response.model}:{summary_response.content}")
            self._trace_model_decision("summary provider-error deterministic-fallback", title, summary_response.model)
        else:
            reason = summary_rejection or "low-confidence"
            self._trace_model_decision(
                f"summary rejected reason={reason} deterministic-fallback",
                title,
                summary_response.model,
            )

        if allow_classification_override:
            classify_prompt = self._build_classification_prompt(
                title=title,
                labels=labels,
                diff_summary=diff.summary,
                commits=group.commits,
            )
            self._trace_model_call_start("classify", title)
            classify_response = provider.classify(
                ProviderRequest(task="classify", prompt=classify_prompt, evidence=evidence)
            )
            self._trace_model_call("classify", title, classify_response.model, classify_response.confidence)
            provider_classification = self._parse_provider_classification(classify_response.content)
            if provider_classification and classify_response.confidence >= 0.65:
                kind, area, audience, semver = provider_classification
                confidence = max(confidence, min(classify_response.confidence, 0.98))
                commit_signals.append(f"provider-classification:{classify_response.model}")
                self._trace_model_decision(
                    f"classification accepted value={kind}/{area}/{audience}/{semver}",
                    title,
                    classify_response.model,
                )
            else:
                self._trace_model_decision("classification rejected deterministic-fallback", title, classify_response.model)
        else:
            self._trace_model_decision("classification skipped deterministic-rule-confidence", title, "deterministic")

        return kind, area, audience, semver, summary, confidence

    def _build_summary_prompt(
        self,
        *,
        title: str,
        labels: list[str],
        pr_body: str,
        diff,
        commits: list,
        suggestion_count: int = 1,
        kind: str | None = None,
        audience: str | None = None,
        tone: str = "release-note",
        style_guidance: str = "",
        project_vocabulary: list[str] | None = None,
        banned_phrases: list[str] | None = None,
        prompt_profile: str = "summary",
    ) -> str:
        return self._build_summary_prompt_with_budget(
            title=title,
            labels=labels,
            pr_body=pr_body,
            diff=diff,
            commits=commits,
            suggestion_count=suggestion_count,
            kind=kind,
            audience=audience,
            tone=tone,
            style_guidance=style_guidance,
            project_vocabulary=project_vocabulary,
            banned_phrases=banned_phrases,
            prompt_profile=prompt_profile,
        ).prompt

    def _build_summary_prompt_with_budget(
        self,
        *,
        title: str,
        labels: list[str],
        pr_body: str,
        diff,
        commits: list,
        suggestion_count: int = 1,
        kind: str | None = None,
        audience: str | None = None,
        tone: str = "release-note",
        style_guidance: str = "",
        project_vocabulary: list[str] | None = None,
        banned_phrases: list[str] | None = None,
        prompt_profile: str = "summary",
    ) -> SummaryPrompt:
        profile = _prompt_budget_profile(prompt_profile)
        commit_subjects = [commit.message.splitlines()[0] for commit in commits[:profile.commits]]
        commit_lines = "; ".join(commit_subjects) or "none"
        if len(commits) > profile.commits:
            commit_lines += f"; ... and {len(commits) - profile.commits} more"
        label_text = ", ".join(labels) if labels else "none"
        file_statuses = sorted(diff.file_statuses.items())
        included_files = file_statuses[:profile.changed_files]
        file_lines = "\n".join(
            f"- {status} {path}" for path, status in included_files
        ) or "- none"
        if len(file_statuses) > profile.changed_files:
            file_lines += f"\n- ... and {len(file_statuses) - profile.changed_files} more"
        hunk_lines = []
        total_hunk_files = len(diff.representative_hunks)
        total_hunks = sum(len(hunks) for hunks in diff.representative_hunks.values())
        total_hunk_chars = sum(len(hunk) for hunks in diff.representative_hunks.values() for hunk in hunks)
        included_hunk_files = 0
        included_hunks = 0
        included_hunk_chars = 0
        for path, hunks in list(diff.representative_hunks.items())[:profile.hunk_files]:
            included_hunk_files += 1
            hunk_lines.append(f"{path}:")
            for hunk in hunks[:profile.hunks_per_file]:
                compact_hunk = _truncate_prompt_text(hunk, profile.hunk_chars)
                included_hunks += 1
                included_hunk_chars += len(compact_hunk)
                hunk_lines.append(f"  {compact_hunk}")
            if len(hunks) > profile.hunks_per_file:
                hunk_lines.append(f"  ... and {len(hunks) - profile.hunks_per_file} more hunk(s)")
        hunk_text = "\n".join(hunk_lines) or "none"
        pr_body_text = _truncate_prompt_text(pr_body.strip(), profile.pr_body_chars) or "none"
        diff_summary = _truncate_prompt_text(diff.summary, profile.diff_summary_chars)
        context_bits = []
        if kind:
            context_bits.append(f"kind={kind}")
        if audience:
            context_bits.append(f"audience={audience}")
        context_text = ", ".join(context_bits) or "unspecified"
        output_instruction = (
            "Return one complete release-note entry. Use a short paragraph or focused bullet points when that helps "
            "downstream consumers understand the change; avoid padding."
            if suggestion_count <= 1
            else f"Return exactly {suggestion_count} distinct complete release-note options as a numbered list. "
            f"Each option may use a short paragraph or focused bullet points when that helps downstream consumers "
            f"understand the change; avoid padding."
        )
        tone_instruction = _summary_tone_instruction(tone)
        style_lines = _summary_style_lines(
            guidance=style_guidance,
            project_vocabulary=project_vocabulary or [],
            banned_phrases=banned_phrases or [],
        )
        style_text = f"{style_lines}\n" if style_lines else ""
        prompt = (
            f"Write a useful Verlato release-note description based primarily on the file diffs.\n"
            f"Verlato renders the final release note as Markdown with a version heading and change-type "
            f"subheadings; return only the entry body for this change, without top-level Markdown headings.\n"
            f"Lead with what changed and why it matters. Name the observable behavior, workflow, API, docs, "
            f"test, or maintenance impact.\n"
            f"Prefer concrete active verbs such as Adds, Fixes, Clarifies, Reduces, Preserves, Documents, "
            f"or Validates.\n"
            f"Avoid generic filler, file-name summaries, and text that just repeats the title or commits.\n"
            f"For external/user-facing changes, use polished release-note wording. For internal, docs, tests, "
            f"or maintenance changes, use concise commit-like wording.\n"
            f"Tone: {tone_instruction}\n"
            f"{style_text}"
            f"{output_instruction}\n"
            f"Title: {title}\n"
            f"Release context: {context_text}\n"
            f"Labels: {label_text}\n"
            f"Commits: {commit_lines}\n"
            f"PR body: {pr_body_text}\n"
            f"Changed files:\n{file_lines}\n"
            f"Diff summary: {diff_summary}\n"
            f"Representative hunks:\n{hunk_text}"
        )
        pr_body_chars_included = 0 if pr_body_text == "none" else len(pr_body_text)
        return SummaryPrompt(
            prompt=prompt,
            budget={
                "profile": profile.name,
                "prompt_chars": len(prompt),
                "files_included": len(included_files),
                "files_total": len(file_statuses),
                "files_skipped": max(len(file_statuses) - len(included_files), 0),
                "commits_included": len(commit_subjects),
                "commits_total": len(commits),
                "commits_skipped": max(len(commits) - len(commit_subjects), 0),
                "pr_body_chars_included": pr_body_chars_included,
                "pr_body_chars_total": len(pr_body.strip()),
                "pr_body_chars_skipped": max(len(pr_body.strip()) - pr_body_chars_included, 0),
                "diff_summary_chars_included": len(diff_summary),
                "diff_summary_chars_total": len(diff.summary),
                "diff_summary_chars_skipped": max(len(diff.summary) - len(diff_summary), 0),
                "hunk_files_included": included_hunk_files,
                "hunk_files_total": total_hunk_files,
                "hunk_files_skipped": max(total_hunk_files - included_hunk_files, 0),
                "hunks_included": included_hunks,
                "hunks_total": total_hunks,
                "hunks_skipped": max(total_hunks - included_hunks, 0),
                "hunk_chars_included": included_hunk_chars,
                "hunk_chars_total": total_hunk_chars,
                "hunk_chars_skipped": max(total_hunk_chars - included_hunk_chars, 0),
            },
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
        return self._summary_rejection_reason(content, title, group) is not None

    def _summary_rejection_reason(self, content: str, title: str, group: ChangeGroup) -> str | None:
        normalized = (content or "").strip()
        if not normalized:
            return "empty"
        lowered = normalized.lower()
        if lowered.startswith(
            (
                "[github-copilot-",
                "[ollama-",
                "[codex-cli-",
                "[claude-code-",
                "[litellm-",
                "[plugin-",
                "[fallback-",
                "[stub-",
            )
        ):
            return "provider-error-marker"
        if lowered.startswith("mock summary:"):
            return "mock-prompt-echo"
        candidates = {title.strip().lower()}
        candidates.update(commit.message.splitlines()[0].strip().lower() for commit in group.commits)
        if lowered.rstrip(".") in {candidate.rstrip(".") for candidate in candidates if candidate}:
            return "repeats-title-or-commit"
        words = re.findall(r"[a-z0-9]+", lowered)
        if len(words) < 3 or lowered.rstrip(".") in self.LOW_DETAIL_SUGGESTIONS:
            return "too-short-or-generic"
        return None

    def _is_provider_error(self, content: str) -> bool:
        lowered = (content or "").strip().lower()
        return lowered.startswith(
            (
                "[github-copilot-",
                "[ollama-",
                "[codex-cli-",
                "[claude-code-",
                "[litellm-",
                "[plugin-",
                "[fallback-",
            )
        )

    def _classify_area(self, labels: list[str], files_by_area: dict[str, list[str]]) -> str:
        for label in labels:
            normalized = label.strip().lower()
            if normalized in self.LABEL_AREA_RULES:
                return self.LABEL_AREA_RULES[normalized]

        if not files_by_area:
            return "general"

        ordered = sorted(files_by_area.items(), key=lambda item: (-len(item[1]), item[0]))
        return ordered[0][0]

    def _trace_model_call(self, task: str, title: str, model: str, confidence: float) -> None:
        if os.getenv("VERLATO_TRACE_MODEL_CALLS") != "1":
            return
        print(
            f"[verlato:model] {task} model={model} confidence={confidence:.2f} title={title}",
            file=sys.stderr,
            flush=True,
        )

    def _trace_model_call_start(self, task: str, title: str) -> None:
        if os.getenv("VERLATO_TRACE_MODEL_CALLS") != "1":
            return
        print(f"[verlato:model] starting {task} title={title}", file=sys.stderr, flush=True)

    def _trace_model_decision(self, decision: str, title: str, model: str) -> None:
        if os.getenv("VERLATO_TRACE_MODEL_CALLS") != "1":
            return
        print(f"[verlato:model] decision {decision} model={model} title={title}", file=sys.stderr, flush=True)


def _truncate_prompt_text(value: str, limit: int) -> str:
    normalized = value.strip()
    if len(normalized) <= limit:
        return normalized
    return f"{normalized[:limit].rstrip()}..."


def _summary_style_lines(*, guidance: str, project_vocabulary: list[str], banned_phrases: list[str]) -> str:
    lines: list[str] = []
    if guidance.strip():
        lines.append(f"Project style guidance: {_truncate_prompt_text(guidance, 300)}")
    vocabulary = [term.strip() for term in project_vocabulary if term.strip()]
    if vocabulary:
        lines.append("Preferred project vocabulary: " + ", ".join(vocabulary[:20]))
    banned = [phrase.strip() for phrase in banned_phrases if phrase.strip()]
    if banned:
        lines.append("Avoid these phrases exactly: " + ", ".join(banned[:20]))
    return "\n".join(lines)
