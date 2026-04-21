from __future__ import annotations

import subprocess

from releasepilot.core.models import Commit, PullRequest
from releasepilot.github.adapter import GitHubAdapter


class Collector:
    def __init__(self, github: GitHubAdapter) -> None:
        self.github = github

    def collect_commits(self, base: str, head: str) -> list[Commit]:
        cmd = ["git", "log", "--pretty=format:%H%x09%s%x09%an", f"{base}..{head}"]
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        commits: list[Commit] = []
        for line in result.stdout.splitlines():
            sha, message, author = (line.split("\t", 2) + [""])[:3]
            commits.append(Commit(sha=sha, message=message, author=author))
        return list(reversed(commits))

    def map_commits_to_prs(self, commits: list[Commit]) -> dict[str, PullRequest]:
        mapping: dict[str, PullRequest] = {}
        for commit in commits:
            pr = self.github.find_pr_for_commit(commit.sha)
            if pr:
                mapping[commit.sha] = pr
        return mapping
