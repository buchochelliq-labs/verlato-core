from __future__ import annotations

import argparse
import json
import os
import re
import sys
from dataclasses import asdict
from datetime import date

from releasepilot.config import ReleasePilotConfig, load_config
from releasepilot.core.adapters import NullPullRequestAdapter, PullRequestAdapter, StandardGitAdapter
from releasepilot.core.classifier import Classifier
from releasepilot.core.collector import Collector
from releasepilot.core.drafts import DraftDocument, DraftService
from releasepilot.core.grouper import Grouper
from releasepilot.core.models import ChangeGroup, ChangeItem
from releasepilot.core.reconciler import Reconciler
from releasepilot.github.adapter import GitHubApiAdapter
from releasepilot.providers.base import ProviderRequest
from releasepilot.providers.factory import build_provider
from releasepilot.state.store import StateStore

MODEL_PROVIDER_CHOICES = ["stub", "mock", "ollama", "github-copilot"]


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="releasepilot")
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
    draft_suggest.add_argument("--reviewer")
    draft_suggest.add_argument("--model-provider", choices=MODEL_PROVIDER_CHOICES, help="Override the configured model provider.")
    draft_prompt = draft_sub.add_parser("prompt")
    draft_prompt.add_argument("--base")
    draft_prompt.add_argument("--head", default="HEAD")
    draft_prompt.add_argument("--from-latest-tag", action="store_true")
    draft_prompt.add_argument("--from-latest-published", action="store_true")
    draft_prompt.add_argument("--id", required=True)
    draft_prompt.add_argument("--model-provider", choices=MODEL_PROVIDER_CHOICES, help="Optional provider hint for the previewed prompt.")

    render = sub.add_parser("render")
    render.add_argument("--format", choices=["github-release", "changelog"], required=True)
    render.add_argument("--version", default="Unreleased")
    render.add_argument("--include-confidence", action="store_true")
    render.add_argument("--include-provenance", action="store_true")

    sub.add_parser("serve")

    state = sub.add_parser("state")
    state_sub = state.add_subparsers(dest="state_command", required=True)
    state_sub.add_parser("show")

    mark_published = state_sub.add_parser("mark-published")
    mark_published.add_argument("--version", required=True)
    mark_published.add_argument("--ids", nargs="*")
    mark_published.add_argument("--all-unpublished", action="store_true")

    return parser


def _build_store() -> StateStore:
    config = _load_config()
    path = os.getenv("RELEASEPILOT_STATE_PATH", config.state_path)
    return StateStore(path=path)


def _build_adapter() -> PullRequestAdapter:
    mode = os.getenv("RELEASEPILOT_PR_ADAPTER", "standard-git").lower()
    if mode == "none":
        return NullPullRequestAdapter()
    if mode == "github":
        return GitHubApiAdapter.from_env() or StandardGitAdapter()
    return StandardGitAdapter()


def run_scan(base: str, head: str, persist: bool, model_provider: str | None = None) -> list[dict]:
    return [asdict(i) for i in scan_change_items(base, head, persist, model_provider=model_provider)]


def detect_change_items(base: str, head: str, model_provider: str | None = None) -> list[ChangeItem]:
    config = _load_config()
    groups = _collect_groups(base, head)
    max_model_items = _resolve_max_model_items(config.provider.max_items_per_scan)
    provider = None
    if max_model_items < 0 or len(groups) <= max_model_items:
        provider = build_provider(config.provider, mode=model_provider)
    classifier = Classifier(provider=provider)
    return [item for group in groups for item in classifier.classify(group, base, head)]


def _collect_groups(base: str, head: str):
    collector = Collector(pr_adapter=_build_adapter())
    grouper = Grouper()
    commits = collector.collect_commits(base, head)
    prs_by_commit = collector.map_commits_to_prs(commits)
    return grouper.group(commits, prs_by_commit)


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
    existing = store.load().items
    unpublished = reconciler.unpublished(existing, detected)
    if persist and unpublished:
        store.append_new(unpublished)
    return unpublished


def _detect_change_items_with_provider(base: str, head: str, model_provider: str | None) -> list[ChangeItem]:
    if model_provider:
        return detect_change_items(base, head, model_provider=model_provider)
    return detect_change_items(base, head)


def _run_scan_with_provider(base: str, head: str, persist: bool, model_provider: str | None) -> list[dict]:
    if model_provider:
        return run_scan(base, head, persist=persist, model_provider=model_provider)
    return run_scan(base, head, persist=persist)


def _resolve_suggest_model_provider(model_provider: str | None) -> str | None:
    if model_provider:
        return model_provider
    config = _load_config()
    configured_name = os.getenv("RELEASEPILOT_MODEL_PROVIDER", config.provider.name).strip().lower()
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
    items = _merge_review_state(_detect_change_items_with_provider(base, head, model_provider))
    return DraftService(config=config).build_document(
        items=items,
        base_ref=base,
        head_ref=head,
        version=version,
        include_confidence=include_confidence,
        include_provenance=include_provenance,
    )


def _latest_published_version() -> str | None:
    state = _build_store().load()
    published_versions = [item.published_in for item in state.items if item.published and item.published_in]
    if not published_versions:
        return None
    # Preserve recency by taking the last published marker observed in state history.
    return published_versions[-1]


def _load_config() -> ReleasePilotConfig:
    return load_config()


def _resolve_max_model_items(configured: int) -> int:
    raw = os.getenv("RELEASEPILOT_MODEL_MAX_ITEMS")
    if raw is None:
        return configured
    try:
        return int(raw)
    except ValueError:
        return configured


def _resolve_suggestion_count(configured: int, requested: int | None = None) -> int:
    raw = str(requested) if requested is not None else os.getenv("RELEASEPILOT_SUGGESTION_COUNT")
    value = configured
    if raw is not None:
        try:
            value = int(raw)
        except ValueError:
            value = configured
    return max(1, min(value, 5))


def _merge_review_state(items: list[ChangeItem]) -> list[ChangeItem]:
    existing = _build_store().load().items
    by_checksum = {(item.checksum_version, item.checksum): item for item in existing}
    by_id = {item.id: item for item in existing}
    merged: list[ChangeItem] = []
    for item in items:
        persisted = by_checksum.get((item.checksum_version, item.checksum)) or by_id.get(item.id)
        if persisted:
            item.status = persisted.status
            item.reviewed_by = persisted.reviewed_by
            item.reviewed_at = persisted.reviewed_at
            item.edited_summary = persisted.edited_summary
            item.hidden = persisted.hidden
            for signal in persisted.evidence.commit_signals:
                if signal.startswith("provider-summary:") and signal not in item.evidence.commit_signals:
                    item.evidence.commit_signals.append(signal)
        merged.append(item)
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
) -> ChangeItem:
    if status is None and summary is None and reviewer is None:
        raise SystemExit("Provide at least one review update: --status, --summary, or --reviewer")

    candidates = _detect_change_items_with_provider(base, head, model_provider)
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
    return _persist_review_update(candidate=match or candidate, status=status, summary=summary, reviewer=reviewer)


def _run_draft_review(
    *,
    base: str,
    head: str,
    item_id: str,
    status: str | None,
    summary: str | None,
    reviewer: str | None,
    model_provider: str | None = None,
) -> int:
    updated = _update_draft_review(
        base=base,
        head=head,
        item_id=item_id,
        status=status,
        summary=summary,
        reviewer=reviewer,
        model_provider=model_provider,
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
) -> ChangeItem:
    effective_model_provider = _resolve_suggest_model_provider(model_provider)
    target_group = _find_group_for_item_id(base, head, item_id)

    config = _load_config()
    provider_classifier = Classifier(provider=build_provider(config.provider, mode=effective_model_provider))
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

    return _persist_review_update(
        candidate=candidate,
        status="edited",
        summary=candidate.content.summary,
        reviewer=reviewer,
    )


def _preview_draft_model_suggestions(
    *,
    base: str,
    head: str,
    item_id: str | None,
    all_visible: bool,
    count: int | None,
    model_provider: str | None = None,
) -> dict:
    if all_visible == bool(item_id):
        raise SystemExit("Provide exactly one suggestion target: --id or --all-visible")

    config = _load_config()
    suggestion_count = _resolve_suggestion_count(config.suggestion_count, requested=count)
    suggestion_count_source = (
        "cli" if count is not None else "env" if os.getenv("RELEASEPILOT_SUGGESTION_COUNT") else "config"
    )
    effective_model_provider = _resolve_suggest_model_provider(model_provider)
    groups = _collect_groups(base, head)
    baseline_classifier = Classifier()
    _trace_model_suggest_stage(
        "target-selection",
        f"base={base} head={head} groups={len(groups)} item_id={item_id or 'none'}",
    )

    targets = []
    for group in groups:
        baseline_item = baseline_classifier.classify(group, base, head)[0]
        if item_id and baseline_item.id != item_id:
            continue
        targets.append((group, baseline_item))

    if item_id and not targets:
        raise SystemExit(f"Could not find draft item {item_id} for range {base}..{head}")

    merged = _merge_review_state([item for _group, item in targets])
    merged_by_id = {item.id: item for item in merged}
    if all_visible:
        visible_ids = {item.id for item in merged if not item.hidden}
        targets = [(group, merged_by_id.get(item.id, item)) for group, item in targets if item.id in visible_ids]
    else:
        targets = [(group, merged_by_id.get(item.id, item)) for group, item in targets]

    max_model_items = _resolve_max_model_items(config.provider.max_items_per_scan)
    if all_visible and max_model_items >= 0 and len(targets) > max_model_items:
        raise SystemExit(
            f"Refusing to request model suggestions for {len(targets)} visible item(s); "
            f"fanout guard is {max_model_items}. Suggest one item at a time or raise RELEASEPILOT_MODEL_MAX_ITEMS."
        )
    _trace_model_suggest_plan(
        base=base,
        head=head,
        target_count=len(targets),
        suggestion_count=suggestion_count,
        suggestion_count_source=suggestion_count_source,
        provider=effective_model_provider or "configured",
        all_visible=all_visible,
    )

    provider = build_provider(config.provider, mode=effective_model_provider)
    provider_classifier = Classifier(provider=provider)

    items = []
    for group, baseline_item in targets:
        diff = provider_classifier.diff_builder.build(group.pr.files if group.pr else group.files)
        hunk_count = sum(len(hunks) for hunks in diff.representative_hunks.values())
        file_count = len(diff.file_statuses)
        _trace_model_suggest_stage(
            "evidence-built",
            f"item_id={baseline_item.id} files={file_count} hunks={hunk_count}",
        )
        prompt = provider_classifier._build_summary_prompt(
            title=baseline_item.content.title,
            labels=[label.lower() for label in (group.pr.labels if group.pr else [])],
            pr_body=group.pr.body if group.pr else "",
            diff=diff,
            commits=group.commits,
            suggestion_count=suggestion_count,
            kind=baseline_item.classification.kind,
            audience=baseline_item.classification.audience,
        )
        evidence = {
            "title": baseline_item.content.title,
            "item_id": baseline_item.id,
            "suggestion_count": suggestion_count,
            "diff_summary": diff.summary,
            "representative_hunks": diff.representative_hunks,
            "file_count": file_count,
            "hunk_count": hunk_count,
        }
        _trace_model_suggest_stage("prompt-built", f"item_id={baseline_item.id} prompt_chars={len(prompt)}")
        _trace_model_call_start("suggest", baseline_item.content.title, effective_model_provider or "configured")
        response = provider.summarize(ProviderRequest(task="suggest", prompt=prompt, evidence=evidence))
        _trace_model_call_result("suggest", baseline_item.content.title, response.model, response.confidence)
        if provider_classifier._is_provider_error(response.content):
            raise SystemExit(
                f"No model suggestions were produced for draft item {baseline_item.id}: {response.content}"
            )
        suggestions = _parse_suggestion_variants(
            response.content,
            suggestion_count,
            baseline_item,
            group,
            trace_title=baseline_item.content.title,
            trace_model=response.model,
        )
        _trace_model_suggestion_decision(
            title=baseline_item.content.title,
            model=response.model,
            parsed_count=len(suggestions),
            requested_count=suggestion_count,
        )
        if not suggestions:
            raise SystemExit(f"No model suggestions were produced for draft item {baseline_item.id}.")
        items.append(
            {
                "item_id": baseline_item.id,
                "title": baseline_item.content.title,
                "current_summary": baseline_item.edited_summary or baseline_item.content.summary,
                "provider": response.model,
                "suggestions": suggestions,
            }
        )

    return {
        "base_ref": base,
        "head_ref": head,
        "provider": effective_model_provider or "configured",
        "count": suggestion_count,
        "items": items,
    }


def _parse_suggestion_variants(
    content: str,
    count: int,
    item: ChangeItem,
    group: ChangeGroup,
    *,
    trace_title: str | None = None,
    trace_model: str | None = None,
) -> list[str]:
    normalized = (content or "").strip()
    if not normalized:
        _trace_model_suggestion_filter("raw-response", "empty", trace_title, trace_model)
        return []
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
    suggestions: list[str] = []
    classifier = Classifier()
    for line in lines:
        rejection_reason = classifier._summary_rejection_reason(line, item.content.title, group)
        if rejection_reason:
            _trace_model_suggestion_filter(
                "reject",
                f"reason={rejection_reason} candidate={line}",
                trace_title,
                trace_model,
            )
            continue
        if line in suggestions:
            _trace_model_suggestion_filter("reject", f"reason=duplicate candidate={line}", trace_title, trace_model)
            continue
        suggestions.append(line)
        _trace_model_suggestion_filter("accept", f"index={len(suggestions)} candidate={line}", trace_title, trace_model)
        if len(suggestions) >= count:
            break
    return suggestions


def _trace_model_enabled() -> bool:
    return os.getenv("RELEASEPILOT_TRACE_MODEL_CALLS") == "1"


def _trace_model_call_start(task: str, title: str, provider: str) -> None:
    if not _trace_model_enabled():
        return
    print(f"[releasepilot:model] starting {task} provider={provider} title={title}", file=sys.stderr, flush=True)


def _trace_model_suggest_plan(
    *,
    base: str,
    head: str,
    target_count: int,
    suggestion_count: int,
    suggestion_count_source: str,
    provider: str,
    all_visible: bool,
) -> None:
    if not _trace_model_enabled():
        return
    scope = "all-visible" if all_visible else "single-item"
    print(
        f"[releasepilot:model] planning suggest provider={provider} scope={scope} base={base} head={head} "
        f"targets={target_count} suggestions_per_target={suggestion_count} count_source={suggestion_count_source}",
        file=sys.stderr,
        flush=True,
    )


def _trace_model_call_result(task: str, title: str, model: str, confidence: float) -> None:
    if not _trace_model_enabled():
        return
    print(
        f"[releasepilot:model] finished {task} model={model} confidence={confidence:.2f} title={title}",
        file=sys.stderr,
        flush=True,
    )


def _trace_model_suggestion_decision(*, title: str, model: str, parsed_count: int, requested_count: int) -> None:
    if not _trace_model_enabled():
        return
    print(
        f"[releasepilot:model] decision suggestions parsed={parsed_count} requested={requested_count} "
        f"model={model} title={title}",
        file=sys.stderr,
        flush=True,
    )


def _trace_model_suggest_stage(stage: str, detail: str) -> None:
    if not _trace_model_enabled():
        return
    print(f"[releasepilot:model] suggest stage={stage} {detail}", file=sys.stderr, flush=True)


def _trace_model_suggestion_filter(stage: str, detail: str, title: str | None, model: str | None) -> None:
    if not _trace_model_enabled():
        return
    title_part = f" title={title}" if title else ""
    model_part = f" model={model}" if model else ""
    print(f"[releasepilot:model] suggestion {stage} {detail}{model_part}{title_part}", file=sys.stderr, flush=True)


def _build_draft_model_prompt(
    *,
    base: str,
    head: str,
    item_id: str,
    model_provider: str | None = None,
) -> dict[str, str]:
    effective_model_provider = _resolve_suggest_model_provider(model_provider)
    target_group = _find_group_for_item_id(base, head, item_id)
    classifier = Classifier()
    draft_item = classifier.classify(target_group, base, head)[0]
    diff = classifier.diff_builder.build(target_group.pr.files if target_group.pr else target_group.files)
    prompt = classifier._build_summary_prompt(
        title=draft_item.content.title,
        labels=[label.lower() for label in (target_group.pr.labels if target_group.pr else [])],
        pr_body=target_group.pr.body if target_group.pr else "",
        diff=diff,
        commits=target_group.commits,
    )
    return {
        "item_id": draft_item.id,
        "title": draft_item.content.title,
        "provider": effective_model_provider or "configured",
        "prompt": prompt,
    }


def _persist_review_update(
    *,
    candidate: ChangeItem,
    status: str | None,
    summary: str | None,
    reviewer: str | None,
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
        match.edited_summary = normalized_summary or None
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
) -> int:
    updated = _request_draft_model_input(
        base=base,
        head=head,
        item_id=item_id,
        reviewer=reviewer,
        model_provider=model_provider,
    )
    print(json.dumps(updated.to_dict(), indent=2))
    return 0


def _run_draft_prompt(
    *,
    base: str,
    head: str,
    item_id: str,
    model_provider: str | None = None,
) -> int:
    prompt_preview = _build_draft_model_prompt(
        base=base,
        head=head,
        item_id=item_id,
        model_provider=model_provider,
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
        from releasepilot.mcp.server import run_mcp_stdio

        try:
            return run_mcp_stdio()
        except RuntimeError as exc:
            raise SystemExit(str(exc)) from exc

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
            )
        if args.draft_command == "suggest":
            base, head = _resolve_range(args)
            if args.preview:
                suggestions = _preview_draft_model_suggestions(
                    base=base,
                    head=head,
                    item_id=args.id,
                    all_visible=args.all_visible,
                    count=args.count,
                    model_provider=args.model_provider,
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
            )
        if args.draft_command == "prompt":
            base, head = _resolve_range(args)
            return _run_draft_prompt(
                base=base,
                head=head,
                item_id=args.id,
                model_provider=args.model_provider,
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
            print(document.rendered["github-release"])
            return 0
        if args.draft_command == "json":
            print(json.dumps(document.to_dict(), indent=2))
            return 0
        if args.draft_command == "apply":
            result = DraftService(config=_load_config()).apply_document(
                document=document,
                target_name=args.target,
                target_path=args.file,
            )
            print(json.dumps(result.to_dict(), indent=2))
            return 0

    if args.command == "state" and args.state_command == "show":
        print(json.dumps({"items": [asdict(i) for i in _build_store().load().items]}, indent=2))
        return 0

    if args.command == "state" and args.state_command == "mark-published":
        if not args.all_unpublished and not args.ids:
            raise SystemExit("Provide --all-unpublished or one/more --ids values")
        return _run_mark_published(version=args.version, ids=args.ids, all_unpublished=args.all_unpublished)

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
