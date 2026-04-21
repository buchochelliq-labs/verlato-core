from __future__ import annotations

from collections import defaultdict

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

        for commit in no_pr:
            results.append(ChangeGroup(pr=None, commits=[commit]))

        return results
