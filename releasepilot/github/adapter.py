from __future__ import annotations

from abc import ABC, abstractmethod

from releasepilot.core.models import PullRequest


class GitHubAdapter(ABC):
    @abstractmethod
    def find_pr_for_commit(self, commit_sha: str) -> PullRequest | None:
        raise NotImplementedError


class StubGitHubAdapter(GitHubAdapter):
    def __init__(self, pr_by_commit: dict[str, PullRequest] | None = None) -> None:
        self.pr_by_commit = pr_by_commit or {}

    def find_pr_for_commit(self, commit_sha: str) -> PullRequest | None:
        return self.pr_by_commit.get(commit_sha)
