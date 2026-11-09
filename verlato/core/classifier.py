from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass

from verlato.core.change_summary import build_change_summary_payload, raw_patch_chars_available
from verlato.core.changelog import VALID_CHANGE_KINDS, normalize_change_kind
from verlato.core.checksum import CHECKSUM_VERSION, build_checksum
from verlato.core.commit_intent import (
    commit_intent_payload,
    conventional_commit_message,
    effective_conventional_commit,
)
from verlato.core.conventional import parse_conventional_commit
from verlato.core.diff_evidence import DiffEvidenceBuilder
from verlato.core.model_privacy import DEFAULT_MODEL_PRIVACY, OMITTED_PATCH_EVIDENCE, model_request_record
from verlato.core.models import (
    ChangeGroup,
    ChangeItem,
    Classification,
    Content,
    Evidence,
    SourceRef,
    changed_files_for_group,
    default_review_status,
)
from verlato.core.semantic import SemanticEvidence, render_semantic_diff
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
    budget: dict[str, object]


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
    INTERNAL_LABELS = {"internal", "chore", "maintenance", "docs", "documentation", "test", "testing", "build", "ci"}

    PROVIDER_CLASSIFICATION = re.compile(
        r"(?P<kind>[a-z-]+)/(?P<area>[a-z0-9_-]+)/(?P<audience>[a-z-]+)/(?P<semver>[a-z-]+)"
    )
    VALID_KINDS = VALID_CHANGE_KINDS
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
        model_privacy: str = DEFAULT_MODEL_PRIVACY,
        provider_mode: str | None = None,
    ) -> None:
        self.diff_builder = DiffEvidenceBuilder()
        self.provider = provider
        self.summary_tone = summary_tone
        self.style_guidance = style_guidance
        self.project_vocabulary = project_vocabulary or []
        self.banned_phrases = banned_phrases or []
        self.model_privacy = model_privacy
        self.provider_mode = provider_mode

    def classify(self, group: ChangeGroup, base_ref: str, head_ref: str, *, summary_only: bool = False) -> list[ChangeItem]:
        pr = group.pr
        labels = [label.lower() for label in (pr.labels if pr else [])]
        changed_files = changed_files_for_group(group)
        diff = self.diff_builder.build(changed_files, commits=group.commits)
        semantic = diff.semantic

        kind = "changed"
        semver = "patch"
        area = self._classify_area(labels=labels, files_by_area=diff.files_by_area)
        audience = "external"
        confidence = 0.55
        commit_signals: list[str] = []
        label_kind = self._classify_kind_from_labels(labels)

        parsed = []
        best_authored = None
        inferred_conventional_used = False
        for commit in group.commits:
            parsed_commit, used_inferred = effective_conventional_commit(commit)
            if parsed_commit:
                parsed.append(parsed_commit)
                authored_commit = parse_conventional_commit(commit.message)
                if best_authored is None and authored_commit:
                    best_authored = authored_commit
                if used_inferred:
                    inferred_conventional_used = True
        if parsed:
            best = parsed[0]
            if best_authored:
                commit_signals.append(f"conventional:{best_authored.type}")
            if inferred_conventional_used:
                scope_suffix = f"({best.scope})" if best.scope else ""
                commit_signals.append(f"inferred-conventional:{best.type}{scope_suffix}")
                if best.breaking and not (best_authored and best_authored.breaking):
                    commit_signals.append("inferred-breaking")
            elif not best_authored:
                commit_signals.append(f"conventional:{best.type}")
            if best.breaking:
                kind, semver = "breaking", "major"
            elif best.type == "feat":
                kind, semver = "added", "minor"
            elif best.type == "fix":
                kind, semver = "fixed", "patch"
            elif best.type == "docs":
                kind, audience, semver = "documentation", "internal", "none"
            elif best.type == "test":
                kind, audience, semver = "tests", "internal", "none"
            elif best.type in {"build", "ci"}:
                kind, audience, semver = "build", "internal", "none"
            elif best.type == "perf":
                kind, semver = "performance", "patch"
            elif best.type == "refactor":
                kind, semver = "refactoring", "patch"
            elif best.type in {"deprecate", "deprecated"}:
                kind, semver = "deprecated", "minor"
            elif best.type in {"remove", "removed", "delete"}:
                kind, semver = "removed", "patch"
            elif best.type in {"security", "sec"}:
                kind, semver = "security", "patch"
            elif best.type in {"chore", "style"}:
                kind, audience = "internal", "internal"
            confidence = 0.85
        elif label_kind:
            kind = label_kind
            semver = self._semver_for_kind(kind)
            if kind in {"documentation", "tests", "build", "internal"}:
                audience = "internal"
            confidence = max(confidence, 0.78)
            commit_signals.append(f"label:{kind}")
        else:
            status_kind = self._classify_kind_from_statuses(diff.file_statuses)
            if status_kind:
                kind = status_kind
                semver = self._semver_for_kind(kind)

        semantic_decided = False
        if semantic is not None:
            kind, audience, semver, confidence, semantic_decided = self._apply_semantic_evidence(
                semantic,
                kind=kind,
                audience=audience,
                semver=semver,
                confidence=confidence,
                commit_signals=commit_signals,
                authored_type=parsed[0].type if parsed else None,
                label_kind=label_kind,
            )

        general_files = diff.files_by_area.get("general", [])
        has_breaking_signal = kind == "breaking" or self._has_breaking_signal(group, labels)
        if has_breaking_signal:
            kind, semver, confidence = "breaking", "major", max(confidence, 0.86)
        has_security_signal = kind == "security" or any("security" in label for label in labels) or any(
            "security" in path.lower() for path in general_files
        )
        if has_security_signal and kind != "breaking":
            kind, semver, confidence = "security", "patch", max(confidence, 0.8)

        if diff.docs_only and kind not in {"breaking", "security"}:
            kind, audience, semver = "documentation", "internal", "none"
            area = "docs"
        elif diff.tests_only and kind not in {"breaking", "security"}:
            kind, audience, semver = "tests", "internal", "none"
            area = "docs" if diff.docs_only else "tests"

        if any(lbl in self.INTERNAL_LABELS for lbl in labels):
            audience = "internal"
            confidence = max(confidence, 0.78)
            if (
                "label:internal" not in commit_signals
                and not any(signal.startswith("label:") for signal in commit_signals)
            ):
                commit_signals.append("label:internal")

        if not parsed:
            confidence = max(confidence, 0.62)
            commit_signals.append("diff-fallback")
        if semantic is not None and (semantic.api_break_candidates or semantic.guardrails) and kind != "breaking":
            # Removed/renamed/re-signatured public symbols or protected-config edits always
            # deserve a human look before publishing, whatever the commit message claims.
            confidence = min(confidence, 0.79)

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
                    not summary_only
                    and not parsed
                    and not semantic_decided
                    and not has_security_signal
                    and not (diff.docs_only or diff.tests_only)
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
            derived_conventional_commits=_derived_conventional_commit_payloads(group.commits),
            diff_summary={
                "summary": diff.summary,
                "files_by_area": diff.files_by_area,
                "file_statuses": diff.file_statuses,
                "commit_boundaries": _commit_boundaries(group.commits),
                "api_changes": diff.api_changes,
                "migrations": diff.migration_changes,
                "config_changes": diff.config_changes,
                "representative_hunks": diff.representative_hunks,
                "grouping_reason": group.grouping_reason,
                "engine": "intentumdiff" if semantic is not None else "git",
                "semantic": semantic.to_dict() if semantic is not None else None,
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

    def _apply_semantic_evidence(
        self,
        semantic: SemanticEvidence,
        *,
        kind: str,
        audience: str,
        semver: str,
        confidence: float,
        commit_signals: list[str],
        authored_type: str | None,
        label_kind: str | None,
    ) -> tuple[str, str, str, float, bool]:
        """Fold IntentumDiff's semantic verdict into the deterministic classification.

        Authored conventional commits and labels keep precedence; the semantic review decides
        kind only on the diff-fallback path, where it replaces path/status guessing. Returns the
        updated ``(kind, audience, semver, confidence, decided)``.
        """

        commit_signals.append(f"semantic:{semantic.verdict}")
        if not semantic.complete:
            commit_signals.append("semantic:partial")
        decided = False
        if authored_type is None and label_kind is None:
            if semantic.verdict == "style-only" and semantic.complete:
                kind, audience, semver = "internal", "internal", "none"
                confidence = max(confidence, 0.88)
                decided = True
            elif semantic.verdict == "refactor-only" and semantic.complete:
                kind, semver = "refactoring", "patch"
                confidence = max(confidence, 0.82)
                decided = True
            elif semantic.verdict == "additive" and semantic.added_symbols:
                kind, semver = "added", "minor"
                confidence = max(confidence, 0.74)
                decided = True
            elif semantic.verdict in {"behavioral", "additive"}:
                confidence = max(confidence, 0.7)
        elif authored_type in {"feat", "fix", "perf"} and semantic.verdict == "style-only" and semantic.complete:
            # The message claims a behavior change but IntentumDiff found only formatting.
            commit_signals.append("semantic:conflict")
            confidence = min(confidence, 0.7)
        if decided:
            commit_signals.append("semantic-classified")
        if semantic.api_break_candidates:
            commit_signals.append("semantic:api-break-candidate")
        if semantic.guardrails:
            commit_signals.append("semantic:guardrail")
        if semantic.cross_file:
            commit_signals.append("semantic:cross-file")
        return kind, audience, semver, confidence, decided

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
            "change_summary": build_change_summary_payload(group=group, diff=diff),
            "inferred_commit_intents": [
                payload for commit in group.commits if (payload := commit_intent_payload(commit.inferred_intent))
            ],
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
            include_hunks=False,
        )
        evidence["request"] = model_request_record(
            task="summarize",
            provider=self.provider_mode,
            privacy_mode=self.model_privacy,
            prompt=summary_prompt,
            evidence=evidence,
            raw_patch_chars_available=raw_patch_chars_available(changed_files_for_group(group)),
            raw_patch_chars_sent=0,
            omitted_fields=list(OMITTED_PATCH_EVIDENCE),
            omission_reasons=["classifier provider assist uses ChangeSummary evidence by default"],
        )
        self._trace_model_call_start("summarize", title)
        summary_response = provider.summarize(
            ProviderRequest(
                task="summarize",
                prompt=summary_prompt,
                evidence=evidence,
                metadata={"privacy_mode": self.model_privacy, "request": evidence["request"]},
            )
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
                ProviderRequest(
                    task="classify",
                    prompt=classify_prompt,
                    evidence=evidence,
                    metadata={"privacy_mode": self.model_privacy, "request": evidence["request"]},
                )
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
        include_hunks: bool = False,
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
            include_hunks=include_hunks,
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
        include_hunks: bool = False,
    ) -> SummaryPrompt:
        profile = _prompt_budget_profile(prompt_profile)
        commit_subjects = [_commit_prompt_line(commit) for commit in commits[:profile.commits]]
        commit_lines = "; ".join(commit_subjects) or "none"
        if len(commits) > profile.commits:
            commit_lines += f"; ... and {len(commits) - profile.commits} more"
        boundary_text = _commit_boundary_prompt_text(commits)
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
        # With IntentumDiff evidence the model gets the semantic diff, never raw patch hunks.
        # `include_hunks` (the privacy mode allows source content) then only decides whether
        # IntentumDiff's change descriptions, which may contain literal values, are included.
        semantic_records = list(getattr(diff, "semantic_diff", None) or [])
        use_semantic_diff = getattr(diff, "semantic", None) is not None
        semantic_detail = "detailed" if include_hunks else "structure"
        semantic_text = ""
        semantic_budget = max(profile.hunk_files * profile.hunks_per_file * profile.hunk_chars, 1200)
        if use_semantic_diff:
            semantic_text = render_semantic_diff(semantic_records, detailed=include_hunks, max_chars=semantic_budget)
            hunk_text = semantic_text or "no reviewed changes"
        elif include_hunks:
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
        else:
            hunk_text = "omitted by privacy mode; use the deterministic change summary and file/status signals"
        raw_hunks_sent = include_hunks and not use_semantic_diff
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
            f"Write a useful Verlato release-note description based on privacy-filtered change evidence.\n"
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
            f"{boundary_text}\n"
            f"Commits: {commit_lines}\n"
            f"PR body: {pr_body_text}\n"
            f"Changed files:\n{file_lines}\n"
            f"Diff summary: {diff_summary}\n"
            + (
                "Base the entry on the IntentumDiff semantic diff below. It lists, per file, the meaningful "
                "changes (with the enclosing symbol), behavior-preserving refactorings and moved code, and "
                "formatting that was ignored. Describe meaningful changes; never present moved code, "
                "refactorings or formatting as new behavior.\n"
                f"Semantic diff (IntentumDiff, {semantic_detail}):\n{hunk_text}"
                if use_semantic_diff
                else f"Representative hunks:\n{hunk_text}"
            )
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
                "commit_boundaries_included": bool(commits),
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
                "evidence_source": "intentumdiff-semantic-diff" if use_semantic_diff else "git-patch",
                "semantic_diff_detail": semantic_detail if use_semantic_diff else None,
                "semantic_diff_chars_included": len(semantic_text),
                "semantic_diff_files": len(semantic_records),
                "raw_patch_text_included": raw_hunks_sent,
                "raw_patch_chars_sent": included_hunk_chars if raw_hunks_sent else 0,
                "omitted_fields": "" if raw_hunks_sent else ", ".join(OMITTED_PATCH_EVIDENCE),
            },
        )

    def _build_classification_prompt(self, *, title: str, labels: list[str], diff_summary: str, commits: list) -> str:
        commit_lines = "; ".join(_commit_prompt_line(commit) for commit in commits)
        label_text = ", ".join(labels) if labels else "none"
        valid_kinds = ", ".join(sorted(self.VALID_KINDS))
        return (
            f"Classify this change as kind/area/audience/semver.\n"
            f"Valid changelog kinds: {valid_kinds}.\n"
            f"Title: {title}\n"
            f"Labels: {label_text}\n"
            f"Commits: {commit_lines}\n"
            f"Diff: {diff_summary}"
        )

    def _parse_provider_classification(self, content: str) -> tuple[str, str, str, str] | None:
        match = self.PROVIDER_CLASSIFICATION.search((content or "").lower())
        if not match:
            return None
        kind = normalize_change_kind(match.group("kind"))
        area = match.group("area")
        audience = match.group("audience")
        semver = match.group("semver")
        if (
            kind is None
            or kind not in self.VALID_KINDS
            or audience not in self.VALID_AUDIENCES
            or semver not in self.VALID_SEMVER
        ):
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

    def _classify_kind_from_labels(self, labels: list[str]) -> str | None:
        for label in labels:
            kind = normalize_change_kind(label)
            if kind:
                return kind
        return None

    def _classify_kind_from_statuses(self, file_statuses: dict[str, str]) -> str | None:
        statuses = {status.lower() for status in file_statuses.values() if status}
        if not statuses:
            return None
        if statuses <= {"added"}:
            return "added"
        if statuses <= {"removed"}:
            return "removed"
        return None

    def _has_breaking_signal(self, group: ChangeGroup, labels: list[str]) -> bool:
        if any(normalize_change_kind(label) == "breaking" for label in labels):
            return True
        haystack = "\n".join(
            [
                group.pr.title if group.pr else "",
                group.pr.body if group.pr else "",
                "\n".join(commit.message for commit in group.commits),
            ]
        )
        return re.search(r"\bbreaking[- ]change\b", haystack, flags=re.IGNORECASE) is not None

    def _semver_for_kind(self, kind: str) -> str:
        if kind == "breaking":
            return "major"
        if kind in {"added", "deprecated"}:
            return "minor"
        if kind in {"documentation", "tests", "build", "internal", "other"}:
            return "none"
        return "patch"

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


def _commit_prompt_line(commit) -> str:
    subject = commit.message.splitlines()[0] if commit.message else ""
    annotations = []
    intent = getattr(commit, "inferred_intent", None)
    if intent:
        annotations.append(f"inferred {conventional_commit_message(intent)}")
    derived = getattr(commit, "derived_intent", None)
    if derived:
        annotations.append(f"derived {conventional_commit_message(derived)}")
    if not annotations:
        return subject
    return f"{subject} [{' | '.join(annotations)}]"


def _commit_boundary_prompt_text(commits: list) -> str:
    if not commits:
        return "First commit: none\nLast commit: none"
    first = commits[0]
    last = commits[-1]
    return "\n".join(
        [
            f"First commit: {_commit_boundary_line(first)}",
            f"Last commit: {_commit_boundary_line(last)}",
        ]
    )


def _commit_boundary_line(commit) -> str:
    previous = getattr(commit, "parent_sha", None)
    base = "initial commit" if previous is None else f"previous {previous[:12]}"
    subject = commit.message.splitlines()[0] if commit.message else ""
    derived = getattr(commit, "derived_intent", None)
    derived_text = f"; derived {conventional_commit_message(derived)}" if derived else ""
    return f"{commit.sha[:12]} ({base}) {subject}{derived_text}"


def _commit_boundaries(commits: list) -> dict[str, object]:
    if not commits:
        return {"first": None, "last": None}
    return {
        "first": _commit_boundary_payload(commits[0]),
        "last": _commit_boundary_payload(commits[-1]),
    }


def _commit_boundary_payload(commit) -> dict[str, object]:
    derived = getattr(commit, "derived_intent", None)
    return {
        "sha": commit.sha,
        "subject": commit.message.splitlines()[0] if commit.message else "",
        "previous_sha": getattr(commit, "parent_sha", None),
        "initial_commit": getattr(commit, "parent_sha", None) is None,
        "derived_conventional": conventional_commit_message(derived) if derived else None,
    }


def _derived_conventional_commit_payloads(commits: list) -> list[dict[str, object]]:
    payloads: list[dict[str, object]] = []
    for commit in commits:
        derived = getattr(commit, "derived_intent", None)
        if derived is None:
            continue
        payload = commit_intent_payload(derived) or {}
        payloads.append(
            {
                "sha": commit.sha,
                "previous_sha": getattr(commit, "parent_sha", None),
                "initial_commit": getattr(commit, "parent_sha", None) is None,
                "authored_subject": commit.message.splitlines()[0] if commit.message else "",
                **payload,
            }
        )
    return payloads


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
