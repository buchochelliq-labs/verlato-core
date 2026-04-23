from __future__ import annotations

import re
import subprocess
from abc import ABC, abstractmethod

from releasepilot.core.models import ChangedFile, Commit, PullRequest

_PR_PATTERNS = [
    re.compile(r"\(#(?P<num>\d+)\)"),
    re.compile(r"pull request #(?P<num>\d+)", re.IGNORECASE),
    re.compile(r"merge pr #(?P<num>\d+)", re.IGNORECASE),
]


class PullRequestAdapter(ABC):
    @abstractmethod
    def find_pr_for_commit(self, commit_sha: str) -> PullRequest | None:
        raise NotImplementedError

    def map_commits_to_prs(self, commits: list[Commit]) -> dict[str, PullRequest]:
        mapping: dict[str, PullRequest] = {}
        for commit in commits:
            pr = self.find_pr_for_commit(commit.sha)
            if pr:
                mapping[commit.sha] = pr
        return mapping


class NullPullRequestAdapter(PullRequestAdapter):
    def find_pr_for_commit(self, commit_sha: str) -> PullRequest | None:
        return None


class StandardGitAdapter(PullRequestAdapter):
    """Portable local-git adapter that infers PR context from commit messages."""

    def __init__(self, repo_path: str = ".") -> None:
        self.repo_path = repo_path
        self._commit_cache: dict[str, PullRequest | None] = {}

    def find_pr_for_commit(self, commit_sha: str) -> PullRequest | None:
        if commit_sha in self._commit_cache:
            return self._commit_cache[commit_sha]
        subject = self._git(["show", "-s", "--format=%s", commit_sha]).strip()
        pr_number = self._extract_pr_number(subject)
        if pr_number is None:
            self._commit_cache[commit_sha] = None
            return None
        files = self._git_changed_files(commit_sha)
        pr = PullRequest(number=pr_number, title=subject, files=files)
        self._commit_cache[commit_sha] = pr
        return pr

    def _extract_pr_number(self, subject: str) -> int | None:
        for pattern in _PR_PATTERNS:
            match = pattern.search(subject)
            if match:
                return int(match.group("num"))
        return None

    def _git_changed_files(self, commit_sha: str) -> list[ChangedFile]:
        raw = self._git(["show", "--pretty=format:", "--name-status", commit_sha])
        files: list[ChangedFile] = []
        for line in raw.splitlines():
            if not line.strip() or "\t" not in line:
                continue
            status, path = line.split("\t", 1)
            normalized = {
                "A": "added",
                "M": "modified",
                "D": "removed",
                "R": "renamed",
            }.get(status[:1], "modified")
            files.append(ChangedFile(path=path, status=normalized))
        return files

    def _git(self, args: list[str]) -> str:
        result = subprocess.run(["git", *args], cwd=self.repo_path, check=True, capture_output=True, text=True)
        return result.stdout
