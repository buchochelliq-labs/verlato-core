from __future__ import annotations

from collections import defaultdict

from releasepilot.core.conventional import parse_conventional_commit
from releasepilot.core.models import ChangeGroup, Commit, PullRequest


class Grouper:
    def group(self, commits: list[Commit], prs_by_commit: dict[str, PullRequest]) -> list[ChangeGroup]:
        grouped: dict[int, list[Commit]] = defaultdict(list)
        no_pr: list[Commit] = []
        for commit in commits:
            pr = prs_by_commit.get(commit.sha)
            if pr:
                grouped[pr.number].append(commit)
            else:
                no_pr.append(commit)

        results: list[ChangeGroup] = []
        for pr_number, pr_commits in grouped.items():
            pr = prs_by_commit[pr_commits[0].sha]
            results.append(ChangeGroup(pr=pr, commits=pr_commits))

        results.extend(self._group_non_pr_commits(no_pr))

        return results

    def _group_non_pr_commits(self, commits: list[Commit]) -> list[ChangeGroup]:
        if not commits:
            return []

        groups: list[ChangeGroup] = []
        bucket: list[Commit] = []
        bucket_key: tuple[str, ...] | None = None

        for commit in commits:
            key = self._non_pr_group_key(commit)
            if not bucket:
                bucket = [commit]
                bucket_key = key
                continue
            if bucket_key == key and key[1] != "misc":
                bucket.append(commit)
                continue
            groups.append(ChangeGroup(pr=None, commits=bucket))
            bucket = [commit]
            bucket_key = key

        if bucket:
            groups.append(ChangeGroup(pr=None, commits=bucket))
        return groups

    def _non_pr_group_key(self, commit: Commit) -> tuple[str, ...]:
        parsed = parse_conventional_commit(commit.message)
        if parsed:
            scope = parsed.scope or "*"
            return ("conventional", parsed.type.lower(), scope.lower(), commit.author.lower())

        subject = commit.message.strip().splitlines()[0].lower() if commit.message.strip() else ""
        category = "misc"
        for prefix in ("docs", "test", "chore", "refactor", "ci", "build", "fix", "feat"):
            if subject.startswith(prefix):
                category = prefix
                break
        return ("heuristic", category, commit.author.lower())
