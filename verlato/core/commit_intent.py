from __future__ import annotations

import json
import re
from dataclasses import asdict
from typing import Any

from verlato.core.conventional import ConventionalCommit, parse_conventional_commit
from verlato.core.models import ChangedFile, Commit, CommitIntent
from verlato.providers.base import ProviderRequest, VerlatoProvider

COMMIT_INTENT_INFERENCE_CHOICES = {"off", "low-signal"}
COMMIT_INTENT_MIN_CONFIDENCE = 0.65
MAX_COMMIT_INTENT_FILES = 8
MAX_COMMIT_INTENT_PATCH_CHARS = 900
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


def normalize_commit_intent_mode(value: object, *, default: str = "off") -> str:
    normalized = str(value or "").strip().lower()
    if normalized in COMMIT_INTENT_INFERENCE_CHOICES:
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
    def __init__(self, provider: VerlatoProvider, *, min_confidence: float = COMMIT_INTENT_MIN_CONFIDENCE) -> None:
        self.provider = provider
        self.min_confidence = min_confidence

    def infer(self, commit: Commit) -> CommitIntent | None:
        if not is_low_signal_commit(commit):
            return None
        response = self.provider.summarize(
            ProviderRequest(
                task="infer-conventional",
                prompt=build_commit_intent_prompt(commit),
                evidence=commit_intent_evidence(commit),
                response_format=commit_intent_response_format(),
            )
        )
        if _is_provider_error(response.content):
            return None
        return parse_commit_intent_response(
            response.content,
            model=response.model,
            min_confidence=self.min_confidence,
        )


def infer_commit_intents(commits: list[Commit], provider: VerlatoProvider) -> int:
    inferer = CommitIntentInferer(provider)
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


def parse_commit_intent_response(
    content: str,
    *,
    model: str,
    min_confidence: float = COMMIT_INTENT_MIN_CONFIDENCE,
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
        source="model-inferred",
        rationale=rationale,
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


def build_commit_intent_prompt(commit: Commit) -> str:
    payload = commit_intent_evidence(commit)
    return (
        "Infer the conventional commit that should have been written for this exact commit.\n"
        "Use only the provided commit message, file paths, statuses, and patch excerpts.\n"
        "Return strict JSON matching the schema. Prefer a concise scope such as api, ui, cli, docs, tests, infra, "
        "mcp, vscode, config, auth, release, or cache when supported by the evidence.\n"
        "Do not invent product behavior not shown in the patch.\n\n"
        f"COMMIT_EVIDENCE:\n{json.dumps(payload, indent=2, sort_keys=True)}"
    )


def commit_intent_evidence(commit: Commit) -> dict[str, Any]:
    return {
        "sha": commit.sha,
        "subject": commit.message.splitlines()[0] if commit.message else "",
        "message": commit.message,
        "files": [_file_payload(file) for file in commit.files[:MAX_COMMIT_INTENT_FILES]],
        "files_total": len(commit.files),
    }


def commit_intent_payload(intent: CommitIntent | None) -> dict[str, Any] | None:
    if intent is None:
        return None
    return asdict(intent)


def _file_payload(file: ChangedFile) -> dict[str, Any]:
    patch = file.patch or ""
    return {
        "path": file.path,
        "status": file.status,
        "patch_excerpt": patch[:MAX_COMMIT_INTENT_PATCH_CHARS],
        "patch_chars": len(patch),
        "patch_truncated": len(patch) > MAX_COMMIT_INTENT_PATCH_CHARS,
    }


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
