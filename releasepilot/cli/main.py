from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict
from datetime import date

from releasepilot.config import ReleasePilotConfig, load_config
from releasepilot.core.adapters import NullPullRequestAdapter, PullRequestAdapter, StandardGitAdapter
from releasepilot.core.classifier import Classifier
from releasepilot.core.collector import Collector
from releasepilot.core.drafts import DraftDocument, DraftService
from releasepilot.core.grouper import Grouper
from releasepilot.core.models import ChangeItem
from releasepilot.core.reconciler import Reconciler
from releasepilot.github.adapter import GitHubApiAdapter
from releasepilot.providers.factory import build_provider
from releasepilot.state.store import StateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="releasepilot")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("scan", "generate", "reconcile"):
        p = sub.add_parser(name)
        p.add_argument("--base")
        p.add_argument("--head", default="HEAD")
        p.add_argument("--from-latest-tag", action="store_true")
        p.add_argument("--from-latest-published", action="store_true")

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

    render = sub.add_parser("render")
    render.add_argument("--format", choices=["github-release", "changelog"], required=True)
    render.add_argument("--version", default="Unreleased")
    render.add_argument("--include-confidence", action="store_true")
    render.add_argument("--include-provenance", action="store_true")

    serve = sub.add_parser("serve")
    serve.add_argument("--transport", choices=["stdio"], default="stdio")

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


def run_scan(base: str, head: str, persist: bool) -> list[dict]:
    return [asdict(i) for i in scan_change_items(base, head, persist)]


def detect_change_items(base: str, head: str) -> list[ChangeItem]:
    collector = Collector(pr_adapter=_build_adapter())
    grouper = Grouper()
    classifier = Classifier(provider=build_provider())

    commits = collector.collect_commits(base, head)
    prs_by_commit = collector.map_commits_to_prs(commits)
    groups = grouper.group(commits, prs_by_commit)
    return [item for group in groups for item in classifier.classify(group, base, head)]


def scan_change_items(base: str, head: str, persist: bool) -> list[ChangeItem]:
    store = _build_store()
    reconciler = Reconciler()

    detected = detect_change_items(base, head)
    existing = store.load().items
    unpublished = reconciler.unpublished(existing, detected)
    if persist and unpublished:
        store.append_new(unpublished)
    return unpublished


def build_draft_document(
    *,
    base: str,
    head: str,
    version: str,
    include_confidence: bool = False,
    include_provenance: bool = False,
) -> DraftDocument:
    config = _load_config()
    items = _merge_review_state(detect_change_items(base, head))
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
) -> ChangeItem:
    if status is None and summary is None and reviewer is None:
        raise SystemExit("Provide at least one review update: --status, --summary, or --reviewer")

    candidates = detect_change_items(base, head)
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


def _run_draft_review(
    *,
    base: str,
    head: str,
    item_id: str,
    status: str | None,
    summary: str | None,
    reviewer: str | None,
) -> int:
    updated = _update_draft_review(
        base=base,
        head=head,
        item_id=item_id,
        status=status,
        summary=summary,
        reviewer=reviewer,
    )
    print(json.dumps(updated.to_dict(), indent=2))
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
        print(json.dumps(run_scan(base, head, persist=False), indent=2))
        return 0

    if args.command == "generate":
        base, head = _resolve_range(args)
        items = run_scan(base, head, persist=True)
        print(json.dumps(items, indent=2))
        return 0

    if args.command == "reconcile":
        base, head = _resolve_range(args)
        items = run_scan(base, head, persist=False)
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
        if args.transport != "stdio":
            raise SystemExit(f"Unsupported transport: {args.transport}")
        from releasepilot.server import ReleasePilotServer

        return ReleasePilotServer().serve_stdio()

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
            )
        base, head = _resolve_range(args)
        document = build_draft_document(
            base=base,
            head=head,
            version=args.version,
            include_confidence=args.include_confidence,
            include_provenance=args.include_provenance,
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
