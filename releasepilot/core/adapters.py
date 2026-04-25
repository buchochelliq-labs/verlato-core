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
        raw = self._git(["show", "--format=", "--find-renames", "--name-status", "--patch", commit_sha])
        return parse_git_show_changed_files(raw)

    def _git(self, args: list[str]) -> str:
        result = subprocess.run(["git", *args], cwd=self.repo_path, check=True, capture_output=True, text=True)
        return result.stdout


def parse_git_show_changed_files(raw: str) -> list[ChangedFile]:
    files: list[ChangedFile] = []
    by_path: dict[str, ChangedFile] = {}
    patch_by_path: dict[str, list[str]] = {}
    current_path: str | None = None

    for line in raw.splitlines():
        if line.startswith("diff --git "):
            current_path = _path_from_diff_header(line)
            if current_path:
                patch_by_path.setdefault(current_path, [line])
            continue

        if current_path:
            patch_by_path.setdefault(current_path, []).append(line)
            if line.startswith("+++ b/"):
                current_path = line.removeprefix("+++ b/")
                patch_by_path.setdefault(current_path, []).append(line)
            continue

        parsed_status = _parse_name_status_line(line)
        if parsed_status:
            status, path = parsed_status
            changed = ChangedFile(path=path, status=status)
            files.append(changed)
            by_path[path] = changed

    for path, patch_lines in patch_by_path.items():
        changed = by_path.get(path)
        if changed is None:
            changed = ChangedFile(path=path, status="modified")
            files.append(changed)
            by_path[path] = changed
        changed.patch = "\n".join(patch_lines).strip()

    return files


def _parse_name_status_line(line: str) -> tuple[str, str] | None:
    if not line.strip() or "\t" not in line:
        return None
    parts = line.split("\t")
    status_code = parts[0]
    if not status_code or status_code.startswith("@@"):
        return None
    path = parts[-1]
    normalized = {
        "A": "added",
        "M": "modified",
        "D": "removed",
        "R": "renamed",
        "C": "copied",
    }.get(status_code[:1])
    if normalized is None:
        return None
    return normalized, path


def _path_from_diff_header(line: str) -> str | None:
    parts = line.split()
    if len(parts) < 4:
        return None
    path = parts[3]
    if path.startswith("b/"):
        return path[2:]
    return path
