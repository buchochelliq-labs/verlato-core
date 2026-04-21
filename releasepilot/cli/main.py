from __future__ import annotations

import argparse
import json
from dataclasses import asdict

from releasepilot.core.classifier import Classifier
from releasepilot.core.collector import Collector
from releasepilot.core.generator import MarkdownGenerator
from releasepilot.core.grouper import Grouper
from releasepilot.core.reconciler import Reconciler
from releasepilot.github.adapter import StubGitHubAdapter
from releasepilot.state.store import StateStore


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="releasepilot")
    sub = parser.add_subparsers(dest="command", required=True)

    for name in ("scan", "generate"):
        p = sub.add_parser(name)
        p.add_argument("--base", required=True)
        p.add_argument("--head", required=True)

    render = sub.add_parser("render")
    render.add_argument("--format", choices=["github-release", "changelog"], required=True)
    render.add_argument("--version", default="Unreleased")

    state = sub.add_parser("state")
    state_sub = state.add_subparsers(dest="state_command", required=True)
    state_sub.add_parser("show")

    return parser


def run_scan(base: str, head: str, persist: bool) -> list[dict]:
    store = StateStore()
    collector = Collector(github=StubGitHubAdapter())
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


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)

    if args.command == "scan":
        print(json.dumps(run_scan(args.base, args.head, persist=False), indent=2))
        return 0

    if args.command == "generate":
        items = run_scan(args.base, args.head, persist=True)
        print(json.dumps(items, indent=2))
        return 0

    if args.command == "render":
        state = StateStore().load().items
        generator = MarkdownGenerator()
        if args.format == "github-release":
            print(generator.render_github_release(state, version=args.version))
        else:
            print(generator.render_changelog_entry(state, version=args.version))
        return 0

    if args.command == "state" and args.state_command == "show":
        print(json.dumps({"items": [asdict(i) for i in StateStore().load().items]}, indent=2))
        return 0

    return 1


if __name__ == "__main__":
    raise SystemExit(main())
