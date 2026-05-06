from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from collections import Counter
from dataclasses import asdict, dataclass
from datetime import date
from pathlib import Path
from time import perf_counter

from verlato.config import VerlatoConfig, load_config
from verlato.core.adapters import NullPullRequestAdapter, PullRequestAdapter, StandardGitAdapter
from verlato.core.classifier import Classifier
from verlato.core.collector import Collector
from verlato.core.drafts import DraftDocument, DraftService
from verlato.core.grouper import Grouper
from verlato.core.models import ChangeGroup, ChangeItem
from verlato.core.reconciler import Reconciler
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
from verlato.state.suggestion_cache import CachedSuggestion, SuggestionCacheBackend, build_suggestion_cache_backend
from verlato.state.store import StateStore

MODEL_PROVIDER_CHOICES = ["stub", "mock", "ollama", "github-copilot"]
SUGGESTION_TONE_CHOICES = ["release-note", "concise", "technical", "customer-facing"]
MAX_SUGGESTION_PARALLELISM = 8
SUGGESTION_CACHE_PROMPT_PROFILE = "suggestion-conservative"


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
    evidence: dict[str, object]
    source_evidence: list[dict[str, str]]
    prompt_sha: str
    prompt_budget: dict[str, object]
    cache_key: str
    suggestion_count: int
    suggestion_tone: str
    suggestion_style: dict[str, object]
    effective_model_provider: str | None
    evidence_ms: int
    prompt_ms: int
    started_at: float


@dataclass
class _SuggestionResult:
    order: int
    item: dict[str, object]
    model_ms: int
    parse_ms: int


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="verlato")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("scan", "generate", "reconcile"):
        p = sub.add_parser(name)
        p.add_argument("--base")
        p.add_argument("--head", default="HEAD")
        p.add_argument("--from-latest-tag", action="store_true")
        p.add_argument("--from-latest-published", action="store_true")
        p.add_argument("--model-provider", choices=MODEL_PROVIDER_CHOICES, help="Override the configured model provider.")

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
    draft_suggest = draft_sub.add_parser("suggest")
    draft_suggest.add_argument("--base")
    draft_suggest.add_argument("--head", default="HEAD")
    draft_suggest.add_argument("--from-latest-tag", action="store_true")
    draft_suggest.add_argument("--from-latest-published", action="store_true")
    draft_suggest.add_argument("--id")
    draft_suggest.add_argument("--all-visible", action="store_true")
    draft_suggest.add_argument("--preview", action="store_true")
    draft_suggest.add_argument("--count", type=int)
    draft_suggest.add_argument("--parallelism", type=int)
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
    draft_prompt = draft_sub.add_parser("prompt")
    draft_prompt.add_argument("--base")
    draft_prompt.add_argument("--head", default="HEAD")
    draft_prompt.add_argument("--from-latest-tag", action="store_true")
    draft_prompt.add_argument("--from-latest-published", action="store_true")
    draft_prompt.add_argument("--id", required=True)
    draft_prompt.add_argument("--tone", choices=SUGGESTION_TONE_CHOICES)
    draft_prompt.add_argument("--model-provider", choices=MODEL_PROVIDER_CHOICES, help="Optional provider hint for the previewed prompt.")

    render = sub.add_parser("render")
    render.add_argument("--format", choices=["github-release", "changelog"], required=True)
    render.add_argument("--version", default="Unreleased")
    render.add_argument("--include-confidence", action="store_true")
    render.add_argument("--include-provenance", action="store_true")

    sub.add_parser("serve")

    doctor = sub.add_parser("doctor")
    doctor_sub = doctor.add_subparsers(dest="doctor_command", required=True)
    doctor_ollama = doctor_sub.add_parser("ollama")
    doctor_ollama.add_argument(
        "--parallel-probe",
        action="store_true",
        help="Run two tiny Ollama suggestion probes concurrently to detect server queueing.",
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
    event: dict[str, object] = {
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
    payload: dict[str, object] = {"event": event}
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


def run_scan(base: str, head: str, persist: bool, model_provider: str | None = None) -> list[dict]:
    return [asdict(i) for i in scan_change_items(base, head, persist, model_provider=model_provider)]


def detect_change_items(base: str, head: str, model_provider: str | None = None) -> list[ChangeItem]:
    config = _load_config()
    groups = _collect_groups(base, head)
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
    )
    return _classify_groups(groups, classifier, base, head)


def _collect_groups(base: str, head: str):
    collector = Collector(pr_adapter=_build_adapter())
    grouper = Grouper()
    _trace_progress("collecting_commits", "Collecting commits", detail=f"{base}..{head}")
    commits = collector.collect_commits(base, head)
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


def _find_group_for_item_id(base: str, head: str, item_id: str) -> ChangeGroup:
    groups = _collect_groups(base, head)
    baseline_classifier = Classifier()
    for group in groups:
        baseline_item = baseline_classifier.classify(group, base, head)[0]
        if baseline_item.id == item_id:
            return group
    raise SystemExit(f"Could not find draft item {item_id} for range {base}..{head}")


def scan_change_items(base: str, head: str, persist: bool, model_provider: str | None = None) -> list[ChangeItem]:
    store = _build_store()
    reconciler = Reconciler()

    detected = detect_change_items(base, head, model_provider=model_provider)
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


def _detect_change_items_with_provider(base: str, head: str, model_provider: str | None) -> list[ChangeItem]:
    if model_provider:
        return detect_change_items(base, head, model_provider=model_provider)
    return _detect_review_candidates(base, head, model_provider=None)


def _detect_review_candidates(base: str, head: str, model_provider: str | None) -> list[ChangeItem]:
    if model_provider:
        return detect_change_items(base, head, model_provider=model_provider)
    groups = _collect_groups(base, head)
    classifier = Classifier()
    return _classify_groups(
        groups,
        classifier,
        base,
        head,
        stage="detecting_review_candidates",
        label="Detecting review candidates",
    )


def _run_scan_with_provider(base: str, head: str, persist: bool, model_provider: str | None) -> list[dict]:
    if model_provider:
        return run_scan(base, head, persist=persist, model_provider=model_provider)
    return run_scan(base, head, persist=persist)


def _resolve_suggest_model_provider(model_provider: str | None) -> str | None:
    if model_provider:
        return model_provider
    config = _load_config()
    configured_name = os.getenv("VERLATO_MODEL_PROVIDER", config.provider.name).strip().lower()
    if configured_name and configured_name != "stub":
        return configured_name
    return "ollama"


def build_draft_document(
    *,
    base: str,
    head: str,
    version: str,
    include_confidence: bool = False,
    include_provenance: bool = False,
    model_provider: str | None = None,
) -> DraftDocument:
    config = _load_config()
    _trace_progress("draft_detecting_changes", "Detecting draft changes", detail=f"{base}..{head}")
    items = _merge_review_state(_detect_change_items_with_provider(base, head, model_provider))
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


def _resolve_suggestion_style(config: VerlatoConfig) -> dict[str, object]:
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


def _normalized_provider_timings(metadata: dict[str, object], model_ms: int) -> dict[str, object]:
    timings: dict[str, object] = {}
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


def _run_ollama_doctor(*, parallel_probe: bool = False) -> dict[str, object]:
    config = _load_config()
    suggestion_parallelism = _resolve_suggestion_parallelism(config.suggestion_parallelism)
    provider = build_provider(config.provider, mode="ollama")
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
    probes = [_run_ollama_doctor_probe(provider, request, name="single")]
    if parallel_probe:
        with ThreadPoolExecutor(max_workers=2, thread_name_prefix="verlato-ollama-doctor") as executor:
            futures = [
                executor.submit(_run_ollama_doctor_probe, provider, request, name="parallel-a"),
                executor.submit(_run_ollama_doctor_probe, provider, request, name="parallel-b"),
            ]
            probes.extend(future.result() for future in as_completed(futures))
        probes[1:] = sorted(probes[1:], key=lambda probe: str(probe.get("name") or ""))
    return {
        "schema": "ollama_doctor.v1",
        "provider": "ollama",
        "config": config_snapshot,
        "probes": probes,
        "summary": _summarize_ollama_doctor(probes, config_snapshot),
    }


def _ollama_doctor_config(provider: object, request: ProviderRequest, suggestion_parallelism: int) -> dict[str, object]:
    options = {}
    option_builder = getattr(provider, "_options_for_task", None)
    if callable(option_builder):
        try:
            options = option_builder("suggest", request_data=request)
        except TypeError:
            options = {}
    return {
        "endpoint": getattr(provider, "endpoint", None),
        "model": getattr(provider, "model", None),
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


def _run_ollama_doctor_probe(provider, request: ProviderRequest, *, name: str) -> dict[str, object]:
    started = perf_counter()
    try:
        response = provider.summarize(request)
    except BaseException as exc:
        elapsed_ms = _elapsed_ms(started)
        return {
            "name": name,
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


def _summarize_ollama_doctor(probes: list[dict[str, object]], config_snapshot: dict[str, object]) -> dict[str, object]:
    ok_probes = [probe for probe in probes if probe.get("ok") is True]
    if not ok_probes:
        diagnostics = [str(probe.get("error") or "Ollama probe failed") for probe in probes]
        return {
            "status": "error",
            "bottleneck": "unavailable",
            "diagnostics": diagnostics,
            "recommendations": [
                "Check that Ollama is running and reachable at the configured endpoint.",
                "Confirm the configured model is pulled and the endpoint matches Ollama's /api/chat route.",
            ],
        }
    bottleneck, diagnostic = _ollama_doctor_bottleneck(ok_probes)
    recommendations = _ollama_doctor_recommendations(bottleneck, config_snapshot)
    status = "ok" if bottleneck == "healthy" else "warning"
    return {
        "status": status,
        "bottleneck": bottleneck,
        "diagnostics": [diagnostic],
        "recommendations": recommendations,
    }


def _ollama_doctor_bottleneck(probes: list[dict[str, object]]) -> tuple[str, str]:
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


def _ollama_doctor_recommendations(bottleneck: str, config_snapshot: dict[str, object]) -> list[str]:
    parallelism = config_snapshot.get("suggestion_parallelism")
    if bottleneck == "queue":
        return [
            f"Lower Verlato suggestion parallelism from {parallelism} if the local server is serializing requests.",
            "Set OLLAMA_NUM_PARALLEL before starting Ollama if your hardware has memory for parallel requests.",
        ]
    if bottleneck == "load":
        return [
            "Keep the model warm with VERLATO_OLLAMA_KEEP_ALIVE or provider.keep_alive.",
            "Run a small warmup request before large all-visible suggestion runs.",
        ]
    if bottleneck == "prompt_eval":
        return [
            "Reduce prompt budget further or inspect prompt budget telemetry for files/hunks that dominate.",
            "Consider a later multi-item batching pass if prompt eval remains the main cost.",
        ]
    if bottleneck == "generation":
        return [
            "Lower VERLATO_OLLAMA_SUGGEST_NUM_PREDICT or provider.suggest_num_predict.",
            "Try a smaller/faster local model for suggestion generation.",
        ]
    if bottleneck == "unavailable":
        return [
            "Check that Ollama is running and reachable at the configured endpoint.",
            "Confirm the configured model is pulled and available to Ollama.",
        ]
    return ["No obvious Ollama bottleneck was detected by the probe."]


def _max_probe_timing(probes: list[dict[str, object]], key: str) -> int:
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
    summary_provenance: dict[str, object] | None = None,
) -> ChangeItem:
    if status is None and summary is None and reviewer is None:
        raise SystemExit("Provide at least one review update: --status, --summary, or --reviewer")

    candidates = _detect_review_candidates(base, head, model_provider)
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
) -> ChangeItem:
    effective_model_provider = _resolve_suggest_model_provider(model_provider)
    target_group = _find_group_for_item_id(base, head, item_id)

    config = _load_config()
    suggestion_tone = _resolve_suggestion_tone(config.suggestion_tone, requested=tone)
    suggestion_style = _resolve_suggestion_style(config)
    provider_classifier = Classifier(
        provider=build_provider(config.provider, mode=effective_model_provider),
        summary_tone=suggestion_tone,
        style_guidance=str(suggestion_style["guidance"]),
        project_vocabulary=list(suggestion_style["project_vocabulary"]),
        banned_phrases=list(suggestion_style["banned_phrases"]),
    )
    candidate = provider_classifier.classify(target_group, base, head, summary_only=True)[0]

    if not any(signal.startswith("provider-summary:") for signal in candidate.evidence.commit_signals):
        provider_error = next(
            (signal.removeprefix("provider-error:") for signal in candidate.evidence.commit_signals if signal.startswith("provider-error:")),
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
    model_provider: str | None = None,
    tone: str | None = None,
    no_cache: bool = False,
    refresh_cache: bool = False,
) -> dict:
    if all_visible == bool(item_id):
        raise SystemExit("Provide exactly one suggestion target: --id or --all-visible")
    if no_cache and refresh_cache:
        raise SystemExit("Use --no-cache or --refresh-cache, not both")

    total_start = perf_counter()
    config = _load_config()
    suggestion_count = _resolve_suggestion_count(config.suggestion_count, requested=count)
    suggestion_parallelism = _resolve_suggestion_parallelism(config.suggestion_parallelism, requested=parallelism)
    suggestion_tone = _resolve_suggestion_tone(config.suggestion_tone, requested=tone)
    suggestion_style = _resolve_suggestion_style(config)
    cache_read_enabled = not no_cache and not refresh_cache
    cache_write_enabled = not no_cache
    suggestion_cache = _build_suggestion_cache_store(config) if (cache_read_enabled or cache_write_enabled) else None
    suggestion_count_source = (
        "cli" if count is not None else "env" if os.getenv("VERLATO_SUGGESTION_COUNT") else "config"
    )
    effective_model_provider = _resolve_suggest_model_provider(model_provider)
    target_selection_start = perf_counter()
    groups = _collect_groups(base, head)
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
        batch_size = 0
        target_batches = []
    else:
        batch_size = max_model_items if all_visible and max_model_items > 0 else len(targets) or 1
        target_batches = [
            targets[index : index + batch_size]
            for index in range(0, len(targets), batch_size)
        ] or [[]]
    if all_visible and max_model_items >= 0 and len(target_batches) > 1:
        _trace_model_suggest_stage(
            "fanout-batches",
            (
                f"visible_items={len(targets)} fanout_guard={max_model_items} "
                f"batch_size={batch_size} batches={len(target_batches)}"
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
        batch_size=batch_size,
        batch_total=len(target_batches),
        fanout_guard=max_model_items,
    )
    _trace_model_suggest_plan(
        base=base,
        head=head,
        target_count=len(targets),
        suggestion_count=suggestion_count,
        suggestion_count_source=suggestion_count_source,
        provider=effective_model_provider or "configured",
        parallelism=suggestion_parallelism,
        all_visible=all_visible,
    )
    _trace_suggestion_event(
        "suggestion_preview_started",
        preview={
            "base_ref": base,
            "head_ref": head,
            "provider": effective_model_provider or "configured",
            "count": suggestion_count,
            "tone": suggestion_tone,
            "style_profile": suggestion_style,
            "performance": {
                "parallelism": suggestion_parallelism,
                "target_selection_ms": target_selection_ms,
                "preparation_ms": 0,
                "prompt_chars": 0,
                "model_ms": 0,
                "parse_ms": 0,
                "total_ms": 0,
            },
            "visible_item_count": visible_target_count,
            "fanout_guard": max_model_items,
            "batch_size": batch_size,
            "batch_count": len(target_batches),
            "items": [],
        },
        total=total_targets,
        batch_size=batch_size,
        batch_total=len(target_batches),
        fanout_guard=max_model_items,
    )

    if not any(target_batches):
        _trace_progress("suggestions_ready", "No model suggestions requested", current=0, total=0, unit="item")
        if suggestion_cache is not None and suggestion_cache.dirty:
            suggestion_cache.save()
        total_ms = _elapsed_ms(total_start)
        return {
            "base_ref": base,
            "head_ref": head,
            "provider": effective_model_provider or "configured",
            "count": suggestion_count,
            "tone": suggestion_tone,
            "style_profile": suggestion_style,
            "performance": {
                "parallelism": suggestion_parallelism,
                "target_selection_ms": target_selection_ms,
                "preparation_ms": 0,
                "prompt_chars": 0,
                "model_ms": 0,
                "parse_ms": 0,
                "total_ms": total_ms,
            },
            "visible_item_count": visible_target_count,
            "fanout_guard": max_model_items,
            "batch_size": batch_size,
            "batch_count": len(target_batches),
            "items": [],
        }

    provider = build_provider(config.provider, mode=effective_model_provider)
    cache_provider_mode = effective_model_provider or "configured"
    cache_model_id = _provider_cache_model_id(provider, cache_provider_mode)
    provider_classifier = Classifier(
        provider=provider,
        summary_tone=suggestion_tone,
        style_guidance=str(suggestion_style["guidance"]),
        project_vocabulary=list(suggestion_style["project_vocabulary"]),
        banned_phrases=list(suggestion_style["banned_phrases"]),
    )

    items: list[dict[str, object]] = []
    completed_count = 0
    preparation_ms = 0
    prompt_chars = 0
    model_ms = 0
    parse_ms = 0

    def prepare_work(order: int, group: ChangeGroup, baseline_item: ChangeItem, batch_index: int) -> _SuggestionWork:
        nonlocal preparation_ms, prompt_chars
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
        diff = provider_classifier.diff_builder.build(group.pr.files if group.pr else group.files)
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
            prompt_profile="suggestion",
        )
        prompt = prompt_result.prompt
        prompt_budget = dict(prompt_result.budget)
        prompt_chars += int(prompt_budget.get("prompt_chars", len(prompt)))
        evidence = {
            "title": baseline_item.content.title,
            "item_id": baseline_item.id,
            "suggestion_count": suggestion_count,
            "tone": suggestion_tone,
            "style_profile": suggestion_style,
            "diff_summary": diff.summary,
            "representative_hunks": diff.representative_hunks,
            "file_count": file_count,
            "hunk_count": hunk_count,
        }
        source_evidence = _build_suggestion_source_evidence(diff, group)
        prompt_sha = hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
        cache_key = _suggestion_cache_key(
            item=baseline_item,
            prompt_sha=prompt_sha,
            provider_mode=cache_provider_mode,
            model_id=cache_model_id,
            tone=suggestion_tone,
            style_profile=suggestion_style,
            suggestion_count=suggestion_count,
            prompt_profile=str(prompt_budget.get("profile") or SUGGESTION_CACHE_PROMPT_PROFILE),
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
        _trace_model_prompt_budget(prompt_result.budget)
        _trace_model_payload("prompt-preview", prompt)
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
            suggestion_count=suggestion_count,
            suggestion_tone=suggestion_tone,
            suggestion_style=suggestion_style,
            effective_model_provider=effective_model_provider,
            evidence_ms=evidence_ms,
            prompt_ms=prompt_ms,
            started_at=item_start,
        )

    def record_result(result: _SuggestionResult) -> None:
        nonlocal completed_count, model_ms, parse_ms
        completed_count += 1
        model_ms += result.model_ms
        parse_ms += result.parse_ms
        timings = result.item.get("timings")
        elapsed = timings.get("total_ms") if isinstance(timings, dict) else None
        _trace_progress(
            "suggestion_item_done",
            f"Generated suggestion for {result.item['item_id']}",
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
        if suggestion_parallelism <= 1 or len(batch) <= 1:
            for batch_offset, (group, baseline_item) in enumerate(batch):
                order = len(items) + batch_offset
                work = prepare_work(order, group, baseline_item, batch_index)
                result = cached_result_for_work(work)
                if result is None:
                    try:
                        result = _run_suggestion_model_work(provider, provider_classifier, work)
                    except (Exception, SystemExit) as exc:
                        record_failure(work, exc)
                        raise
                batch_results[result.order] = result
                record_result(result)
        else:
            workers = min(suggestion_parallelism, len(batch))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="verlato-suggest") as executor:
                future_to_work = {}
                for batch_offset, (group, baseline_item) in enumerate(batch):
                    order = len(items) + batch_offset
                    work = prepare_work(order, group, baseline_item, batch_index)
                    result = cached_result_for_work(work)
                    if result is not None:
                        batch_results[result.order] = result
                        record_result(result)
                        continue
                    future_to_work[executor.submit(_run_suggestion_model_work, provider, provider_classifier, work)] = work
                try:
                    for future in as_completed(future_to_work):
                        try:
                            result = future.result()
                        except (Exception, SystemExit) as exc:
                            record_failure(future_to_work[future], exc)
                            raise
                        batch_results[result.order] = result
                        record_result(result)
                except BaseException:
                    for pending in future_to_work:
                        pending.cancel()
                    raise
        for order in sorted(batch_results):
            result_item = dict(batch_results[order].item)
            result_item.pop("batch", None)
            items.append(result_item)

    if suggestion_cache is not None and suggestion_cache.dirty:
        suggestion_cache.save()
    total_ms = _elapsed_ms(total_start)
    _trace_progress(
        "suggestions_ready",
        "Generated suggestions",
        current=len(items),
        total=total_targets,
        unit="item",
        elapsed_ms=total_ms,
        model_ms=model_ms,
    )
    return {
        "base_ref": base,
        "head_ref": head,
        "provider": effective_model_provider or "configured",
        "count": suggestion_count,
        "tone": suggestion_tone,
        "style_profile": suggestion_style,
            "performance": {
                "parallelism": suggestion_parallelism,
                "target_selection_ms": target_selection_ms,
                "preparation_ms": preparation_ms,
                "prompt_chars": prompt_chars,
                "model_ms": model_ms,
                "parse_ms": parse_ms,
                "total_ms": total_ms,
        },
        "visible_item_count": visible_target_count,
        "fanout_guard": max_model_items,
        "batch_size": batch_size,
        "batch_count": len(target_batches),
        "items": items,
    }


def _provider_cache_model_id(provider: object, provider_mode: str) -> str:
    normalized_mode = (provider_mode or "configured").strip().lower()
    raw_model = getattr(provider, "model", None)
    model = str(raw_model).strip() if raw_model is not None else ""
    if normalized_mode in {"ollama", "local"}:
        return f"ollama:{model or 'default'}"
    if normalized_mode in {"github-copilot", "github", "copilot"}:
        return f"github-copilot:{model or 'default'}"
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
    style_profile: dict[str, object],
    suggestion_count: int,
    prompt_profile: str,
) -> str:
    current_summary = item.edited_summary or item.content.summary
    payload = {
        "schema": "v1",
        "item_id": item.id,
        "checksum_version": item.checksum_version,
        "checksum": item.checksum,
        "current_summary_sha": hashlib.sha256(current_summary.encode("utf-8")).hexdigest()[:12],
        "prompt_sha": prompt_sha,
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


def _suggestion_cache_stored_item(item: dict[str, object]) -> dict[str, object]:
    transient = {"batch", "cache", "provider_metadata", "provider_reasoning", "timings"}
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
        "provider": cached_item.get("provider") if isinstance(cached_item.get("provider"), str) else work.effective_model_provider or "configured",
        "prompt_sha": work.prompt_sha,
        "tone": work.suggestion_tone,
        "style_profile": work.suggestion_style,
        "source_evidence": work.source_evidence,
        "prompt_budget": work.prompt_budget,
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
        },
        "batch": work.batch,
    }
    return _SuggestionResult(order=work.order, item=item, model_ms=0, parse_ms=0)


def _cached_suggestion_details(
    cached_item: dict[str, object],
    suggestions: list[str],
    work: _SuggestionWork,
    current_summary: str,
) -> list[dict[str, object]]:
    raw_details = cached_item.get("suggestion_details")
    details_by_summary = {}
    if isinstance(raw_details, list):
        for detail in raw_details:
            if isinstance(detail, dict) and isinstance(detail.get("summary"), str):
                details_by_summary[detail["summary"]] = detail
    details: list[dict[str, object]] = []
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


def _run_suggestion_model_work(provider, provider_classifier: Classifier, work: _SuggestionWork) -> _SuggestionResult:
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
    response = provider.summarize(ProviderRequest(task="suggest", prompt=work.prompt, evidence=work.evidence))
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
        "suggestion_details": suggestion_details,
        "rejected_candidates": rejected_candidates,
        "suggestions": suggestions,
        "cache": {
            "hit": False,
            "key": work.cache_key,
        },
        "timings": {
            "evidence_ms": work.evidence_ms,
            "prompt_ms": work.prompt_ms,
            "model_ms": model_ms,
            "parse_ms": parse_ms,
            "total_ms": _elapsed_ms(work.started_at),
            **provider_timings,
        },
        "batch": work.batch,
    }
    return _SuggestionResult(order=work.order, item=item, model_ms=model_ms, parse_ms=parse_ms)


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
) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
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
    suggestions: list[dict[str, object]] = []
    rejected_candidates: list[dict[str, object]] = []
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
            " ".join(file.path for file in (group.pr.files if group.pr else group.files)),
        ]
    )
    evidence_tokens = set(_summary_tokens(evidence_text))
    overlap = len(tokens & evidence_tokens)
    length_score = min(len(tokens) / 12, 1.0) * 0.55
    overlap_score = min(overlap / 4, 1.0) * 0.45
    return round(length_score + overlap_score, 4)


def _build_suggestion_source_evidence(diff, group: ChangeGroup) -> list[dict[str, str]]:
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


def _trace_model_response_detail(model: str, content: str, metadata: dict[str, object], reasoning: str = "") -> None:
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


def _trace_model_prompt_budget(budget: dict[str, object]) -> None:
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
    attributes: dict[str, object] | None = None,
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
    attributes: dict[str, object] | None = None,
    payload: str | None = None,
) -> None:
    path = os.getenv("VERLATO_MODEL_LOG_PATH")
    if not path:
        return
    record: dict[str, object] = {
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


def _parse_summary_provenance(raw: str | None) -> dict[str, object] | None:
    if not raw:
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid summary provenance JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise SystemExit("Invalid summary provenance JSON: expected an object")
    return _normalize_summary_provenance(parsed)


def _normalize_summary_provenance(provenance: dict[str, object]) -> dict[str, object]:
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
    style_profile: dict[str, object] | None,
    response_metadata: dict[str, object] | None,
) -> dict[str, object]:
    provenance: dict[str, object] = {
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
    records: list[dict[str, object]] = []
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
    metadata: dict[str, object] = {}
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


def _build_feedback_recommendations(events: list[dict]) -> dict[str, object]:
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
        metadata = event.get("metadata") if isinstance(event.get("metadata"), dict) else {}
        event_name = str(event.get("event") or "unknown")
        tone = metadata.get("tone")
        if isinstance(tone, str) and tone:
            tone_counts.setdefault(tone, Counter())[event_name] += 1
        specificity = _feedback_float(metadata.get("specificity_score"))
        if specificity is not None and event_name in {"suggestion_applied", "summary_edited"}:
            specificity_scores.append(specificity)

    recommendations: list[dict[str, object]] = []
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


def _append_feedback_event(*, event: str, item_id: str, metadata: dict[str, object] | None = None) -> None:
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


def _feedback_summary_metadata(summary: str, *, include_text: bool) -> dict[str, object]:
    metadata: dict[str, object] = {
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


def _normalize_model_log_record(raw: dict[str, object], *, line_number: int) -> dict[str, object]:
    if "EventName" in raw or "Body" in raw:
        attributes = raw.get("Attributes") if isinstance(raw.get("Attributes"), dict) else {}
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
            "resource": raw.get("Resource") if isinstance(raw.get("Resource"), dict) else {},
            "instrumentation_scope": raw.get("InstrumentationScope")
            if isinstance(raw.get("InstrumentationScope"), dict)
            else {},
        }

    attributes = raw.get("attributes") if isinstance(raw.get("attributes"), dict) else {}
    message, message_redacted = _redact_export_string(str(raw.get("message") or ""))
    safe_attributes, attributes_redacted = _redact_export_value(attributes)
    payload = raw.get("payload")
    payload_redacted = False
    normalized: dict[str, object] = {
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
        values: dict[str, object] = {}
        for key, item in value.items():
            safe_item, item_changed = _redact_export_value(item)
            values[str(key)] = safe_item
            changed = changed or item_changed
        return values, changed
    return value, False


def _redact_export_string(value: str) -> tuple[str, bool]:
    if not model_payload_redaction_enabled():
        return value, False
    return redact_model_payload(value)


def _attributes_with_redaction(attributes: object, redacted: bool) -> dict[str, object]:
    safe = dict(attributes) if isinstance(attributes, dict) else {}
    safe["redacted"] = bool(safe.get("redacted")) or redacted
    return safe


def _suggestions_preview(suggestions: list[dict[str, object]]) -> str:
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
) -> dict[str, object]:
    effective_model_provider = _resolve_suggest_model_provider(model_provider)
    config = _load_config()
    suggestion_tone = _resolve_suggestion_tone(config.suggestion_tone, requested=tone)
    suggestion_style = _resolve_suggestion_style(config)
    target_group = _find_group_for_item_id(base, head, item_id)
    classifier = Classifier()
    draft_item = classifier.classify(target_group, base, head)[0]
    diff = classifier.diff_builder.build(target_group.pr.files if target_group.pr else target_group.files)
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
    )
    return {
        "item_id": draft_item.id,
        "title": draft_item.content.title,
        "provider": effective_model_provider or "configured",
        "tone": suggestion_tone,
        "style_profile": suggestion_style,
        "prompt": prompt_result.prompt,
        "prompt_budget": prompt_result.budget,
    }


def _persist_review_update(
    *,
    candidate: ChangeItem,
    status: str | None,
    summary: str | None,
    reviewer: str | None,
    summary_provenance: dict[str, object] | None = None,
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
                "tone": match.summary_provenance.get("tone") if match.summary_provenance else None,
                "style_profile": match.summary_provenance.get("style_profile") if match.summary_provenance else None,
                "similarity_to_current": match.summary_provenance.get("similarity_to_current") if match.summary_provenance else None,
                "specificity_score": match.summary_provenance.get("specificity_score") if match.summary_provenance else None,
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
) -> int:
    updated = _request_draft_model_input(
        base=base,
        head=head,
        item_id=item_id,
        reviewer=reviewer,
        model_provider=model_provider,
        tone=tone,
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
) -> int:
    prompt_preview = _build_draft_model_prompt(
        base=base,
        head=head,
        item_id=item_id,
        model_provider=model_provider,
        tone=tone,
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

    if args.command == "scan":
        base, head = _resolve_range(args)
        print(json.dumps(_run_scan_with_provider(base, head, persist=False, model_provider=args.model_provider), indent=2))
        return 0

    if args.command == "generate":
        base, head = _resolve_range(args)
        items = _run_scan_with_provider(base, head, persist=True, model_provider=args.model_provider)
        print(json.dumps(items, indent=2))
        return 0

    if args.command == "reconcile":
        base, head = _resolve_range(args)
        items = _run_scan_with_provider(base, head, persist=False, model_provider=args.model_provider)
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

    if args.command == "doctor" and args.doctor_command == "ollama":
        print(json.dumps(_run_ollama_doctor(parallel_probe=args.parallel_probe), indent=2))
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
            )
        if args.draft_command == "suggest":
            base, head = _resolve_range(args)
            if args.trace_suggestions:
                os.environ["VERLATO_TRACE_MODEL_CALLS"] = "1"
            if args.preview:
                suggestions = _preview_draft_model_suggestions(
                    base=base,
                    head=head,
                    item_id=args.id,
                    all_visible=args.all_visible,
                    count=args.count,
                    parallelism=args.parallelism,
                    model_provider=args.model_provider,
                    tone=args.tone,
                    no_cache=args.no_cache,
                    refresh_cache=args.refresh_cache,
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
            )
        if args.draft_command == "prompt":
            base, head = _resolve_range(args)
            return _run_draft_prompt(
                base=base,
                head=head,
                item_id=args.id,
                model_provider=args.model_provider,
                tone=args.tone,
            )
        base, head = _resolve_range(args)
        document = build_draft_document(
            base=base,
            head=head,
            version=args.version,
            include_confidence=args.include_confidence,
            include_provenance=args.include_provenance,
            model_provider=args.model_provider,
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
            result = DraftService(config=_load_config()).apply_document(
                document=document,
                target_name=args.target,
                target_path=args.file,
            )
            _trace_progress(
                "draft_apply_writing",
                "Wrote release notes",
                detail=result.target,
                current=1,
                total=1,
                unit="target",
            )
            print(json.dumps(result.to_dict(), indent=2))
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
