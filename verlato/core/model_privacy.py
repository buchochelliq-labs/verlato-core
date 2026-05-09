from __future__ import annotations

import hashlib
import json
import os
from typing import Any

DEFAULT_MODEL_PRIVACY = "metadata-only"
MODEL_PRIVACY_CHOICES = (
    "metadata-only",
    "local-summary-then-ai",
    "trusted-provider-patch",
)
LOCAL_PATCH_PROVIDERS = {"ollama", "local", "stub", "mock"}
RAW_PATCH_FIELDS = {
    "patch",
    "patch_excerpt",
    "representative_hunks",
    "hunks",
    "diff_hunks",
}
OMITTED_PATCH_EVIDENCE = ["raw patch text", "patch excerpts", "representative diff hunks"]
OMITTED_COMMIT_PATCH_EVIDENCE = ["raw patch text", "patch excerpts"]
RAW_PATCH_MARKERS = ("diff --git", "\n@@", "@@ ", "\n+++", "\n---")


def normalize_model_privacy(value: object, *, default: str = DEFAULT_MODEL_PRIVACY) -> str:
    normalized = str(value or "").strip().lower()
    return normalized if normalized in MODEL_PRIVACY_CHOICES else default


def resolve_model_privacy(
    configured: object = None,
    *,
    requested: object = None,
    allow_remote_patches: bool = False,
) -> str:
    if allow_remote_patches:
        return "trusted-provider-patch"
    env_value = os.getenv("VERLATO_MODEL_PRIVACY")
    return normalize_model_privacy(requested or env_value or configured or DEFAULT_MODEL_PRIVACY)


def normalize_provider_mode(provider_mode: object) -> str:
    return str(provider_mode or "").strip().lower()


def is_local_patch_provider(provider_mode: object) -> bool:
    return normalize_provider_mode(provider_mode) in LOCAL_PATCH_PROVIDERS


def can_send_raw_patches(provider_mode: object, privacy_mode: object) -> bool:
    privacy = normalize_model_privacy(privacy_mode)
    if privacy == "metadata-only":
        return False
    if privacy == "local-summary-then-ai":
        return is_local_patch_provider(provider_mode)
    if privacy == "trusted-provider-patch":
        return True
    return False


def strictest_privacy_mode(*modes: object) -> str:
    rank = {
        "metadata-only": 0,
        "local-summary-then-ai": 1,
        "trusted-provider-patch": 2,
    }
    normalized = [normalize_model_privacy(mode) for mode in modes if mode is not None]
    if not normalized:
        return DEFAULT_MODEL_PRIVACY
    return min(normalized, key=lambda mode: rank[mode])


def estimate_token_count(chars: int) -> int:
    return max((max(chars, 0) + 3) // 4, 0)


def stable_json_hash(value: Any) -> str:
    payload = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def prompt_hash(prompt: str) -> str:
    return hashlib.sha256(prompt.encode("utf-8")).hexdigest()


def model_request_record(
    *,
    task: str,
    provider: str | None,
    model: str | None = None,
    privacy_mode: str,
    prompt: str,
    evidence: Any,
    raw_patch_chars_available: int,
    raw_patch_chars_sent: int,
    omitted_fields: list[str] | None = None,
    omission_reasons: list[str] | None = None,
) -> dict[str, Any]:
    evidence_chars = len(json.dumps(evidence, sort_keys=True, default=str))
    prompt_chars = len(prompt)
    return {
        "task": task,
        "provider": provider or "configured",
        "model": model,
        "privacy_mode": normalize_model_privacy(privacy_mode),
        "request_hash": stable_json_hash(
            {
                "task": task,
                "provider": provider,
                "model": model,
                "privacy_mode": normalize_model_privacy(privacy_mode),
                "prompt_hash": prompt_hash(prompt),
                "evidence_hash": stable_json_hash(evidence),
            }
        ),
        "prompt_hash": prompt_hash(prompt),
        "evidence_hash": stable_json_hash(evidence),
        "prompt_chars": prompt_chars,
        "evidence_chars": evidence_chars,
        "estimated_tokens": estimate_token_count(prompt_chars + evidence_chars),
        "raw_patch_chars_available": max(raw_patch_chars_available, 0),
        "raw_patch_chars_sent": max(raw_patch_chars_sent, 0),
        "omitted_fields": sorted(set(omitted_fields or [])),
        "omission_reasons": sorted(set(omission_reasons or [])),
        "contains_raw_patch_markers": contains_raw_patch_markers({"prompt": prompt, "evidence": evidence}),
    }


def privacy_summary(
    *,
    provider: str | None,
    privacy_mode: str,
    raw_patch_chars_available: int,
    raw_patch_chars_sent: int,
) -> dict[str, Any]:
    sends_patches = raw_patch_chars_sent > 0
    return {
        "mode": normalize_model_privacy(privacy_mode),
        "provider": provider or "configured",
        "raw_patch_chars_available": max(raw_patch_chars_available, 0),
        "raw_patch_chars_sent": max(raw_patch_chars_sent, 0),
        "will_send": [
            "change summary",
            "PR metadata",
            "commit metadata",
            "file paths and statuses",
            "deterministic diff facts",
        ]
        + (["raw patches"] if sends_patches else []),
        "will_not_send": [] if sends_patches else ["raw patches", "patch excerpts", "representative hunks"],
    }


def contains_raw_patch_markers(value: Any) -> bool:
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in RAW_PATCH_FIELDS:
                return True
            if contains_raw_patch_markers(item):
                return True
        return False
    if isinstance(value, list | tuple | set):
        return any(contains_raw_patch_markers(item) for item in value)
    if isinstance(value, str):
        return any(marker in value for marker in RAW_PATCH_MARKERS)
    return False
