from __future__ import annotations

import subprocess

from releasepilot.core.adapters import PullRequestAdapter
from releasepilot.core.models import Commit, PullRequest


class Collector:
    def __init__(self, pr_adapter: PullRequestAdapter) -> None:
        self.pr_adapter = pr_adapter

    def collect_commits(self, base: str, head: str) -> list[Commit]:
        cmd = ["git", "log", "--pretty=format:%H%x09%s%x09%an", f"{base}..{head}"]
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        commits: list[Commit] = []
        for line in result.stdout.splitlines():
            sha, message, author = (line.split("\t", 2) + [""])[:3]
            commits.append(Commit(sha=sha, message=message, author=author))
        return list(reversed(commits))

    def latest_tag(self, head: str = "HEAD") -> str | None:
        cmd = ["git", "describe", "--tags", "--abbrev=0", head]
        result = subprocess.run(cmd, check=False, capture_output=True, text=True)
        if result.returncode != 0:
            return None
        tag = result.stdout.strip()
        return tag or None

    def map_commits_to_prs(self, commits: list[Commit]) -> dict[str, PullRequest]:
        return self.pr_adapter.map_commits_to_prs(commits)
