from __future__ import annotations

import subprocess

from verlato.core.adapters import PullRequestAdapter, parse_git_show_changed_files
from verlato.core.models import ChangedFile, Commit, PullRequest

_COMMIT_PATCH_MARKER = "__VERLATO_COMMIT_PATCH__"


class Collector:
    def __init__(self, pr_adapter: PullRequestAdapter) -> None:
        self.pr_adapter = pr_adapter

    def collect_commits(self, base: str, head: str) -> list[Commit]:
        cmd = ["git", "log", "--first-parent", "--pretty=format:%H%x09%s%x09%an", f"{base}..{head}"]
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        commits: list[Commit] = []
        for line in result.stdout.splitlines():
            sha, message, author = (line.split("\t", 2) + [""])[:3]
            commits.append(Commit(sha=sha, message=message, author=author))
        patch_by_commit = self.changed_files_for_commits([commit.sha for commit in commits])
        for commit in commits:
            commit.files = patch_by_commit.get(commit.sha, [])
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

    def changed_files_for_commit(self, commit_sha: str) -> list[ChangedFile]:
        cmd = ["git", "show", "--first-parent", "--format=", "--find-renames", "--name-status", "--patch", commit_sha]
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return parse_git_show_changed_files(result.stdout)

    def changed_files_for_commits(self, commit_shas: list[str]) -> dict[str, list[ChangedFile]]:
        if not commit_shas:
            return {}
        cmd = [
            "git",
            "show",
            "--first-parent",
            f"--format={_COMMIT_PATCH_MARKER}%H",
            "--find-renames",
            "--name-status",
            "--patch",
            *commit_shas,
        ]
        result = subprocess.run(cmd, check=True, capture_output=True, text=True)
        return _parse_changed_files_by_commit(result.stdout)


def _parse_changed_files_by_commit(raw: str) -> dict[str, list[ChangedFile]]:
    files_by_commit: dict[str, list[ChangedFile]] = {}
    current_sha: str | None = None
    current_lines: list[str] = []

    for line in raw.splitlines():
        if line.startswith(_COMMIT_PATCH_MARKER):
            if current_sha is not None:
                files_by_commit[current_sha] = parse_git_show_changed_files("\n".join(current_lines))
            current_sha = line.removeprefix(_COMMIT_PATCH_MARKER).strip()
            current_lines = []
            continue
        current_lines.append(line)

    if current_sha is not None:
        files_by_commit[current_sha] = parse_git_show_changed_files("\n".join(current_lines))
    return files_by_commit
