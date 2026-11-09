from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
from collections import Counter
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import date, datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any
from urllib import error as urlerror
from urllib import request as urlrequest

from verlato.config import ProviderConfig, VerlatoConfig, load_config
from verlato.core.adapters import NullPullRequestAdapter, PullRequestAdapter, StandardGitAdapter
from verlato.core.change_summary import build_change_summary_payload, raw_patch_chars_available
from verlato.core.classifier import Classifier
from verlato.core.collector import Collector
from verlato.core.commit_intent import (
    commit_intent_payload,
    conventional_commit_message,
    derive_conventional_commits,
    is_low_signal_commit,
    normalize_commit_intent_mode,
)
from verlato.core.drafts import DraftDocument, DraftService
from verlato.core.grouper import Grouper
from verlato.core.model_privacy import (
    MODEL_PRIVACY_CHOICES,
    OMITTED_PATCH_EVIDENCE,
    can_send_raw_patches,
    model_request_record,
    normalize_model_privacy,
    privacy_summary,
    resolve_model_privacy,
)
from verlato.core.models import ChangeGroup, ChangeItem, Commit, changed_files_for_group
from verlato.core.reconciler import Reconciler
from verlato.core.semantic import (
    DIFF_ENGINE_CHOICES,
    IntentumDiffEngine,
    build_semantic_engine,
    render_semantic_diff,
    resolve_diff_engine,
    semantic_diff_for_prompt,
    semantic_engine_info,
    semantic_prompt_payload,
)
from verlato.core.suggestion_tools import SuggestionEvidenceToolProvider
from verlato.github.adapter import GitHubApiAdapter
from verlato.providers.base import ProviderRequest, ProviderResponse
from verlato.providers.factory import build_provider
from verlato.providers.model_logging import (
    append_log_line,
    model_payload_redaction_enabled,
    print_stderr_line,
    print_stderr_lines,
    redact_model_payload,
)
from verlato.state.store import StateStore
from verlato.state.suggestion_cache import CachedSuggestion, SuggestionCacheBackend, build_suggestion_cache_backend

MODEL_PROVIDER_CHOICES = ["stub", "mock", "ollama", "litellm", "plugin", "github-copilot", "codex-cli", "claude-code"]
SUGGESTION_TONE_CHOICES = ["release-note", "concise", "technical", "customer-facing"]
SUGGESTION_PROMPT_PROFILE_CHOICES = ["conservative", "lean", "minimal"]
SUGGESTION_STRATEGY_CHOICES = ["auto", "single", "paired", "full-diff-json", "tool-retrieval"]
MODEL_PRIVACY_MODE_CHOICES = list(MODEL_PRIVACY_CHOICES)
MAX_SUGGESTION_PARALLELISM = 8
MAX_SUGGESTION_BATCH_SIZE = 5
SUGGESTION_CACHE_PROMPT_PROFILE = "suggestion-conservative"
SINGLE_SUGGESTION_PROMPT_SHAPE = "single-prompt-v1"
PAIRED_SUGGESTION_PROMPT_SHAPE = "paired-prepared-prompt-v1"
FULL_DIFF_SUGGESTION_PROMPT_SHAPE = "full-diff-json-v1"
TOOL_RETRIEVAL_SUGGESTION_PROMPT_SHAPE = "tool-retrieval-v1"
FULL_DIFF_BATCH_PROMPT_CHAR_GUARD = 80_000
SUGGESTION_TOOL_MAX_ROUNDS = 4


@dataclass
class _SuggestionWork:
    order: int
    item_number: int
    batch: int
    batch_total: int
    total_targets: int
    group: ChangeGroup
    baseline_item: ChangeItem
    prompt: str
    evidence: dict[str, Any]
    source_evidence: list[dict[str, str]]
    prompt_sha: str
    prompt_budget: dict[str, Any]
    cache_key: str
    strategy: str
    prompt_shape: str
    full_diff_item: dict[str, Any] | None
    full_diff_file_count: int
    full_diff_patch_chars: int
    full_diff_missing_patch_count: int
    tools: list[dict[str, Any]]
    tool_executor: Callable[[str, dict[str, Any]], Any] | None
    max_tool_rounds: int
    suggestion_count: int
    suggestion_tone: str
    suggestion_style: dict[str, Any]
    effective_model_provider: str | None
    evidence_ms: int
    prompt_ms: int
    started_at: float


@dataclass
class _SuggestionResult:
    order: int
    item: dict[str, Any]
    model_ms: int
    parse_ms: int
    aggregate_model_ms: int = 0
    model_call_count: int = 0
    batched_model_calls: int = 0
    single_model_calls: int = 0
    bulk_model_calls: int = 0
    fallback_single_calls: int = 0
    fallback_item_count: int = 0
    full_diff_prompt_chars: int = 0


@dataclass
class _SuggestionBatchOutcome:
    results: list[_SuggestionResult]
    fallback_works: list[_SuggestionWork]
    model_ms: int = 0
    model_call_count: int = 0
    batched_model_calls: int = 0
    bulk_model_calls: int = 0
    full_diff_prompt_chars: int = 0


class _SuggestionWorkFailure(Exception):
    def __init__(
        self,
        work: _SuggestionWork,
        exc: BaseException,
        completed_results: list[_SuggestionResult] | None = None,
    ) -> None:
        self.work = work
        self.exc = exc
        self.completed_results = completed_results or []
        super().__init__(str(exc))


class _SuggestionBatchFallback(Exception):
    def __init__(
        self,
        works: list[_SuggestionWork],
        *,
        model_ms: int = 0,
        model_call_count: int = 0,
        batched_model_calls: int = 0,
        bulk_model_calls: int = 0,
        full_diff_prompt_chars: int = 0,
    ) -> None:
        self.works = works
        self.model_ms = model_ms
        self.model_call_count = model_call_count
        self.batched_model_calls = batched_model_calls
        self.bulk_model_calls = bulk_model_calls
        self.full_diff_prompt_chars = full_diff_prompt_chars
        super().__init__("Batch suggestion response could not be used.")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="verlato")
    parser.add_argument(
        "--diff-engine",
        choices=list(DIFF_ENGINE_CHOICES),
        help="Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches).",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("scan", "generate", "reconcile"):
        p = sub.add_parser(name)
        p.add_argument("--base")
        p.add_argument("--head", default="HEAD")
        p.add_argument("--from-latest-tag", action="store_true")
        p.add_argument("--from-latest-published", action="store_true")
        p.add_argument("--model-provider", choices=MODEL_PROVIDER_CHOICES, help="Override the configured model provider.")
        _add_commit_intent_inference_args(p)
        _add_model_privacy_args(p)

    draft = sub.add_parser("draft")
    draft_sub = draft.add_subparsers(dest="draft_command", required=True)
    for name in ("preview", "json", "apply"):
        p = draft_sub.add_parser(name)
        p.add_argument("--base")
        p.add_argument("--head", default="HEAD")
        p.add_argument("--from-latest-tag", action="store_true")
        p.add_argument("--from-latest-published", action="store_true")
        p.add_argument("--version", default="Unreleased")
        p.add_argument("--include-confidence", action="store_true")
        p.add_argument("--include-provenance", action="store_true")
        p.add_argument("--model-provider", choices=MODEL_PROVIDER_CHOICES, help="Override the configured model provider.")
        _add_commit_intent_inference_args(p)
        _add_model_privacy_args(p)
    draft_apply = draft_sub.choices["apply"]
    draft_apply.add_argument("--target", choices=["changelog", "github-release"], default="changelog")
    draft_apply.add_argument("--file")
    draft_review = draft_sub.add_parser("review")
    draft_review.add_argument("--base")
    draft_review.add_argument("--head", default="HEAD")
    draft_review.add_argument("--from-latest-tag", action="store_true")
    draft_review.add_argument("--from-latest-published", action="store_true")
    draft_review.add_argument("--id", required=True)
    draft_review.add_argument("--status", choices=["needs-review", "ready", "accepted", "edited", "hidden"])
    draft_review.add_argument("--summary")
    draft_review.add_argument("--summary-provenance-json", help=argparse.SUPPRESS)
    draft_review.add_argument("--reviewer")
    draft_review.add_argument("--model-provider", choices=MODEL_PROVIDER_CHOICES, help="Override the configured model provider.")
    _add_commit_intent_inference_args(draft_review)
    _add_model_privacy_args(draft_review)
    draft_suggest = draft_sub.add_parser("suggest")
    draft_suggest.add_argument("--base")
    draft_suggest.add_argument("--head", default="HEAD")
    draft_suggest.add_argument("--from-latest-tag", action="store_true")
    draft_suggest.add_argument("--from-latest-published", action="store_true")
    draft_suggest.add_argument("--id")
    draft_suggest.add_argument("--all-visible", action="store_true")
    draft_suggest.add_argument("--preview", action="store_true")
    draft_suggest.add_argument(
        "--preflight",
        action="store_true",
        help="Build planned AI request envelopes without calling the provider.",
    )
    draft_suggest.add_argument("--count", type=int)
    draft_suggest.add_argument("--parallelism", type=int)
    draft_suggest.add_argument("--batch-size", type=int)
    draft_suggest.add_argument("--strategy", choices=SUGGESTION_STRATEGY_CHOICES)
    draft_suggest.add_argument("--prompt-profile", choices=SUGGESTION_PROMPT_PROFILE_CHOICES)
    draft_suggest.add_argument("--adaptive-recommendations", action="store_true")
    draft_suggest.add_argument("--suggest-model")
    draft_suggest.add_argument("--no-cache", action="store_true")
    draft_suggest.add_argument("--refresh-cache", action="store_true")
    draft_suggest.add_argument("--tone", choices=SUGGESTION_TONE_CHOICES)
    draft_suggest.add_argument("--reviewer")
    draft_suggest.add_argument(
        "--trace-suggestions",
        action="store_true",
        help="Emit compact suggestion lifecycle trace logs for this invocation.",
    )
    draft_suggest.add_argument(
        "--model-provider",
        choices=MODEL_PROVIDER_CHOICES,
        help="Override the configured model provider.",
    )
    _add_commit_intent_inference_args(draft_suggest)
    _add_model_privacy_args(draft_suggest)
    draft_prompt = draft_sub.add_parser("prompt")
    draft_prompt.add_argument("--base")
    draft_prompt.add_argument("--head", default="HEAD")
    draft_prompt.add_argument("--from-latest-tag", action="store_true")
    draft_prompt.add_argument("--from-latest-published", action="store_true")
    draft_prompt.add_argument("--id", required=True)
    draft_prompt.add_argument("--tone", choices=SUGGESTION_TONE_CHOICES)
    draft_prompt.add_argument("--model-provider", choices=MODEL_PROVIDER_CHOICES, help="Optional provider hint for the previewed prompt.")
    _add_commit_intent_inference_args(draft_prompt)
    _add_model_privacy_args(draft_prompt)

    render = sub.add_parser("render")
    render.add_argument("--format", choices=["github-release", "changelog"], required=True)
    render.add_argument("--version", default="Unreleased")
    render.add_argument("--include-confidence", action="store_true")
    render.add_argument("--include-provenance", action="store_true")

    sub.add_parser("serve")
    sub.add_parser("service")

    commit = sub.add_parser("commit")
    commit_sub = commit.add_subparsers(dest="commit_command", required=True)
    commit_conventional = commit_sub.add_parser("conventional")
    commit_conventional.add_argument("--base", required=True)
    commit_conventional.add_argument("--head", default="HEAD")
    commit_conventional.add_argument("--sha", required=True)
    commit_conventional.add_argument("--model-provider", choices=MODEL_PROVIDER_CHOICES, help="Override the configured model provider.")
    commit_conventional.add_argument(
        "--evidence-mode",
        choices=["diff-only", "subject-hint"],
        default="diff-only",
        help="Use diff-only to omit authored commit text, or subject-hint to include the authored subject/message.",
    )
    _add_model_privacy_args(commit_conventional)

    doctor = sub.add_parser("doctor")
    doctor_sub = doctor.add_subparsers(dest="doctor_command", required=True)
    doctor_ollama = doctor_sub.add_parser("ollama")
    doctor_ollama.add_argument(
        "--parallel-probe",
        action="store_true",
        help="Run two tiny Ollama suggestion probes concurrently to detect server queueing.",
    )
    doctor_ollama.add_argument(
        "--warmup",
        action="store_true",
        help="Run a tiny suggestion-style warmup request before measuring Ollama health.",
    )
    doctor_ollama.add_argument(
        "--adaptive-recommendations",
        action="store_true",
        help="Include opt-in adaptive suggestion tuning recommendations in the JSON output.",
    )
    doctor_ollama.add_argument(
        "--suggest-model",
        help="Use a specific Ollama model for the suggestion-style health probe.",
    )
    doctor_sub.add_parser("engine", help="Report the IntentumDiff semantic engine status.")
    doctor_model = doctor_sub.add_parser("model")
    doctor_model.add_argument("--model-provider", choices=MODEL_PROVIDER_CHOICES, help="Override the configured model provider.")
    doctor_model.add_argument("--suggest-model", help="Use a specific model for the suggestion-style health probe.")
    doctor_model.add_argument(
        "--timeout-seconds",
        type=float,
        help="Advisory timeout budget used to label slow probes. Provider-specific timeouts still come from config/env.",
    )

    model_logs = sub.add_parser("model-logs")
    model_logs_sub = model_logs.add_subparsers(dest="model_logs_command", required=True)
    model_logs_export = model_logs_sub.add_parser("export")
    model_logs_export.add_argument("--file", required=True, help="JSONL model debug log produced by VERLATO_MODEL_LOG_PATH.")
    model_logs_export.add_argument("--format", choices=["json", "jsonl"], default="json")
    model_logs_export.add_argument("--output", help="Optional output path. Defaults to stdout.")
    model_logs_export.add_argument("--event", action="append", help="Filter to one event name. Can be provided more than once.")

    feedback = sub.add_parser("feedback")
    feedback_sub = feedback.add_subparsers(dest="feedback_command", required=True)
    feedback_record = feedback_sub.add_parser("record")
    feedback_record.add_argument("--event", required=True)
    feedback_record.add_argument("--item-id", required=True)
    feedback_record.add_argument("--metadata-json")
    feedback_export = feedback_sub.add_parser("export")
    feedback_export.add_argument("--format", choices=["json", "jsonl"], default="json")
    feedback_export.add_argument("--output")
    feedback_export.add_argument(
        "--event",
        action="append",
        help="Filter to one feedback event type. Can be provided more than once.",
    )
    feedback_sub.add_parser("recommend")

    state = sub.add_parser("state")
    state_sub = state.add_subparsers(dest="state_command", required=True)
    state_sub.add_parser("show")
    state_sub.add_parser("clear-working")
    state_sub.add_parser("clear-suggestion-cache")

    mark_published = state_sub.add_parser("mark-published")
    mark_published.add_argument("--version", required=True)
    mark_published.add_argument("--ids", nargs="*")
    mark_published.add_argument("--all-unpublished", action="store_true")

    return parser


def _add_commit_intent_inference_args(parser: argparse.ArgumentParser) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument(
        "--infer-commit-intents",
        action="store_true",
        help="Infer conventional commit intent for low-signal commits before grouping.",
    )
    group.add_argument(
        "--no-infer-commit-intents",
        action="store_true",
        help="Disable inferred conventional commit intent for this command.",
    )


def _add_model_privacy_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--privacy",
        choices=MODEL_PRIVACY_MODE_CHOICES,
        help="Privacy mode for AI model evidence. Defaults to metadata-only.",
    )
    parser.add_argument(
        "--allow-remote-patches",
        action="store_true",
        help="Explicitly opt into trusted-provider-patch mode for this invocation.",
    )


def _build_store() -> StateStore:
    config = _load_config()
    path = os.getenv("VERLATO_STATE_PATH", config.state_path)
    return StateStore(path=path)


def _build_suggestion_cache_store(config: VerlatoConfig | None = None) -> SuggestionCacheBackend:
    config = config or _load_config()
    backend = _resolve_suggestion_cache_backend(config.suggestion_cache_backend)
    path = os.getenv("VERLATO_SUGGESTION_CACHE_PATH", config.suggestion_cache_path)
    ttl_days = _resolve_suggestion_cache_ttl_days(config.suggestion_cache_ttl_days)
    return build_suggestion_cache_backend(backend=backend, path=path, ttl_days=ttl_days)


def _build_semantic_engine(requested: str | None = None) -> IntentumDiffEngine | None:
    """The IntentumDiff engine that powers Verlato's change evidence (None for legacy git)."""

    config = _load_config()
    engine = resolve_diff_engine(config.diff_engine, requested=requested)
    return build_semantic_engine(
        engine,
        cache_path=config.semantic_cache_path,
        parallelism=config.semantic_parallelism,
        cross_file=config.semantic_cross_file,
    )


def _semantic_progress(current: int, total: int, sha: str) -> None:
    _trace_progress(
        "semantic_review",
        "IntentumDiff semantic review",
        detail=sha[:12],
        current=current,
        total=total,
        unit="commit",
    )


def _annotate_semantics(commits: list[Commit]) -> None:
    """Run IntentumDiff over an explicit commit selection (e.g. a single commit to draft)."""

    engine = _build_semantic_engine()
    if engine is not None and commits:
        engine.annotate(commits, progress=_semantic_progress)


def _diff_engine_info() -> dict[str, Any]:
    config = _load_config()
    engine = resolve_diff_engine(config.diff_engine)
    info = semantic_engine_info(engine)
    info.update(
        {
            "configured": config.diff_engine,
            "semantic_cache_path": os.getenv("VERLATO_SEMANTIC_CACHE_PATH", config.semantic_cache_path),
            "semantic_cross_file": config.semantic_cross_file,
        }
    )
    return info


def _build_collector(*, semantic: bool = True, diff_engine: str | None = None) -> Collector:
    return Collector(
        pr_adapter=_build_adapter(),
        semantic_engine=_build_semantic_engine(diff_engine) if semantic else None,
        semantic_progress=_semantic_progress,
    )


def _build_adapter() -> PullRequestAdapter:
    mode = os.getenv("VERLATO_PR_ADAPTER", "standard-git").lower()
    if mode == "none":
        return NullPullRequestAdapter()
    if mode == "github":
        return GitHubApiAdapter.from_env() or StandardGitAdapter()
    return StandardGitAdapter()


def _trace_progress_enabled() -> bool:
    return os.getenv("VERLATO_TRACE_PROGRESS") == "1"


def _trace_progress(
    stage: str,
    label: str,
    *,
    detail: str | None = None,
    current: int | None = None,
    total: int | None = None,
    unit: str | None = None,
    **attributes: object,
) -> None:
    if not _trace_progress_enabled():
        return
    event: dict[str, Any] = {
        "event": "progress",
        "stage": stage,
        "label": label,
    }
    if detail:
        event["detail"] = detail
    if current is not None:
        event["current"] = current
    if total is not None:
        event["total"] = total
    if unit:
        event["unit"] = unit
    event.update({key: value for key, value in attributes.items() if value is not None})
    print_stderr_line("[verlato:progress] json " + json.dumps(event, sort_keys=True))


def _trace_suggestion_event(event: str, **attributes: object) -> None:
    if not _trace_progress_enabled():
        return
    payload: dict[str, Any] = {"event": event}
    payload.update({key: value for key, value in attributes.items() if value is not None})
    print_stderr_line("[verlato:suggestion] json " + json.dumps(payload, sort_keys=True))


def _group_progress_detail(group: ChangeGroup) -> str:
    if group.pr:
        return f"PR #{group.pr.number}"
    if group.commits:
        return group.commits[0].sha[:12]
    return "changeset"


def _classify_groups(
    groups: list[ChangeGroup],
    classifier: Classifier,
    base: str,
    head: str,
    *,
    stage: str = "classifying_changes",
    label: str = "Classifying changesets",
) -> list[ChangeItem]:
    total = len(groups)
    if total == 0:
        _trace_progress(stage, label, current=0, total=0, unit="changeset")
        return []
    items: list[ChangeItem] = []
    for index, group in enumerate(groups):
        detail = _group_progress_detail(group)
        _trace_progress(stage, label, detail=detail, current=index, total=total, unit="changeset")
        classified = classifier.classify(group, base, head)
        items.extend(classified)
        item_id = classified[0].id if classified else detail
        _trace_progress(stage, "Classified changeset", detail=item_id, current=index + 1, total=total, unit="changeset")
    return items


def run_scan(
    base: str,
    head: str,
    persist: bool,
    model_provider: str | None = None,
    commit_intent_inference: str | None = None,
    model_privacy: str | None = None,
    allow_remote_patches: bool = False,
) -> list[dict]:
    return [
        asdict(i)
        for i in scan_change_items(
            base,
            head,
            persist,
            model_provider=model_provider,
            commit_intent_inference=commit_intent_inference,
            model_privacy=model_privacy,
            allow_remote_patches=allow_remote_patches,
        )
    ]


def detect_change_items(
    base: str,
    head: str,
    model_provider: str | None = None,
    commit_intent_inference: str | None = None,
    model_privacy: str | None = None,
    allow_remote_patches: bool = False,
) -> list[ChangeItem]:
    config = _load_config()
    privacy_mode = resolve_model_privacy(
        config.model_privacy,
        requested=model_privacy,
        allow_remote_patches=allow_remote_patches,
    )
    groups = _collect_groups(
        base,
        head,
        model_provider=model_provider,
        commit_intent_inference=commit_intent_inference,
        model_privacy=privacy_mode,
        allow_remote_patches=allow_remote_patches,
    )
    max_model_items = _resolve_max_model_items(config.provider.max_items_per_scan)
    provider = None
    if max_model_items < 0 or len(groups) <= max_model_items:
        provider = build_provider(config.provider, mode=model_provider)
    style = _resolve_suggestion_style(config)
    classifier = Classifier(
        provider=provider,
        summary_tone=_resolve_suggestion_tone(config.suggestion_tone),
        style_guidance=str(style["guidance"]),
        project_vocabulary=list(style["project_vocabulary"]),
        banned_phrases=list(style["banned_phrases"]),
        model_privacy=privacy_mode,
        provider_mode=model_provider or config.provider.name,
    )
    return _classify_groups(groups, classifier, base, head)


def _collect_groups(
    base: str,
    head: str,
    *,
    model_provider: str | None = None,
    commit_intent_inference: str | None = None,
    model_privacy: str | None = None,
    allow_remote_patches: bool = False,
):
    collector = _build_collector()
    grouper = Grouper()
    _trace_progress("collecting_commits", "Collecting commits", detail=f"{base}..{head}")
    commits = collector.collect_commits(base, head)
    _maybe_infer_commit_intents(
        commits,
        model_provider=model_provider,
        commit_intent_inference=commit_intent_inference,
        model_privacy=model_privacy,
        allow_remote_patches=allow_remote_patches,
    )
    _trace_progress(
        "mapping_pull_requests",
        "Mapping commits to PRs",
        current=len(commits),
        total=len(commits),
        unit="commit",
    )
    prs_by_commit = collector.map_commits_to_prs(commits)
    _trace_progress(
        "grouping_changesets",
        "Grouping changesets",
        detail=f"{len(commits)} commit(s)",
        current=0,
        total=len(commits),
        unit="commit",
    )
    groups = grouper.group(commits, prs_by_commit)
    _trace_progress(
        "grouping_changesets",
        "Grouped changesets",
        current=len(groups),
        total=len(groups),
        unit="changeset",
    )
    return groups


def _maybe_infer_commit_intents(
    commits: list[Commit],
    *,
    model_provider: str | None,
    commit_intent_inference: str | None,
    model_privacy: str | None = None,
    allow_remote_patches: bool = False,
) -> None:
    config = _load_config()
    mode = _resolve_commit_intent_inference(config.commit_intent_inference, requested=commit_intent_inference)
    privacy_mode = resolve_model_privacy(
        config.model_privacy,
        requested=model_privacy,
        allow_remote_patches=allow_remote_patches,
    )
    if mode == "off":
        return
    eligible = [commit for commit in commits if is_low_signal_commit(commit)]
    total = len(commits)
    _trace_progress(
        "inferring_commit_intents",
        "Deriving conventional commit intents from diffs",
        detail=f"{total} commit(s), {len(eligible)} eligible for grouping inference",
        current=0,
        total=total,
        unit="commit",
    )
    if not commits:
        return
    max_model_items = _resolve_max_model_items(config.provider.max_items_per_scan)
    if max_model_items >= 0 and total > max_model_items:
        _trace_progress(
            "inferring_commit_intents",
            "Skipped commit intent inference",
            detail=f"{total} commit(s) exceeds fanout guard {max_model_items}",
            current=0,
            total=total,
            unit="commit",
        )
        return
    try:
        provider = build_provider(config.provider, mode=model_provider)
        dispatch_privacy_mode = (
            privacy_mode
            if _can_send_raw_patches_for_config(config.provider, model_provider or config.provider.name, privacy_mode)
            else "metadata-only"
        )
        derived = derive_conventional_commits(
            commits,
            provider,
            privacy_mode=dispatch_privacy_mode,
            provider_mode=model_provider or config.provider.name,
        )
        inferred = 0
        for commit in eligible:
            if commit.derived_intent is None:
                continue
            commit.inferred_intent = commit.derived_intent
            inferred += 1
    except Exception as exc:
        _trace_progress(
            "inferring_commit_intents",
            "Commit intent inference unavailable",
            detail=str(exc),
            current=0,
            total=total,
            unit="commit",
        )
        return
    _trace_progress(
        "inferring_commit_intents",
        "Derived conventional commit intents",
        detail=f"{derived}/{total} derived; {inferred}/{len(eligible)} used for grouping inference",
        current=total,
        total=total,
        unit="commit",
    )


def _find_group_for_item_id(
    base: str,
    head: str,
    item_id: str,
    *,
    model_provider: str | None = None,
    commit_intent_inference: str | None = None,
    model_privacy: str | None = None,
    allow_remote_patches: bool = False,
) -> ChangeGroup:
    groups = _collect_groups(
        base,
        head,
        model_provider=model_provider,
        commit_intent_inference=commit_intent_inference,
        model_privacy=model_privacy,
        allow_remote_patches=allow_remote_patches,
    )
    baseline_classifier = Classifier()
    for group in groups:
        baseline_item = baseline_classifier.classify(group, base, head)[0]
        if baseline_item.id == item_id:
            return group
    raise SystemExit(f"Could not find draft item {item_id} for range {base}..{head}")


def scan_change_items(
    base: str,
    head: str,
    persist: bool,
    model_provider: str | None = None,
    commit_intent_inference: str | None = None,
    model_privacy: str | None = None,
    allow_remote_patches: bool = False,
) -> list[ChangeItem]:
    store = _build_store()
    reconciler = Reconciler()

    detected = detect_change_items(
        base,
        head,
        model_provider=model_provider,
        commit_intent_inference=commit_intent_inference,
        model_privacy=model_privacy,
        allow_remote_patches=allow_remote_patches,
    )
    _trace_progress(
        "reconciling_unpublished",
        "Reconciling unpublished changes",
        current=0,
        total=len(detected),
        unit="item",
    )
    existing = store.load().items
    unpublished = reconciler.unpublished(existing, detected)
    _trace_progress(
        "reconciling_unpublished",
        "Reconciled unpublished changes",
        current=len(detected),
        total=len(detected),
        unit="item",
        unpublished=len(unpublished),
    )
    if persist and unpublished:
        _trace_progress(
            "persisting_working_state",
            "Persisting working state",
            current=len(unpublished),
            total=len(unpublished),
            unit="item",
        )
        store.append_new(unpublished)
    return unpublished


def _detect_change_items_with_provider(
    base: str,
    head: str,
    model_provider: str | None,
    commit_intent_inference: str | None = None,
    model_privacy: str | None = None,
    allow_remote_patches: bool = False,
) -> list[ChangeItem]:
    if model_provider:
        return detect_change_items(
            base,
            head,
            model_provider=model_provider,
            commit_intent_inference=commit_intent_inference,
            model_privacy=model_privacy,
            allow_remote_patches=allow_remote_patches,
        )
    return _detect_review_candidates(
        base,
        head,
        model_provider=None,
        commit_intent_inference=commit_intent_inference,
        model_privacy=model_privacy,
        allow_remote_patches=allow_remote_patches,
    )


def _detect_review_candidates(
    base: str,
    head: str,
    model_provider: str | None,
    commit_intent_inference: str | None = None,
    model_privacy: str | None = None,
    allow_remote_patches: bool = False,
) -> list[ChangeItem]:
    if model_provider:
        return detect_change_items(
            base,
            head,
            model_provider=model_provider,
            commit_intent_inference=commit_intent_inference,
            model_privacy=model_privacy,
            allow_remote_patches=allow_remote_patches,
        )
    groups = _collect_groups(
        base,
        head,
        commit_intent_inference=commit_intent_inference,
        model_privacy=model_privacy,
        allow_remote_patches=allow_remote_patches,
    )
    classifier = Classifier()
    return _classify_groups(
        groups,
        classifier,
        base,
        head,
        stage="detecting_review_candidates",
        label="Detecting review candidates",
    )


def _run_scan_with_provider(
    base: str,
    head: str,
    persist: bool,
    model_provider: str | None,
    commit_intent_inference: str | None = None,
    model_privacy: str | None = None,
    allow_remote_patches: bool = False,
) -> list[dict]:
    if model_provider:
        return run_scan(
            base,
            head,
            persist=persist,
            model_provider=model_provider,
            commit_intent_inference=commit_intent_inference,
            model_privacy=model_privacy,
            allow_remote_patches=allow_remote_patches,
        )
    return run_scan(
        base,
        head,
        persist=persist,
        commit_intent_inference=commit_intent_inference,
        model_privacy=model_privacy,
        allow_remote_patches=allow_remote_patches,
    )


def _resolve_suggest_model_provider(model_provider: str | None) -> str | None:
    if model_provider:
        return model_provider
    config = _load_config()
    configured_name = os.getenv("VERLATO_MODEL_PROVIDER", config.provider.name).strip().lower()
    if configured_name and configured_name != "stub":
        return configured_name
    return "ollama"


def _suggestion_batching_allowed_for_provider(provider_mode: str | None) -> bool:
    return (provider_mode or "").strip().lower() in {"ollama", "local", "litellm", "plugin", "external-plugin"}


def _can_send_raw_patches_for_config(config: ProviderConfig, provider_mode: str | None, privacy_mode: str) -> bool:
    if not can_send_raw_patches(provider_mode or config.name, privacy_mode):
        return False
    if normalize_model_privacy(privacy_mode) != "local-summary-then-ai":
        return True
    chain_modes = [provider_mode or config.name]
    chain_modes.extend(str(fallback.get("name") or "").strip() for fallback in config.fallbacks if isinstance(fallback, dict))
    return all(can_send_raw_patches(mode, privacy_mode) for mode in chain_modes)


def _build_provider_for_suggestion(config: ProviderConfig, *, mode: str | None, suggest_model: str | None):
    if suggest_model is not None:
        return build_provider(config, mode=mode, suggest_model=suggest_model)
    return build_provider(config, mode=mode)


def _normalized_suggest_model(value: object) -> str | None:
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _provider_effective_model(provider: object, task: str) -> str:
    model_for_task = getattr(provider, "model_for_task", None)
    if callable(model_for_task):
        try:
            model = model_for_task(task)
        except TypeError:
            model = None
        if model:
            return str(model)
    raw_model = getattr(provider, "model", None)
    return str(raw_model).strip() if raw_model is not None else ""


def _provider_model_context(provider: object, provider_mode: str | None) -> dict[str, Any]:
    normalized_mode = (provider_mode or "").strip().lower()
    if normalized_mode not in {"ollama", "local", "litellm", "plugin", "external-plugin"}:
        return {}
    model = str(getattr(provider, "model", "") or "")
    suggest_model = _normalized_suggest_model(getattr(provider, "suggest_model", None))
    effective_suggest_model = _provider_effective_model(provider, "suggest") or model
    return {
        "model": model,
        "suggest_model": suggest_model,
        "effective_suggest_model": effective_suggest_model,
    }


def _configured_ollama_model_context(config: ProviderConfig, *, suggest_model: str | None) -> dict[str, Any]:
    model = os.getenv("VERLATO_OLLAMA_MODEL") or config.model or "gemma4:latest"
    resolved_suggest_model = _normalized_suggest_model(
        suggest_model
        if suggest_model is not None
        else os.getenv("VERLATO_OLLAMA_SUGGEST_MODEL", config.suggest_model or "")
    )
    return {
        "model": model,
        "suggest_model": resolved_suggest_model,
        "effective_suggest_model": resolved_suggest_model or model,
    }


def build_draft_document(
    *,
    base: str,
    head: str,
    version: str,
    include_confidence: bool = False,
    include_provenance: bool = False,
    model_provider: str | None = None,
    commit_intent_inference: str | None = None,
    model_privacy: str | None = None,
    allow_remote_patches: bool = False,
) -> DraftDocument:
    config = _load_config()
    _trace_progress("draft_detecting_changes", "Detecting draft changes", detail=f"{base}..{head}")
    items = _merge_review_state(
        _detect_change_items_with_provider(
            base,
            head,
            model_provider,
            commit_intent_inference=commit_intent_inference,
            model_privacy=model_privacy,
            allow_remote_patches=allow_remote_patches,
        )
    )
    _trace_progress(
        "draft_rendering",
        "Rendering release notes",
        current=0,
        total=len(items),
        unit="item",
    )
    document = DraftService(config=config).build_document(
        items=items,
        base_ref=base,
        head_ref=head,
        version=version,
        include_confidence=include_confidence,
        include_provenance=include_provenance,
    )
    _trace_progress(
        "draft_ready",
        "Release notes ready",
        current=document.item_count,
        total=document.item_count,
        unit="item",
    )
    return document


def _latest_published_version() -> str | None:
    state = _build_store().load()
    published_versions = [item.published_in for item in state.items if item.published and item.published_in]
    if not published_versions:
        return None
    # Preserve recency by taking the last published marker observed in state history.
    return published_versions[-1]


def _as_dict(value: object) -> dict[str, Any]:
    """`value` when it is a JSON object, else an empty dict (typed for nested payload reads)."""

    return value if isinstance(value, dict) else {}


def _load_config() -> VerlatoConfig:
    return load_config()


def _resolve_max_model_items(configured: int) -> int:
    raw = os.getenv("VERLATO_MODEL_MAX_ITEMS")
    if raw is None:
        return configured
    try:
        return int(raw)
    except ValueError:
        return configured


def _resolve_commit_intent_inference(configured: str, requested: str | None = None) -> str:
    if requested is not None:
        return normalize_commit_intent_mode(requested)
    raw = os.getenv("VERLATO_COMMIT_INTENT_INFERENCE")
    if raw is not None:
        return normalize_commit_intent_mode(raw, default=normalize_commit_intent_mode(configured))
    return normalize_commit_intent_mode(configured)


def _commit_intent_inference_from_args(args: argparse.Namespace) -> str | None:
    if bool(getattr(args, "infer_commit_intents", False)):
        return "low-signal"
    if bool(getattr(args, "no_infer_commit_intents", False)):
        return "off"
    return None


def _model_privacy_from_args(args: argparse.Namespace) -> str | None:
    value = getattr(args, "privacy", None)
    return normalize_model_privacy(value) if value else None


def _allow_remote_patches_from_args(args: argparse.Namespace) -> bool:
    return bool(getattr(args, "allow_remote_patches", False))


def _resolve_suggestion_count(configured: int, requested: int | None = None) -> int:
    raw = str(requested) if requested is not None else os.getenv("VERLATO_SUGGESTION_COUNT")
    value = configured
    if raw is not None:
        try:
            value = int(raw)
        except ValueError:
            value = configured
    return max(1, min(value, 5))


def _resolve_suggestion_parallelism(configured: int, requested: int | None = None) -> int:
    raw = str(requested) if requested is not None else os.getenv("VERLATO_SUGGESTION_PARALLELISM")
    value = configured
    if raw is not None:
        try:
            value = int(raw)
        except ValueError:
            value = configured
    return max(1, min(value, MAX_SUGGESTION_PARALLELISM))


def _resolve_suggestion_batch_size(configured: int, requested: int | None = None) -> int:
    raw = str(requested) if requested is not None else os.getenv("VERLATO_SUGGESTION_BATCH_SIZE")
    value = configured
    if raw is not None:
        try:
            value = int(raw)
        except ValueError:
            value = configured
    return max(1, min(value, MAX_SUGGESTION_BATCH_SIZE))


def _resolve_suggestion_strategy(configured: str, requested: str | None = None) -> str:
    raw = requested or os.getenv("VERLATO_SUGGESTION_STRATEGY") or configured or "auto"
    strategy = str(raw).strip().lower()
    if strategy == "instant":
        raise SystemExit(
            "The instant suggestion strategy has been removed. "
            "Use --strategy auto so suggestions are generated from Verlato-built diff evidence."
        )
    return strategy if strategy in SUGGESTION_STRATEGY_CHOICES else "auto"


def _effective_suggestion_strategy(
    strategy: str,
    provider_mode: str | None,
    *,
    privacy_mode: str = "metadata-only",
) -> str:
    normalized = _resolve_suggestion_strategy(strategy)
    normalized_provider = (provider_mode or "").strip().lower()
    provider_allows_batch = _suggestion_batching_allowed_for_provider(provider_mode)
    patches_allowed = can_send_raw_patches(provider_mode, privacy_mode)
    if normalized == "auto":
        if not patches_allowed:
            return "single"
        if normalized_provider in {"ollama", "local"}:
            return "tool-retrieval"
        return "full-diff-json" if provider_allows_batch else "single"
    if normalized in {"tool-retrieval", "full-diff-json", "paired"} and not patches_allowed:
        return "single"
    if normalized == "tool-retrieval" and normalized_provider not in {"ollama", "local"}:
        return "single"
    if normalized in {"paired", "full-diff-json"} and not provider_allows_batch:
        return "single"
    return normalized


def _suggestion_prompt_shape(strategy: str) -> str:
    if strategy == "tool-retrieval":
        return TOOL_RETRIEVAL_SUGGESTION_PROMPT_SHAPE
    if strategy == "full-diff-json":
        return FULL_DIFF_SUGGESTION_PROMPT_SHAPE
    if strategy == "paired":
        return PAIRED_SUGGESTION_PROMPT_SHAPE
    return SINGLE_SUGGESTION_PROMPT_SHAPE


def _resolve_suggestion_prompt_profile(configured: str, requested: str | None = None) -> str:
    raw = requested or os.getenv("VERLATO_SUGGESTION_PROMPT_PROFILE") or configured or "conservative"
    profile = str(raw).strip().lower()
    return profile if profile in SUGGESTION_PROMPT_PROFILE_CHOICES else "conservative"


def _resolve_suggestion_adaptive_recommendations(configured: bool, requested: bool = False) -> bool:
    if requested:
        return True
    raw = os.getenv("VERLATO_SUGGESTION_ADAPTIVE_RECOMMENDATIONS")
    if raw is None:
        return bool(configured)
    return _parse_bool(raw, default=bool(configured))


def _resolve_suggestion_cache_backend(configured: str) -> str:
    raw = os.getenv("VERLATO_SUGGESTION_CACHE_BACKEND") or configured or "json"
    return raw.strip().lower() or "json"


def _resolve_suggestion_cache_ttl_days(configured: int) -> int:
    raw = os.getenv("VERLATO_SUGGESTION_CACHE_TTL_DAYS")
    value = configured
    if raw is not None:
        try:
            value = int(raw)
        except ValueError:
            value = configured
    return max(0, value)


def _resolve_suggestion_tone(configured: str, requested: str | None = None) -> str:
    raw = requested or os.getenv("VERLATO_SUGGESTION_TONE") or configured
    tone = (raw or "release-note").strip().lower()
    return tone if tone in SUGGESTION_TONE_CHOICES else "release-note"


def _parse_bool(value: object, *, default: bool = False) -> bool:
    normalized = str(value).strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    return default


def _resolve_suggestion_style(config: VerlatoConfig) -> dict[str, Any]:
    guidance = os.getenv("VERLATO_STYLE_GUIDANCE") or config.suggestion_style.guidance
    vocabulary = _env_list("VERLATO_PROJECT_VOCABULARY") or config.suggestion_style.project_vocabulary
    banned = _env_list("VERLATO_BANNED_PHRASES") or config.suggestion_style.banned_phrases
    floor_raw = os.getenv("VERLATO_SPECIFICITY_FLOOR")
    specificity_floor = config.suggestion_style.specificity_floor
    if floor_raw is not None:
        try:
            specificity_floor = float(floor_raw)
        except ValueError:
            specificity_floor = config.suggestion_style.specificity_floor
    return {
        "guidance": guidance.strip(),
        "project_vocabulary": [term for term in vocabulary if term],
        "banned_phrases": [phrase for phrase in banned if phrase],
        "specificity_floor": max(0.0, min(float(specificity_floor), 1.0)),
    }


def _env_list(name: str) -> list[str]:
    raw = os.getenv(name)
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _elapsed_ms(start: float) -> int:
    return max(0, int(round((perf_counter() - start) * 1000)))


def _normalized_provider_timings(metadata: dict[str, Any], model_ms: int) -> dict[str, Any]:
    timings: dict[str, Any] = {}
    duration_fields = {
        "total_duration": "ollama_total_ms",
        "load_duration": "load_ms",
        "prompt_eval_duration": "prompt_eval_ms",
        "eval_duration": "eval_ms",
    }
    for source_key, target_key in duration_fields.items():
        duration_ms = _nanoseconds_to_ms(metadata.get(source_key))
        if duration_ms is not None:
            timings[target_key] = duration_ms
    prompt_tokens = _optional_int(metadata.get("prompt_eval_count"))
    if prompt_tokens is not None:
        timings["prompt_tokens"] = prompt_tokens
    eval_tokens = _optional_int(metadata.get("eval_count"))
    if eval_tokens is not None:
        timings["eval_tokens"] = eval_tokens
    ollama_total_ms = timings.get("ollama_total_ms")
    if isinstance(ollama_total_ms, int):
        timings["queue_ms"] = max(0, model_ms - ollama_total_ms)
    eval_duration_ms = timings.get("eval_ms")
    if eval_tokens is not None and isinstance(eval_duration_ms, int) and eval_duration_ms > 0:
        timings["tokens_per_second"] = round(eval_tokens / (eval_duration_ms / 1000), 1)
    return timings


def _ollama_doctor_prompt() -> str:
    return (
        "Return one concise release-note style sentence for this diagnostic change: "
        "Verlato adds a model health check for suggestion performance."
    )


def _run_ollama_doctor(
    *,
    parallel_probe: bool = False,
    warmup: bool = False,
    adaptive_recommendations: bool = False,
    suggest_model: str | None = None,
) -> dict[str, Any]:
    config = _load_config()
    suggestion_parallelism = _resolve_suggestion_parallelism(config.suggestion_parallelism)
    suggestion_prompt_profile = _resolve_suggestion_prompt_profile(config.suggestion_prompt_profile)
    adaptive_enabled = _resolve_suggestion_adaptive_recommendations(
        config.suggestion_adaptive_recommendations,
        requested=adaptive_recommendations,
    )
    provider = _build_provider_for_suggestion(config.provider, mode="ollama", suggest_model=suggest_model)
    prompt = _ollama_doctor_prompt()
    request = ProviderRequest(
        task="suggest",
        prompt=prompt,
        evidence={
            "title": "Ollama doctor probe",
            "item_id": "doctor_ollama",
            "suggestion_count": 1,
            "doctor": True,
        },
    )
    config_snapshot = _ollama_doctor_config(provider, request, suggestion_parallelism)
    probes = []
    if warmup:
        probes.append(_run_ollama_doctor_probe(provider, request, name="warmup", purpose="warmup"))
    probes.append(_run_ollama_doctor_probe(provider, request, name="single"))
    if parallel_probe:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="verlato-ollama-doctor") as executor:
            futures = [
                executor.submit(_run_ollama_doctor_probe, provider, request, name="parallel-a"),
                executor.submit(_run_ollama_doctor_probe, provider, request, name="parallel-b"),
            ]
            probes.extend(future.result() for future in as_completed(futures))
        parallel_start = 2 if warmup else 1
        probes[parallel_start:] = sorted(probes[parallel_start:], key=lambda probe: str(probe.get("name") or ""))
    runtime = _collect_ollama_runtime(provider, config_snapshot)
    result = {
        "schema": "ollama_doctor.v1",
        "provider": "ollama",
        "config": config_snapshot,
        "runtime": runtime,
        "probes": probes,
        "summary": _summarize_ollama_doctor(probes, config_snapshot, runtime=runtime, warmup_requested=warmup),
    }
    if adaptive_enabled:
        result["adaptive_recommendations"] = _build_suggestion_adaptive_recommendations(
            current={
                "provider": "ollama",
                "prompt_profile": suggestion_prompt_profile,
                "suggestion_count": _resolve_suggestion_count(config.suggestion_count),
                "suggestion_parallelism": suggestion_parallelism,
                "suggestion_batch_size": _resolve_suggestion_batch_size(config.suggestion_batch_size),
                "suggestion_strategy": _resolve_suggestion_strategy(config.suggestion_strategy),
                "cache_warming": None,
            },
            timing_sources=probes,
            feedback_events=_build_store().load().feedback_events,
            cache_hit_count=0,
            item_count=0,
            prompt_chars=sum(int(probe.get("prompt_chars") or 0) for probe in probes),
            source="doctor",
        )
    return result


def _ollama_doctor_config(provider: object, request: ProviderRequest, suggestion_parallelism: int) -> dict[str, Any]:
    options = {}
    option_builder = getattr(provider, "_options_for_task", None)
    if callable(option_builder):
        try:
            options = option_builder("suggest", request_data=request)
        except TypeError:
            options = {}
    model = getattr(provider, "model", None)
    suggest_model = _normalized_suggest_model(getattr(provider, "suggest_model", None))
    effective_suggest_model = _provider_effective_model(provider, "suggest") or str(model or "")
    return {
        "endpoint": getattr(provider, "endpoint", None),
        "model": model,
        "suggest_model": suggest_model,
        "effective_suggest_model": effective_suggest_model,
        "timeout_seconds": getattr(provider, "timeout_seconds", None),
        "stream": getattr(provider, "stream", None),
        "keep_alive": getattr(provider, "keep_alive", None),
        "num_ctx": getattr(provider, "num_ctx", None),
        "num_predict": getattr(provider, "num_predict", None),
        "suggest_num_predict": getattr(provider, "suggest_num_predict", None),
        "suggest_num_predict_effective": options.get("num_predict") if isinstance(options, dict) else None,
        "suggest_think": getattr(provider, "suggest_think", None),
        "suggestion_parallelism": suggestion_parallelism,
    }


def _collect_ollama_runtime(provider: object, config_snapshot: dict[str, Any]) -> dict[str, Any]:
    api_ps = _collect_ollama_api_ps(provider)
    cli_ps = _collect_ollama_cli_ps()
    selected_model = _select_ollama_runtime_model(
        configured_model=str(config_snapshot.get("effective_suggest_model") or config_snapshot.get("model") or ""),
        api_ps=api_ps,
        cli_ps=cli_ps,
    )
    return {
        "api_ps": api_ps,
        "cli_ps": cli_ps,
        "selected_model": selected_model,
    }


def _collect_ollama_api_ps(provider: object) -> dict[str, Any]:
    endpoint = getattr(provider, "endpoint", None)
    url = _ollama_api_ps_url(endpoint)
    if not url:
        return {"ok": False, "url": None, "models": [], "error": "No Ollama endpoint configured."}
    timeout = _optional_float(getattr(provider, "timeout_seconds", None)) or 15.0
    try:
        data = _get_json(url, timeout_seconds=timeout)
    except (urlerror.URLError, TimeoutError, json.JSONDecodeError, OSError) as exc:
        return {"ok": False, "url": url, "models": [], "error": str(exc)}
    models = data.get("models") if isinstance(data, dict) else []
    return {
        "ok": isinstance(models, list),
        "url": url,
        "models": models if isinstance(models, list) else [],
        "response": data,
    }


def _collect_ollama_cli_ps() -> dict[str, Any]:
    executable = shutil.which("ollama")
    if not executable:
        return {"ok": False, "available": False, "rows": []}
    try:
        result = subprocess.run(
            [executable, "ps"],
            capture_output=True,
            text=True,
            timeout=3,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return {"ok": False, "available": True, "rows": [], "error": str(exc)}
    stdout = result.stdout.strip()
    stderr = result.stderr.strip()
    payload: dict[str, Any] = {
        "ok": result.returncode == 0,
        "available": True,
        "returncode": result.returncode,
        "rows": _parse_ollama_ps_rows(stdout) if result.returncode == 0 else [],
        "raw": stdout,
    }
    if stderr:
        payload["stderr"] = stderr
    if result.returncode != 0 and stderr:
        payload["error"] = stderr
    return payload


def _ollama_api_ps_url(endpoint: object) -> str | None:
    base = _ollama_base_url(endpoint)
    return f"{base}/api/ps" if base else None


def _ollama_base_url(endpoint: object) -> str | None:
    if not isinstance(endpoint, str) or not endpoint.strip():
        return None
    normalized = endpoint.strip().rstrip("/")
    for suffix in ("/api/chat", "/api/generate"):
        if normalized.endswith(suffix):
            return normalized[: -len(suffix)].rstrip("/")
    if normalized.endswith("/api"):
        return normalized[: -len("/api")].rstrip("/")
    return normalized


def _get_json(url: str, *, timeout_seconds: float) -> dict[str, Any]:
    req = urlrequest.Request(url, headers={"Accept": "application/json"}, method="GET")
    with urlrequest.urlopen(req, timeout=timeout_seconds) as resp:
        data = json.loads(resp.read().decode("utf-8"))
    if not isinstance(data, dict):
        raise json.JSONDecodeError("expected JSON object", str(data), 0)
    return data


def _parse_ollama_ps_rows(raw: str) -> list[dict[str, str]]:
    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) < 2:
        return []
    headers = [_normalize_ollama_ps_header(part) for part in re.split(r"\s{2,}", lines[0]) if part.strip()]
    rows: list[dict[str, str]] = []
    for line in lines[1:]:
        cells = [part.strip() for part in re.split(r"\s{2,}", line) if part.strip()]
        if not cells or not headers:
            continue
        if len(cells) > len(headers):
            cells = cells[: len(headers) - 1] + [" ".join(cells[len(headers) - 1 :])]
        row = {header: cells[index] for index, header in enumerate(headers[: len(cells)])}
        rows.append(row)
    return rows


def _normalize_ollama_ps_header(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", value.strip().lower()).strip("_")


def _select_ollama_runtime_model(
    *,
    configured_model: str,
    api_ps: dict[str, Any],
    cli_ps: dict[str, Any],
) -> dict[str, Any]:
    models = api_ps.get("models") if api_ps.get("ok") is True else []
    if isinstance(models, list):
        for model in models:
            if isinstance(model, dict) and _ollama_model_matches(configured_model, model):
                selected = dict(model)
                selected["loaded"] = True
                processor = _ollama_cli_processor_for_model(configured_model, cli_ps)
                if processor:
                    selected["processor"] = processor
                return selected
    return {"name": configured_model, "model": configured_model, "loaded": False if api_ps.get("ok") is True else None}


def _ollama_model_matches(configured_model: str, candidate: dict[str, Any]) -> bool:
    aliases = _ollama_model_aliases(configured_model)
    candidate_names = [
        str(candidate.get("name") or ""),
        str(candidate.get("model") or ""),
    ]
    return any(alias in {_normalize_model_name(name) for name in candidate_names if name} for alias in aliases)


def _ollama_model_aliases(model: str) -> set[str]:
    normalized = _normalize_model_name(model)
    aliases = {normalized} if normalized else set()
    if normalized and ":" not in normalized:
        aliases.add(f"{normalized}:latest")
    if normalized.endswith(":latest"):
        aliases.add(normalized.removesuffix(":latest"))
    return aliases


def _normalize_model_name(model: str) -> str:
    return model.strip().lower()


def _ollama_cli_processor_for_model(configured_model: str, cli_ps: dict[str, Any]) -> str | None:
    rows = cli_ps.get("rows")
    if not isinstance(rows, list):
        return None
    aliases = _ollama_model_aliases(configured_model)
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_names = [
            str(row.get("name") or ""),
            str(row.get("model") or ""),
        ]
        if any(_normalize_model_name(name) in aliases for name in row_names if name):
            processor = row.get("processor")
            return str(processor) if processor else None
    return None


def _run_ollama_doctor_probe(
    provider,
    request: ProviderRequest,
    *,
    name: str,
    purpose: str = "measure",
) -> dict[str, Any]:
    started = perf_counter()
    try:
        response = provider.summarize(request)
    except BaseException as exc:
        elapsed_ms = _elapsed_ms(started)
        return {
            "name": name,
            "purpose": purpose,
            "ok": False,
            "error": str(exc),
            "elapsed_ms": elapsed_ms,
            "prompt_chars": len(request.prompt),
            "timings": {"model_ms": elapsed_ms},
            "metadata": {},
        }
    elapsed_ms = _elapsed_ms(started)
    metadata = dict(response.metadata)
    timings = _normalized_provider_timings(metadata, elapsed_ms)
    ok, error_message = _ollama_probe_status(response)
    return {
        "name": name,
        "purpose": purpose,
        "ok": ok,
        "error": error_message,
        "elapsed_ms": elapsed_ms,
        "prompt_chars": len(request.prompt),
        "model": response.model,
        "content_chars": len(response.content),
        "timings": {
            "model_ms": elapsed_ms,
            **timings,
        },
        "metadata": metadata,
    }


def _ollama_probe_status(response: ProviderResponse) -> tuple[bool, str | None]:
    content = (response.content or "").strip()
    lowered = content.lower()
    if lowered.startswith("[ollama-unavailable]"):
        return False, content
    if lowered.startswith("[ollama-error]"):
        return False, content
    if lowered.startswith("[ollama-empty]") or not content:
        return False, content or "[ollama-empty] no content"
    return True, None


def _run_model_doctor(
    *,
    model_provider: str | None = None,
    suggest_model: str | None = None,
    timeout_seconds: float | None = None,
) -> dict[str, Any]:
    config = _load_config()
    provider_mode = model_provider or _resolve_suggest_model_provider(None) or config.provider.name
    provider = _build_provider_for_suggestion(config.provider, mode=provider_mode, suggest_model=suggest_model)
    item_id = "doctor_model"
    prompt = _build_full_diff_suggestion_batch_prompt(
        [
            _doctor_suggestion_work(
                item_id=item_id,
                provider_mode=provider_mode,
                suggestion_tone=_resolve_suggestion_tone(config.suggestion_tone),
                suggestion_style=_resolve_suggestion_style(config),
            )
        ]
    )
    request_data = ProviderRequest(
        task="suggest",
        prompt=prompt,
        evidence={
            "doctor": True,
            "strategy": "full-diff-json",
            "prompt_shape": FULL_DIFF_SUGGESTION_PROMPT_SHAPE,
            "item_ids": [item_id],
            "item_count": 1,
            "suggestion_count": 1,
            "full_diff_file_count": 1,
            "full_diff_patch_chars": 86,
            "missing_patch_count": 0,
        },
        response_format=_suggestion_batch_response_format(1),
    )
    started = perf_counter()
    try:
        response = provider.summarize(request_data)
        elapsed_ms = _elapsed_ms(started)
    except BaseException as exc:
        elapsed_ms = _elapsed_ms(started)
        return {
            "schema": "model_doctor.v1",
            "provider": provider_mode,
            "ok": False,
            "status": "error",
            "warnings": ["provider probe raised an exception"],
            "error": str(exc),
            "elapsed_ms": elapsed_ms,
            "prompt_chars": len(prompt),
        }
    parsed = _parse_suggestion_batch_json(response.content)
    provider_error = Classifier()._is_provider_error(response.content)
    valid = bool(parsed and parsed.get(item_id)) and not provider_error
    warnings: list[str] = []
    budget_ms = int((timeout_seconds or config.provider.timeout_seconds or 10.0) * 1000)
    if elapsed_ms >= budget_ms:
        warnings.append(f"probe exceeded the advisory timeout budget ({budget_ms}ms)")
    elif elapsed_ms >= 5000:
        warnings.append("cold probe was slow; the selected suggest model may not feel responsive")
    elif elapsed_ms >= 2000:
        warnings.append("warm response may feel sluggish for editor suggestions")
    if provider_error:
        warnings.append("provider returned an error marker")
    if not parsed:
        warnings.append("provider did not return valid structured JSON")
    elif not parsed.get(item_id):
        warnings.append("provider structured JSON omitted the requested item")
    return {
        "schema": "model_doctor.v1",
        "provider": provider_mode,
        **_provider_model_context(provider, provider_mode),
        "ok": valid,
        "status": "ok" if valid and not warnings else "warning" if valid else "error",
        "warnings": warnings,
        "elapsed_ms": elapsed_ms,
        "prompt_chars": len(prompt),
        "model": response.model,
        "content_chars": len(response.content),
        "metadata": response.metadata,
    }


def _doctor_suggestion_work(
    *,
    item_id: str,
    provider_mode: str,
    suggestion_tone: str,
    suggestion_style: dict[str, Any],
) -> _SuggestionWork:
    from verlato.core.models import ChangeItem, Classification, Content, Evidence, SourceRef

    item = ChangeItem(
        id=item_id,
        source=SourceRef(pr_number=None, commit_shas=["doctor"], base_ref="doctor-base", head_ref="doctor-head"),
        content=Content(
            title="Doctor model diff probe",
            summary="Checks whether the selected model can produce structured suggestions from diff evidence.",
        ),
        classification=Classification(kind="changed", area="general", audience="external", semver="patch"),
        evidence=Evidence(commit_signals=[], files=["docs/release-notes.md"]),
        confidence=1.0,
        checksum="doctor",
    )
    full_diff_item = {
        "item_id": item_id,
        "title": item.content.title,
        "current_summary": item.content.summary,
        "classification": {
            "kind": "changed",
            "area": "general",
            "audience": "external",
            "semver": "patch",
        },
        "labels": [],
        "pr": None,
        "commits": [
            {"sha": "doctor", "subject": "docs: tune release note copy", "message": "docs: tune release note copy", "author": "Verlato"}
        ],
        "files": [
            {
                "path": "docs/release-notes.md",
                "status": "M",
                "patch": "@@ -1,2 +1,2 @@\n-Old release note text\n+Clearer release note text for external readers\n",
                "patch_chars": 86,
                "patch_available": True,
                "patch_unavailable_reason": "",
            }
        ],
        "file_count": 1,
        "patch_chars": 86,
        "missing_patch_count": 0,
        "diff_summary": {
            "summary": "M docs/release-notes.md",
            "files_by_area": {"docs": ["docs/release-notes.md"]},
            "file_statuses": {"docs/release-notes.md": "M"},
            "api_changes": [],
            "migration_changes": [],
            "config_changes": [],
        },
    }
    return _SuggestionWork(
        order=0,
        item_number=1,
        batch=1,
        batch_total=1,
        total_targets=1,
        group=ChangeGroup(pr=None, commits=[], files=[]),
        baseline_item=item,
        prompt="",
        evidence={},
        source_evidence=[],
        prompt_sha="doctor",
        prompt_budget={},
        cache_key="",
        strategy="full-diff-json",
        prompt_shape=FULL_DIFF_SUGGESTION_PROMPT_SHAPE,
        full_diff_item=full_diff_item,
        full_diff_file_count=1,
        full_diff_patch_chars=86,
        full_diff_missing_patch_count=0,
        tools=[],
        tool_executor=None,
        max_tool_rounds=0,
        suggestion_count=1,
        suggestion_tone=suggestion_tone,
        suggestion_style=suggestion_style,
        effective_model_provider=provider_mode,
        evidence_ms=0,
        prompt_ms=0,
        started_at=perf_counter(),
    )


def _summarize_ollama_doctor(
    probes: list[dict[str, Any]],
    config_snapshot: dict[str, Any],
    *,
    runtime: dict[str, Any] | None = None,
    warmup_requested: bool = False,
) -> dict[str, Any]:
    runtime = runtime or {}
    runtime_diagnostics = _ollama_runtime_diagnostics(runtime, config_snapshot)
    runtime_recommendations = _ollama_runtime_recommendations(runtime, config_snapshot)
    ok_probes = [probe for probe in probes if probe.get("ok") is True]
    if not ok_probes:
        diagnostics = [str(probe.get("error") or "Ollama probe failed") for probe in probes]
        diagnostics.extend(runtime_diagnostics)
        return {
            "status": "error",
            "bottleneck": "unavailable",
            "warmed_up": False,
            "diagnostics": diagnostics,
            "recommendations": _unique_strings(
                [
                    "Check that Ollama is running and reachable at the configured endpoint.",
                    "Confirm the configured model is pulled and the endpoint matches Ollama's /api/chat route.",
                    *runtime_recommendations,
                ]
            ),
            "recommended_settings": {},
        }
    warmup_probe = next((probe for probe in probes if probe.get("purpose") == "warmup"), None)
    measured_probes = [
        probe
        for probe in ok_probes
        if probe.get("purpose") != "warmup"
    ] or ok_probes
    bottleneck, diagnostic = _ollama_doctor_bottleneck(measured_probes)
    diagnostics = [diagnostic]
    warmup_ok = bool(warmup_probe and warmup_probe.get("ok") is True)
    if warmup_requested:
        diagnostics.append(_ollama_warmup_diagnostic(warmup_probe))
    context_diagnostic = _ollama_context_diagnostic(config_snapshot)
    if context_diagnostic:
        diagnostics.append(context_diagnostic)
    diagnostics.extend(runtime_diagnostics)
    recommendations = _unique_strings(
        [
            *_ollama_doctor_recommendations(
                bottleneck,
                config_snapshot,
                warmup_requested=warmup_requested,
            ),
            *runtime_recommendations,
        ]
    )
    status = "ok" if bottleneck == "healthy" else "warning"
    return {
        "status": status,
        "bottleneck": bottleneck,
        "warmed_up": warmup_ok,
        "diagnostics": diagnostics,
        "recommendations": recommendations,
        "recommended_settings": _ollama_doctor_recommended_settings(bottleneck, config_snapshot),
    }


def _ollama_doctor_bottleneck(probes: list[dict[str, Any]]) -> tuple[str, str]:
    max_values = {
        "queue": _max_probe_timing(probes, "queue_ms"),
        "load": _max_probe_timing(probes, "load_ms"),
        "prompt_eval": _max_probe_timing(probes, "prompt_eval_ms"),
        "generation": _max_probe_timing(probes, "eval_ms"),
        "model": max(_max_probe_timing(probes, "model_ms"), _max_probe_timing(probes, "ollama_total_ms")),
    }
    model_ms = max_values["model"]
    if max_values["queue"] >= 250 and (model_ms <= 0 or max_values["queue"] / model_ms >= 0.2):
        return "queue", f"Ollama queue time reached {max_values['queue']}ms during the probe."
    if max_values["load"] >= 500 and _timing_ratio(max_values["load"], model_ms) >= 0.35:
        return "load", f"Ollama model load time reached {max_values['load']}ms."
    if max_values["prompt_eval"] >= 500 and _timing_ratio(max_values["prompt_eval"], model_ms) >= 0.35:
        return "prompt_eval", f"Ollama prompt evaluation reached {max_values['prompt_eval']}ms."
    if max_values["generation"] >= 500 and _timing_ratio(max_values["generation"], model_ms) >= 0.35:
        return "generation", f"Ollama generation reached {max_values['generation']}ms."
    if model_ms >= 2500:
        return "generation", f"Ollama probe took {model_ms}ms without a more specific timing breakdown."
    return "healthy", f"Ollama probe completed in {model_ms}ms with no obvious bottleneck."


def _ollama_doctor_recommendations(
    bottleneck: str,
    config_snapshot: dict[str, Any],
    *,
    warmup_requested: bool = False,
) -> list[str]:
    parallelism = config_snapshot.get("suggestion_parallelism")
    if bottleneck == "queue":
        recommendations = [
            _ollama_parallelism_recommendation(parallelism),
            "Set OLLAMA_NUM_PARALLEL before starting Ollama if your hardware has memory for parallel requests.",
            "Keep OLLAMA_CONTEXT_LENGTH or VERLATO_OLLAMA_NUM_CTX conservative because each parallel slot uses memory.",
        ]
        return recommendations
    if bottleneck == "load":
        recommendations = [
            "Keep the model warm with VERLATO_OLLAMA_KEEP_ALIVE or provider.keep_alive.",
            "Use verlato doctor ollama --warmup or the VS Code Warm model action before large all-visible suggestion runs.",
        ]
        if warmup_requested:
            recommendations.append("Warmup was requested; if load time remains high, raise keep_alive or check memory pressure.")
        return recommendations
    if bottleneck == "prompt_eval":
        return [
            "Reduce prompt budget further or inspect prompt budget telemetry for files/hunks that dominate.",
            _ollama_context_recommendation(config_snapshot),
            "Consider a later multi-item batching pass if prompt eval remains the main cost.",
        ]
    if bottleneck == "generation":
        recommendation = "Try a smaller/faster local model with provider.suggest_model or VERLATO_OLLAMA_SUGGEST_MODEL."
        if config_snapshot.get("suggest_model"):
            recommendation = "Try a smaller/faster local suggestion model if generation remains slow."
        return [
            "Lower VERLATO_OLLAMA_SUGGEST_NUM_PREDICT or provider.suggest_num_predict.",
            recommendation,
        ]
    if bottleneck == "unavailable":
        return [
            "Check that Ollama is running and reachable at the configured endpoint.",
            "Confirm the configured model is pulled and available to Ollama.",
        ]
    return ["No obvious Ollama bottleneck was detected by the probe."]


def _ollama_doctor_recommended_settings(
    bottleneck: str,
    config_snapshot: dict[str, Any],
) -> dict[str, Any]:
    parallelism = config_snapshot.get("suggestion_parallelism")
    settings: dict[str, Any] = {}
    if bottleneck == "queue" and isinstance(parallelism, int) and parallelism > 1:
        settings["suggestion_parallelism"] = 1
    suggest_cap = config_snapshot.get("suggest_num_predict_effective")
    if bottleneck == "generation" and isinstance(suggest_cap, int) and suggest_cap > 96:
        settings["suggest_num_predict"] = 96
    if bottleneck == "generation" and not config_snapshot.get("suggest_model"):
        settings["suggest_model"] = "smaller-local-model"
    return settings


def _ollama_warmup_diagnostic(probe: dict[str, Any] | None) -> str:
    if not probe:
        return "Warmup was requested but no warmup probe was recorded."
    if probe.get("ok") is not True:
        return f"Warmup failed: {probe.get('error') or 'Ollama returned no usable content'}."
    timings = probe.get("timings")
    load_ms = timings.get("load_ms") if isinstance(timings, dict) else None
    elapsed_ms = probe.get("elapsed_ms")
    if isinstance(load_ms, int) and load_ms > 0:
        return f"Warmup completed in {elapsed_ms}ms and absorbed {load_ms}ms of model load time."
    return f"Warmup completed in {elapsed_ms}ms."


def _ollama_context_diagnostic(config_snapshot: dict[str, Any]) -> str | None:
    num_ctx = config_snapshot.get("num_ctx")
    if isinstance(num_ctx, int) and num_ctx >= 8192:
        return f"Configured num_ctx is {num_ctx}; large contexts increase memory use and can limit parallel requests."
    return None


def _ollama_parallelism_recommendation(parallelism: object) -> str:
    if isinstance(parallelism, int) and parallelism > 1:
        return f"Lower Verlato suggestion parallelism from {parallelism} to 1 if Ollama is serializing requests."
    return "Keep Verlato suggestion parallelism at 1 unless Ollama is configured for server-side parallelism."


def _ollama_context_recommendation(config_snapshot: dict[str, Any]) -> str:
    num_ctx = config_snapshot.get("num_ctx")
    if isinstance(num_ctx, int) and num_ctx >= 8192:
        return f"Configured num_ctx is {num_ctx}; lower it if suggestion prompts do not need that much context."
    return "Avoid oversized OLLAMA_CONTEXT_LENGTH or VERLATO_OLLAMA_NUM_CTX values until prompt eval is healthy."


def _ollama_runtime_diagnostics(runtime: dict[str, Any], config_snapshot: dict[str, Any]) -> list[str]:
    diagnostics: list[str] = []
    api_ps = _as_dict(runtime.get("api_ps"))
    selected = _as_dict(runtime.get("selected_model"))
    if api_ps.get("ok") is not True:
        diagnostics.append(f"Ollama runtime state unavailable from /api/ps: {api_ps.get('error') or 'unknown error'}.")
        return diagnostics

    model_name = str(config_snapshot.get("model") or selected.get("model") or selected.get("name") or "configured model")
    if selected.get("loaded") is False:
        diagnostics.append(f"Configured model {model_name} was not reported by Ollama /api/ps.")
        return diagnostics

    expires_seconds = _seconds_until(selected.get("expires_at"))
    if expires_seconds is not None and expires_seconds <= 120:
        diagnostics.append(f"Loaded model expires in {max(0, expires_seconds)}s; short keep-alive can cause reloads.")

    context_length = _optional_int(selected.get("context_length"))
    parallelism = config_snapshot.get("suggestion_parallelism")
    if context_length is not None and context_length >= 8192 and isinstance(parallelism, int) and parallelism > 1:
        diagnostics.append(
            f"Loaded model context length is {context_length}; with suggestion parallelism {parallelism}, memory pressure can multiply."
        )

    size_vram = _optional_int(selected.get("size_vram"))
    processor = str(selected.get("processor") or "").strip()
    if size_vram is None or size_vram <= 0:
        diagnostics.append("Loaded model did not report GPU VRAM residency; check ollama ps processor placement.")
    if processor and ("cpu" in processor.lower()) and ("gpu" not in processor.lower()):
        diagnostics.append(f"Ollama processor placement is {processor}; CPU-only inference can slow suggestions.")
    return diagnostics


def _ollama_runtime_recommendations(runtime: dict[str, Any], config_snapshot: dict[str, Any]) -> list[str]:
    recommendations: list[str] = []
    api_ps = _as_dict(runtime.get("api_ps"))
    selected = _as_dict(runtime.get("selected_model"))
    if api_ps.get("ok") is not True:
        return ["Check Ollama /api/ps or ollama ps when timing telemetry is inconclusive."]
    if selected.get("loaded") is False:
        recommendations.append("Use Warm model or raise keep_alive so the configured model is loaded before all-visible suggestions.")

    expires_seconds = _seconds_until(selected.get("expires_at"))
    if expires_seconds is not None and expires_seconds <= 120:
        recommendations.append("Raise VERLATO_OLLAMA_KEEP_ALIVE or provider.keep_alive if suggestions often reload the model.")

    context_length = _optional_int(selected.get("context_length"))
    parallelism = config_snapshot.get("suggestion_parallelism")
    if context_length is not None and context_length >= 8192 and isinstance(parallelism, int) and parallelism > 1:
        recommendations.append(
            "Lower suggestionParallelism to 1 or reduce OLLAMA_CONTEXT_LENGTH/VERLATO_OLLAMA_NUM_CTX "
            "before enabling server-side parallelism."
        )

    size_vram = _optional_int(selected.get("size_vram"))
    processor = str(selected.get("processor") or "").strip()
    if size_vram is None or size_vram <= 0 or ("cpu" in processor.lower() and "gpu" not in processor.lower()):
        recommendations.append("Check ollama ps processor placement and consider a smaller/faster model if generation remains slow.")
    return recommendations


def _seconds_until(value: object) -> int | None:
    if not isinstance(value, str) or not value.strip():
        return None
    raw = value.strip()
    try:
        expires = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if expires.tzinfo is None:
        expires = expires.replace(tzinfo=timezone.utc)
    now = datetime.now(expires.tzinfo)
    return int(round((expires - now).total_seconds()))


def _unique_strings(values: list[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for value in values:
        if not value or value in seen:
            continue
        unique.append(value)
        seen.add(value)
    return unique


def _max_probe_timing(probes: list[dict[str, Any]], key: str) -> int:
    values = []
    for probe in probes:
        timings = probe.get("timings")
        if isinstance(timings, dict):
            value = timings.get(key)
            if isinstance(value, (int, float)):
                values.append(int(value))
    return max(values) if values else 0


def _timing_ratio(value: int, total: int) -> float:
    if total <= 0:
        return 0.0
    return value / total


def _suggestion_adaptive_current_settings(
    *,
    provider: str,
    prompt_profile: str,
    suggestion_count: int,
    suggestion_parallelism: int,
    suggestion_batch_size: int,
    cache_warming: bool | None,
    suggestion_strategy: str | None = None,
) -> dict[str, Any]:
    return {
        "provider": provider,
        "prompt_profile": prompt_profile,
        "suggestion_count": suggestion_count,
        "suggestion_parallelism": suggestion_parallelism,
        "suggestion_batch_size": suggestion_batch_size,
        "suggestion_strategy": suggestion_strategy,
        "cache_warming": cache_warming,
    }


def _build_suggestion_adaptive_recommendations(
    *,
    current: dict[str, Any],
    timing_sources: list[dict[str, Any]],
    feedback_events: list[dict],
    cache_hit_count: int,
    item_count: int,
    prompt_chars: int,
    source: str,
) -> dict[str, Any]:
    timing_signals = _adaptive_timing_signals(timing_sources)
    feedback_signals = _adaptive_feedback_signals(feedback_events)
    cache_hit_rate = round(cache_hit_count / item_count, 3) if item_count else None
    signals: dict[str, Any] = {
        "source": source,
        "item_count": item_count,
        "prompt_chars": prompt_chars,
        "average_prompt_chars": round(prompt_chars / item_count) if item_count else None,
        "cache_hit_count": cache_hit_count,
        "cache_hit_rate": cache_hit_rate,
        **timing_signals,
        "feedback": feedback_signals,
    }
    recommendations = _adaptive_recommendation_items(current=current, signals=signals)
    return {
        "schema_version": "verlato.suggestion_adaptive.v1",
        "current": current,
        "signals": signals,
        "recommendations": recommendations,
    }


def _adaptive_timing_signals(sources: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {
        "model_ms": 0,
        "queue_ms": 0,
        "load_ms": 0,
        "prompt_eval_ms": 0,
        "eval_ms": 0,
    }
    token_rates: list[float] = []
    for source in sources:
        timings = source.get("timings") if isinstance(source, dict) else None
        if not isinstance(timings, dict):
            continue
        for key in totals:
            value = timings.get(key)
            if isinstance(value, (int, float)):
                totals[key] += int(value)
        rate = timings.get("tokens_per_second")
        if isinstance(rate, (int, float)) and rate > 0:
            token_rates.append(float(rate))
    component_total_ms = totals["queue_ms"] + totals["load_ms"] + totals["prompt_eval_ms"] + totals["eval_ms"]
    model_ms = max(totals["model_ms"], component_total_ms)
    totals["model_ms"] = model_ms
    shares = {
        "queue_share": round(_timing_ratio(totals["queue_ms"], model_ms), 3),
        "load_share": round(_timing_ratio(totals["load_ms"], model_ms), 3),
        "prompt_eval_share": round(_timing_ratio(totals["prompt_eval_ms"], model_ms), 3),
        "generation_share": round(_timing_ratio(totals["eval_ms"], model_ms), 3),
    }
    candidates = {
        "queue": totals["queue_ms"],
        "load": totals["load_ms"],
        "prompt_eval": totals["prompt_eval_ms"],
        "generation": totals["eval_ms"],
    }
    dominant = max(candidates.items(), key=lambda item: item[1])[0] if any(candidates.values()) else "unknown"
    return {
        **totals,
        **shares,
        "dominant_bottleneck": dominant,
        "average_tokens_per_second": round(sum(token_rates) / len(token_rates), 1) if token_rates else None,
    }


def _adaptive_feedback_signals(events: list[dict]) -> dict[str, Any]:
    safe_events = [event for event in events if isinstance(event, dict)]
    event_counts = Counter(str(event.get("event") or "unknown") for event in safe_events)
    positive_names = {
        "suggestion_applied",
        "summary_edited",
        "suggestion_marked_useful",
    }
    positive_count = (
        event_counts["suggestion_applied"]
        + event_counts["summary_edited"]
        + event_counts["suggestion_marked_useful"]
    )
    negative_names = {
        "suggestion_skipped",
        "suggestion_rejected",
        "suggestion_not_useful",
        "suggestion_marked_not_useful",
        "suggestion_regenerated",
    }
    negative_count = sum(event_counts[name] for name in negative_names)
    source_feedback = _adaptive_source_feedback(safe_events, positive_names, negative_names)
    return {
        "event_count": len(safe_events),
        "event_counts": dict(sorted(event_counts.items())),
        "positive_count": positive_count,
        "negative_count": negative_count,
        "regenerated_count": event_counts["suggestion_regenerated"],
        "source_feedback": source_feedback,
    }


def _adaptive_source_feedback(
    events: list[dict],
    positive_names: set[str],
    negative_names: set[str],
) -> dict[str, dict[str, Any]]:
    sources: dict[str, dict[str, Any]] = {
        "model-suggestion": {
            "event_count": 0,
            "positive_count": 0,
            "negative_count": 0,
        },
    }
    for event in events:
        metadata = _as_dict(event.get("metadata"))
        source = str(metadata.get("source") or "")
        if source not in sources:
            continue
        event_name = str(event.get("event") or "unknown")
        bucket = sources[source]
        bucket["event_count"] = int(bucket["event_count"]) + 1
        is_positive = event_name in positive_names
        is_negative = event_name in negative_names
        if is_positive:
            bucket["positive_count"] = int(bucket["positive_count"]) + 1
        if is_negative:
            bucket["negative_count"] = int(bucket["negative_count"]) + 1

    for bucket in sources.values():
        total = int(bucket["event_count"])
        bucket["positive_rate"] = round(int(bucket["positive_count"]) / total, 3) if total else None
        bucket["negative_rate"] = round(int(bucket["negative_count"]) / total, 3) if total else None
    return sources


def _adaptive_recommendation_items(current: dict[str, Any], signals: dict[str, Any]) -> list[dict[str, Any]]:
    recommendations: list[dict[str, Any]] = []
    prompt_profile = str(current.get("prompt_profile") or "conservative")
    parallelism = current.get("suggestion_parallelism")
    suggestion_count = current.get("suggestion_count")
    feedback = _as_dict(signals.get("feedback"))
    positive_count = _optional_int(feedback.get("positive_count")) or 0
    negative_count = _optional_int(feedback.get("negative_count")) or 0
    event_count = _optional_int(feedback.get("event_count")) or 0
    negative_dominates = negative_count >= max(3, positive_count)
    if negative_dominates:
        recommendations.append(
            {
                "kind": "tune-style-before-slimming",
                "title": "Tune style before stronger prompt slimming",
                "detail": (
                    f"{negative_count} negative/regenerate event(s) versus {positive_count} positive event(s) "
                    "suggest wording quality needs attention before cutting more evidence."
                ),
                "confidence": "medium",
                "suggested_config": {
                    "suggestion_style": {
                        "guidance": (
                            "Describe the user-visible outcome first, then include the concrete changed area."
                        )
                    }
                },
            }
        )

    prompt_eval_ms = _optional_int(signals.get("prompt_eval_ms")) or 0
    prompt_eval_share = _optional_float(signals.get("prompt_eval_share")) or 0.0
    if prompt_eval_ms >= 500 and prompt_eval_share >= 0.35:
        if prompt_profile == "conservative":
            recommendations.append(
                {
                    "kind": "use-lean-prompt-profile",
                    "title": "Try the lean suggestion prompt profile",
                    "detail": (
                        "Prompt evaluation dominates suggestion time; "
                        "lean keeps core evidence while trimming prompt budget."
                    ),
                    "confidence": "medium" if not negative_dominates else "low",
                    "suggested_config": {"suggestion_prompt_profile": "lean"},
                }
            )
        elif prompt_profile == "lean" and not negative_dominates:
            recommendations.append(
                {
                    "kind": "use-minimal-prompt-profile",
                    "title": "Consider the minimal suggestion prompt profile",
                    "detail": "Lean prompts still spend most model time on prompt evaluation.",
                    "confidence": "low" if event_count < 3 else "medium",
                    "suggested_config": {"suggestion_prompt_profile": "minimal"},
                }
            )
        elif prompt_profile == "minimal":
            recommendations.append(
                {
                    "kind": "prompt-profile-at-minimum",
                    "title": "Prompt profile is already minimal",
                    "detail": (
                        "Prompt evaluation is still high, so the next lever is a faster model "
                        "or server/runtime tuning."
                    ),
                    "confidence": "medium",
                }
            )

    queue_ms = _optional_int(signals.get("queue_ms")) or 0
    queue_share = _optional_float(signals.get("queue_share")) or 0.0
    if queue_ms >= 250 and queue_share >= 0.2:
        suggested_config: dict[str, Any] = {}
        if isinstance(parallelism, int) and parallelism > 1:
            suggested_config["suggestion_parallelism"] = 1
        recommendations.append(
            {
                "kind": "reduce-queueing",
                "title": "Reduce Ollama queueing",
                "detail": (
                    "Queue time suggests Ollama is serializing or overloaded; lower client parallelism "
                    "or configure OLLAMA_NUM_PARALLEL before starting Ollama."
                ),
                "confidence": "medium",
                "suggested_config": suggested_config,
            }
        )

    eval_ms = _optional_int(signals.get("eval_ms")) or 0
    generation_share = _optional_float(signals.get("generation_share")) or 0.0
    if eval_ms >= 500 and generation_share >= 0.35:
        suggested_config = {"provider": {"suggest_num_predict": 96}}
        if isinstance(suggestion_count, int) and suggestion_count > 1:
            suggested_config["suggestion_count"] = 1
        recommendations.append(
            {
                "kind": "reduce-generation-cost",
                "title": "Reduce generation cost",
                "detail": (
                    "Generation dominates suggestion time; lower the suggestion output cap/count "
                    "or use a faster suggestion model."
                ),
                "confidence": "medium",
                "suggested_config": suggested_config,
            }
        )

    cache_hit_rate = signals.get("cache_hit_rate")
    item_count = _optional_int(signals.get("item_count")) or 0
    if isinstance(cache_hit_rate, (int, float)) and item_count >= 2 and cache_hit_rate < 0.5:
        recommendations.append(
            {
                "kind": "improve-cache-hit-rate",
                "title": "Improve suggestion cache hit rate",
                "detail": (
                    "Most visible suggestions were fresh model calls; "
                    "keep VS Code background cache warming enabled for Ollama."
                ),
                "confidence": "medium",
                "suggested_config": {"verlato.suggestionAutoWarmCache": True},
            }
        )

    if not recommendations:
        recommendations.append(
            {
                "kind": "keep-current-settings",
                "title": "Keep current suggestion settings",
                "detail": (
                    "Timing and feedback signals do not show a strong reason "
                    "to change profiles or request settings."
                ),
                "confidence": "medium",
            }
        )
    return recommendations


def _nanoseconds_to_ms(value: object) -> int | None:
    numeric = _optional_float(value)
    if numeric is None:
        return None
    return max(0, int(round(numeric / 1_000_000)))


def _optional_int(value: object) -> int | None:
    numeric = _optional_float(value)
    if numeric is None:
        return None
    return int(numeric)


def _optional_float(value: object) -> float | None:
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str) and value.strip():
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _merge_review_state(items: list[ChangeItem]) -> list[ChangeItem]:
    existing = _build_store().load().items
    by_checksum = {(item.checksum_version, item.checksum): item for item in existing}
    by_id = {item.id: item for item in existing}
    merged: list[ChangeItem] = []
    total = len(items)
    if total == 0:
        _trace_progress("merging_review_state", "Merging review state", current=0, total=0, unit="item")
        return merged
    for index, item in enumerate(items):
        _trace_progress(
            "merging_review_state",
            "Merging review state",
            detail=item.id,
            current=index,
            total=total,
            unit="item",
        )
        persisted = by_checksum.get((item.checksum_version, item.checksum)) or by_id.get(item.id)
        if persisted:
            if persisted.published:
                _trace_progress(
                    "merging_review_state",
                    "Skipped published item",
                    detail=item.id,
                    current=index + 1,
                    total=total,
                    unit="item",
                )
                continue
            item.status = persisted.status
            item.reviewed_by = persisted.reviewed_by
            item.reviewed_at = persisted.reviewed_at
            item.edited_summary = persisted.edited_summary
            item.summary_provenance = persisted.summary_provenance
            item.hidden = persisted.hidden
            for signal in persisted.evidence.commit_signals:
                if signal.startswith("provider-summary:") and signal not in item.evidence.commit_signals:
                    item.evidence.commit_signals.append(signal)
        merged.append(item)
        _trace_progress(
            "merging_review_state",
            "Merged review state",
            detail=item.id,
            current=index + 1,
            total=total,
            unit="item",
        )
    return merged


def _update_draft_review(
    *,
    base: str,
    head: str,
    item_id: str,
    status: str | None,
    summary: str | None,
    reviewer: str | None,
    model_provider: str | None = None,
    summary_provenance: dict[str, Any] | None = None,
    commit_intent_inference: str | None = None,
) -> ChangeItem:
    if status is None and summary is None and reviewer is None:
        raise SystemExit("Provide at least one review update: --status, --summary, or --reviewer")

    candidates = _detect_review_candidates(
        base,
        head,
        model_provider,
        commit_intent_inference=commit_intent_inference,
    )
    candidate = next((item for item in candidates if item.id == item_id), None)
    if not candidate:
        raise SystemExit(f"Could not find draft item {item_id} for range {base}..{head}")

    store = _build_store()
    state = store.load()
    match = next(
        (
            item
            for item in state.items
            if (item.checksum_version, item.checksum) == (candidate.checksum_version, candidate.checksum) or item.id == candidate.id
        ),
        None,
    )
    return _persist_review_update(
        candidate=match or candidate,
        status=status,
        summary=summary,
        reviewer=reviewer,
        summary_provenance=summary_provenance,
    )


def _run_draft_review(
    *,
    base: str,
    head: str,
    item_id: str,
    status: str | None,
    summary: str | None,
    reviewer: str | None,
    model_provider: str | None = None,
    summary_provenance_json: str | None = None,
    commit_intent_inference: str | None = None,
) -> int:
    summary_provenance = _parse_summary_provenance(summary_provenance_json)
    updated = _update_draft_review(
        base=base,
        head=head,
        item_id=item_id,
        status=status,
        summary=summary,
        reviewer=reviewer,
        model_provider=model_provider,
        summary_provenance=summary_provenance,
        commit_intent_inference=commit_intent_inference,
    )
    print(json.dumps(updated.to_dict(), indent=2))
    return 0


def _request_draft_model_input(
    *,
    base: str,
    head: str,
    item_id: str,
    reviewer: str | None,
    model_provider: str | None = None,
    tone: str | None = None,
    commit_intent_inference: str | None = None,
    model_privacy: str | None = None,
    allow_remote_patches: bool = False,
) -> ChangeItem:
    effective_model_provider = _resolve_suggest_model_provider(model_provider)
    config = _load_config()
    privacy_mode = resolve_model_privacy(
        config.model_privacy,
        requested=model_privacy,
        allow_remote_patches=allow_remote_patches,
    )
    target_group = _find_group_for_item_id(
        base,
        head,
        item_id,
        model_provider=effective_model_provider,
        commit_intent_inference=commit_intent_inference,
        model_privacy=privacy_mode,
        allow_remote_patches=allow_remote_patches,
    )

    suggestion_tone = _resolve_suggestion_tone(config.suggestion_tone, requested=tone)
    suggestion_style = _resolve_suggestion_style(config)
    provider_classifier = Classifier(
        provider=build_provider(config.provider, mode=effective_model_provider),
        summary_tone=suggestion_tone,
        style_guidance=str(suggestion_style["guidance"]),
        project_vocabulary=list(suggestion_style["project_vocabulary"]),
        banned_phrases=list(suggestion_style["banned_phrases"]),
        model_privacy=privacy_mode,
        provider_mode=effective_model_provider or config.provider.name,
    )
    candidate = provider_classifier.classify(target_group, base, head, summary_only=True)[0]

    if not any(signal.startswith("provider-summary:") for signal in candidate.evidence.commit_signals):
        provider_error = next(
            (
                signal.removeprefix("provider-error:")
                for signal in candidate.evidence.commit_signals
                if signal.startswith("provider-error:")
            ),
            None,
        )
        if provider_error:
            raise SystemExit(f"No model summary was produced for draft item {item_id}: {provider_error}")
        raise SystemExit(
            f"No model summary was produced for draft item {item_id}. "
            "Configure a local model provider or pass --model-provider ollama."
        )

    provenance = _summary_provenance(
        source="model-generated",
        provider=effective_model_provider,
        model=_provider_model_from_signals(candidate.evidence.commit_signals),
        prompt_sha=None,
        tone=suggestion_tone,
        style_profile=suggestion_style,
        response_metadata={},
    )
    return _persist_review_update(
        candidate=candidate,
        status="edited",
        summary=candidate.content.summary,
        reviewer=reviewer,
        summary_provenance=provenance,
    )


def _preview_draft_model_suggestions(
    *,
    base: str,
    head: str,
    item_id: str | None,
    all_visible: bool,
    count: int | None,
    parallelism: int | None = None,
    batch_size: int | None = None,
    strategy: str | None = None,
    prompt_profile: str | None = None,
    suggest_model: str | None = None,
    model_provider: str | None = None,
    tone: str | None = None,
    no_cache: bool = False,
    refresh_cache: bool = False,
    adaptive_recommendations: bool = False,
    commit_intent_inference: str | None = None,
    model_privacy: str | None = None,
    allow_remote_patches: bool = False,
    preflight: bool = False,
) -> dict:
    if all_visible == bool(item_id):
        raise SystemExit("Provide exactly one suggestion target: --id or --all-visible")
    if no_cache and refresh_cache:
        raise SystemExit("Use --no-cache or --refresh-cache, not both")

    total_start = perf_counter()
    config = _load_config()
    suggestion_count = _resolve_suggestion_count(config.suggestion_count, requested=count)
    suggestion_parallelism = _resolve_suggestion_parallelism(config.suggestion_parallelism, requested=parallelism)
    requested_suggestion_batch_size = _resolve_suggestion_batch_size(config.suggestion_batch_size, requested=batch_size)
    requested_suggestion_strategy = _resolve_suggestion_strategy(config.suggestion_strategy, requested=strategy)
    suggestion_prompt_profile = _resolve_suggestion_prompt_profile(
        config.suggestion_prompt_profile,
        requested=prompt_profile,
    )
    adaptive_enabled = _resolve_suggestion_adaptive_recommendations(
        config.suggestion_adaptive_recommendations,
        requested=adaptive_recommendations,
    )
    privacy_mode = resolve_model_privacy(
        config.model_privacy,
        requested=model_privacy,
        allow_remote_patches=allow_remote_patches,
    )
    suggestion_tone = _resolve_suggestion_tone(config.suggestion_tone, requested=tone)
    suggestion_style = _resolve_suggestion_style(config)
    cache_read_enabled = not no_cache and not refresh_cache
    cache_write_enabled = not no_cache
    suggestion_cache = _build_suggestion_cache_store(config) if (cache_read_enabled or cache_write_enabled) else None
    suggestion_count_source = (
        "cli" if count is not None else "env" if os.getenv("VERLATO_SUGGESTION_COUNT") else "config"
    )
    effective_model_provider = _resolve_suggest_model_provider(model_provider)
    effective_suggestion_strategy = _effective_suggestion_strategy(
        requested_suggestion_strategy,
        effective_model_provider or config.provider.name,
        privacy_mode=privacy_mode,
    )
    raw_patches_allowed = _can_send_raw_patches_for_config(
        config.provider,
        effective_model_provider or config.provider.name,
        privacy_mode,
    )
    if not raw_patches_allowed and effective_suggestion_strategy in {"paired", "full-diff-json", "tool-retrieval"}:
        effective_suggestion_strategy = "single"
    suggestion_provider_label = effective_model_provider or "configured"
    if (effective_model_provider or config.provider.name or "").strip().lower() in {"ollama", "local"}:
        model_context = _configured_ollama_model_context(config.provider, suggest_model=suggest_model)
    else:
        model_context = {}
    effective_suggestion_batch_size = (
        1
        if effective_suggestion_strategy in {"single", "tool-retrieval"}
        else min(requested_suggestion_batch_size, 2)
        if effective_suggestion_strategy == "paired"
        else requested_suggestion_batch_size
    )
    target_selection_start = perf_counter()
    groups = _collect_groups(
        base,
        head,
        model_provider=effective_model_provider,
        commit_intent_inference=commit_intent_inference,
        model_privacy=privacy_mode,
        allow_remote_patches=allow_remote_patches,
    )
    baseline_classifier = Classifier()
    _trace_model_suggest_stage(
        "target-selection",
        f"base={base} head={head} groups={len(groups)} item_id={item_id or 'none'}",
    )
    _trace_progress(
        "selecting_suggestion_targets",
        "Selecting suggestion targets",
        current=0,
        total=len(groups),
        unit="changeset",
    )

    targets = []
    for index, group in enumerate(groups):
        _trace_progress(
            "selecting_suggestion_targets",
            "Selecting suggestion targets",
            detail=_group_progress_detail(group),
            current=index,
            total=len(groups),
            unit="changeset",
        )
        baseline_item = baseline_classifier.classify(group, base, head)[0]
        if item_id and baseline_item.id != item_id:
            _trace_progress(
                "selecting_suggestion_targets",
                "Checked suggestion target",
                detail=baseline_item.id,
                current=index + 1,
                total=len(groups),
                unit="changeset",
            )
            continue
        targets.append((group, baseline_item))
        _trace_progress(
            "selecting_suggestion_targets",
            "Selected suggestion target",
            detail=baseline_item.id,
            current=index + 1,
            total=len(groups),
            unit="changeset",
            item_id=baseline_item.id,
        )

    if item_id and not targets:
        raise SystemExit(f"Could not find draft item {item_id} for range {base}..{head}")

    merged = _merge_review_state([item for _group, item in targets])
    merged_by_id = {item.id: item for item in merged}
    if all_visible:
        visible_ids = {item.id for item in merged if not item.hidden}
        targets = [(group, merged_by_id.get(item.id, item)) for group, item in targets if item.id in visible_ids]
        _trace_progress(
            "filtering_visible_suggestions",
            "Filtering visible items",
            current=len(targets),
            total=len(merged),
            unit="item",
        )
    else:
        targets = [(group, merged_by_id.get(item.id, item)) for group, item in targets]
    target_selection_ms = _elapsed_ms(target_selection_start)

    max_model_items = _resolve_max_model_items(config.provider.max_items_per_scan)
    visible_target_count = len(targets)
    if all_visible and max_model_items == 0:
        fanout_batch_size = 0
        target_batches = []
    else:
        fanout_batch_size = max_model_items if all_visible and max_model_items > 0 else len(targets) or 1
        target_batches = [
            targets[index : index + fanout_batch_size]
            for index in range(0, len(targets), fanout_batch_size)
        ] or [[]]
    if all_visible and max_model_items >= 0 and len(target_batches) > 1:
        _trace_model_suggest_stage(
            "fanout-batches",
            (
                f"visible_items={len(targets)} fanout_guard={max_model_items} "
                f"batch_size={fanout_batch_size} batches={len(target_batches)}"
            ),
        )
    total_targets = sum(len(batch) for batch in target_batches)
    _trace_progress(
        "planning_suggestions",
        "Planning model suggestions",
        detail=f"{len(target_batches)} batch(es)",
        current=0,
        total=total_targets,
        unit="item",
        batch_size=fanout_batch_size,
        batch_total=len(target_batches),
        fanout_guard=max_model_items,
    )
    _trace_model_suggest_plan(
        base=base,
        head=head,
        target_count=len(targets),
        suggestion_count=suggestion_count,
        suggestion_count_source=suggestion_count_source,
        provider=suggestion_provider_label,
        parallelism=suggestion_parallelism,
        all_visible=all_visible,
    )
    _trace_suggestion_event(
        "suggestion_preview_started",
        preview={
            "base_ref": base,
            "head_ref": head,
            "provider": suggestion_provider_label,
            **model_context,
            "privacy": privacy_summary(
                provider=suggestion_provider_label,
                privacy_mode=privacy_mode,
                raw_patch_chars_available=0,
                raw_patch_chars_sent=0,
            ),
            "preflight": preflight,
            "count": suggestion_count,
            "prompt_profile": suggestion_prompt_profile,
            "tone": suggestion_tone,
            "style_profile": suggestion_style,
            "performance": {
                "strategy": effective_suggestion_strategy,
                "parallelism": suggestion_parallelism,
                "target_selection_ms": target_selection_ms,
                "preparation_ms": 0,
                "prompt_chars": 0,
                "full_diff_prompt_chars": 0,
                "full_diff_file_count": 0,
                "full_diff_patch_chars": 0,
                "raw_patch_chars_available": 0,
                "raw_patch_chars_sent": 0,
                "privacy_mode": privacy_mode,
                "model_ms": 0,
                "parse_ms": 0,
                "total_ms": 0,
                "batch_size": effective_suggestion_batch_size,
                "model_call_count": 0,
                "batched_model_calls": 0,
                "single_model_calls": 0,
                "bulk_model_calls": 0,
                "fallback_single_calls": 0,
                "fallback_item_count": 0,
                "model_calls_saved": 0,
            },
            "visible_item_count": visible_target_count,
            "fanout_guard": max_model_items,
            "batch_size": fanout_batch_size,
            "batch_count": len(target_batches),
            "items": [],
        },
        total=total_targets,
        batch_size=fanout_batch_size,
        batch_total=len(target_batches),
        fanout_guard=max_model_items,
    )

    if not any(target_batches):
        _trace_progress("suggestions_ready", "No model suggestions requested", current=0, total=0, unit="item")
        if suggestion_cache is not None and suggestion_cache.dirty:
            suggestion_cache.save()
        total_ms = _elapsed_ms(total_start)
        result = {
            "base_ref": base,
            "head_ref": head,
            "provider": suggestion_provider_label,
            **model_context,
            "privacy": privacy_summary(
                provider=suggestion_provider_label,
                privacy_mode=privacy_mode,
                raw_patch_chars_available=0,
                raw_patch_chars_sent=0,
            ),
            "preflight": preflight,
            "count": suggestion_count,
            "prompt_profile": suggestion_prompt_profile,
            "tone": suggestion_tone,
            "style_profile": suggestion_style,
            "performance": {
                "strategy": effective_suggestion_strategy,
                "parallelism": suggestion_parallelism,
                "target_selection_ms": target_selection_ms,
                "preparation_ms": 0,
                "prompt_chars": 0,
                "full_diff_prompt_chars": 0,
                "full_diff_file_count": 0,
                "full_diff_patch_chars": 0,
                "raw_patch_chars_available": 0,
                "raw_patch_chars_sent": 0,
                "privacy_mode": privacy_mode,
                "model_ms": 0,
                "parse_ms": 0,
                "total_ms": total_ms,
                "batch_size": effective_suggestion_batch_size,
                "model_call_count": 0,
                "batched_model_calls": 0,
                "single_model_calls": 0,
                "bulk_model_calls": 0,
                "fallback_single_calls": 0,
                "fallback_item_count": 0,
                "model_calls_saved": 0,
            },
            "visible_item_count": visible_target_count,
            "fanout_guard": max_model_items,
            "batch_size": fanout_batch_size,
            "batch_count": len(target_batches),
            "items": [],
        }
        if adaptive_enabled:
            result["adaptive_recommendations"] = _build_suggestion_adaptive_recommendations(
                current=_suggestion_adaptive_current_settings(
                    provider=suggestion_provider_label,
                    prompt_profile=suggestion_prompt_profile,
                    suggestion_count=suggestion_count,
                    suggestion_parallelism=suggestion_parallelism,
                    suggestion_batch_size=effective_suggestion_batch_size,
                    suggestion_strategy=effective_suggestion_strategy,
                    cache_warming=None,
                ),
                timing_sources=[],
                feedback_events=_build_store().load().feedback_events,
                cache_hit_count=0,
                item_count=0,
                prompt_chars=0,
                source="preview",
            )
        return result

    provider = None
    if preflight:
        cache_provider_mode = effective_model_provider or "configured"
        cache_model_id = _normalized_suggest_model(suggest_model) or config.provider.model or ""
    else:
        provider = _build_provider_for_suggestion(config.provider, mode=effective_model_provider, suggest_model=suggest_model)
        model_context = _provider_model_context(provider, effective_model_provider or config.provider.name)
        cache_provider_mode = effective_model_provider or "configured"
        cache_model_id = _provider_cache_model_id(provider, cache_provider_mode)
    provider_classifier = Classifier(
        provider=provider,
        summary_tone=suggestion_tone,
        style_guidance=str(suggestion_style["guidance"]),
        project_vocabulary=list(suggestion_style["project_vocabulary"]),
        banned_phrases=list(suggestion_style["banned_phrases"]),
        model_privacy=privacy_mode,
        provider_mode=effective_model_provider or config.provider.name,
    )

    items: list[dict[str, Any]] = []
    completed_count = 0
    preparation_ms = 0
    prompt_chars = 0
    full_diff_prompt_chars = 0
    full_diff_file_count = 0
    full_diff_patch_chars = 0
    raw_patch_chars_available_total = 0
    model_ms = 0
    parse_ms = 0
    uncached_item_count = 0
    model_call_count = 0
    batched_model_calls = 0
    single_model_calls = 0
    bulk_model_calls = 0
    fallback_single_calls = 0
    fallback_item_count = 0

    def prepare_work(order: int, group: ChangeGroup, baseline_item: ChangeItem, batch_index: int) -> _SuggestionWork:
        nonlocal preparation_ms, prompt_chars, full_diff_file_count, full_diff_patch_chars
        nonlocal raw_patch_chars_available_total
        item_number = order + 1
        item_start = perf_counter()
        _trace_progress(
            "suggestion_evidence",
            f"Building suggestion evidence for {baseline_item.id}",
            current=item_number,
            total=total_targets,
            unit="item",
            item_id=baseline_item.id,
            batch=batch_index,
            batch_total=len(target_batches),
        )
        evidence_start = perf_counter()
        diff = provider_classifier.diff_builder.build(changed_files_for_group(group), commits=group.commits)
        evidence_ms = _elapsed_ms(evidence_start)
        hunk_count = sum(len(hunks) for hunks in diff.representative_hunks.values())
        file_count = len(diff.file_statuses)
        _trace_model_suggest_stage(
            "evidence-built",
            f"item_id={baseline_item.id} files={file_count} hunks={hunk_count}",
        )
        _trace_progress(
            "suggestion_prompt",
            f"Building model prompt for {baseline_item.id}",
            current=item_number,
            total=total_targets,
            unit="item",
            item_id=baseline_item.id,
            batch=batch_index,
            batch_total=len(target_batches),
            elapsed_ms=_elapsed_ms(item_start),
        )
        prompt_start = perf_counter()
        changed_files = changed_files_for_group(group)
        raw_patch_chars_for_item = raw_patch_chars_available(changed_files)
        change_summary = build_change_summary_payload(group=group, item=baseline_item, diff=diff)
        evidence = {
            "title": baseline_item.content.title,
            "item_id": baseline_item.id,
            "suggestion_count": suggestion_count,
            "tone": suggestion_tone,
            "style_profile": suggestion_style,
            "change_summary": change_summary,
            "file_count": file_count,
            "hunk_count": hunk_count,
            "privacy_mode": privacy_mode,
            "raw_patch_chars_available": raw_patch_chars_for_item,
            "raw_patch_chars_sent": 0,
        }
        source_evidence = _build_suggestion_source_evidence(diff, group, include_hunks=raw_patches_allowed)
        prompt_shape = _suggestion_prompt_shape(effective_suggestion_strategy)
        full_diff_item: dict[str, Any] | None = None
        tools: list[dict[str, Any]] = []
        tool_executor = None
        max_tool_rounds = 0
        cache_key = ""
        prompt_result = provider_classifier._build_summary_prompt_with_budget(
            title=baseline_item.content.title,
            labels=[label.lower() for label in (group.pr.labels if group.pr else [])],
            pr_body=group.pr.body if group.pr else "",
            diff=diff,
            commits=group.commits,
            suggestion_count=suggestion_count,
            kind=baseline_item.classification.kind,
            audience=baseline_item.classification.audience,
            tone=suggestion_tone,
            style_guidance=str(suggestion_style["guidance"]),
            project_vocabulary=list(suggestion_style["project_vocabulary"]),
            banned_phrases=list(suggestion_style["banned_phrases"]),
            prompt_profile=suggestion_prompt_profile,
            # The semantic diff is compact, so every strategy gets the privacy-allowed detail level;
            # raw hunks (legacy git engine only) stay limited to the single-item strategy.
            include_hunks=raw_patches_allowed
            and (effective_suggestion_strategy == "single" or diff.semantic is not None),
        )
        prompt = prompt_result.prompt
        prompt_budget = dict(prompt_result.budget)
        prompt_chars += int(prompt_budget.get("prompt_chars", len(prompt)))
        full_diff_item = _build_full_diff_suggestion_payload_item(
            group,
            baseline_item,
            diff,
            include_patches=raw_patches_allowed,
        )
        if effective_suggestion_strategy == "tool-retrieval":
            old_prompt_chars = int(prompt_budget.get("prompt_chars", len(prompt)))
            tool_provider = SuggestionEvidenceToolProvider.from_groups(
                [group], [baseline_item], semantic_detail=raw_patches_allowed
            )
            tools = tool_provider.ollama_tools()
            tool_executor = tool_provider.call_tool
            max_tool_rounds = SUGGESTION_TOOL_MAX_ROUNDS
            prompt = _build_tool_retrieval_suggestion_prompt(
                tool_provider=tool_provider,
                item=baseline_item,
                suggestion_count=suggestion_count,
                tone=suggestion_tone,
                style_profile=suggestion_style,
            )
            prompt_budget["prompt_chars"] = len(prompt)
            prompt_budget["tool_count"] = len(tools)
            prompt_budget["max_tool_rounds"] = max_tool_rounds
            prompt_chars += len(prompt) - old_prompt_chars
        full_diff_file_count += int(full_diff_item.get("file_count") or 0)
        full_diff_patch_chars += int(full_diff_item.get("patch_chars_sent") or 0)
        raw_patch_chars_available_total += int(full_diff_item.get("patch_chars_available") or 0)
        prompt_sha_source = (
            json.dumps(full_diff_item, sort_keys=True, separators=(",", ":"), default=str)
            if effective_suggestion_strategy == "full-diff-json"
            else json.dumps(
                {
                    "prompt": prompt,
                    "tools": tools,
                    "manifest": full_diff_item if effective_suggestion_strategy == "tool-retrieval" else None,
                },
                sort_keys=True,
                separators=(",", ":"),
                default=str,
            )
            if effective_suggestion_strategy == "tool-retrieval"
            else prompt
        )
        prompt_budget["strategy"] = effective_suggestion_strategy
        prompt_budget["prompt_shape"] = prompt_shape
        prompt_budget["full_diff_file_count"] = full_diff_item.get("file_count", 0)
        prompt_budget["full_diff_patch_chars_available"] = full_diff_item.get("patch_chars_available", 0)
        prompt_budget["full_diff_patch_chars_sent"] = full_diff_item.get("patch_chars_sent", 0)
        prompt_budget["full_diff_missing_patch_count"] = full_diff_item.get("missing_patch_count", 0)
        prompt_budget["privacy_mode"] = privacy_mode
        prompt_budget["raw_patch_chars_available"] = raw_patch_chars_for_item
        prompt_budget["raw_patch_chars_sent"] = int(full_diff_item.get("patch_chars_sent") or 0)
        prompt_budget["omitted_fields"] = [] if raw_patches_allowed else list(OMITTED_PATCH_EVIDENCE)
        evidence["strategy"] = effective_suggestion_strategy
        evidence["raw_patch_chars_sent"] = int(prompt_budget["raw_patch_chars_sent"])
        evidence["request"] = model_request_record(
            task="suggest-release-note",
            provider=suggestion_provider_label,
            privacy_mode=privacy_mode,
            prompt=prompt,
            evidence=evidence,
            raw_patch_chars_available=raw_patch_chars_for_item,
            raw_patch_chars_sent=int(prompt_budget["raw_patch_chars_sent"]),
            omitted_fields=[] if raw_patches_allowed else list(OMITTED_PATCH_EVIDENCE),
            omission_reasons=[]
            if raw_patches_allowed
            else ["metadata-only mode sends ChangeSummary instead of raw diff text"],
        )
        if tools:
            evidence["tool_count"] = len(tools)
            evidence["max_tool_rounds"] = max_tool_rounds
        prompt_sha = hashlib.sha256(prompt_sha_source.encode("utf-8")).hexdigest()[:12]
        cache_key = _suggestion_cache_key(
            item=baseline_item,
            prompt_sha=prompt_sha,
            provider_mode=cache_provider_mode,
            model_id=cache_model_id,
            tone=suggestion_tone,
            style_profile=suggestion_style,
            suggestion_count=suggestion_count,
            prompt_profile=str(prompt_budget.get("profile") or SUGGESTION_CACHE_PROMPT_PROFILE),
            strategy=effective_suggestion_strategy,
            prompt_shape=prompt_shape,
            privacy_mode=privacy_mode,
        )
        prompt_ms = _elapsed_ms(prompt_start)
        preparation_ms += _elapsed_ms(item_start)
        _trace_model_suggest_stage(
            "evidence-detail",
            f"item_id={baseline_item.id} files={','.join(list(diff.file_statuses)[:8]) or 'none'} "
            f"source_entries={len(source_evidence)}",
        )
        _trace_model_suggest_stage(
            "prompt-built",
            f"item_id={baseline_item.id} prompt_chars={len(prompt)} prompt_sha={prompt_sha}",
        )
        _trace_model_prompt_budget(prompt_budget)
        if prompt:
            _trace_model_payload("prompt-preview", prompt)
        full_diff_file_count_for_work = int(full_diff_item.get("file_count") or 0) if full_diff_item is not None else 0
        full_diff_patch_chars_for_work = (
            int(full_diff_item.get("patch_chars_sent") or 0) if full_diff_item is not None else 0
        )
        full_diff_missing_patch_count_for_work = (
            int(full_diff_item.get("missing_patch_count") or 0) if full_diff_item is not None else 0
        )
        return _SuggestionWork(
            order=order,
            item_number=item_number,
            batch=batch_index,
            batch_total=len(target_batches),
            total_targets=total_targets,
            group=group,
            baseline_item=baseline_item,
            prompt=prompt,
            evidence=evidence,
            source_evidence=source_evidence,
            prompt_sha=prompt_sha,
            prompt_budget=prompt_budget,
            cache_key=cache_key,
            strategy=effective_suggestion_strategy,
            prompt_shape=prompt_shape,
            full_diff_item=full_diff_item,
            full_diff_file_count=full_diff_file_count_for_work,
            full_diff_patch_chars=full_diff_patch_chars_for_work,
            full_diff_missing_patch_count=full_diff_missing_patch_count_for_work,
            tools=tools,
            tool_executor=tool_executor,
            max_tool_rounds=max_tool_rounds,
            suggestion_count=suggestion_count,
            suggestion_tone=suggestion_tone,
            suggestion_style=suggestion_style,
            effective_model_provider=effective_model_provider,
            evidence_ms=evidence_ms,
            prompt_ms=prompt_ms,
            started_at=item_start,
        )

    def record_result(result: _SuggestionResult) -> None:
        nonlocal completed_count, model_ms, parse_ms, model_call_count, batched_model_calls, single_model_calls
        nonlocal bulk_model_calls, fallback_single_calls, fallback_item_count, full_diff_prompt_chars
        completed_count += 1
        model_ms += result.aggregate_model_ms
        parse_ms += result.parse_ms
        model_call_count += result.model_call_count
        batched_model_calls += result.batched_model_calls
        single_model_calls += result.single_model_calls
        bulk_model_calls += result.bulk_model_calls
        fallback_single_calls += result.fallback_single_calls
        fallback_item_count += result.fallback_item_count
        full_diff_prompt_chars += result.full_diff_prompt_chars
        timings = result.item.get("timings")
        elapsed = timings.get("total_ms") if isinstance(timings, dict) else None
        _trace_progress(
            "suggestion_item_done",
            f"Prepared suggestion preflight for {result.item['item_id']}"
            if result.item.get("preflight")
            else f"Generated suggestion for {result.item['item_id']}",
            current=completed_count,
            total=total_targets,
            unit="item",
            item_id=result.item["item_id"],
            batch=result.item.get("batch"),
            batch_total=len(target_batches),
            elapsed_ms=elapsed,
            model_ms=result.model_ms,
            queue_ms=timings.get("queue_ms") if isinstance(timings, dict) else None,
        )
        event_item = dict(result.item)
        event_item.pop("batch", None)
        _trace_suggestion_event(
            "suggestion_item_result",
            order=result.order,
            item_id=result.item["item_id"],
            current=completed_count,
            total=total_targets,
            batch=result.item.get("batch"),
            batch_total=len(target_batches),
            item=event_item,
        )
        cache_info = result.item.get("cache")
        if (
            cache_write_enabled
            and suggestion_cache is not None
            and isinstance(cache_info, dict)
            and cache_info.get("hit") is False
            and isinstance(cache_info.get("key"), str)
        ):
            suggestion_cache.put(cache_info["key"], _suggestion_cache_stored_item(result.item))

    def record_failure(work: _SuggestionWork, exc: BaseException) -> None:
        code = exc.code if isinstance(exc, SystemExit) else exc
        message = str(code) if code is not None else exc.__class__.__name__
        _trace_suggestion_event(
            "suggestion_item_failed",
            order=work.order,
            item_id=work.baseline_item.id,
            message=message,
            current=completed_count,
            total=total_targets,
            batch=work.batch,
            batch_total=len(target_batches),
        )

    def cached_result_for_work(work: _SuggestionWork) -> _SuggestionResult | None:
        if not cache_read_enabled or suggestion_cache is None:
            return None
        cached = suggestion_cache.get(work.cache_key)
        if cached is None:
            return None
        return _cached_suggestion_result(work, cached)

    for batch_index, batch in enumerate(target_batches, start=1):
        _trace_progress(
            "suggestion_batch",
            "Preparing suggestion batch",
            detail=f"batch {batch_index}/{len(target_batches)}",
            current=batch_index,
            total=len(target_batches),
            unit="batch",
            batch=batch_index,
            batch_total=len(target_batches),
        )
        if len(target_batches) > 1:
            _trace_model_suggest_stage(
                "batch-start",
                f"batch={batch_index}/{len(target_batches)} targets={len(batch)} fanout_guard={max_model_items}",
            )
        batch_results: dict[int, _SuggestionResult] = {}
        miss_chunks: list[list[_SuggestionWork]] = []
        pending_chunk: list[_SuggestionWork] = []
        batch_base_order = len(items)
        for batch_offset, (group, baseline_item) in enumerate(batch):
            order = batch_base_order + batch_offset
            work = prepare_work(order, group, baseline_item, batch_index)
            if preflight:
                preflight_result = _preflight_suggestion_result(work)
                batch_results[preflight_result.order] = preflight_result
                record_result(preflight_result)
                continue
            cached_result = cached_result_for_work(work)
            if cached_result is not None:
                batch_results[cached_result.order] = cached_result
                record_result(cached_result)
                continue
            uncached_item_count += 1
            if effective_suggestion_strategy == "full-diff-json":
                if pending_chunk:
                    candidate_chunk = pending_chunk + [work]
                    if (
                        len(candidate_chunk) > effective_suggestion_batch_size
                        or _full_diff_batch_prompt_chars(candidate_chunk) > FULL_DIFF_BATCH_PROMPT_CHAR_GUARD
                    ):
                        miss_chunks.append(pending_chunk)
                        pending_chunk = []
                pending_chunk.append(work)
                if len(pending_chunk) >= effective_suggestion_batch_size:
                    miss_chunks.append(pending_chunk)
                    pending_chunk = []
            else:
                pending_chunk.append(work)
                if len(pending_chunk) >= effective_suggestion_batch_size:
                    miss_chunks.append(pending_chunk)
                    pending_chunk = []
        if pending_chunk:
            miss_chunks.append(pending_chunk)

        if suggestion_parallelism <= 1 or len(miss_chunks) <= 1:
            for chunk in miss_chunks:
                try:
                    chunk_results = _run_suggestion_work_chunk(provider, provider_classifier, chunk)
                except _SuggestionWorkFailure as exc:
                    for work_result in exc.completed_results:
                        batch_results[work_result.order] = work_result
                        record_result(work_result)
                    record_failure(exc.work, exc.exc)
                    raise exc.exc from exc
                for work_result in chunk_results:
                    batch_results[work_result.order] = work_result
                    record_result(work_result)
        else:
            workers = min(suggestion_parallelism, len(miss_chunks))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="verlato-suggest") as executor:
                future_to_chunk = {
                    executor.submit(_run_suggestion_work_chunk, provider, provider_classifier, chunk): chunk
                    for chunk in miss_chunks
                }
                try:
                    for future in as_completed(future_to_chunk):
                        try:
                            chunk_results = future.result()
                        except _SuggestionWorkFailure as exc:
                            for work_result in exc.completed_results:
                                batch_results[work_result.order] = work_result
                                record_result(work_result)
                            record_failure(exc.work, exc.exc)
                            raise exc.exc from exc
                        except (Exception, SystemExit) as exc:
                            for work in future_to_chunk[future]:
                                record_failure(work, exc)
                            raise
                        for work_result in chunk_results:
                            batch_results[work_result.order] = work_result
                            record_result(work_result)
                except BaseException:
                    for pending in future_to_chunk:
                        pending.cancel()
                    raise
        for order in sorted(batch_results):
            result_item = dict(batch_results[order].item)
            result_item.pop("batch", None)
            items.append(result_item)

    if suggestion_cache is not None and suggestion_cache.dirty:
        suggestion_cache.save()
    total_ms = _elapsed_ms(total_start)
    model_calls_saved = max(0, uncached_item_count - model_call_count)
    _trace_progress(
        "suggestions_ready",
        "Generated suggestions",
        current=len(items),
        total=total_targets,
        unit="item",
        elapsed_ms=total_ms,
        model_ms=model_ms,
    )
    result = {
        "base_ref": base,
        "head_ref": head,
        "provider": suggestion_provider_label,
        **model_context,
        "privacy": privacy_summary(
            provider=suggestion_provider_label,
            privacy_mode=privacy_mode,
            raw_patch_chars_available=raw_patch_chars_available_total,
            raw_patch_chars_sent=full_diff_patch_chars,
        ),
        "preflight": preflight,
        "count": suggestion_count,
        "prompt_profile": suggestion_prompt_profile,
        "tone": suggestion_tone,
        "style_profile": suggestion_style,
        "performance": {
            "strategy": effective_suggestion_strategy,
            "parallelism": suggestion_parallelism,
            "target_selection_ms": target_selection_ms,
            "preparation_ms": preparation_ms,
            "prompt_chars": prompt_chars,
            "full_diff_prompt_chars": full_diff_prompt_chars,
            "full_diff_file_count": full_diff_file_count,
            "full_diff_patch_chars": full_diff_patch_chars,
            "raw_patch_chars_available": raw_patch_chars_available_total,
            "raw_patch_chars_sent": full_diff_patch_chars,
            "privacy_mode": privacy_mode,
            "model_ms": model_ms,
            "parse_ms": parse_ms,
            "total_ms": total_ms,
            "batch_size": effective_suggestion_batch_size,
            "model_call_count": model_call_count,
            "batched_model_calls": batched_model_calls,
            "single_model_calls": single_model_calls,
            "bulk_model_calls": bulk_model_calls,
            "fallback_single_calls": fallback_single_calls,
            "fallback_item_count": fallback_item_count,
            "model_calls_saved": model_calls_saved,
        },
        "visible_item_count": visible_target_count,
        "fanout_guard": max_model_items,
        "batch_size": fanout_batch_size,
        "batch_count": len(target_batches),
        "items": items,
    }
    if adaptive_enabled:
        result["adaptive_recommendations"] = _build_suggestion_adaptive_recommendations(
            current=_suggestion_adaptive_current_settings(
                provider=suggestion_provider_label,
                prompt_profile=suggestion_prompt_profile,
                suggestion_count=suggestion_count,
                suggestion_parallelism=suggestion_parallelism,
                suggestion_batch_size=effective_suggestion_batch_size,
                suggestion_strategy=effective_suggestion_strategy,
                cache_warming=None,
            ),
            timing_sources=items,
            feedback_events=_build_store().load().feedback_events,
            cache_hit_count=sum(
                1
                for item in items
                if isinstance(cache_info := item.get("cache"), dict) and cache_info.get("hit") is True
            ),
            item_count=len(items),
            prompt_chars=full_diff_prompt_chars or prompt_chars,
            source="preview",
        )
    return result


def _provider_cache_model_id(provider: object, provider_mode: str) -> str:
    normalized_mode = (provider_mode or "configured").strip().lower()
    raw_model = (
        _provider_effective_model(provider, "suggest")
        if normalized_mode in {"ollama", "local"}
        else getattr(provider, "model", None)
    )
    model = str(raw_model).strip() if raw_model is not None else ""
    if normalized_mode in {"ollama", "local"}:
        return f"ollama:{model or 'default'}"
    if normalized_mode in {"github-copilot", "github", "copilot"}:
        return f"github-copilot:{model or 'default'}"
    if normalized_mode in {"codex-cli", "codex"}:
        return f"codex-cli:{model or 'default'}"
    if normalized_mode in {"claude-code", "claude"}:
        return f"claude-code:{model or 'default'}"
    if normalized_mode in {"litellm", "lite-llm"}:
        return f"litellm:{model or 'default'}"
    if normalized_mode in {"plugin", "external-plugin", "model-plugin"}:
        return f"plugin:{model or provider.__class__.__name__}"
    if normalized_mode == "mock":
        return "mock-local"
    if normalized_mode == "stub":
        return "stub"
    return model or provider.__class__.__name__


def _suggestion_cache_key(
    *,
    item: ChangeItem,
    prompt_sha: str,
    provider_mode: str,
    model_id: str,
    tone: str,
    style_profile: dict[str, Any],
    suggestion_count: int,
    prompt_profile: str,
    strategy: str = "single",
    prompt_shape: str = SINGLE_SUGGESTION_PROMPT_SHAPE,
    privacy_mode: str = "metadata-only",
) -> str:
    current_summary = item.edited_summary or item.content.summary
    payload = {
        "schema": "v2",
        "item_id": item.id,
        "checksum_version": item.checksum_version,
        "checksum": item.checksum,
        "current_summary_sha": hashlib.sha256(current_summary.encode("utf-8")).hexdigest()[:12],
        "prompt_sha": prompt_sha,
        "strategy": strategy,
        "prompt_shape": prompt_shape,
        "privacy_mode": privacy_mode,
        "provider_mode": provider_mode,
        "model_id": model_id,
        "tone": tone,
        "style_profile_sha": _stable_json_sha(style_profile),
        "suggestion_count": suggestion_count,
        "prompt_profile": prompt_profile,
    }
    serialized = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return "suggestion-v1:" + hashlib.sha256(serialized.encode("utf-8")).hexdigest()


def _stable_json_sha(value: object) -> str:
    serialized = json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(serialized.encode("utf-8")).hexdigest()[:12]


def _suggestion_cache_stored_item(item: dict[str, Any]) -> dict[str, Any]:
    transient = {
        "batch",
        "cache",
        "provider_metadata",
        "provider_reasoning",
        "timings",
        "ai_request",
        "request_envelope",
    }
    return {key: value for key, value in item.items() if key not in transient}


def _cached_suggestion_result(work: _SuggestionWork, cached: CachedSuggestion) -> _SuggestionResult | None:
    cached_item = dict(cached.item)
    raw_suggestions = cached_item.get("suggestions")
    if not isinstance(raw_suggestions, list):
        return None
    suggestions = [suggestion for suggestion in raw_suggestions if isinstance(suggestion, str) and suggestion.strip()]
    if not suggestions:
        return None
    current_summary = work.baseline_item.edited_summary or work.baseline_item.content.summary
    cached_item.pop("provider_metadata", None)
    cached_item.pop("provider_reasoning", None)
    cached_item.pop("timings", None)
    cached_item.pop("cache", None)
    cached_item.pop("batch", None)
    item = {
        **cached_item,
        "item_id": work.baseline_item.id,
        "title": work.baseline_item.content.title,
        "current_summary": current_summary,
        "provider": (
            cached_item.get("provider")
            if isinstance(cached_item.get("provider"), str)
            else work.effective_model_provider or "configured"
        ),
        "prompt_sha": work.prompt_sha,
        "tone": work.suggestion_tone,
        "style_profile": work.suggestion_style,
        "source_evidence": work.source_evidence,
        "prompt_budget": work.prompt_budget,
        "privacy": privacy_summary(
            provider=work.effective_model_provider or "configured",
            privacy_mode=str(work.prompt_budget.get("privacy_mode") or "metadata-only"),
            raw_patch_chars_available=int(work.prompt_budget.get("raw_patch_chars_available") or 0),
            raw_patch_chars_sent=int(work.prompt_budget.get("raw_patch_chars_sent") or 0),
        ),
        "ai_request": work.evidence.get("request") if isinstance(work.evidence.get("request"), dict) else {},
        "ai_response": {"parse_result": "cache-hit", "accepted_count": len(suggestions), "rejected_count": 0},
        "suggestion_details": _cached_suggestion_details(cached_item, suggestions, work, current_summary),
        "rejected_candidates": cached_item.get("rejected_candidates") if isinstance(cached_item.get("rejected_candidates"), list) else [],
        "suggestions": suggestions,
        "cache": {
            "hit": True,
            "key": work.cache_key,
            "age_seconds": cached.age_seconds,
        },
        "timings": {
            "evidence_ms": work.evidence_ms,
            "prompt_ms": work.prompt_ms,
            "model_ms": 0,
            "parse_ms": 0,
            "total_ms": _elapsed_ms(work.started_at),
            "strategy": work.strategy,
            "file_count": work.full_diff_file_count,
            "patch_chars": work.full_diff_patch_chars,
            "missing_patch_count": work.full_diff_missing_patch_count,
        },
        "batch": work.batch,
    }
    return _SuggestionResult(order=work.order, item=item, model_ms=0, parse_ms=0)


def _preflight_suggestion_result(work: _SuggestionWork) -> _SuggestionResult:
    request_record = work.evidence.get("request") if isinstance(work.evidence.get("request"), dict) else {}
    privacy_mode = str(work.prompt_budget.get("privacy_mode") or "metadata-only")
    raw_patch_chars_available = int(work.prompt_budget.get("raw_patch_chars_available") or 0)
    raw_patch_chars_sent = int(work.prompt_budget.get("raw_patch_chars_sent") or 0)
    item = {
        "item_id": work.baseline_item.id,
        "title": work.baseline_item.content.title,
        "current_summary": work.baseline_item.edited_summary or work.baseline_item.content.summary,
        "provider": work.effective_model_provider or "configured",
        "prompt_sha": work.prompt_sha,
        "tone": work.suggestion_tone,
        "style_profile": work.suggestion_style,
        "source_evidence": work.source_evidence,
        "prompt_budget": work.prompt_budget,
        "privacy": privacy_summary(
            provider=work.effective_model_provider or "configured",
            privacy_mode=privacy_mode,
            raw_patch_chars_available=raw_patch_chars_available,
            raw_patch_chars_sent=raw_patch_chars_sent,
        ),
        "ai_request": request_record,
        "request_envelope": {
            "task": "suggest",
            "prompt": work.prompt,
            "evidence": work.evidence,
            "tools": [tool.get("function", {}).get("name") for tool in work.tools if isinstance(tool, dict)],
            "response_format": "suggestion text" if work.strategy != "full-diff-json" else "suggestion batch JSON",
        },
        "suggestion_details": [],
        "rejected_candidates": [],
        "suggestions": [],
        "preflight": True,
        "timings": {
            "evidence_ms": work.evidence_ms,
            "prompt_ms": work.prompt_ms,
            "model_ms": 0,
            "parse_ms": 0,
            "total_ms": _elapsed_ms(work.started_at),
            "strategy": work.strategy,
            "file_count": work.full_diff_file_count,
            "patch_chars": work.full_diff_patch_chars,
            "missing_patch_count": work.full_diff_missing_patch_count,
        },
        "batch": work.batch,
    }
    return _SuggestionResult(order=work.order, item=item, model_ms=0, parse_ms=0)


def _cached_suggestion_details(
    cached_item: dict[str, Any],
    suggestions: list[str],
    work: _SuggestionWork,
    current_summary: str,
) -> list[dict[str, Any]]:
    raw_details = cached_item.get("suggestion_details")
    details_by_summary = {}
    if isinstance(raw_details, list):
        for detail in raw_details:
            if isinstance(detail, dict) and isinstance(detail.get("summary"), str):
                details_by_summary[detail["summary"]] = detail
    details: list[dict[str, Any]] = []
    for suggestion in suggestions:
        detail = dict(details_by_summary.get(suggestion) or {})
        detail["summary"] = suggestion
        detail["similarity_to_current"] = _summary_similarity(suggestion, current_summary)
        detail["specificity_score"] = _summary_specificity(suggestion, work.group)
        detail["comparison"] = {
            "current": current_summary,
            "suggested": suggestion,
        }
        details.append(detail)
    return details


def _run_suggestion_work_chunk(provider, provider_classifier: Classifier, works: list[_SuggestionWork]) -> list[_SuggestionResult]:
    if works and works[0].strategy == "full-diff-json":
        return _run_full_diff_suggestion_work_chunk(provider, provider_classifier, works)
    if len(works) <= 1:
        return [_run_single_suggestion_with_failure(provider, provider_classifier, works[0])]

    try:
        outcome = _run_suggestion_batch_model_work(provider, provider_classifier, works)
    except _SuggestionBatchFallback as fallback:
        fallback_results: list[_SuggestionResult] = []
        for work in fallback.works:
            try:
                fallback_results.append(_run_suggestion_model_work(provider, provider_classifier, work))
            except (Exception, SystemExit) as exc:
                raise _SuggestionWorkFailure(work, exc, completed_results=fallback_results) from exc
        if fallback_results:
            fallback_results[0].aggregate_model_ms += fallback.model_ms
            fallback_results[0].model_call_count += fallback.model_call_count
            fallback_results[0].batched_model_calls += fallback.batched_model_calls
            fallback_results[0].bulk_model_calls += fallback.bulk_model_calls
            fallback_results[0].full_diff_prompt_chars += fallback.full_diff_prompt_chars
        return sorted(fallback_results, key=lambda result: result.order)

    results = list(outcome.results)
    if outcome.fallback_works:
        fallback_results = []
        for work in outcome.fallback_works:
            try:
                fallback_results.append(_run_suggestion_model_work(provider, provider_classifier, work))
            except (Exception, SystemExit) as exc:
                raise _SuggestionWorkFailure(work, exc, completed_results=results + fallback_results) from exc
        if not results and fallback_results:
            fallback_results[0].aggregate_model_ms += outcome.model_ms
            fallback_results[0].model_call_count += outcome.model_call_count
            fallback_results[0].batched_model_calls += outcome.batched_model_calls
            fallback_results[0].bulk_model_calls += outcome.bulk_model_calls
        results.extend(fallback_results)
    return sorted(results, key=lambda result: result.order)


def _run_full_diff_suggestion_work_chunk(
    provider,
    provider_classifier: Classifier,
    works: list[_SuggestionWork],
) -> list[_SuggestionResult]:
    try:
        outcome = _run_full_diff_suggestion_batch_model_work(provider, provider_classifier, works)
    except _SuggestionBatchFallback as fallback:
        fallback_results: list[_SuggestionResult] = []
        for work in fallback.works:
            try:
                fallback_results.append(
                    _run_suggestion_model_work(
                        provider,
                        provider_classifier,
                        work,
                        fallback_reason="full-diff-json-unusable",
                    )
                )
            except (Exception, SystemExit) as exc:
                raise _SuggestionWorkFailure(work, exc, completed_results=fallback_results) from exc
        if fallback_results:
            fallback_results[0].aggregate_model_ms += fallback.model_ms
            fallback_results[0].model_call_count += fallback.model_call_count
            fallback_results[0].batched_model_calls += fallback.batched_model_calls
            fallback_results[0].bulk_model_calls += fallback.bulk_model_calls
        return sorted(fallback_results, key=lambda result: result.order)

    results = list(outcome.results)
    if outcome.fallback_works:
        fallback_results = []
        for work in outcome.fallback_works:
            try:
                fallback_results.append(
                    _run_suggestion_model_work(
                        provider,
                        provider_classifier,
                        work,
                        fallback_reason="full-diff-json-item-unusable",
                    )
                )
            except (Exception, SystemExit) as exc:
                raise _SuggestionWorkFailure(work, exc, completed_results=results + fallback_results) from exc
        if not results and fallback_results:
            fallback_results[0].aggregate_model_ms += outcome.model_ms
            fallback_results[0].model_call_count += outcome.model_call_count
            fallback_results[0].batched_model_calls += outcome.batched_model_calls
            fallback_results[0].bulk_model_calls += outcome.bulk_model_calls
            fallback_results[0].full_diff_prompt_chars += outcome.full_diff_prompt_chars
        results.extend(fallback_results)
    return sorted(results, key=lambda result: result.order)


def _run_single_suggestion_with_failure(provider, provider_classifier: Classifier, work: _SuggestionWork) -> _SuggestionResult:
    try:
        return _run_suggestion_model_work(provider, provider_classifier, work)
    except (Exception, SystemExit) as exc:
        raise _SuggestionWorkFailure(work, exc) from exc


def _run_full_diff_suggestion_batch_model_work(
    provider,
    provider_classifier: Classifier,
    works: list[_SuggestionWork],
) -> _SuggestionBatchOutcome:
    provider_name = works[0].effective_model_provider or "configured"
    item_ids = [work.baseline_item.id for work in works]
    _trace_model_call_start("suggest", f"full-diff:{','.join(item_ids)}", provider_name)
    _trace_progress(
        "suggestion_model_request",
        f"Prompting model with full diff for {len(works)} suggestion item(s)",
        current=works[0].item_number,
        total=works[0].total_targets,
        unit="item",
        item_id=",".join(item_ids),
        batch=works[0].batch,
        batch_total=works[0].batch_total,
        batch_item_count=len(works),
        elapsed_ms=max(_elapsed_ms(work.started_at) for work in works),
    )
    prompt = _build_full_diff_suggestion_batch_prompt(works)
    prompt_chars = len(prompt)
    _trace_model_payload("prompt-preview", prompt)
    model_start = perf_counter()
    response = provider.summarize(
        ProviderRequest(
            task="suggest",
            prompt=prompt,
            evidence={
                "batch": True,
                "strategy": "full-diff-json",
                "prompt_shape": FULL_DIFF_SUGGESTION_PROMPT_SHAPE,
                "item_ids": item_ids,
                "item_count": len(works),
                "suggestion_count": works[0].suggestion_count,
                "privacy_mode": works[0].prompt_budget.get("privacy_mode"),
                "full_diff_file_count": sum(work.full_diff_file_count for work in works),
                "full_diff_patch_chars": sum(work.full_diff_patch_chars for work in works),
                "raw_patch_chars_available": sum(
                    int(work.prompt_budget.get("raw_patch_chars_available") or 0) for work in works
                ),
                "raw_patch_chars_sent": sum(work.full_diff_patch_chars for work in works),
                "missing_patch_count": sum(work.full_diff_missing_patch_count for work in works),
            },
            metadata={
                "privacy_mode": works[0].prompt_budget.get("privacy_mode"),
                "request": model_request_record(
                    task="suggest",
                    provider=provider_name,
                    privacy_mode=str(works[0].prompt_budget.get("privacy_mode") or "metadata-only"),
                    prompt=prompt,
                    evidence={"item_ids": item_ids, "item_count": len(works), "strategy": "full-diff-json"},
                    raw_patch_chars_available=sum(
                        int(work.prompt_budget.get("raw_patch_chars_available") or 0) for work in works
                    ),
                    raw_patch_chars_sent=sum(work.full_diff_patch_chars for work in works),
                    omitted_fields=[]
                    if sum(work.full_diff_patch_chars for work in works)
                    else list(OMITTED_PATCH_EVIDENCE),
                    omission_reasons=[]
                    if sum(work.full_diff_patch_chars for work in works)
                    else ["metadata-only mode sends ChangeSummary instead of raw diff text"],
                ),
            },
            response_format=_suggestion_batch_response_format(works[0].suggestion_count),
        )
    )
    model_ms = _elapsed_ms(model_start)
    provider_timings = _normalized_provider_timings(response.metadata, model_ms)
    _trace_model_call_result("suggest", f"full-diff:{','.join(item_ids)}", response.model, response.confidence)
    _trace_model_response_detail(response.model, response.content, response.metadata, response.reasoning)
    _trace_progress(
        "suggestion_model_response",
        f"Reading full-diff response for {len(works)} suggestion item(s)",
        current=works[-1].item_number,
        total=works[-1].total_targets,
        unit="item",
        item_id=",".join(item_ids),
        batch=works[0].batch,
        batch_total=works[0].batch_total,
        elapsed_ms=max(_elapsed_ms(work.started_at) for work in works),
        model_ms=model_ms,
        queue_ms=provider_timings.get("queue_ms"),
        ollama_total_ms=provider_timings.get("ollama_total_ms"),
        prompt_eval_ms=provider_timings.get("prompt_eval_ms"),
        eval_ms=provider_timings.get("eval_ms"),
    )
    if provider_classifier._is_provider_error(response.content):
        raise _SuggestionBatchFallback(
            works,
            model_ms=model_ms,
            model_call_count=1,
            batched_model_calls=1,
            bulk_model_calls=1,
            full_diff_prompt_chars=prompt_chars,
        )

    parsed_items = _parse_suggestion_batch_json(response.content)
    if parsed_items is None:
        raise _SuggestionBatchFallback(
            works,
            model_ms=model_ms,
            model_call_count=1,
            batched_model_calls=1,
            bulk_model_calls=1,
            full_diff_prompt_chars=prompt_chars,
        )

    provider_reasoning = response.reasoning if _trace_model_reasoning_payloads_enabled() else ""
    results: list[_SuggestionResult] = []
    fallback_works: list[_SuggestionWork] = []
    for work in works:
        raw_suggestions = parsed_items.get(work.baseline_item.id)
        if not raw_suggestions:
            fallback_works.append(work)
            continue
        parse_start = perf_counter()
        suggestion_details, rejected_candidates = _parse_suggestion_variants(
            "\n".join(raw_suggestions),
            work.suggestion_count,
            work.baseline_item,
            work.group,
            trace_title=work.baseline_item.content.title,
            trace_model=response.model,
            banned_phrases=list(work.suggestion_style["banned_phrases"]),
            specificity_floor=float(work.suggestion_style["specificity_floor"]),
        )
        parse_ms = _elapsed_ms(parse_start)
        _trace_model_suggestion_decision(
            title=work.baseline_item.content.title,
            model=response.model,
            parsed_count=len(suggestion_details),
            requested_count=work.suggestion_count,
            accepted=_suggestions_preview(suggestion_details),
            rejected_count=len(rejected_candidates),
        )
        if not suggestion_details:
            fallback_works.append(work)
            continue
        item = _build_suggestion_result_item(
            work=work,
            response=response,
            suggestion_details=suggestion_details,
            rejected_candidates=rejected_candidates,
            model_ms=model_ms,
            parse_ms=parse_ms,
            provider_timings=provider_timings,
            provider_reasoning=provider_reasoning,
            batch_model_ms=model_ms,
            batch_item_count=len(works),
        )
        timings = item.get("timings")
        if isinstance(timings, dict):
            timings["batch_prompt_chars"] = prompt_chars
        item["full_diff"] = {
            "prompt_shape": FULL_DIFF_SUGGESTION_PROMPT_SHAPE,
            "prompt_chars": prompt_chars,
            "file_count": work.full_diff_file_count,
            "patch_chars": work.full_diff_patch_chars,
            "missing_patch_count": work.full_diff_missing_patch_count,
            "single_item_over_batch_guard": len(works) == 1 and prompt_chars > FULL_DIFF_BATCH_PROMPT_CHAR_GUARD,
        }
        results.append(_SuggestionResult(order=work.order, item=item, model_ms=model_ms, parse_ms=parse_ms))

    if not results:
        return _SuggestionBatchOutcome(
            results=[],
            fallback_works=fallback_works or works,
            model_ms=model_ms,
            model_call_count=1,
            batched_model_calls=1,
            bulk_model_calls=1,
            full_diff_prompt_chars=prompt_chars,
        )
    results[0].aggregate_model_ms = model_ms
    results[0].model_call_count = 1
    results[0].batched_model_calls = 1
    results[0].bulk_model_calls = 1
    results[0].full_diff_prompt_chars = prompt_chars
    return _SuggestionBatchOutcome(
        results=results,
        fallback_works=fallback_works,
        model_ms=model_ms,
        model_call_count=1,
        batched_model_calls=1,
        bulk_model_calls=1,
        full_diff_prompt_chars=prompt_chars,
    )


def _run_suggestion_batch_model_work(
    provider,
    provider_classifier: Classifier,
    works: list[_SuggestionWork],
) -> _SuggestionBatchOutcome:
    provider_name = works[0].effective_model_provider or "configured"
    item_ids = [work.baseline_item.id for work in works]
    _trace_model_call_start("suggest", f"batch:{','.join(item_ids)}", provider_name)
    _trace_progress(
        "suggestion_model_request",
        f"Prompting model for {len(works)} suggestion items",
        current=works[0].item_number,
        total=works[0].total_targets,
        unit="item",
        item_id=",".join(item_ids),
        batch=works[0].batch,
        batch_total=works[0].batch_total,
        batch_item_count=len(works),
        elapsed_ms=max(_elapsed_ms(work.started_at) for work in works),
    )
    prompt = _build_suggestion_batch_prompt(works)
    _trace_model_payload("prompt-preview", prompt)
    model_start = perf_counter()
    response = provider.summarize(
        ProviderRequest(
            task="suggest",
            prompt=prompt,
            evidence={
                "batch": True,
                "item_ids": item_ids,
                "item_count": len(works),
                "suggestion_count": works[0].suggestion_count,
                "privacy_mode": works[0].prompt_budget.get("privacy_mode"),
                "raw_patch_chars_available": sum(
                    int(work.prompt_budget.get("raw_patch_chars_available") or 0) for work in works
                ),
                "raw_patch_chars_sent": sum(work.full_diff_patch_chars for work in works),
            },
            metadata={
                "privacy_mode": works[0].prompt_budget.get("privacy_mode"),
                "request": model_request_record(
                    task="suggest",
                    provider=provider_name,
                    privacy_mode=str(works[0].prompt_budget.get("privacy_mode") or "metadata-only"),
                    prompt=prompt,
                    evidence={"item_ids": item_ids, "item_count": len(works), "strategy": works[0].strategy},
                    raw_patch_chars_available=sum(
                        int(work.prompt_budget.get("raw_patch_chars_available") or 0) for work in works
                    ),
                    raw_patch_chars_sent=sum(work.full_diff_patch_chars for work in works),
                    omitted_fields=[]
                    if sum(work.full_diff_patch_chars for work in works)
                    else list(OMITTED_PATCH_EVIDENCE),
                    omission_reasons=[]
                    if sum(work.full_diff_patch_chars for work in works)
                    else ["metadata-only mode sends ChangeSummary instead of raw diff text"],
                ),
            },
            response_format=_suggestion_batch_response_format(works[0].suggestion_count),
        )
    )
    model_ms = _elapsed_ms(model_start)
    provider_timings = _normalized_provider_timings(response.metadata, model_ms)
    _trace_model_call_result("suggest", f"batch:{','.join(item_ids)}", response.model, response.confidence)
    _trace_model_response_detail(response.model, response.content, response.metadata, response.reasoning)
    _trace_progress(
        "suggestion_model_response",
        f"Reading model response for {len(works)} suggestion items",
        current=works[-1].item_number,
        total=works[-1].total_targets,
        unit="item",
        item_id=",".join(item_ids),
        batch=works[0].batch,
        batch_total=works[0].batch_total,
        elapsed_ms=max(_elapsed_ms(work.started_at) for work in works),
        model_ms=model_ms,
        queue_ms=provider_timings.get("queue_ms"),
        ollama_total_ms=provider_timings.get("ollama_total_ms"),
        prompt_eval_ms=provider_timings.get("prompt_eval_ms"),
        eval_ms=provider_timings.get("eval_ms"),
    )
    if provider_classifier._is_provider_error(response.content):
        raise _SuggestionBatchFallback(
            works,
            model_ms=model_ms,
            model_call_count=1,
            batched_model_calls=1,
        )

    parsed_items = _parse_suggestion_batch_json(response.content)
    if parsed_items is None:
        raise _SuggestionBatchFallback(
            works,
            model_ms=model_ms,
            model_call_count=1,
            batched_model_calls=1,
        )

    provider_reasoning = response.reasoning if _trace_model_reasoning_payloads_enabled() else ""
    results: list[_SuggestionResult] = []
    fallback_works: list[_SuggestionWork] = []
    for work in works:
        raw_suggestions = parsed_items.get(work.baseline_item.id)
        if not raw_suggestions:
            fallback_works.append(work)
            continue
        parse_start = perf_counter()
        suggestion_details, rejected_candidates = _parse_suggestion_variants(
            "\n".join(raw_suggestions),
            work.suggestion_count,
            work.baseline_item,
            work.group,
            trace_title=work.baseline_item.content.title,
            trace_model=response.model,
            banned_phrases=list(work.suggestion_style["banned_phrases"]),
            specificity_floor=float(work.suggestion_style["specificity_floor"]),
        )
        parse_ms = _elapsed_ms(parse_start)
        _trace_model_suggestion_decision(
            title=work.baseline_item.content.title,
            model=response.model,
            parsed_count=len(suggestion_details),
            requested_count=work.suggestion_count,
            accepted=_suggestions_preview(suggestion_details),
            rejected_count=len(rejected_candidates),
        )
        if not suggestion_details:
            fallback_works.append(work)
            continue
        item = _build_suggestion_result_item(
            work=work,
            response=response,
            suggestion_details=suggestion_details,
            rejected_candidates=rejected_candidates,
            model_ms=model_ms,
            parse_ms=parse_ms,
            provider_timings=provider_timings,
            provider_reasoning=provider_reasoning,
            batch_model_ms=model_ms,
            batch_item_count=len(works),
        )
        results.append(_SuggestionResult(order=work.order, item=item, model_ms=model_ms, parse_ms=parse_ms))

    if not results:
        return _SuggestionBatchOutcome(
            results=[],
            fallback_works=fallback_works or works,
            model_ms=model_ms,
            model_call_count=1,
            batched_model_calls=1,
        )
    results[0].aggregate_model_ms = model_ms
    results[0].model_call_count = 1
    results[0].batched_model_calls = 1
    return _SuggestionBatchOutcome(
        results=results,
        fallback_works=fallback_works,
        model_ms=model_ms,
        model_call_count=1,
        batched_model_calls=1,
    )


def _build_suggestion_batch_prompt(works: list[_SuggestionWork]) -> str:
    payload = {
        "suggestion_count": works[0].suggestion_count,
        "tone": works[0].suggestion_tone,
        "style_profile": works[0].suggestion_style,
        "items": [
            {
                "item_id": work.baseline_item.id,
                "title": work.baseline_item.content.title,
                "current_summary": work.baseline_item.edited_summary or work.baseline_item.content.summary,
                "prepared_prompt": work.prompt,
            }
            for work in works
        ],
    }
    return (
        "Generate release-note suggestion variants for each item independently.\n"
        "Return only JSON that matches the requested schema. Do not include Markdown fences or prose.\n"
        f"For each item, return exactly {works[0].suggestion_count} suggestion string(s) in the suggestions array.\n"
        "Do not combine items, rename item ids, or omit an item.\n"
        "Input JSON:\n"
        + json.dumps(payload, indent=2, sort_keys=True)
    )


def _build_full_diff_suggestion_batch_prompt(works: list[_SuggestionWork]) -> str:
    payload = {
        "suggestion_count": works[0].suggestion_count,
        "tone": works[0].suggestion_tone,
        "style_profile": works[0].suggestion_style,
        "prompt_shape": FULL_DIFF_SUGGESTION_PROMPT_SHAPE,
        "items": [work.full_diff_item for work in works if work.full_diff_item is not None],
    }
    raw_patch_chars_sent = sum(work.full_diff_patch_chars for work in works)
    semantic = any(
        (work.full_diff_item or {}).get("evidence_source") == "intentumdiff-semantic-diff" for work in works
    )
    if semantic:
        evidence_name = "IntentumDiff semantic diff"
        source_instruction = (
            "Use each file's semantic_diff and the item's semantic_review as the primary source of truth. "
            "They list meaningful changes with their enclosing symbols, behavior-preserving refactorings, "
            "moved code and ignored formatting. Describe the meaningful changes; never present moved code, "
            "refactorings or formatting as new behavior. Do not infer behavior from titles alone."
        )
    elif raw_patch_chars_sent:
        evidence_name = "full diff evidence"
        source_instruction = (
            "Use the file patches as the primary source of truth. Do not infer behavior from titles alone."
        )
    else:
        evidence_name = "privacy-filtered ChangeSummary evidence"
        source_instruction = (
            "Use the change_summary, commit metadata, file paths/statuses, and deterministic facts. "
            "Do not invent line-level behavior that is not present in the sanitized evidence."
        )
    return (
        f"Generate release-note suggestion variants from the {evidence_name}.\n"
        f"{source_instruction}\n"
        "Return only JSON that matches the requested schema. Do not include Markdown fences or prose.\n"
        f"For each item, return exactly {works[0].suggestion_count} suggestion string(s) in the suggestions array.\n"
        "Do not combine items, rename item ids, omit an item, or mention implementation details unless they matter to "
        "release-note readers.\n"
        "If a file has no semantic_diff (and patch_available=false), treat that file as changed but do not invent "
        "unavailable details.\n"
        "Input JSON:\n"
        + json.dumps(payload, indent=2, sort_keys=True)
    )


def _build_tool_retrieval_suggestion_prompt(
    *,
    tool_provider: SuggestionEvidenceToolProvider,
    item: ChangeItem,
    suggestion_count: int,
    tone: str,
    style_profile: dict[str, Any],
) -> str:
    manifest = tool_provider.get_suggestion_item(item_id=item.id)
    payload = {
        "suggestion_count": suggestion_count,
        "tone": tone,
        "style_profile": style_profile,
        "prompt_shape": TOOL_RETRIEVAL_SUGGESTION_PROMPT_SHAPE,
        "item": manifest,
    }
    if manifest.get("evidence_source") == "intentumdiff-semantic-diff":
        evidence_instruction = (
            "Start from the compact item manifest and its semantic_review, then call "
            "verlato_get_file_semantic_diff for the files you need (or verlato_search_semantic_diff for a "
            "symbol). The IntentumDiff semantic diff is the primary source of truth: describe meaningful "
            "changes, never present moved code, refactorings or formatting as new behavior, and do not infer "
            "behavior from titles alone.\n"
        )
    else:
        evidence_instruction = (
            "Start from the compact item manifest, then call tools to inspect the specific patches needed. "
            "Prefer targeted file patches over reading every file. Use patches as the primary source of truth, "
            "and do not infer behavior from titles alone.\n"
        )
    return (
        "Generate release-note suggestion variants for this item using the available Verlato tools.\n"
        + evidence_instruction
        + "Return only the suggestion text. Do not include JSON, Markdown fences, tool names, or commentary.\n"
        f"Return exactly {suggestion_count} distinct complete release-note option(s), one per line.\n"
        "Compact input JSON:\n"
        + json.dumps(payload, indent=2, sort_keys=True)
    )


def _full_diff_batch_prompt_chars(works: list[_SuggestionWork]) -> int:
    return len(_build_full_diff_suggestion_batch_prompt(works))


def _suggestion_batch_response_format(suggestion_count: int) -> dict[str, Any]:
    return {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "item_id": {"type": "string"},
                        "suggestions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "minItems": 1,
                            "maxItems": suggestion_count,
                        },
                    },
                    "required": ["item_id", "suggestions"],
                },
            }
        },
        "required": ["items"],
    }


def _parse_suggestion_batch_json(content: str) -> dict[str, list[str]] | None:
    try:
        parsed = json.loads(content)
    except json.JSONDecodeError:
        return None
    if not isinstance(parsed, dict):
        return None
    raw_items = parsed.get("items")
    if not isinstance(raw_items, list):
        return None
    suggestions_by_item: dict[str, list[str]] = {}
    for raw_item in raw_items:
        if not isinstance(raw_item, dict):
            continue
        item_id = raw_item.get("item_id")
        raw_suggestions = raw_item.get("suggestions")
        if not isinstance(item_id, str) or not isinstance(raw_suggestions, list):
            continue
        suggestions = [suggestion.strip() for suggestion in raw_suggestions if isinstance(suggestion, str) and suggestion.strip()]
        if suggestions:
            suggestions_by_item[item_id] = suggestions
    return suggestions_by_item

def _build_suggestion_result_item(
    *,
    work: _SuggestionWork,
    response: ProviderResponse,
    suggestion_details: list[dict[str, Any]],
    rejected_candidates: list[dict[str, Any]],
    model_ms: int,
    parse_ms: int,
    provider_timings: dict[str, Any],
    provider_reasoning: str,
    batch_model_ms: int | None = None,
    batch_item_count: int | None = None,
    fallback_reason: str | None = None,
) -> dict[str, Any]:
    timings: dict[str, Any] = {
        "evidence_ms": work.evidence_ms,
        "prompt_ms": work.prompt_ms,
        "model_ms": model_ms,
        "parse_ms": parse_ms,
        "total_ms": _elapsed_ms(work.started_at),
        **provider_timings,
    }
    if batch_model_ms is not None:
        timings["batch_model_ms"] = batch_model_ms
    if batch_item_count is not None:
        timings["batch_item_count"] = batch_item_count
    timings["strategy"] = work.strategy
    timings["file_count"] = work.full_diff_file_count
    timings["patch_chars"] = work.full_diff_patch_chars
    timings["missing_patch_count"] = work.full_diff_missing_patch_count
    if fallback_reason:
        timings["fallback_used_capped_evidence"] = True
        timings["fallback_reason"] = fallback_reason
    request_record = work.evidence.get("request") if isinstance(work.evidence.get("request"), dict) else {}
    response_record = {
        "model": response.model,
        "response_chars": len(response.content or ""),
        "parse_result": "accepted" if suggestion_details else "rejected",
        "accepted_count": len(suggestion_details),
        "rejected_count": len(rejected_candidates),
    }
    fallback = (
        {
            "from_strategy": work.strategy,
            "reason": fallback_reason,
            "used_capped_evidence": True,
        }
        if fallback_reason
        else None
    )
    return {
        "item_id": work.baseline_item.id,
        "title": work.baseline_item.content.title,
        "current_summary": work.baseline_item.edited_summary or work.baseline_item.content.summary,
        "provider": response.model,
        "prompt_sha": work.prompt_sha,
        "tone": work.suggestion_tone,
        "style_profile": work.suggestion_style,
        "provider_metadata": response.metadata,
        "provider_reasoning": provider_reasoning,
        "source_evidence": work.source_evidence,
        "prompt_budget": work.prompt_budget,
        "privacy": privacy_summary(
            provider=work.effective_model_provider or "configured",
            privacy_mode=str(work.prompt_budget.get("privacy_mode") or "metadata-only"),
            raw_patch_chars_available=int(work.prompt_budget.get("raw_patch_chars_available") or 0),
            raw_patch_chars_sent=int(work.prompt_budget.get("raw_patch_chars_sent") or 0),
        ),
        "ai_request": request_record,
        "ai_response": response_record,
        "suggestion_details": suggestion_details,
        "rejected_candidates": rejected_candidates,
        "suggestions": [detail["summary"] for detail in suggestion_details],
        "cache": {
            "hit": False,
            "key": work.cache_key,
        },
        "timings": timings,
        **({"fallback": fallback} if fallback is not None else {}),
        "batch": work.batch,
    }


def _run_suggestion_model_work(
    provider,
    provider_classifier: Classifier,
    work: _SuggestionWork,
    *,
    fallback_reason: str | None = None,
) -> _SuggestionResult:
    baseline_item = work.baseline_item
    provider_name = work.effective_model_provider or "configured"
    _trace_model_call_start("suggest", baseline_item.content.title, provider_name)
    _trace_progress(
        "suggestion_model_request",
        f"Prompting model for {baseline_item.id}",
        current=work.item_number,
        total=work.total_targets,
        unit="item",
        item_id=baseline_item.id,
        batch=work.batch,
        batch_total=work.batch_total,
        elapsed_ms=_elapsed_ms(work.started_at),
    )
    model_start = perf_counter()
    response = provider.summarize(
        ProviderRequest(
            task="suggest",
            prompt=work.prompt,
            evidence=work.evidence,
            metadata={
                "privacy_mode": work.prompt_budget.get("privacy_mode"),
                "request": work.evidence.get("request"),
            },
            tools=work.tools,
            tool_executor=work.tool_executor,
            max_tool_rounds=work.max_tool_rounds,
        )
    )
    model_ms = _elapsed_ms(model_start)
    provider_timings = _normalized_provider_timings(response.metadata, model_ms)
    _trace_model_call_result("suggest", baseline_item.content.title, response.model, response.confidence)
    _trace_model_response_detail(response.model, response.content, response.metadata, response.reasoning)
    _trace_progress(
        "suggestion_model_response",
        f"Reading model response for {baseline_item.id}",
        current=work.item_number,
        total=work.total_targets,
        unit="item",
        item_id=baseline_item.id,
        batch=work.batch,
        batch_total=work.batch_total,
        elapsed_ms=_elapsed_ms(work.started_at),
        model_ms=model_ms,
        queue_ms=provider_timings.get("queue_ms"),
        ollama_total_ms=provider_timings.get("ollama_total_ms"),
        prompt_eval_ms=provider_timings.get("prompt_eval_ms"),
        eval_ms=provider_timings.get("eval_ms"),
    )
    provider_reasoning = response.reasoning if _trace_model_reasoning_payloads_enabled() else ""
    if provider_classifier._is_provider_error(response.content):
        raise SystemExit(
            f"No model suggestions were produced for draft item {baseline_item.id}: {response.content}"
        )
    parse_start = perf_counter()
    suggestion_details, rejected_candidates = _parse_suggestion_variants(
        response.content,
        work.suggestion_count,
        baseline_item,
        work.group,
        trace_title=baseline_item.content.title,
        trace_model=response.model,
        banned_phrases=list(work.suggestion_style["banned_phrases"]),
        specificity_floor=float(work.suggestion_style["specificity_floor"]),
    )
    parse_ms = _elapsed_ms(parse_start)
    _trace_model_suggestion_decision(
        title=baseline_item.content.title,
        model=response.model,
        parsed_count=len(suggestion_details),
        requested_count=work.suggestion_count,
        accepted=_suggestions_preview(suggestion_details),
        rejected_count=len(rejected_candidates),
    )
    if not suggestion_details:
        raise SystemExit(f"No model suggestions were produced for draft item {baseline_item.id}.")
    suggestions = [detail["summary"] for detail in suggestion_details]
    timings: dict[str, Any] = {
        "evidence_ms": work.evidence_ms,
        "prompt_ms": work.prompt_ms,
        "model_ms": model_ms,
        "parse_ms": parse_ms,
        "total_ms": _elapsed_ms(work.started_at),
        "strategy": work.strategy,
        "file_count": work.full_diff_file_count,
        "patch_chars": work.full_diff_patch_chars,
        "missing_patch_count": work.full_diff_missing_patch_count,
        **provider_timings,
    }
    if fallback_reason:
        timings["fallback_used_capped_evidence"] = True
        timings["fallback_reason"] = fallback_reason
    item = {
        "item_id": baseline_item.id,
        "title": baseline_item.content.title,
        "current_summary": baseline_item.edited_summary or baseline_item.content.summary,
        "provider": response.model,
        "prompt_sha": work.prompt_sha,
        "tone": work.suggestion_tone,
        "style_profile": work.suggestion_style,
        "provider_metadata": response.metadata,
        "provider_reasoning": provider_reasoning,
        "source_evidence": work.source_evidence,
        "prompt_budget": work.prompt_budget,
        "privacy": privacy_summary(
            provider=work.effective_model_provider or "configured",
            privacy_mode=str(work.prompt_budget.get("privacy_mode") or "metadata-only"),
            raw_patch_chars_available=int(work.prompt_budget.get("raw_patch_chars_available") or 0),
            raw_patch_chars_sent=int(work.prompt_budget.get("raw_patch_chars_sent") or 0),
        ),
        "ai_request": work.evidence.get("request") if isinstance(work.evidence.get("request"), dict) else {},
        "ai_response": {
            "model": response.model,
            "response_chars": len(response.content or ""),
            "parse_result": "accepted" if suggestion_details else "rejected",
            "accepted_count": len(suggestion_details),
            "rejected_count": len(rejected_candidates),
        },
        "suggestion_details": suggestion_details,
        "rejected_candidates": rejected_candidates,
        "suggestions": suggestions,
        "cache": {
            "hit": False,
            "key": work.cache_key,
        },
        "timings": timings,
        **(
            {
                "fallback": {
                    "from_strategy": work.strategy,
                    "reason": fallback_reason,
                    "used_capped_evidence": True,
                }
            }
            if fallback_reason
            else {}
        ),
        "batch": work.batch,
    }
    return _SuggestionResult(
        order=work.order,
        item=item,
        model_ms=model_ms,
        parse_ms=parse_ms,
        aggregate_model_ms=model_ms,
        model_call_count=1,
        single_model_calls=1,
        fallback_single_calls=1 if fallback_reason else 0,
        fallback_item_count=1 if fallback_reason else 0,
    )


def _parse_suggestion_variants(
    content: str,
    count: int,
    item: ChangeItem,
    group: ChangeGroup,
    *,
    trace_title: str | None = None,
    trace_model: str | None = None,
    banned_phrases: list[str] | None = None,
    specificity_floor: float = 0.0,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    normalized = (content or "").strip()
    if not normalized:
        _trace_model_suggestion_filter("raw-response", "empty", trace_title, trace_model)
        return [], []
    lines = []
    for raw in normalized.splitlines():
        line = raw.strip()
        if not line:
            continue
        line = re.sub(r"^\s*(?:[-*]|\d+[\.)])\s*", "", line).strip()
        line = line.strip("\"'")
        if line:
            lines.append(line)
    if not lines:
        lines = [normalized]
    _trace_model_suggestion_filter(
        "parse",
        f"raw_lines={len(normalized.splitlines())} candidates={len(lines)} requested={count}",
        trace_title,
        trace_model,
    )
    current_summary = item.edited_summary or item.content.summary
    suggestions: list[dict[str, Any]] = []
    rejected_candidates: list[dict[str, Any]] = []
    classifier = Classifier()
    for line in lines:
        rejection_reason = classifier._summary_rejection_reason(line, item.content.title, group)
        banned_phrase = _matched_banned_phrase(line, banned_phrases or [])
        if not rejection_reason and banned_phrase:
            rejection_reason = f"banned-phrase:{banned_phrase}"
        similarity = _summary_similarity(line, current_summary)
        specificity = _summary_specificity(line, group)
        if not rejection_reason and similarity >= 0.82:
            rejection_reason = "too-similar-to-current"
        if not rejection_reason and specificity < specificity_floor:
            rejection_reason = "below-specificity-floor"
        if rejection_reason:
            rejected_candidates.append(
                {
                    "summary": line,
                    "reason": rejection_reason,
                    "similarity_to_current": similarity,
                    "specificity_score": specificity,
                }
            )
            _trace_model_suggestion_filter(
                "reject",
                f"reason={rejection_reason} similarity_to_current={similarity:.2f} specificity={specificity:.2f} candidate={line}",
                trace_title,
                trace_model,
            )
            continue
        if any(detail["summary"] == line for detail in suggestions):
            rejected_candidates.append(
                {
                    "summary": line,
                    "reason": "duplicate",
                    "similarity_to_current": similarity,
                    "specificity_score": specificity,
                }
            )
            _trace_model_suggestion_filter(
                "reject",
                f"reason=duplicate similarity_to_current={similarity:.2f} specificity={specificity:.2f} candidate={line}",
                trace_title,
                trace_model,
            )
            continue
        suggestions.append(
            {
                "summary": line,
                "similarity_to_current": similarity,
                "specificity_score": specificity,
                "comparison": {
                    "current": current_summary,
                    "suggested": line,
                },
            }
        )
        _trace_model_suggestion_filter(
            "accept",
            f"index={len(suggestions)} similarity_to_current={similarity:.2f} specificity={specificity:.2f} candidate={line}",
            trace_title,
            trace_model,
        )
        if len(suggestions) >= count:
            break
    return suggestions, rejected_candidates


def _summary_similarity(candidate: str, current: str) -> float:
    candidate_tokens = _summary_tokens(candidate)
    current_tokens = _summary_tokens(current)
    if not candidate_tokens or not current_tokens:
        return 0.0
    if " ".join(candidate_tokens) == " ".join(current_tokens):
        return 1.0
    intersection = len(set(candidate_tokens) & set(current_tokens))
    union = len(set(candidate_tokens) | set(current_tokens))
    return intersection / union if union else 0.0


def _summary_tokens(value: str) -> list[str]:
    stopwords = {
        "a",
        "an",
        "and",
        "for",
        "in",
        "of",
        "on",
        "the",
        "to",
        "with",
    }
    return [token for token in re.findall(r"[a-z0-9]+", value.lower()) if token not in stopwords]


def _matched_banned_phrase(candidate: str, banned_phrases: list[str]) -> str | None:
    lowered = candidate.lower()
    return next((phrase for phrase in banned_phrases if phrase and phrase.lower() in lowered), None)


def _summary_specificity(candidate: str, group: ChangeGroup) -> float:
    tokens = set(_summary_tokens(candidate))
    if not tokens:
        return 0.0
    evidence_text = " ".join(
        [
            group.pr.title if group.pr else "",
            group.pr.body if group.pr else "",
            " ".join(group.pr.labels if group.pr else []),
            " ".join(commit.message for commit in group.commits),
            " ".join(file.path for file in changed_files_for_group(group)),
        ]
    )
    evidence_tokens = set(_summary_tokens(evidence_text))
    overlap = len(tokens & evidence_tokens)
    length_score = min(len(tokens) / 12, 1.0) * 0.55
    overlap_score = min(overlap / 4, 1.0) * 0.45
    return round(length_score + overlap_score, 4)


def _build_full_diff_suggestion_payload_item(
    group: ChangeGroup,
    item: ChangeItem,
    diff,
    *,
    include_patches: bool,
) -> dict[str, Any]:
    files = changed_files_for_group(group)
    file_payloads: list[dict[str, Any]] = []
    patch_chars_available = 0
    patch_chars_sent = 0
    missing_patch_count = 0
    use_semantic_diff = getattr(diff, "semantic", None) is not None
    if use_semantic_diff:
        # IntentumDiff evidence replaces raw patches; `include_patches` (privacy allows source
        # content) only decides whether the engine's change descriptions are included.
        semantic_by_path: dict[str, list[dict[str, Any]]] = {}
        for record in semantic_diff_for_prompt(list(diff.semantic_diff), detailed=include_patches):
            semantic_by_path.setdefault(str(record["path"]), []).append(
                {key: value for key, value in record.items() if key != "path"}
            )
    for changed_file in files:
        patch = changed_file.patch or ""
        patch_chars_available += len(patch)
        if use_semantic_diff:
            records = semantic_by_path.get(changed_file.path, [])
            file_payloads.append(
                {
                    "path": changed_file.path,
                    "status": changed_file.status,
                    "semantic_diff": records,
                    "semantic_diff_available": bool(records),
                    "patch_chars_available": len(patch),
                    "patch_chars_sent": 0,
                    "patch_available": False,
                }
            )
            if not records:
                missing_patch_count += 1
            continue
        if not patch:
            missing_patch_count += 1
        file_payload = {
            "path": changed_file.path,
            "status": changed_file.status,
            "patch_chars_available": len(patch),
            "patch_chars_sent": len(patch) if include_patches else 0,
            "patch_available": bool(patch),
            "patch_unavailable_reason": "" if patch else "missing-or-unavailable",
        }
        if include_patches:
            file_payload["patch"] = patch
            patch_chars_sent += len(patch)
        file_payloads.append(file_payload)
    change_summary = build_change_summary_payload(group=group, item=item, diff=diff)
    return {
        "item_id": item.id,
        "title": item.content.title,
        "current_summary": item.edited_summary or item.content.summary,
        "change_summary": change_summary,
        "classification": {
            "kind": item.classification.kind,
            "area": item.classification.area,
            "audience": item.classification.audience,
            "semver": item.classification.semver,
        },
        "labels": list(group.pr.labels if group.pr else []),
        "pr": (
            {
                "number": group.pr.number,
                "title": group.pr.title,
                "body": group.pr.body,
                "base_ref": group.pr.base_ref,
                "head_ref": group.pr.head_ref,
            }
            if group.pr
            else None
        ),
        "commit_boundaries": _commit_boundaries_payload(group.commits),
        "commits": [_commit_payload_for_prompt(commit) for commit in group.commits],
        "files": file_payloads,
        "file_count": len(file_payloads),
        "patch_chars_available": patch_chars_available,
        "patch_chars_sent": patch_chars_sent,
        "patch_chars": patch_chars_sent,
        "missing_patch_count": missing_patch_count,
        "evidence_source": "intentumdiff-semantic-diff" if use_semantic_diff else "git-patch",
        "semantic_diff_detail": ("detailed" if include_patches else "structure") if use_semantic_diff else None,
        "semantic_review": semantic_prompt_payload(diff.semantic) if use_semantic_diff else None,
        "omitted_fields": [] if include_patches and not use_semantic_diff else list(OMITTED_PATCH_EVIDENCE),
        "diff_summary": {
            "summary": diff.summary,
            "files_by_area": diff.files_by_area,
            "file_statuses": diff.file_statuses,
            "api_changes": diff.api_changes,
            "migration_changes": diff.migration_changes,
            "config_changes": diff.config_changes,
        },
    }


def _commit_payload_for_prompt(commit: Commit) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "sha": commit.sha,
        "subject": commit.message.splitlines()[0] if commit.message else "",
        "message": commit.message,
        "author": commit.author,
        "previous_sha": commit.parent_sha,
        "initial_commit": commit.parent_sha is None,
    }
    if commit.inferred_intent:
        payload["inferred_conventional"] = commit_intent_payload(commit.inferred_intent)
    if commit.derived_intent:
        payload["derived_conventional"] = commit_intent_payload(commit.derived_intent)
    return payload


def _commit_boundaries_payload(commits: list[Commit]) -> dict[str, Any]:
    if not commits:
        return {"first": None, "last": None}
    return {
        "first": _commit_boundary_payload(commits[0]),
        "last": _commit_boundary_payload(commits[-1]),
    }


def _commit_boundary_payload(commit: Commit) -> dict[str, Any]:
    return {
        "sha": commit.sha,
        "subject": commit.message.splitlines()[0] if commit.message else "",
        "previous_sha": commit.parent_sha,
        "initial_commit": commit.parent_sha is None,
        "derived_conventional": conventional_commit_message(commit.derived_intent) if commit.derived_intent else None,
    }


def _build_suggestion_source_evidence(diff, group: ChangeGroup, *, include_hunks: bool = False) -> list[dict[str, str]]:
    evidence: list[dict[str, str]] = []
    if group.pr:
        evidence.append(
            {
                "kind": "pull-request",
                "label": f"PR #{group.pr.number}",
                "detail": group.pr.title,
            }
        )
    for commit in group.commits[:3]:
        evidence.append(
            {
                "kind": "commit",
                "label": commit.sha[:7],
                "detail": commit.message.splitlines()[0][:180],
            }
        )
    for path, status in list(sorted(diff.file_statuses.items()))[:6]:
        evidence.append(
            {
                "kind": "file",
                "label": path,
                "detail": status,
            }
        )
    if getattr(diff, "semantic", None) is not None:
        for record in semantic_diff_for_prompt(list(diff.semantic_diff), detailed=include_hunks)[:3]:
            excerpt = render_semantic_diff([record], detailed=include_hunks).replace("\n", " ")
            evidence.append(
                {
                    "kind": "semantic-diff",
                    "label": str(record["path"]),
                    "detail": " ".join(excerpt.split())[:240],
                }
            )
    elif include_hunks:
        for path, hunks in list(diff.representative_hunks.items())[:3]:
            excerpt = " ".join(hunk.replace("\n", " ") for hunk in hunks)
            evidence.append(
                {
                    "kind": "diff",
                    "label": path,
                    "detail": excerpt[:240],
                }
            )
    if diff.summary:
        evidence.append(
            {
                "kind": "summary",
                "label": "diff summary",
                "detail": diff.summary[:300],
            }
        )
    return evidence[:12]


def _trace_model_enabled() -> bool:
    return os.getenv("VERLATO_TRACE_MODEL_CALLS") == "1"


def _trace_model_reasoning_payloads_enabled() -> bool:
    return os.getenv("VERLATO_TRACE_MODEL_REASONING") == "1"


def _trace_model_call_start(task: str, title: str, provider: str) -> None:
    if not _trace_model_enabled():
        return
    _trace_model_line(f"starting {task} provider={provider} title={title}", event="request_started")


def _trace_model_suggest_plan(
    *,
    base: str,
    head: str,
    target_count: int,
    suggestion_count: int,
    suggestion_count_source: str,
    provider: str,
    parallelism: int,
    all_visible: bool,
) -> None:
    if not _trace_model_enabled():
        return
    scope = "all-visible" if all_visible else "single-item"
    _trace_model_line(
        f"[verlato:model] planning suggest provider={provider} scope={scope} base={base} head={head} "
        f"targets={target_count} suggestions_per_target={suggestion_count} "
        f"parallelism={parallelism} count_source={suggestion_count_source}",
        event="suggestion_plan",
        attributes={
            "provider": provider,
            "scope": scope,
            "base": base,
            "head": head,
            "targets": target_count,
            "suggestions_per_target": suggestion_count,
            "parallelism": parallelism,
            "count_source": suggestion_count_source,
        },
    )


def _trace_model_call_result(task: str, title: str, model: str, confidence: float) -> None:
    if not _trace_model_enabled():
        return
    _trace_model_line(
        f"[verlato:model] finished {task} model={model} confidence={confidence:.2f} title={title}",
        event="request_finished",
        attributes={"task": task, "model": model, "confidence": confidence, "title": title},
    )


def _trace_model_response_detail(model: str, content: str, metadata: dict[str, Any], reasoning: str = "") -> None:
    if not _trace_model_enabled():
        return
    metadata_text = " ".join(f"{key}={value}" for key, value in sorted(metadata.items()))
    metadata_suffix = f" {metadata_text}" if metadata_text else ""
    reasoning_status = "true" if reasoning else "false"
    _trace_model_line(
        f"[verlato:model] response-detail model={model} content_chars={len(content)} "
        f"reasoning_exposed={reasoning_status} reasoning_chars={len(reasoning)}{metadata_suffix}",
        event="response_detail",
        attributes={
            "model": model,
            "content_chars": len(content),
            "reasoning_exposed": bool(reasoning),
            "reasoning_chars": len(reasoning),
            **metadata,
        },
    )
    _trace_model_payload("response-preview", content)
    if _trace_model_reasoning_payloads_enabled():
        _trace_model_payload("reasoning-preview", reasoning)


def _trace_model_suggestion_decision(
    *,
    title: str,
    model: str,
    parsed_count: int,
    requested_count: int,
    accepted: str,
    rejected_count: int,
) -> None:
    if not _trace_model_enabled():
        return
    _trace_model_line(
        f"[verlato:model] decision suggestions parsed={parsed_count} requested={requested_count} "
        f"accepted={accepted} rejected={rejected_count} model={model} title={title}",
        event="suggestion_decision",
        attributes={
            "title": title,
            "model": model,
            "parsed": parsed_count,
            "requested": requested_count,
            "accepted": accepted,
            "rejected": rejected_count,
        },
    )


def _trace_model_suggest_stage(stage: str, detail: str) -> None:
    if not _trace_model_enabled():
        return
    _trace_model_line(f"[verlato:model] suggest stage={stage} {detail}", event=stage.replace("-", "_"))


def _trace_model_prompt_budget(budget: dict[str, Any]) -> None:
    if not _trace_model_enabled():
        return
    _trace_model_line(
        "[verlato:model] prompt-budget "
        f"profile={budget.get('profile', 'summary')} "
        f"chars={budget.get('prompt_chars', 0)} "
        f"files={budget.get('files_included', 0)}/{budget.get('files_total', 0)} "
        f"commits={budget.get('commits_included', 0)}/{budget.get('commits_total', 0)} "
        f"hunks={budget.get('hunks_included', 0)}/{budget.get('hunks_total', 0)} "
        f"pr_body_chars={budget.get('pr_body_chars_included', 0)}/{budget.get('pr_body_chars_total', 0)} "
        f"diff_summary_chars={budget.get('diff_summary_chars_included', 0)}/{budget.get('diff_summary_chars_total', 0)}",
        event="prompt_budget",
        attributes=budget,
    )


def _trace_model_payload(stage: str, payload: str) -> None:
    if not _trace_model_enabled() or os.getenv("VERLATO_TRACE_MODEL_PAYLOADS") != "1":
        return
    safe_payload, redacted = _safe_model_payload(payload)
    compact = re.sub(r"\s+", " ", safe_payload).strip()[:800]
    _trace_model_line(
        f"[verlato:model] {stage} chars={len(payload)} redacted={str(redacted).lower()} excerpt={compact}",
        event=stage.replace("-", "_"),
        payload=payload,
        attributes={"chars": len(payload), "redacted": redacted},
    )


def _trace_model_suggestion_filter(stage: str, detail: str, title: str | None, model: str | None) -> None:
    if not _trace_model_enabled():
        return
    title_part = f" title={title}" if title else ""
    model_part = f" model={model}" if model else ""
    _trace_model_line(
        f"[verlato:model] suggestion {stage} {detail}{model_part}{title_part}",
        event=f"suggestion_{stage.replace('-', '_')}",
        attributes={"title": title, "model": model},
    )


def _trace_model_line(
    message: str,
    *,
    event: str,
    attributes: dict[str, Any] | None = None,
    payload: str | None = None,
) -> None:
    line = message if message.startswith("[verlato:model]") else f"[verlato:model] {message}"
    lines = [line]
    if payload is not None:
        safe_payload, redacted = _safe_model_payload(payload)
        safe_attributes = {**(attributes or {}), "redacted": redacted}
        lines.append(
            "[verlato:model] json "
            + json.dumps(
                {
                    "event": event,
                    "message": message,
                    "attributes": safe_attributes,
                    "payload": safe_payload,
                },
                sort_keys=True,
            )
        )
    print_stderr_lines(lines)
    _write_model_debug_event(event=event, message=line, attributes=attributes, payload=payload)


def _write_model_debug_event(
    *,
    event: str,
    message: str,
    attributes: dict[str, Any] | None = None,
    payload: str | None = None,
) -> None:
    path = os.getenv("VERLATO_MODEL_LOG_PATH")
    if not path:
        return
    record: dict[str, Any] = {
        "event": event,
        "message": message,
        "attributes": {key: value for key, value in (attributes or {}).items() if value is not None},
    }
    if payload is not None:
        safe_payload, redacted = _safe_model_payload(payload)
        record["payload"] = safe_payload
        record["attributes"]["redacted"] = redacted
    append_log_line(path, json.dumps(record, sort_keys=True))


def _safe_model_payload(payload: str) -> tuple[str, bool]:
    if not model_payload_redaction_enabled():
        return payload, False
    return redact_model_payload(payload)


def _parse_summary_provenance(raw: str | None) -> dict[str, Any] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid summary provenance JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit("Invalid summary provenance JSON: expected an object")
    return _normalize_summary_provenance(parsed)


def _normalize_summary_provenance(provenance: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(provenance)
    normalized["source"] = str(normalized.get("source") or "model-suggestion")
    normalized.setdefault("applied_at", date.today().isoformat())
    return normalized


def _summary_provenance(
    *,
    source: str,
    provider: str | None,
    model: str | None,
    prompt_sha: str | None,
    tone: str | None,
    style_profile: dict[str, Any] | None,
    response_metadata: dict[str, Any] | None,
) -> dict[str, Any]:
    provenance: dict[str, Any] = {
        "source": source,
        "applied_at": date.today().isoformat(),
    }
    if provider:
        provenance["provider"] = provider
    if model:
        provenance["model"] = model
    if prompt_sha:
        provenance["prompt_sha"] = prompt_sha
    if tone:
        provenance["tone"] = tone
    if style_profile:
        provenance["style_profile"] = style_profile
    if response_metadata:
        provenance["response_metadata"] = response_metadata
    return provenance


def _provider_model_from_signals(signals: list[str]) -> str | None:
    signal = next((value for value in signals if value.startswith("provider-summary:")), None)
    if not signal:
        return None
    return signal.split(":", 1)[1]


def _run_model_logs_export(
    *,
    file_path: str,
    output_format: str,
    output_path: str | None,
    events: list[str] | None,
) -> int:
    source = Path(file_path)
    if not source.exists():
        raise SystemExit(f"Model log file not found: {source}")
    event_filter = set(events or [])
    records: list[dict[str, Any]] = []
    for line_number, line in enumerate(source.read_text(encoding="utf-8").splitlines(), start=1):
        if not line.strip():
            continue
        try:
            raw = json.loads(line)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid JSONL model log at line {line_number}: {exc}") from exc
        if not isinstance(raw, dict):
            raise SystemExit(f"Invalid JSONL model log at line {line_number}: expected an object")
        normalized = _normalize_model_log_record(raw, line_number=line_number)
        if event_filter and normalized["event"] not in event_filter:
            continue
        records.append(normalized)

    if output_format == "jsonl":
        text = "\n".join(json.dumps(record, sort_keys=True) for record in records)
        if text:
            text += "\n"
    else:
        text = json.dumps(
            {
                "schema_version": "verlato.model_logs.v1",
                "source": str(source),
                "record_count": len(records),
                "records": records,
            },
            indent=2,
            sort_keys=True,
        )
        text += "\n"

    if output_path:
        Path(output_path).write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


def _run_feedback_record(*, event: str, item_id: str, metadata_json: str | None) -> int:
    metadata: dict[str, Any] = {}
    if metadata_json:
        try:
            parsed = json.loads(metadata_json)
        except json.JSONDecodeError as exc:
            raise SystemExit(f"Invalid feedback metadata JSON: {exc}") from exc
        if not isinstance(parsed, dict):
            raise SystemExit("Invalid feedback metadata JSON: expected an object")
        metadata = parsed
    _append_feedback_event(event=event, item_id=item_id, metadata=metadata)
    print(json.dumps({"recorded": True, "event": event, "item_id": item_id}, indent=2))
    return 0


def _run_feedback_export(*, output_format: str, output_path: str | None, events: list[str] | None) -> int:
    state = _build_store().load()
    event_filter = set(events or [])
    records = [
        event for event in state.feedback_events
        if not event_filter or str(event.get("event")) in event_filter
    ]
    if output_format == "jsonl":
        text = "\n".join(json.dumps(record, sort_keys=True) for record in records)
        if text:
            text += "\n"
    else:
        text = json.dumps(
            {
                "schema_version": "verlato.feedback.v1",
                "record_count": len(records),
                "records": records,
            },
            indent=2,
            sort_keys=True,
        )
        text += "\n"
    if output_path:
        Path(output_path).write_text(text, encoding="utf-8")
    else:
        print(text, end="")
    return 0


def _run_feedback_recommend() -> int:
    payload = _build_feedback_recommendations(_build_store().load().feedback_events)
    print(json.dumps(payload, indent=2, sort_keys=True))
    return 0


def _build_feedback_recommendations(events: list[dict]) -> dict[str, Any]:
    safe_events = [event for event in events if isinstance(event, dict)]
    event_counts = Counter(str(event.get("event") or "unknown") for event in safe_events)
    tone_counts: dict[str, Counter[str]] = {}
    specificity_scores: list[float] = []
    skipped_or_negative = {
        "suggestion_skipped",
        "suggestion_rejected",
        "suggestion_not_useful",
        "suggestion_regenerated",
    }

    for event in safe_events:
        metadata = _as_dict(event.get("metadata"))
        event_name = str(event.get("event") or "unknown")
        tone = metadata.get("tone")
        if isinstance(tone, str) and tone:
            tone_counts.setdefault(tone, Counter())[event_name] += 1
        specificity = _feedback_float(metadata.get("specificity_score"))
        if specificity is not None and event_name in {"suggestion_applied", "summary_edited"}:
            specificity_scores.append(specificity)

    recommendations: list[dict[str, Any]] = []
    positive_count = event_counts["suggestion_applied"] + event_counts["summary_edited"]
    negative_count = sum(event_counts[name] for name in skipped_or_negative)
    regenerated_count = event_counts["suggestion_regenerated"]
    total_count = len(safe_events)

    if total_count == 0:
        recommendations.append(
            {
                "kind": "collect-more-feedback",
                "title": "Collect more feedback before tuning prompts",
                "detail": (
                    "No local feedback events are available yet, so Verlato cannot infer style preferences safely."
                ),
                "confidence": "high",
            }
        )
    if total_count and negative_count >= max(3, positive_count):
        recommendations.append(
            {
                "kind": "tighten-style-guidance",
                "title": "Add project-specific style guidance",
                "detail": (
                    f"{negative_count} skip/reject/regenerate event(s) versus {positive_count} applied/edit event(s) "
                    "suggest that generated summaries are missing the project's preferred voice."
                ),
                "confidence": "medium",
                "suggested_config": {
                    "suggestion_style": {
                        "guidance": "Describe the user-visible outcome first, then include the concrete changed area.",
                    }
                },
            }
        )
    if regenerated_count >= 2:
        recommendations.append(
            {
                "kind": "generate-more-options",
                "title": "Consider a larger suggestion count",
                "detail": (
                    f"{regenerated_count} regenerate event(s) suggest reviewers often need another option before "
                    "applying wording."
                ),
                "confidence": "medium",
                "suggested_config": {"suggestion_count": 2},
            }
        )
    if specificity_scores:
        average_specificity = sum(specificity_scores) / len(specificity_scores)
        if average_specificity < 0.45:
            recommendations.append(
                {
                    "kind": "raise-specificity-floor",
                    "title": "Raise the specificity floor",
                    "detail": (
                        "Accepted or edited suggestions average "
                        f"{average_specificity:.2f} specificity, so vague candidates may still be getting through."
                    ),
                    "confidence": "medium",
                    "suggested_config": {"suggestion_style": {"specificity_floor": 0.45}},
                }
            )
    best_tone = _feedback_best_tone(tone_counts)
    if best_tone:
        tone, accepted, total = best_tone
        recommendations.append(
            {
                "kind": "prefer-tone",
                "title": f"Prefer the {tone} tone",
                "detail": f"{accepted} of {total} feedback event(s) for this tone were applied or edited.",
                "confidence": "low" if total < 5 else "medium",
                "suggested_config": {"suggestion_tone": tone},
            }
        )
    if total_count and not recommendations:
        recommendations.append(
            {
                "kind": "keep-current-style",
                "title": "Keep the current prompt style",
                "detail": (
                    "Current aggregate feedback does not show a strong reason to adjust prompt or style settings."
                ),
                "confidence": "medium",
            }
        )

    return {
        "schema_version": "verlato.feedback_recommendations.v1",
        "event_count": total_count,
        "event_counts": dict(sorted(event_counts.items())),
        "signals": {
            "positive_count": positive_count,
            "negative_count": negative_count,
            "regenerated_count": regenerated_count,
            "average_specificity": round(sum(specificity_scores) / len(specificity_scores), 3)
            if specificity_scores
            else None,
            "tones": {
                tone: dict(sorted(counts.items()))
                for tone, counts in sorted(tone_counts.items())
            },
        },
        "recommendations": recommendations,
    }


def _feedback_float(value: object) -> float | None:
    if isinstance(value, int | float):
        return float(value)
    if isinstance(value, str):
        try:
            return float(value)
        except ValueError:
            return None
    return None


def _feedback_best_tone(tone_counts: dict[str, Counter[str]]) -> tuple[str, int, int] | None:
    best: tuple[str, int, int, float] | None = None
    for tone, counts in tone_counts.items():
        accepted = counts["suggestion_applied"] + counts["summary_edited"]
        total = sum(counts.values())
        if total < 2 or accepted == 0:
            continue
        rate = accepted / total
        if rate < 0.6:
            continue
        candidate = (tone, accepted, total, rate)
        if best is None or (candidate[3], candidate[1], candidate[2]) > (best[3], best[1], best[2]):
            best = candidate
    if best is None:
        return None
    return best[0], best[1], best[2]


def _append_feedback_event(*, event: str, item_id: str, metadata: dict[str, Any] | None = None) -> None:
    store = _build_store()
    state = store.load()
    state.feedback_events.append(
        {
            "event": event,
            "item_id": item_id,
            "recorded_at": date.today().isoformat(),
            "metadata": metadata or {},
        }
    )
    store.save(state)


def _feedback_summary_metadata(summary: str, *, include_text: bool) -> dict[str, Any]:
    metadata: dict[str, Any] = {
        "summary_chars": len(summary),
        "summary_sha": hashlib.sha256(summary.encode("utf-8")).hexdigest()[:12],
    }
    if include_text:
        metadata["summary"] = summary
    return metadata


def _feedback_include_text() -> bool:
    raw = os.getenv("VERLATO_FEEDBACK_INCLUDE_TEXT")
    if raw is not None:
        return raw.strip().lower() in {"1", "true", "yes", "on"}
    return _load_config().feedback_include_text


def _normalize_model_log_record(raw: dict[str, Any], *, line_number: int) -> dict[str, Any]:
    if "EventName" in raw or "Body" in raw:
        attributes = _as_dict(raw.get("Attributes"))
        body, body_redacted = _redact_export_string(str(raw.get("Body") or ""))
        safe_attributes, attributes_redacted = _redact_export_value(attributes)
        return {
            "line_number": line_number,
            "raw_format": "otel",
            "timestamp": raw.get("Timestamp"),
            "observed_timestamp": raw.get("ObservedTimestamp"),
            "severity_text": raw.get("SeverityText"),
            "severity_number": raw.get("SeverityNumber"),
            "event": str(raw.get("EventName") or "verlato.model.log"),
            "body": body,
            "attributes": _attributes_with_redaction(safe_attributes, body_redacted or attributes_redacted),
            "resource": _as_dict(raw.get("Resource")),
            "instrumentation_scope": raw.get("InstrumentationScope")
            if isinstance(raw.get("InstrumentationScope"), dict)
            else {},
        }

    attributes = _as_dict(raw.get("attributes"))
    message, message_redacted = _redact_export_string(str(raw.get("message") or ""))
    safe_attributes, attributes_redacted = _redact_export_value(attributes)
    payload = raw.get("payload")
    payload_redacted = False
    normalized: dict[str, Any] = {
        "line_number": line_number,
        "raw_format": "cli",
        "event": str(raw.get("event") or "verlato.model.event"),
        "message": message,
        "attributes": safe_attributes,
    }
    if isinstance(payload, str):
        safe_payload, payload_redacted = _redact_export_string(payload)
        normalized["payload"] = safe_payload
    normalized["attributes"] = _attributes_with_redaction(
        normalized["attributes"],
        message_redacted or attributes_redacted or payload_redacted,
    )
    return normalized


def _redact_export_value(value: object) -> tuple[object, bool]:
    if isinstance(value, str):
        return _redact_export_string(value)
    if isinstance(value, list):
        changed = False
        values: list[object] = []
        for item in value:
            safe_item, item_changed = _redact_export_value(item)
            values.append(safe_item)
            changed = changed or item_changed
        return values, changed
    if isinstance(value, dict):
        changed = False
        mapping: dict[str, Any] = {}
        for key, item in value.items():
            safe_item, item_changed = _redact_export_value(item)
            mapping[str(key)] = safe_item
            changed = changed or item_changed
        return mapping, changed
    return value, False


def _redact_export_string(value: str) -> tuple[str, bool]:
    if not model_payload_redaction_enabled():
        return value, False
    return redact_model_payload(value)


def _attributes_with_redaction(attributes: object, redacted: bool) -> dict[str, Any]:
    safe = dict(attributes) if isinstance(attributes, dict) else {}
    safe["redacted"] = bool(safe.get("redacted")) or redacted
    return safe


def _suggestions_preview(suggestions: list[dict[str, Any]]) -> str:
    if not suggestions:
        return "none"
    return "|".join(str(detail["summary"])[:80] for detail in suggestions[:3])


def _build_draft_model_prompt(
    *,
    base: str,
    head: str,
    item_id: str,
    model_provider: str | None = None,
    tone: str | None = None,
    commit_intent_inference: str | None = None,
    model_privacy: str | None = None,
    allow_remote_patches: bool = False,
) -> dict[str, Any]:
    effective_model_provider = _resolve_suggest_model_provider(model_provider)
    config = _load_config()
    privacy_mode = resolve_model_privacy(
        config.model_privacy,
        requested=model_privacy,
        allow_remote_patches=allow_remote_patches,
    )
    raw_patches_allowed = _can_send_raw_patches_for_config(
        config.provider,
        effective_model_provider or config.provider.name,
        privacy_mode,
    )
    suggestion_tone = _resolve_suggestion_tone(config.suggestion_tone, requested=tone)
    suggestion_style = _resolve_suggestion_style(config)
    target_group = _find_group_for_item_id(
        base,
        head,
        item_id,
        model_provider=effective_model_provider,
        commit_intent_inference=commit_intent_inference,
        model_privacy=privacy_mode,
        allow_remote_patches=allow_remote_patches,
    )
    classifier = Classifier()
    draft_item = classifier.classify(target_group, base, head)[0]
    diff = classifier.diff_builder.build(changed_files_for_group(target_group), commits=target_group.commits)
    prompt_result = classifier._build_summary_prompt_with_budget(
        title=draft_item.content.title,
        labels=[label.lower() for label in (target_group.pr.labels if target_group.pr else [])],
        pr_body=target_group.pr.body if target_group.pr else "",
        diff=diff,
        commits=target_group.commits,
        tone=suggestion_tone,
        style_guidance=str(suggestion_style["guidance"]),
        project_vocabulary=list(suggestion_style["project_vocabulary"]),
        banned_phrases=list(suggestion_style["banned_phrases"]),
        prompt_profile="suggestion",
        include_hunks=raw_patches_allowed,
    )
    prompt_budget = dict(prompt_result.budget)
    prompt_budget["privacy_mode"] = privacy_mode
    prompt_budget["raw_patch_chars_available"] = raw_patch_chars_available(changed_files_for_group(target_group))
    change_summary = build_change_summary_payload(group=target_group, item=draft_item, diff=diff)
    evidence = {
        "title": draft_item.content.title,
        "item_id": draft_item.id,
        "change_summary": change_summary,
        "privacy_mode": privacy_mode,
    }
    raw_patch_chars = int(prompt_budget["raw_patch_chars_available"])
    request_record = model_request_record(
        task="suggest-release-note",
        provider=effective_model_provider or "configured",
        privacy_mode=privacy_mode,
        prompt=prompt_result.prompt,
        evidence=evidence,
        raw_patch_chars_available=raw_patch_chars,
        raw_patch_chars_sent=int(prompt_result.budget.get("raw_patch_chars_sent") or 0),
        omitted_fields=[] if raw_patches_allowed else list(OMITTED_PATCH_EVIDENCE),
        omission_reasons=[]
        if raw_patches_allowed
        else ["metadata-only mode sends ChangeSummary instead of raw diff text"],
    )
    return {
        "item_id": draft_item.id,
        "title": draft_item.content.title,
        "provider": effective_model_provider or "configured",
        "privacy": privacy_summary(
            provider=effective_model_provider or "configured",
            privacy_mode=privacy_mode,
            raw_patch_chars_available=raw_patch_chars,
            raw_patch_chars_sent=int(prompt_result.budget.get("raw_patch_chars_sent") or 0),
        ),
        "tone": suggestion_tone,
        "style_profile": suggestion_style,
        "prompt": prompt_result.prompt,
        "evidence": evidence,
        "ai_request": request_record,
        "prompt_budget": prompt_budget,
    }


def _persist_review_update(
    *,
    candidate: ChangeItem,
    status: str | None,
    summary: str | None,
    reviewer: str | None,
    summary_provenance: dict[str, Any] | None = None,
) -> ChangeItem:
    store = _build_store()
    state = store.load()
    match = next(
        (
            item
            for item in state.items
            if (item.checksum_version, item.checksum) == (candidate.checksum_version, candidate.checksum) or item.id == candidate.id
        ),
        None,
    )
    if match is None:
        candidate.lineage_releases = list(state.area_release_lineage.get(candidate.classification.area, []))
        state.items.append(candidate)
        match = candidate

    if summary is not None:
        normalized_summary = summary.strip()
        prior_summary = match.edited_summary or match.content.summary
        match.edited_summary = normalized_summary or None
        if normalized_summary:
            if summary_provenance is not None:
                match.summary_provenance = _normalize_summary_provenance(summary_provenance)
                feedback_event = "suggestion_applied"
            elif match.summary_provenance:
                match.summary_provenance = {
                    **match.summary_provenance,
                    "source": "human-edited-model"
                    if str(match.summary_provenance.get("source", "")).startswith("model")
                    else "human-edited",
                    "edited_at": date.today().isoformat(),
                }
                feedback_event = "summary_edited"
            else:
                match.summary_provenance = {
                    "source": "human-edited",
                    "applied_at": date.today().isoformat(),
                }
                feedback_event = "summary_edited"
            feedback_metadata = {
                **_feedback_summary_metadata(normalized_summary, include_text=_feedback_include_text()),
                "previous_summary_sha": hashlib.sha256(prior_summary.encode("utf-8")).hexdigest()[:12],
                "source": match.summary_provenance.get("source") if match.summary_provenance else None,
                "provider": match.summary_provenance.get("provider") if match.summary_provenance else None,
                "model": match.summary_provenance.get("model") if match.summary_provenance else None,
                "prompt_sha": match.summary_provenance.get("prompt_sha") if match.summary_provenance else None,
                "prompt_profile": match.summary_provenance.get("prompt_profile") if match.summary_provenance else None,
                "cache_hit": match.summary_provenance.get("cache_hit") if match.summary_provenance else None,
                "tone": match.summary_provenance.get("tone") if match.summary_provenance else None,
                "style_profile": match.summary_provenance.get("style_profile") if match.summary_provenance else None,
                "similarity_to_current": match.summary_provenance.get("similarity_to_current") if match.summary_provenance else None,
                "specificity_score": match.summary_provenance.get("specificity_score") if match.summary_provenance else None,
                "source_evidence_count": match.summary_provenance.get("source_evidence_count") if match.summary_provenance else None,
            }
            state.feedback_events.append(
                {
                    "event": feedback_event,
                    "item_id": match.id,
                    "recorded_at": date.today().isoformat(),
                    "metadata": {key: value for key, value in feedback_metadata.items() if value is not None},
                }
            )
        else:
            match.summary_provenance = None
        if normalized_summary and status is None and match.status != "hidden":
            match.status = "edited"
    if status is not None:
        match.status = status
        match.hidden = status == "hidden"
    if reviewer is not None:
        match.reviewed_by = reviewer.strip() or None
    if status is not None or summary is not None or reviewer is not None:
        match.reviewed_at = date.today().isoformat()

    store.save(state)
    return match


def _run_draft_suggest(
    *,
    base: str,
    head: str,
    item_id: str,
    reviewer: str | None,
    model_provider: str | None = None,
    tone: str | None = None,
    commit_intent_inference: str | None = None,
    model_privacy: str | None = None,
    allow_remote_patches: bool = False,
) -> int:
    updated = _request_draft_model_input(
        base=base,
        head=head,
        item_id=item_id,
        reviewer=reviewer,
        model_provider=model_provider,
        tone=tone,
        commit_intent_inference=commit_intent_inference,
        model_privacy=model_privacy,
        allow_remote_patches=allow_remote_patches,
    )
    print(json.dumps(updated.to_dict(), indent=2))
    return 0


def _run_draft_prompt(
    *,
    base: str,
    head: str,
    item_id: str,
    model_provider: str | None = None,
    tone: str | None = None,
    commit_intent_inference: str | None = None,
    model_privacy: str | None = None,
    allow_remote_patches: bool = False,
) -> int:
    prompt_preview = _build_draft_model_prompt(
        base=base,
        head=head,
        item_id=item_id,
        model_provider=model_provider,
        tone=tone,
        commit_intent_inference=commit_intent_inference,
        model_privacy=model_privacy,
        allow_remote_patches=allow_remote_patches,
    )
    print(json.dumps(prompt_preview, indent=2))
    return 0


def _resolve_range(args: argparse.Namespace) -> tuple[str, str]:
    if args.base:
        return args.base, args.head

    use_tag = bool(args.from_latest_tag)
    use_published = bool(args.from_latest_published)
    if use_tag and use_published:
        raise SystemExit("Choose only one marker mode: --from-latest-tag or --from-latest-published")
    if not use_tag and not use_published:
        raise SystemExit("Provide --base or one marker mode: --from-latest-tag / --from-latest-published")

    if use_tag:
        marker = Collector(pr_adapter=_build_adapter()).latest_tag(args.head)
        if not marker:
            raise SystemExit(f"No reachable tag found from {args.head}")
        return marker, args.head

    marker = _latest_published_version()
    if not marker:
        raise SystemExit("No published release marker found in state")
    return marker, args.head


def _run_mark_published(version: str, ids: list[str] | None, all_unpublished: bool) -> int:
    store = _build_store()
    state = store.load()
    id_filter = set(ids or [])

    updated = 0
    for item in state.items:
        should_mark = all_unpublished or (item.id in id_filter)
        if should_mark and not item.published:
            item.published = True
            item.published_in = version
            updated += 1

    store.save(state)
    print(json.dumps({"updated": updated, "version": version}, indent=2))
    return 0


def _run_clear_working_state() -> int:
    store = _build_store()
    state = store.load()
    kept_items = [item for item in state.items if item.published]
    removed_ids = {item.id for item in state.items if not item.published}
    removed_feedback_count = 0
    if removed_ids:
        filtered_feedback = []
        for event in state.feedback_events:
            if event.get("item_id") in removed_ids:
                removed_feedback_count += 1
                continue
            filtered_feedback.append(event)
        state.feedback_events = filtered_feedback
    removed_count = len(state.items) - len(kept_items)
    state.items = kept_items
    store.save(state)
    print(
        json.dumps(
            {
                "removed_working_items": removed_count,
                "removed_feedback_events": removed_feedback_count,
                "preserved_published_items": len(kept_items),
            },
            indent=2,
        )
    )
    return 0


def _run_clear_suggestion_cache() -> int:
    config = _load_config()
    backend = _resolve_suggestion_cache_backend(config.suggestion_cache_backend)
    path = os.getenv("VERLATO_SUGGESTION_CACHE_PATH", config.suggestion_cache_path)
    cache = build_suggestion_cache_backend(
        backend=backend,
        path=path,
        ttl_days=_resolve_suggestion_cache_ttl_days(config.suggestion_cache_ttl_days),
    )
    removed_count = cache.clear()
    print(
        json.dumps(
            {
                "removed_entries": removed_count,
                "path": str(cache.path),
            },
            indent=2,
        )
    )
    return 0


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if getattr(args, "diff_engine", None):
        os.environ["VERLATO_DIFF_ENGINE"] = args.diff_engine

    if args.command == "doctor" and args.doctor_command == "engine":
        print(json.dumps(_diff_engine_info(), indent=2))
        return 0

    if args.command == "scan":
        base, head = _resolve_range(args)
        print(json.dumps(_run_scan_with_provider(
            base,
            head,
            persist=False,
            model_provider=args.model_provider,
            commit_intent_inference=_commit_intent_inference_from_args(args),
            model_privacy=_model_privacy_from_args(args),
            allow_remote_patches=_allow_remote_patches_from_args(args),
        ), indent=2))
        return 0

    if args.command == "generate":
        base, head = _resolve_range(args)
        items = _run_scan_with_provider(
            base,
            head,
            persist=True,
            model_provider=args.model_provider,
            commit_intent_inference=_commit_intent_inference_from_args(args),
            model_privacy=_model_privacy_from_args(args),
            allow_remote_patches=_allow_remote_patches_from_args(args),
        )
        print(json.dumps(items, indent=2))
        return 0

    if args.command == "reconcile":
        base, head = _resolve_range(args)
        items = _run_scan_with_provider(
            base,
            head,
            persist=False,
            model_provider=args.model_provider,
            commit_intent_inference=_commit_intent_inference_from_args(args),
            model_privacy=_model_privacy_from_args(args),
            allow_remote_patches=_allow_remote_patches_from_args(args),
        )
        print(json.dumps({"unpublished": items, "count": len(items)}, indent=2))
        return 0

    if args.command == "render":
        state = _build_store().load().items
        document = DraftService(config=_load_config()).build_document(
            items=state,
            base_ref="state",
            head_ref="state",
            version=args.version,
            include_confidence=args.include_confidence,
            include_provenance=args.include_provenance,
        )
        print(document.rendered[args.format])
        return 0

    if args.command == "serve":
        from verlato.mcp.server import run_mcp_stdio

        try:
            return run_mcp_stdio()
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc

    if args.command == "service":
        from verlato.service_stdio import run_service_stdio

        return run_service_stdio()

    if args.command == "commit" and args.commit_command == "conventional":
        from verlato.mcp.service import MCPService

        result = MCPService().draft_conventional_commit(
            base=args.base,
            head=args.head,
            sha=args.sha,
            model_provider=args.model_provider,
            model_privacy=_model_privacy_from_args(args),
            allow_remote_patches=_allow_remote_patches_from_args(args),
            evidence_mode=args.evidence_mode,
        )
        print(json.dumps(result, indent=2))
        return 0

    if args.command == "doctor" and args.doctor_command == "ollama":
        print(
            json.dumps(
                _run_ollama_doctor(
                    parallel_probe=args.parallel_probe,
                    warmup=args.warmup,
                    adaptive_recommendations=args.adaptive_recommendations,
                    suggest_model=args.suggest_model,
                ),
                indent=2,
            )
        )
        return 0

    if args.command == "doctor" and args.doctor_command == "model":
        print(
            json.dumps(
                _run_model_doctor(
                    model_provider=args.model_provider,
                    suggest_model=args.suggest_model,
                    timeout_seconds=args.timeout_seconds,
                ),
                indent=2,
            )
        )
        return 0

    if args.command == "model-logs" and args.model_logs_command == "export":
        return _run_model_logs_export(
            file_path=args.file,
            output_format=args.format,
            output_path=args.output,
            events=args.event,
        )

    if args.command == "feedback":
        if args.feedback_command == "record":
            return _run_feedback_record(event=args.event, item_id=args.item_id, metadata_json=args.metadata_json)
        if args.feedback_command == "export":
            return _run_feedback_export(output_format=args.format, output_path=args.output, events=args.event)
        if args.feedback_command == "recommend":
            return _run_feedback_recommend()

    if args.command == "draft":
        if args.draft_command == "review":
            base, head = _resolve_range(args)
            return _run_draft_review(
                base=base,
                head=head,
                item_id=args.id,
                status=args.status,
                summary=args.summary,
                reviewer=args.reviewer,
                model_provider=args.model_provider,
                summary_provenance_json=args.summary_provenance_json,
                commit_intent_inference=_commit_intent_inference_from_args(args),
            )
        if args.draft_command == "suggest":
            base, head = _resolve_range(args)
            if args.trace_suggestions:
                os.environ["VERLATO_TRACE_MODEL_CALLS"] = "1"
            if args.preview or args.preflight:
                suggestions = _preview_draft_model_suggestions(
                    base=base,
                    head=head,
                    item_id=args.id,
                    all_visible=args.all_visible,
                    count=args.count,
                    parallelism=args.parallelism,
                    batch_size=args.batch_size,
                    strategy=args.strategy,
                    prompt_profile=args.prompt_profile,
                    suggest_model=args.suggest_model,
                    model_provider=args.model_provider,
                    tone=args.tone,
                    no_cache=args.no_cache,
                    refresh_cache=args.refresh_cache,
                    adaptive_recommendations=args.adaptive_recommendations,
                    commit_intent_inference=_commit_intent_inference_from_args(args),
                    model_privacy=_model_privacy_from_args(args),
                    allow_remote_patches=_allow_remote_patches_from_args(args),
                    preflight=args.preflight,
                )
                print(json.dumps(suggestions, indent=2))
                return 0
            if args.all_visible:
                raise SystemExit("--all-visible requires --preview")
            if not args.id:
                raise SystemExit("Provide --id for draft suggest")
            return _run_draft_suggest(
                base=base,
                head=head,
                item_id=args.id,
                reviewer=args.reviewer,
                model_provider=args.model_provider,
                tone=args.tone,
                commit_intent_inference=_commit_intent_inference_from_args(args),
                model_privacy=_model_privacy_from_args(args),
                allow_remote_patches=_allow_remote_patches_from_args(args),
            )
        if args.draft_command == "prompt":
            base, head = _resolve_range(args)
            return _run_draft_prompt(
                base=base,
                head=head,
                item_id=args.id,
                model_provider=args.model_provider,
                tone=args.tone,
                commit_intent_inference=_commit_intent_inference_from_args(args),
                model_privacy=_model_privacy_from_args(args),
                allow_remote_patches=_allow_remote_patches_from_args(args),
            )
        base, head = _resolve_range(args)
        document = build_draft_document(
            base=base,
            head=head,
            version=args.version,
            include_confidence=args.include_confidence,
            include_provenance=args.include_provenance,
            model_provider=args.model_provider,
            commit_intent_inference=_commit_intent_inference_from_args(args),
            model_privacy=_model_privacy_from_args(args),
            allow_remote_patches=_allow_remote_patches_from_args(args),
        )
        if args.draft_command == "preview":
            _trace_progress(
                "draft_preview_rendering",
                "Rendering GitHub release notes",
                current=document.item_count,
                total=document.item_count,
                unit="item",
            )
            print(document.rendered["github-release"])
            return 0
        if args.draft_command == "json":
            _trace_progress(
                "draft_json_serializing",
                "Serializing draft document",
                current=document.item_count,
                total=document.item_count,
                unit="item",
            )
            print(json.dumps(document.to_dict(), indent=2))
            return 0
        if args.draft_command == "apply":
            _trace_progress(
                "draft_apply_writing",
                "Writing release notes",
                detail=args.target,
                current=0,
                total=1,
                unit="target",
            )
            apply_result = DraftService(config=_load_config()).apply_document(
                document=document,
                target_name=args.target,
                target_path=args.file,
            )
            _trace_progress(
                "draft_apply_writing",
                "Wrote release notes",
                detail=apply_result.target,
                current=1,
                total=1,
                unit="target",
            )
            print(json.dumps(apply_result.to_dict(), indent=2))
            return 0

    if args.command == "state" and args.state_command == "show":
        print(json.dumps({"items": [asdict(i) for i in _build_store().load().items]}, indent=2))
        return 0

    if args.command == "state" and args.state_command == "clear-working":
        return _run_clear_working_state()

    if args.command == "state" and args.state_command == "clear-suggestion-cache":
        return _run_clear_suggestion_cache()

    if args.command == "state" and args.state_command == "mark-published":
        if not args.all_unpublished and not args.ids:
            raise SystemExit("Provide --all-unpublished or one/more --ids values")
        return _run_mark_published(version=args.version, ids=args.ids, all_unpublished=args.all_unpublished)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
