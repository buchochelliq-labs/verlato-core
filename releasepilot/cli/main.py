from __future__ import annotations

import argparse
import json
import os
from dataclasses import asdict

from releasepilot.core.adapters import NullPullRequestAdapter, PullRequestAdapter, StandardGitAdapter
from releasepilot.core.classifier import Classifier
from releasepilot.core.collector import Collector
from releasepilot.core.generator import MarkdownGenerator
from releasepilot.core.grouper import Grouper
from releasepilot.core.reconciler import Reconciler
from releasepilot.github.adapter import GitHubApiAdapter
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

    render = sub.add_parser("render")
    render.add_argument("--format", choices=["github-release", "changelog"], required=True)
    render.add_argument("--version", default="Unreleased")
    render.add_argument("--include-confidence", action="store_true")
    render.add_argument("--include-provenance", action="store_true")

    state = sub.add_parser("state")
    state_sub = state.add_subparsers(dest="state_command", required=True)
    state_sub.add_parser("show")

    mark_published = state_sub.add_parser("mark-published")
    mark_published.add_argument("--version", required=True)
    mark_published.add_argument("--ids", nargs="*")
    mark_published.add_argument("--all-unpublished", action="store_true")

    return parser


def _build_store() -> StateStore:
    path = os.getenv("RELEASEPILOT_STATE_PATH", ".releasepilot_state.json")
    return StateStore(path=path)


def _build_adapter() -> PullRequestAdapter:
    mode = os.getenv("RELEASEPILOT_PR_ADAPTER", "standard-git").lower()
    if mode == "none":
        return NullPullRequestAdapter()
    if mode == "github":
        return GitHubApiAdapter.from_env() or StandardGitAdapter()
    return StandardGitAdapter()


def run_scan(base: str, head: str, persist: bool) -> list[dict]:
    store = _build_store()
    collector = Collector(pr_adapter=_build_adapter())
    grouper = Grouper()
    classifier = Classifier()
    reconciler = Reconciler()

    commits = collector.collect_commits(base, head)
    prs_by_commit = collector.map_commits_to_prs(commits)
    groups = grouper.group(commits, prs_by_commit)
    detected = [item for group in groups for item in classifier.classify(group, base, head)]

    existing = store.load().items
    unpublished = reconciler.unpublished(existing, detected)
    if persist and unpublished:
        store.append_new(unpublished)
    return [asdict(i) for i in unpublished]


def _latest_published_version() -> str | None:
    state = _build_store().load()
    published_versions = [item.published_in for item in state.items if item.published and item.published_in]
    if not published_versions:
        return None
    # Preserve recency by taking the last published marker observed in state history.
    return published_versions[-1]


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
        generator = MarkdownGenerator()
        if args.format == "github-release":
            print(
                generator.render_github_release(
                    state,
                    version=args.version,
                    include_confidence=args.include_confidence,
                    include_provenance=args.include_provenance,
                )
            )
        else:
            print(
                generator.render_changelog_entry(
                    state,
                    version=args.version,
                    include_confidence=args.include_confidence,
                    include_provenance=args.include_provenance,
                )
            )
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
