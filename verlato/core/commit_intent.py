from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from typing import Any

from verlato.core.areas import infer_area
from verlato.core.conventional import ConventionalCommit, parse_conventional_commit
from verlato.core.model_privacy import (
    DEFAULT_MODEL_PRIVACY,
    OMITTED_COMMIT_PATCH_EVIDENCE,
    can_send_raw_patches,
    model_request_record,
    normalize_provider_mode,
    privacy_summary,
)
from verlato.core.models import ChangedFile, Commit, CommitIntent
from verlato.core.semantic import (
    CommitSemantics,
    build_semantic_diff,
    commit_semantic_payload,
    semantic_diff_for_prompt,
)
from verlato.providers.base import ProviderRequest, VerlatoProvider

COMMIT_INTENT_INFERENCE_CHOICES = {"off", "low-signal"}
COMMIT_INTENT_MIN_CONFIDENCE = 0.65
MAX_COMMIT_INTENT_FILES = 8
MAX_COMMIT_INTENT_PATCH_CHARS = 900
MAX_CONVENTIONAL_DRAFT_FILES = 12
MAX_CONVENTIONAL_DRAFT_PATCH_FILES = 16
MAX_CONVENTIONAL_DRAFT_PATCH_CHARS = 1800
MAX_CONVENTIONAL_DRAFT_BODY_ITEMS = 8
MAX_CONVENTIONAL_DRAFT_FACTS = 14
MAX_CONVENTIONAL_DRAFT_SYMBOLS = 18
MAX_CONVENTIONAL_DRAFT_FILE_TERMS = 4
MAX_CONVENTIONAL_DRAFT_FILE_SYMBOLS = 3
DERIVED_COMMIT_SOURCE = "model-derived-diff"
CONVENTIONAL_DRAFT_SOURCE = "model-drafted-commit"
CONVENTIONAL_DRAFT_EVIDENCE_MODES = {"diff-only", "subject-hint"}
DEFAULT_CONVENTIONAL_DRAFT_EVIDENCE_MODE = "subject-hint"
ALLOWED_CONVENTIONAL_TYPES = {
    "feat",
    "fix",
    "docs",
    "test",
    "build",
    "ci",
    "perf",
    "refactor",
    "deprecate",
    "deprecated",
    "remove",
    "removed",
    "delete",
    "security",
    "sec",
    "chore",
    "style",
}
SCOPE_PATTERN = re.compile(r"^[a-z0-9][a-z0-9/_-]{0,48}$")
SETTING_PATTERN = re.compile(
    r"""(?:"(?P<quoted>[A-Za-z][A-Za-z0-9_.-]{2,})"\s*[:=]\s*(?P<quoted_value>true|false|null|-?\d+(?:\.\d+)?|"[^"]*")|(?P<bare>[A-Za-z][A-Za-z0-9_]{2,})\s*[:=]\s*(?P<bare_value>true|false|null|-?\d+(?:\.\d+)?|"[^"]*"))"""
)
STRING_LITERAL_PATTERN = re.compile(r'"([^"\n]{3,80})"|`([^`\n]{3,80})`|\'([^\'\n]{3,80})\'')
IDENTIFIER_PATTERN = re.compile(r"\b[A-Za-z][A-Za-z0-9_]{3,}\b")
PUBLIC_SYMBOL_PATTERNS = (
    re.compile(r"^\s*(?:async\s+)?def\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
    re.compile(r"^\s*class\s+([A-Za-z_][A-Za-z0-9_]*)\b"),
    re.compile(
        r"^\s*export\s+(?:default\s+)?(?:async\s+)?"
        r"(?:function|class|interface|type|const|let|var)\s+([A-Za-z_$][A-Za-z0-9_$]*)\b"
    ),
    re.compile(r"^\s*(?:public\s+)?(?:static\s+)?(?:async\s+)?[A-Za-z_$][A-Za-z0-9_$<>[\], ?]*\s+([A-Za-z_$][A-Za-z0-9_$]*)\s*\("),
)
TEST_NAME_PATTERNS = (
    re.compile(r"^\s*(?:async\s+)?def\s+(test_[A-Za-z0-9_]+)\b"),
    re.compile(r"""^\s*(?:test|it)\(\s*["'`]([^"'`]{3,120})["'`]"""),
)
UI_ACTION_PATTERN = re.compile(r"""(?:data-action=|action\s*===|type:\s*)["'`]([A-Za-z][A-Za-z0-9_-]{2,80})["'`]""")
CONFIG_KEY_PATTERN = re.compile(r"""["'`]((?:verlato|VERLATO)[A-Za-z0-9_.-]{2,})["'`]""")


@dataclass
class ConventionalCommitDraft:
    intent: CommitIntent
    body: list[str]
    sha: str = ""
    previous_sha: str | None = None
    initial_commit: bool = False
    authored_subject: str = ""
    privacy: dict[str, Any] | None = None
    ai_request: dict[str, Any] | None = None
    provider_metadata: dict[str, Any] | None = None
    provider_reasoning: str = ""
    evidence_mode: str = DEFAULT_CONVENTIONAL_DRAFT_EVIDENCE_MODE

    def to_dict(self) -> dict[str, Any]:
        header = conventional_commit_message(self.intent)
        body = list(self.body)
        message = header if not body else f"{header}\n\n" + "\n".join(f"- {line}" for line in body)
        intent_payload = commit_intent_payload(self.intent) or {}
        payload = {
            "sha": self.sha,
            "previous_sha": self.previous_sha,
            "initial_commit": self.initial_commit,
            "authored_subject": self.authored_subject,
            "evidence_mode": self.evidence_mode,
            **intent_payload,
            "header": header,
            "message": message,
            "body": body,
            "privacy": self.privacy or {},
            "ai_request": self.ai_request or {},
            "provider_metadata": self.provider_metadata or {},
            "provider_reasoning": self.provider_reasoning,
        }
        return payload


def normalize_commit_intent_mode(value: object, *, default: str = "off") -> str:
    normalized = str(value or "").strip().lower()
    if normalized in COMMIT_INTENT_INFERENCE_CHOICES:
        return normalized
    return default


def normalize_conventional_draft_evidence_mode(
    value: object,
    *,
    default: str = DEFAULT_CONVENTIONAL_DRAFT_EVIDENCE_MODE,
) -> str:
    normalized = str(value or "").strip().lower()
    if normalized in CONVENTIONAL_DRAFT_EVIDENCE_MODES:
        return normalized
    return default


def is_low_signal_commit(commit: Commit) -> bool:
    parsed = parse_conventional_commit(commit.message)
    return parsed is None or not parsed.scope


def effective_conventional_commit(commit: Commit) -> tuple[ConventionalCommit | None, bool]:
    parsed = parse_conventional_commit(commit.message)
    intent = commit.inferred_intent
    if parsed:
        if not intent or parsed.scope:
            return parsed, False
        return (
            ConventionalCommit(
                type=parsed.type,
                scope=intent.scope,
                description=parsed.description,
                breaking=parsed.breaking or intent.breaking,
            ),
            bool(intent.scope or intent.breaking),
        )
    if intent:
        return (
            ConventionalCommit(
                type=intent.type,
                scope=intent.scope,
                description=intent.description,
                breaking=intent.breaking,
            ),
            True,
        )
    return None, False


class CommitIntentInferer:
    def __init__(
        self,
        provider: VerlatoProvider,
        *,
        min_confidence: float = COMMIT_INTENT_MIN_CONFIDENCE,
        privacy_mode: str = DEFAULT_MODEL_PRIVACY,
        provider_mode: str | None = None,
    ) -> None:
        self.provider = provider
        self.min_confidence = min_confidence
        self.privacy_mode = privacy_mode
        self.provider_mode = normalize_provider_mode(provider_mode)

    def infer(self, commit: Commit) -> CommitIntent | None:
        if not is_low_signal_commit(commit):
            return None
        include_patches = can_send_raw_patches(self.provider_mode, self.privacy_mode)
        evidence = commit_intent_evidence(commit, include_patches=include_patches)
        prompt = build_commit_intent_prompt(
            commit,
            include_patches=include_patches,
            evidence=evidence,
            privacy_mode=self.privacy_mode,
        )
        raw_patch_chars_available = sum(len(file.patch or "") for file in commit.files)
        raw_patch_chars_sent = (
            sum(len((file.patch or "")[:MAX_COMMIT_INTENT_PATCH_CHARS]) for file in commit.files[:MAX_COMMIT_INTENT_FILES])
            if include_patches
            else 0
        )
        response = self.provider.summarize(
            ProviderRequest(
                task="infer-conventional",
                prompt=prompt,
                evidence=evidence,
                response_format=commit_intent_response_format(),
                metadata={
                    "privacy_mode": self.privacy_mode,
                    "request": model_request_record(
                        task="infer-conventional",
                        provider=self.provider_mode,
                        privacy_mode=self.privacy_mode,
                        prompt=prompt,
                        evidence=evidence,
                        raw_patch_chars_available=raw_patch_chars_available,
                        raw_patch_chars_sent=raw_patch_chars_sent,
                        omitted_fields=[] if include_patches else list(OMITTED_COMMIT_PATCH_EVIDENCE),
                        omission_reasons=[]
                        if include_patches
                        else ["commit intent inference is using metadata-only evidence"],
                    ),
                },
            )
        )
        if _is_provider_error(response.content):
            return None
        return parse_commit_intent_response(
            response.content,
            model=response.model,
            min_confidence=self.min_confidence,
        )


class DiffConventionalCommitDeriver:
    def __init__(
        self,
        provider: VerlatoProvider,
        *,
        min_confidence: float = COMMIT_INTENT_MIN_CONFIDENCE,
        privacy_mode: str = DEFAULT_MODEL_PRIVACY,
        provider_mode: str | None = None,
    ) -> None:
        self.provider = provider
        self.min_confidence = min_confidence
        self.privacy_mode = privacy_mode
        self.provider_mode = normalize_provider_mode(provider_mode)

    def derive(self, commit: Commit) -> CommitIntent | None:
        include_patches = can_send_raw_patches(self.provider_mode, self.privacy_mode)
        evidence = commit_diff_intent_evidence(commit, include_patches=include_patches)
        prompt = build_diff_conventional_commit_prompt(
            commit,
            include_patches=include_patches,
            evidence=evidence,
            privacy_mode=self.privacy_mode,
        )
        raw_patch_chars_available = sum(len(file.patch or "") for file in commit.files)
        raw_patch_chars_sent = (
            sum(len((file.patch or "")[:MAX_COMMIT_INTENT_PATCH_CHARS]) for file in commit.files[:MAX_COMMIT_INTENT_FILES])
            if include_patches
            else 0
        )
        response = self.provider.summarize(
            ProviderRequest(
                task="derive-conventional",
                prompt=prompt,
                evidence=evidence,
                response_format=commit_intent_response_format(),
                metadata={
                    "privacy_mode": self.privacy_mode,
                    "request": model_request_record(
                        task="derive-conventional",
                        provider=self.provider_mode,
                        privacy_mode=self.privacy_mode,
                        prompt=prompt,
                        evidence=evidence,
                        raw_patch_chars_available=raw_patch_chars_available,
                        raw_patch_chars_sent=raw_patch_chars_sent,
                        omitted_fields=[] if include_patches else list(OMITTED_COMMIT_PATCH_EVIDENCE),
                        omission_reasons=[]
                        if include_patches
                        else ["derived conventional commit is using metadata-only diff evidence"],
                    ),
                },
            )
        )
        if _is_provider_error(response.content):
            return None
        return parse_commit_intent_response(
            response.content,
            model=response.model,
            min_confidence=self.min_confidence,
            source=DERIVED_COMMIT_SOURCE,
        )


def infer_commit_intents(
    commits: list[Commit],
    provider: VerlatoProvider,
    *,
    privacy_mode: str = DEFAULT_MODEL_PRIVACY,
    provider_mode: str | None = None,
) -> int:
    inferer = CommitIntentInferer(provider, privacy_mode=privacy_mode, provider_mode=provider_mode)
    inferred = 0
    for commit in commits:
        try:
            intent = inferer.infer(commit)
        except Exception:
            intent = None
        if intent is None:
            continue
        commit.inferred_intent = intent
        inferred += 1
    return inferred


def derive_conventional_commits(
    commits: list[Commit],
    provider: VerlatoProvider,
    *,
    privacy_mode: str = DEFAULT_MODEL_PRIVACY,
    provider_mode: str | None = None,
) -> int:
    deriver = DiffConventionalCommitDeriver(provider, privacy_mode=privacy_mode, provider_mode=provider_mode)
    derived = 0
    for commit in commits:
        try:
            intent = deriver.derive(commit)
        except Exception:
            intent = None
        if intent is None:
            continue
        commit.derived_intent = intent
        derived += 1
    return derived


def draft_conventional_commit(
    commit: Commit,
    provider: VerlatoProvider,
    *,
    privacy_mode: str = DEFAULT_MODEL_PRIVACY,
    provider_mode: str | None = None,
    min_confidence: float = COMMIT_INTENT_MIN_CONFIDENCE,
    evidence_mode: str = DEFAULT_CONVENTIONAL_DRAFT_EVIDENCE_MODE,
) -> ConventionalCommitDraft | None:
    provider_mode = normalize_provider_mode(provider_mode)
    evidence_mode = normalize_conventional_draft_evidence_mode(evidence_mode)
    include_patches = can_send_raw_patches(provider_mode, privacy_mode)
    evidence = conventional_commit_draft_evidence(
        commit,
        include_patches=include_patches,
        evidence_mode=evidence_mode,
    )
    prompt = build_conventional_commit_draft_prompt(
        commit,
        include_patches=include_patches,
        evidence=evidence,
        privacy_mode=privacy_mode,
        evidence_mode=evidence_mode,
    )
    raw_patch_chars_available = sum(len(file.patch or "") for file in commit.files)
    raw_patch_chars_sent = (
        sum(
            len((file.patch or "")[:MAX_CONVENTIONAL_DRAFT_PATCH_CHARS])
            for file in commit.files[:MAX_CONVENTIONAL_DRAFT_PATCH_FILES]
        )
        if include_patches
        else 0
    )
    request_record = model_request_record(
        task="draft-conventional",
        provider=provider_mode,
        privacy_mode=privacy_mode,
        prompt=prompt,
        evidence=evidence,
        raw_patch_chars_available=raw_patch_chars_available,
        raw_patch_chars_sent=raw_patch_chars_sent,
        omitted_fields=[] if include_patches else list(OMITTED_COMMIT_PATCH_EVIDENCE),
        omission_reasons=[] if include_patches else ["conventional commit drafting is using structured diff facts"],
    )
    request_record["evidence_mode"] = evidence_mode
    request_record["authored_message_omitted"] = evidence_mode == "diff-only"
    response = provider.summarize(
        ProviderRequest(
            task="draft-conventional",
            prompt=prompt,
            evidence=evidence,
            response_format=conventional_commit_draft_response_format(),
            metadata={
                "privacy_mode": privacy_mode,
                "request": request_record,
            },
        )
    )
    if _is_provider_error(response.content):
        return None
    draft = parse_conventional_commit_draft_response(
        response.content,
        model=response.model,
        min_confidence=min_confidence,
        source=CONVENTIONAL_DRAFT_SOURCE,
    )
    if draft is None:
        return None
    draft.sha = commit.sha
    draft.previous_sha = commit.parent_sha
    draft.initial_commit = commit.parent_sha is None
    draft.authored_subject = commit.message.splitlines()[0] if commit.message and evidence_mode == "subject-hint" else ""
    draft.evidence_mode = evidence_mode
    draft.privacy = privacy_summary(
        provider=provider_mode,
        privacy_mode=privacy_mode,
        raw_patch_chars_available=raw_patch_chars_available,
        raw_patch_chars_sent=raw_patch_chars_sent,
    )
    draft.ai_request = request_record
    draft.provider_metadata = dict(response.metadata)
    draft.provider_reasoning = response.reasoning
    return draft


def parse_commit_intent_response(
    content: str,
    *,
    model: str,
    min_confidence: float = COMMIT_INTENT_MIN_CONFIDENCE,
    source: str = "model-inferred",
) -> CommitIntent | None:
    raw = _parse_json_object(content)
    if raw is None:
        return None
    commit_type = _normalized_type(raw.get("type"))
    description = _normalized_description(raw.get("description"))
    confidence = _coerce_confidence(raw.get("confidence"))
    if not commit_type or not description or confidence < min_confidence:
        return None
    raw_scope = raw.get("scope")
    scope = _normalized_scope(raw_scope)
    if raw_scope is not None and str(raw_scope).strip() and scope is None:
        return None
    rationale = str(raw.get("rationale") or "").strip()[:300]
    return CommitIntent(
        type=commit_type,
        scope=scope,
        description=description,
        breaking=_coerce_bool(raw.get("breaking")),
        confidence=confidence,
        model=model,
        source=source,
        rationale=rationale,
    )


def parse_conventional_commit_draft_response(
    content: str,
    *,
    model: str,
    min_confidence: float = COMMIT_INTENT_MIN_CONFIDENCE,
    source: str = CONVENTIONAL_DRAFT_SOURCE,
) -> ConventionalCommitDraft | None:
    raw = _parse_json_object(content)
    if raw is None:
        return None
    commit_type = _normalized_type(raw.get("type"))
    description = _normalized_description(raw.get("description"))
    confidence = _coerce_confidence(raw.get("confidence"))
    if not commit_type or not description or confidence < min_confidence:
        return None
    raw_scope = raw.get("scope")
    scope = _normalized_scope(raw_scope)
    if raw_scope is not None and str(raw_scope).strip() and scope is None:
        return None
    body = _normalized_body(raw.get("body", raw.get("body_bullets", [])))
    return ConventionalCommitDraft(
        intent=CommitIntent(
            type=commit_type,
            scope=scope,
            description=description,
            breaking=_coerce_bool(raw.get("breaking")),
            confidence=confidence,
            model=model,
            source=source,
            rationale=str(raw.get("rationale") or "").strip()[:300],
        ),
        body=body,
    )


def commit_intent_response_format() -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "type": {"type": "string", "enum": sorted(ALLOWED_CONVENTIONAL_TYPES)},
            "scope": {"type": ["string", "null"]},
            "description": {"type": "string"},
            "breaking": {"type": "boolean"},
            "confidence": {"type": "number", "minimum": 0, "maximum": 1},
            "rationale": {"type": "string"},
        },
        "required": ["type", "scope", "description", "breaking", "confidence", "rationale"],
    }


def conventional_commit_draft_response_format() -> dict[str, Any]:
    schema = commit_intent_response_format()
    schema["properties"] = {
        **schema["properties"],
        "body": {
            "type": "array",
            "items": {"type": "string"},
            "maxItems": MAX_CONVENTIONAL_DRAFT_BODY_ITEMS,
        },
    }
    schema["required"] = ["type", "scope", "description", "body", "breaking", "confidence", "rationale"]
    return schema


SEMANTIC_SOURCE_TEXT = (
    ", and the IntentumDiff semantic diff (semantic_review and each file's semantic_diff: meaningful changes "
    "with their enclosing symbols, behavior-preserving refactorings, moved code and ignored formatting). "
    "Treat the semantic diff as the source of truth; never describe moved code, refactorings or formatting "
    "as new behavior"
)


def build_commit_intent_prompt(
    commit: Commit,
    *,
    include_patches: bool = False,
    evidence: dict[str, Any] | None = None,
    privacy_mode: str = DEFAULT_MODEL_PRIVACY,
) -> str:
    payload = evidence if evidence is not None else commit_intent_evidence(commit, include_patches=include_patches)
    source_text = "file paths, statuses, deterministic facts, and commit metadata"
    if payload.get("evidence_source") == "intentumdiff-semantic-diff":
        source_text += SEMANTIC_SOURCE_TEXT
    elif include_patches:
        source_text += ", including bounded patch excerpts"
    return (
        "Infer the conventional commit that should have been written for this exact commit.\n"
        f"Privacy mode: {privacy_mode}. Use only the provided {source_text}.\n"
        "Return strict JSON matching the schema. Prefer a concise scope such as api, ui, cli, docs, tests, infra, "
        "mcp, vscode, config, auth, release, or cache when supported by the evidence.\n"
        "Do not invent product behavior that is not supported by the evidence.\n\n"
        f"COMMIT_EVIDENCE:\n{json.dumps(payload, indent=2, sort_keys=True)}"
    )


def build_conventional_commit_draft_prompt(
    commit: Commit,
    *,
    include_patches: bool = False,
    evidence: dict[str, Any] | None = None,
    privacy_mode: str = DEFAULT_MODEL_PRIVACY,
    evidence_mode: str = DEFAULT_CONVENTIONAL_DRAFT_EVIDENCE_MODE,
) -> str:
    evidence_mode = normalize_conventional_draft_evidence_mode(evidence_mode)
    payload = evidence if evidence is not None else conventional_commit_draft_evidence(
        commit,
        include_patches=include_patches,
        evidence_mode=evidence_mode,
    )
    source_text = "changed file paths, statuses, and deterministic facts extracted from the diff"
    if evidence_mode == "subject-hint":
        source_text = "authored subject, changed file paths, statuses, and deterministic facts extracted from the diff"
    if payload.get("evidence_source") == "intentumdiff-semantic-diff":
        source_text += SEMANTIC_SOURCE_TEXT
    elif include_patches:
        source_text += ", plus bounded patch excerpts"
    subject_instruction = (
        "The authored git commit message is intentionally not provided; infer from commit ID and diff evidence only.\n"
        if evidence_mode == "diff-only"
        else "Use the authored subject as a hint, but let the commit diff decide the type, scope, description, and body.\n"
    )
    return (
        "Draft a full Conventional Commits message for this exact existing git commit.\n"
        f"Privacy mode: {privacy_mode}. Use only the provided {source_text}.\n"
        f"Evidence mode: {evidence_mode}.\n"
        f"{subject_instruction}"
        "Return strict JSON matching the schema. The body array should contain 3-7 concrete, supported bullets for "
        "non-trivial commits and [] for tiny commits. Do not prefix body strings with '-' or '*'.\n"
        "Prefer `refactor` when behavior is reorganized, removed, or renamed without a clear user-facing feature; "
        "prefer `feat` for new user-facing capability, `fix` for bug repairs, `docs` for documentation-only changes, "
        "and `test` only when the commit is primarily tests.\n"
        "Use a concise scope such as api, cli, config, docs, mcp, release, tests, ui, or vscode when supported.\n"
        "Do not invent behavior that is not supported by the evidence.\n\n"
        f"COMMIT_DRAFT_EVIDENCE:\n{json.dumps(payload, sort_keys=True, separators=(',', ':'))}"
    )


def build_diff_conventional_commit_prompt(
    commit: Commit,
    *,
    include_patches: bool = False,
    evidence: dict[str, Any] | None = None,
    privacy_mode: str = DEFAULT_MODEL_PRIVACY,
) -> str:
    payload = evidence if evidence is not None else commit_diff_intent_evidence(commit, include_patches=include_patches)
    source_text = "changed file paths, statuses, patch sizes, and deterministic diff metadata"
    if payload.get("evidence_source") == "intentumdiff-semantic-diff":
        source_text += SEMANTIC_SOURCE_TEXT
    elif include_patches:
        source_text += ", including bounded patch excerpts"
    return (
        "Derive the conventional commit that best describes this exact commit diff.\n"
        f"Privacy mode: {privacy_mode}. Use only the provided {source_text}.\n"
        "The authored git commit message is intentionally not provided; do not infer from commit titles or PR text.\n"
        "The diff is from previous_sha to sha. If initial_commit is true, treat the diff as the initial tree.\n"
        "Return strict JSON matching the schema. Prefer a concise scope such as api, ui, cli, docs, tests, infra, "
        "mcp, vscode, config, auth, release, or cache when supported by the diff evidence.\n"
        "Do not invent product behavior that is not supported by the diff evidence.\n\n"
        f"COMMIT_DIFF_EVIDENCE:\n{json.dumps(payload, indent=2, sort_keys=True)}"
    )


def commit_intent_evidence(commit: Commit, *, include_patches: bool = False) -> dict[str, Any]:
    raw_patch_chars = sum(len(file.patch or "") for file in commit.files)
    return _with_semantics(commit, {
        "schema": "verlato.commit-intent-evidence.v1",
        "sha": commit.sha,
        "subject": commit.message.splitlines()[0] if commit.message else "",
        "message": commit.message,
        "files": _commit_file_payloads(commit, include_patches=include_patches),
        "files_total": len(commit.files),
        "raw_patch_chars_available": raw_patch_chars,
        "raw_patch_chars_sent": (
            sum(len((file.patch or "")[:MAX_COMMIT_INTENT_PATCH_CHARS]) for file in commit.files[:MAX_COMMIT_INTENT_FILES])
            if include_patches and not _has_semantics(commit)
            else 0
        ),
        "omitted_fields": (
            [] if include_patches and not _has_semantics(commit) else list(OMITTED_COMMIT_PATCH_EVIDENCE)
        ),
    }, include_patches=include_patches)


def commit_diff_intent_evidence(commit: Commit, *, include_patches: bool = False) -> dict[str, Any]:
    raw_patch_chars = sum(len(file.patch or "") for file in commit.files)
    return _with_semantics(commit, {
        "schema": "verlato.commit-diff-intent-evidence.v1",
        "sha": commit.sha,
        "previous_sha": commit.parent_sha,
        "initial_commit": commit.parent_sha is None,
        "files": _commit_file_payloads(commit, include_patches=include_patches),
        "files_total": len(commit.files),
        "file_status_counts": _file_status_counts(commit.files),
        "raw_patch_chars_available": raw_patch_chars,
        "raw_patch_chars_sent": (
            sum(len((file.patch or "")[:MAX_COMMIT_INTENT_PATCH_CHARS]) for file in commit.files[:MAX_COMMIT_INTENT_FILES])
            if include_patches and not _has_semantics(commit)
            else 0
        ),
        "omitted_fields": (
            [] if include_patches and not _has_semantics(commit) else list(OMITTED_COMMIT_PATCH_EVIDENCE)
        ),
    }, include_patches=include_patches)


def conventional_commit_draft_evidence(
    commit: Commit,
    *,
    include_patches: bool = False,
    evidence_mode: str = DEFAULT_CONVENTIONAL_DRAFT_EVIDENCE_MODE,
) -> dict[str, Any]:
    evidence_mode = normalize_conventional_draft_evidence_mode(evidence_mode)
    raw_patch_chars = sum(len(file.patch or "") for file in commit.files)
    fact_files = _draft_fact_files(commit.files)
    files = _selected_draft_files(fact_files)
    semantic = _has_semantics(commit)
    payload: dict[str, Any] = {
        "schema": "verlato.conventional-commit-draft-evidence.v1",
        "evidence_mode": evidence_mode,
        "sha": commit.sha,
        "previous_sha": commit.parent_sha,
        "initial_commit": commit.parent_sha is None,
        "authored_message_omitted": evidence_mode == "diff-only",
        "files": (
            _semantic_draft_file_payloads(commit, files, detailed=include_patches)
            if semantic
            else [
                _draft_file_payload(file, include_patch=include_patches and index < MAX_CONVENTIONAL_DRAFT_PATCH_FILES)
                for index, file in enumerate(files)
            ]
        ),
        "files_total": len(commit.files),
        "files_analyzed": len(fact_files),
        "files_included": len(files),
        "omitted_files_count": max(len(commit.files) - len(files), 0),
        "file_status_counts": _file_status_counts(fact_files),
        "files_by_area": _files_by_area(fact_files),
        "notable_paths": _notable_paths(fact_files),
        "diff_facts": (
            _semantic_diff_facts(commit, fact_files, detailed=include_patches) if semantic else _diff_facts(fact_files)
        ),
        "raw_patch_chars_available": raw_patch_chars,
        "raw_patch_chars_sent": (
            sum(len((file.patch or "")[:MAX_CONVENTIONAL_DRAFT_PATCH_CHARS]) for file in commit.files[:MAX_CONVENTIONAL_DRAFT_PATCH_FILES])
            if include_patches and not semantic
            else 0
        ),
        "omitted_fields": [] if include_patches and not semantic else list(OMITTED_COMMIT_PATCH_EVIDENCE),
    }
    if evidence_mode == "subject-hint":
        payload["authored_subject"] = commit.message.splitlines()[0] if commit.message else ""
        payload["authored_message"] = commit.message
    return _with_semantics(commit, payload, include_patches=include_patches)


def _has_semantics(commit: Commit) -> bool:
    return isinstance(getattr(commit, "semantics", None), CommitSemantics)


def _with_semantics(commit: Commit, payload: dict[str, Any], *, include_patches: bool = False) -> dict[str, Any]:
    """Attach IntentumDiff's per-commit semantic review when the commit carries one."""

    semantic = commit_semantic_payload(commit)
    if semantic is not None:
        payload["semantic_review"] = semantic
        payload["evidence_source"] = "intentumdiff-semantic-diff"
        payload["semantic_diff_detail"] = "detailed" if include_patches else "structure"
    return payload


def _semantic_records_by_path(commit: Commit, *, detailed: bool) -> dict[str, list[dict[str, Any]]]:
    by_path: dict[str, list[dict[str, Any]]] = {}
    for record in semantic_diff_for_prompt(build_semantic_diff([commit]), detailed=detailed):
        by_path.setdefault(str(record["path"]), []).append(
            {key: value for key, value in record.items() if key not in {"path", "commit"}}
        )
    return by_path


def _commit_file_payloads(commit: Commit, *, include_patches: bool) -> list[dict[str, Any]]:
    """Per-file evidence: the IntentumDiff semantic diff when available, else bounded patches."""

    files = commit.files[:MAX_COMMIT_INTENT_FILES]
    if not _has_semantics(commit):
        return [_file_payload(file, include_patch=include_patches) for file in files]
    by_path = _semantic_records_by_path(commit, detailed=include_patches)
    return [
        {
            "path": file.path,
            "status": file.status,
            "patch_chars_available": len(file.patch or ""),
            "patch_sent": False,
            "semantic_diff": by_path.get(file.path, []),
        }
        for file in files
    ]


_CONFIG_LANGUAGES = {"json", "yaml", "toml", "ini", "xml", "properties", "terraform", "hcl"}


def _semantic_draft_file_payloads(commit: Commit, files: list[ChangedFile], *, detailed: bool) -> list[dict[str, Any]]:
    by_path = _semantic_records_by_path(commit, detailed=detailed)
    digest = commit.semantics
    symbols_by_path: dict[str, list[str]] = {}
    for file_semantics in digest.files:
        symbols_by_path[file_semantics.path] = [
            symbol.name for symbol in file_semantics.symbols if symbol.change in {"added", "signature", "renamed"}
        ]
    return [
        {
            "path": file.path,
            "area": infer_area(file.path),
            "status": file.status,
            "patch_chars_available": len(file.patch or ""),
            "patch_sent": False,
            "public_symbols": symbols_by_path.get(file.path, [])[:MAX_CONVENTIONAL_DRAFT_FILE_SYMBOLS],
            "semantic_diff": by_path.get(file.path, []),
        }
        for file in files
    ]


def _semantic_diff_facts(commit: Commit, files: list[ChangedFile], *, detailed: bool) -> dict[str, Any]:
    """Draft facts derived from IntentumDiff's review instead of regexes over raw patch lines."""

    digest = commit.semantics
    added_terms: list[str] = []
    removed_terms: list[str] = []
    public_symbols: list[str] = []
    test_names: list[str] = []
    config_keys: list[str] = []
    changed_settings: list[dict[str, str]] = []
    for file_semantics in digest.files:
        is_test = "test" in file_semantics.path.lower()
        for symbol in file_semantics.symbols:
            if symbol.change in {"added", "signature", "renamed"}:
                (test_names if is_test else public_symbols).append(
                    symbol.name if is_test else f"{file_semantics.path}:{symbol.name}"
                )
        for entry in file_semantics.changes:
            for semantic_edit in entry.get("edits", []):
                name = semantic_edit.get("name") or entry.get("symbol")
                if not name:
                    continue
                change = str(semantic_edit.get("change", ""))
                if change == "ADDITION":
                    added_terms.append(name)
                elif change == "DELETION":
                    removed_terms.append(name)
                if file_semantics.language in _CONFIG_LANGUAGES and entry.get("group") == "meaningful":
                    config_keys.append(name)
                    setting = {"name": name, "change": change.lower()}
                    if detailed and semantic_edit.get("detail"):
                        setting["detail"] = str(semantic_edit["detail"])
                    changed_settings.append(setting)
    return {
        "source": "intentumdiff",
        "semantic_files": len(digest.files),
        "changed_settings": changed_settings[:MAX_CONVENTIONAL_DRAFT_FACTS],
        "added_terms": _unique_preserve_order(added_terms)[:MAX_CONVENTIONAL_DRAFT_FACTS],
        "removed_terms": _unique_preserve_order(removed_terms)[:MAX_CONVENTIONAL_DRAFT_FACTS],
        "public_symbols": _unique_preserve_order(public_symbols)[:MAX_CONVENTIONAL_DRAFT_FACTS],
        "test_names": _unique_preserve_order(test_names)[:MAX_CONVENTIONAL_DRAFT_FACTS],
        "config_keys": _unique_preserve_order(config_keys)[:MAX_CONVENTIONAL_DRAFT_FACTS],
        "ui_actions": [],
        "themes": _theme_facts(
            files, added_terms=added_terms, removed_terms=removed_terms, changed_settings=changed_settings
        ),
    }


def commit_intent_payload(intent: CommitIntent | None) -> dict[str, Any] | None:
    if intent is None:
        return None
    payload = asdict(intent)
    payload["message"] = conventional_commit_message(intent)
    return payload


def conventional_commit_message(intent: CommitIntent) -> str:
    scope = f"({intent.scope})" if intent.scope else ""
    breaking = "!" if intent.breaking else ""
    return f"{intent.type}{scope}{breaking}: {intent.description}"


def _file_payload(file: ChangedFile, *, include_patch: bool) -> dict[str, Any]:
    patch = file.patch or ""
    payload = {
        "path": file.path,
        "status": file.status,
        "patch_chars_available": len(patch),
        "patch_sent": include_patch,
    }
    if include_patch:
        payload.update(
            {
                "patch_excerpt": patch[:MAX_COMMIT_INTENT_PATCH_CHARS],
                "patch_chars_sent": len(patch[:MAX_COMMIT_INTENT_PATCH_CHARS]),
                "patch_truncated": len(patch) > MAX_COMMIT_INTENT_PATCH_CHARS,
            }
        )
    return payload


def _draft_file_payload(file: ChangedFile, *, include_patch: bool) -> dict[str, Any]:
    patch = file.patch or ""
    added_lines = _patch_lines(patch, "+")
    removed_lines = _patch_lines(patch, "-")
    payload = {
        "path": file.path,
        "area": infer_area(file.path),
        "status": file.status,
        "patch_chars_available": len(patch),
        "patch_sent": include_patch,
        "added_terms": _notable_terms(added_lines)[:MAX_CONVENTIONAL_DRAFT_FILE_TERMS],
        "removed_terms": _notable_terms(removed_lines)[:MAX_CONVENTIONAL_DRAFT_FILE_TERMS],
        "public_symbols": _public_symbols(added_lines)[:MAX_CONVENTIONAL_DRAFT_FILE_SYMBOLS],
        "test_names": _test_names(added_lines)[:MAX_CONVENTIONAL_DRAFT_FILE_SYMBOLS],
        "config_keys": _config_keys(added_lines)[:MAX_CONVENTIONAL_DRAFT_FILE_SYMBOLS],
        "ui_actions": _ui_actions(added_lines)[:MAX_CONVENTIONAL_DRAFT_FILE_SYMBOLS],
    }
    if include_patch:
        payload.update(
            {
                "patch_excerpt": patch[:MAX_CONVENTIONAL_DRAFT_PATCH_CHARS],
                "patch_chars_sent": len(patch[:MAX_CONVENTIONAL_DRAFT_PATCH_CHARS]),
                "patch_truncated": len(patch) > MAX_CONVENTIONAL_DRAFT_PATCH_CHARS,
            }
        )
    return payload


def _draft_fact_files(files: list[ChangedFile]) -> list[ChangedFile]:
    signal_files = [file for file in files if not _is_low_value_draft_file(file.path)]
    return signal_files or list(files)


def _selected_draft_files(files: list[ChangedFile]) -> list[ChangedFile]:
    return sorted(files, key=_draft_file_rank)[:MAX_CONVENTIONAL_DRAFT_FILES]


def _is_low_value_draft_file(path: str) -> bool:
    normalized = path.replace("\\", "/").lower()
    name = normalized.rsplit("/", 1)[-1]
    return (
        normalized.startswith(".verlato_")
        or "/.verlato_" in normalized
        or name in {".verlato_state.json", ".verlato_suggestion_cache.json.lock"}
        or name.endswith(".vsix")
    )


def _draft_file_rank(file: ChangedFile) -> tuple[int, str]:
    path = file.path.replace("\\", "/").lower()
    rank = 0
    if "vscode-extension/src" in path:
        rank -= 50
    if path.startswith("verlato/"):
        rank -= 45
    if any(part in path for part in ("commit_intent", "model_privacy", "releasecontrol", "commands", "config")):
        rank -= 25
    if "test" in path:
        rank -= 10
    if path.endswith((".md", ".lock")) or "changelog" in path:
        rank += 20
    if _is_low_value_draft_file(file.path):
        rank += 80
    return rank, path


def _file_status_counts(files: list[ChangedFile]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for file in files:
        status = file.status or "modified"
        counts[status] = counts.get(status, 0) + 1
    return dict(sorted(counts.items()))


def _files_by_area(files: list[ChangedFile]) -> dict[str, list[str]]:
    by_area: dict[str, list[str]] = {}
    for file in files:
        by_area.setdefault(infer_area(file.path), []).append(file.path)
    return {area: paths[:20] for area, paths in sorted(by_area.items())}


def _notable_paths(files: list[ChangedFile]) -> list[str]:
    def score(file: ChangedFile) -> tuple[int, str]:
        path = file.path.lower()
        rank = 0
        if "vscode-extension/src" in path:
            rank -= 30
        if "releasecontrol" in path or "settings" in path or "config" in path:
            rank -= 20
        if "test" in path:
            rank -= 8
        if path.endswith((".md", ".lock", ".vsix")) or "release.md" in path:
            rank += 25
        return rank, path

    return [file.path for file in sorted(files, key=score)[:30]]


def _diff_facts(files: list[ChangedFile]) -> dict[str, Any]:
    added_settings: dict[str, str] = {}
    removed_settings: dict[str, str] = {}
    added_terms: list[str] = []
    removed_terms: list[str] = []
    public_symbols: list[str] = []
    test_names: list[str] = []
    config_keys: list[str] = []
    ui_actions: list[str] = []
    patch_files = 0
    for file in files:
        patch = file.patch or ""
        if not patch:
            continue
        patch_files += 1
        added_lines = _patch_lines(patch, "+")
        removed_lines = _patch_lines(patch, "-")
        added_settings.update(_settings_from_lines(added_lines))
        removed_settings.update(_settings_from_lines(removed_lines))
        added_terms.extend(_notable_terms(added_lines))
        removed_terms.extend(_notable_terms(removed_lines))
        public_symbols.extend(f"{file.path}:{symbol}" for symbol in _public_symbols(added_lines))
        test_names.extend(_test_names(added_lines))
        config_keys.extend(_config_keys(added_lines))
        ui_actions.extend(_ui_actions(added_lines))

    changed_settings = []
    for key in sorted(set(added_settings) | set(removed_settings)):
        before = removed_settings.get(key)
        after = added_settings.get(key)
        if before is not None and after is not None and before != after:
            changed_settings.append({"name": key, "from": before, "to": after})
        elif after is not None and before is None:
            changed_settings.append({"name": key, "added": after})
        elif before is not None:
            changed_settings.append({"name": key, "removed": before})

    return {
        "patch_files": patch_files,
        "changed_settings": changed_settings[:MAX_CONVENTIONAL_DRAFT_FACTS],
        "added_terms": _unique_preserve_order(added_terms)[:MAX_CONVENTIONAL_DRAFT_FACTS],
        "removed_terms": _unique_preserve_order(removed_terms)[:MAX_CONVENTIONAL_DRAFT_FACTS],
        "public_symbols": _unique_preserve_order(public_symbols)[:MAX_CONVENTIONAL_DRAFT_FACTS],
        "test_names": _unique_preserve_order(test_names)[:MAX_CONVENTIONAL_DRAFT_FACTS],
        "config_keys": _unique_preserve_order(config_keys)[:MAX_CONVENTIONAL_DRAFT_FACTS],
        "ui_actions": _unique_preserve_order(ui_actions)[:MAX_CONVENTIONAL_DRAFT_FACTS],
        "themes": _theme_facts(
            files, added_terms=added_terms, removed_terms=removed_terms, changed_settings=changed_settings
        ),
    }


def _patch_lines(patch: str, prefix: str) -> list[str]:
    lines: list[str] = []
    for line in patch.splitlines():
        if not line.startswith(prefix) or line.startswith((f"{prefix}{prefix}", f"{prefix}{'-' if prefix == '+' else '+'}")):
            continue
        content = line[1:].strip()
        if content:
            lines.append(content[:240])
    return lines[:200]


def _settings_from_lines(lines: list[str]) -> dict[str, str]:
    settings: dict[str, str] = {}
    for line in lines:
        for match in SETTING_PATTERN.finditer(line):
            name = match.group("quoted") or match.group("bare")
            value = match.group("quoted_value") or match.group("bare_value") or ""
            if not name:
                continue
            settings[name] = value.strip().strip('"')[:80]
    return settings


def _notable_terms(lines: list[str]) -> list[str]:
    terms: list[str] = []
    for line in lines:
        terms.extend(_term for groups in STRING_LITERAL_PATTERN.findall(line) for _term in groups if _term)
        terms.extend(IDENTIFIER_PATTERN.findall(line))
    return [
        term
        for term in _unique_preserve_order(terms)
        if _is_notable_term(term)
    ][:MAX_CONVENTIONAL_DRAFT_SYMBOLS]


def _public_symbols(lines: list[str]) -> list[str]:
    symbols: list[str] = []
    for line in lines:
        for pattern in PUBLIC_SYMBOL_PATTERNS:
            match = pattern.search(line)
            if match:
                symbols.append(match.group(1))
                break
    return _unique_preserve_order(symbols)


def _test_names(lines: list[str]) -> list[str]:
    names: list[str] = []
    for line in lines:
        for pattern in TEST_NAME_PATTERNS:
            match = pattern.search(line)
            if match:
                names.append(re.sub(r"\s+", " ", match.group(1)).strip())
                break
    return _unique_preserve_order(names)


def _config_keys(lines: list[str]) -> list[str]:
    keys: list[str] = []
    for line in lines:
        keys.extend(CONFIG_KEY_PATTERN.findall(line))
        for identifier in IDENTIFIER_PATTERN.findall(line):
            if any(marker in identifier.lower() for marker in ("privacy", "patch", "provider")):
                keys.append(identifier)
        for match in SETTING_PATTERN.finditer(line):
            name = match.group("quoted") or match.group("bare")
            if name and ("privacy" in name.lower() or "patch" in name.lower() or "provider" in name.lower()):
                keys.append(name)
    return _unique_preserve_order(keys)


def _ui_actions(lines: list[str]) -> list[str]:
    actions: list[str] = []
    for line in lines:
        actions.extend(UI_ACTION_PATTERN.findall(line))
        if "showWarningMessage" in line:
            actions.append("showWarningMessage")
        if "Prompt Preview" in line or "prompt preview" in line.lower():
            actions.append("promptPreview")
    return _unique_preserve_order(actions)


def _is_notable_term(term: str) -> bool:
    normalized = term.strip()
    if not normalized or len(normalized) > 80:
        return False
    lowered = normalized.lower()
    noise = {
        "const",
        "return",
        "string",
        "number",
        "boolean",
        "undefined",
        "private",
        "public",
        "function",
    }
    if lowered in noise:
        return False
    return any(
        marker in lowered
        for marker in (
            "suggest",
            "strategy",
            "batch",
            "cache",
            "warm",
            "instant",
            "provisional",
            "confidence",
            "provider",
            "model",
            "mcp",
            "release",
            "control",
            "setting",
            "privacy",
            "prompt",
            "preview",
            "remote",
            "patch",
            "raw",
            "preflight",
            "request",
            "envelope",
            "allow",
            "full-diff",
            "paired",
            "single",
            "auto",
        )
    )


def _theme_facts(
    files: list[ChangedFile],
    *,
    added_terms: list[str],
    removed_terms: list[str],
    changed_settings: list[dict[str, str]],
) -> list[str]:
    paths = " ".join(file.path.lower() for file in files)
    added = " ".join(term.lower() for term in added_terms)
    removed = " ".join(term.lower() for term in removed_terms)
    setting_names = " ".join(str(fact.get("name", "")).lower() for fact in changed_settings)
    combined = f"{paths} {added} {removed} {setting_names}"
    themes: list[str] = []
    if "modelprivacy" in combined or "trusted-provider-patch" in combined or "local-summary-then-ai" in combined:
        themes.append("adds model privacy settings for AI evidence")
    if "allowremotepatches" in combined or "raw_patch_chars_sent" in combined or "patch_excerpt" in combined:
        themes.append("gates raw patch evidence by privacy and provider mode")
    if "--privacy" in combined or "allow-remote-patches" in combined:
        themes.append("adds CLI privacy flags")
    if "preflight" in combined or "request_envelope" in combined:
        themes.append("adds preflight request envelope preview")
    if "draftpromptpreview" in combined or "promptpreview" in combined or "prompt preview" in combined:
        themes.append("updates prompt preview with AI request and privacy details")
    if "showwarningmessage" in combined or "send patch evidence" in combined or "allow remote patches" in combined:
        themes.append("adds confirmation for sending raw patch evidence")
    if "ai_request" in combined or "model_request_record" in combined or "request_hash" in combined:
        themes.append("records model request privacy metadata")
    if "suggestionstrategy" in added + setting_names or "full-diff-json" in added:
        themes.append("adds configurable suggestion strategy modes")
    if "suggestionbatchsize" in setting_names:
        themes.append("changes suggestion batch-size defaults or settings")
    if "instant" in removed or "provisional" in removed:
        themes.append("removes instant provisional suggestion behavior")
    if "releasecontrolview" in paths or "releasecontrolmarkup" in paths:
        themes.append("updates the VS Code release control UI")
    if "commands" in paths or "registercommands" in paths:
        themes.append("updates VS Code commands")
    if "test" in paths:
        themes.append("updates tests for the changed behavior")
    if "readme" in paths or "architecture.md" in paths:
        themes.append("updates documentation")
    return themes[:MAX_CONVENTIONAL_DRAFT_FACTS]


def _unique_preserve_order(values: list[str]) -> list[str]:
    seen: set[str] = set()
    result: list[str] = []
    for value in values:
        normalized = str(value).strip()
        key = normalized.lower()
        if not normalized or key in seen:
            continue
        seen.add(key)
        result.append(normalized)
    return result


def _parse_json_object(content: str) -> dict[str, Any] | None:
    text = content.strip()
    if text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text, flags=re.IGNORECASE).strip()
        text = re.sub(r"\s*```$", "", text).strip()
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def _normalized_type(value: object) -> str | None:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in ALLOWED_CONVENTIONAL_TYPES else None


def _normalized_scope(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip().lower()
    if not normalized:
        return None
    normalized = normalized.replace("\\", "/")
    return normalized if SCOPE_PATTERN.match(normalized) else None


def _normalized_description(value: object) -> str | None:
    description = str(value or "").strip()
    if not description:
        return None
    return description[:160]


def _normalized_body(value: object) -> list[str]:
    if isinstance(value, str):
        candidates = value.splitlines()
    elif isinstance(value, list):
        candidates = [str(item) for item in value]
    else:
        candidates = []
    body: list[str] = []
    for candidate in candidates:
        normalized = re.sub(r"^\s*[-*]\s+", "", candidate).strip()
        normalized = re.sub(r"\s+", " ", normalized)
        if normalized:
            body.append(normalized[:220])
    return _unique_preserve_order(body)[:MAX_CONVENTIONAL_DRAFT_BODY_ITEMS]


def _coerce_confidence(value: object) -> float:
    if isinstance(value, (int, float)):
        return max(0.0, min(float(value), 1.0))
    if isinstance(value, str):
        try:
            return max(0.0, min(float(value), 1.0))
        except ValueError:
            return 0.0
    return 0.0


def _coerce_bool(value: object) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes"}
    return bool(value)


def _is_provider_error(content: str) -> bool:
    lowered = (content or "").strip().lower()
    return lowered.startswith("[") and any(marker in lowered for marker in ("error", "unavailable", "empty"))
