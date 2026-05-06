from __future__ import annotations

import subprocess
from pathlib import Path

from verlato.core.adapters import StandardGitAdapter
from verlato.core.collector import Collector
from verlato.core.models import Commit


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _commit(repo: Path, path: str, content: str, message: str) -> None:
    target = repo / path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content)
    _git(repo, "add", path)
    _git(repo, "commit", "-m", message)


def test_collector_batches_changed_file_lookup(monkeypatch, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")
    _commit(repo, "README.md", "hello\n", "docs: initial")
    _git(repo, "tag", "v0.1.0")
    _commit(repo, "src/app.py", "print('one')\n", "feat: add app")
    _commit(repo, "src/app.py", "print('two')\n", "fix: update app")

    calls: list[list[str]] = []
    original_run = subprocess.run

    def recording_run(cmd, *args, **kwargs):
        if isinstance(cmd, list) and cmd[:2] == ["git", "show"]:
            calls.append(cmd)
        return original_run(cmd, *args, **kwargs)

    monkeypatch.chdir(repo)
    monkeypatch.setattr(subprocess, "run", recording_run)

    commits = Collector(pr_adapter=StandardGitAdapter()).collect_commits("v0.1.0", "HEAD")

    assert [commit.message for commit in commits] == ["feat: add app", "fix: update app"]
    assert all(commit.files for commit in commits)
    assert len(calls) == 1
    assert calls[0][-2:] == [commits[1].sha, commits[0].sha]


def test_standard_git_adapter_reuses_commit_metadata_without_git_show(monkeypatch):
    adapter = StandardGitAdapter()
    commits = [
        Commit(sha="abc", message="Merge pull request #42 from user/branch", files=[]),
        Commit(sha="def", message="feat: no pull request", files=[]),
    ]

    def fail_git(*args, **kwargs):
        raise AssertionError("PR mapping should not shell out when commit metadata is available")

    monkeypatch.setattr(subprocess, "run", fail_git)

    mapped = adapter.map_commits_to_prs(commits)

    assert list(mapped) == ["abc"]
    assert mapped["abc"].number == 42
