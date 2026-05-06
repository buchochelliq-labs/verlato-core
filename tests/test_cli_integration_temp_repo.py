from __future__ import annotations

import subprocess
from pathlib import Path

from verlato.cli.main import main


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def test_scan_in_temp_repo(monkeypatch, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")

    readme = repo / "README.md"
    readme.write_text("hello\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "docs: initial")
    _git(repo, "tag", "v0.1.0")

    app = repo / "app.py"
    app.write_text("print('v2')\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "feat: add app")

    monkeypatch.chdir(repo)
    monkeypatch.setenv("VERLATO_PR_ADAPTER", "none")
    monkeypatch.setenv("VERLATO_MODEL_PROVIDER", "stub")
    monkeypatch.setenv("VERLATO_STATE_PATH", str(repo / ".verlato_state.json"))

    rc = main(["scan", "--base", "v0.1.0", "--head", "HEAD"])
    assert rc == 0


def test_scan_from_latest_tag_marker(monkeypatch, tmp_path: Path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init")
    _git(repo, "config", "user.name", "Test User")
    _git(repo, "config", "user.email", "test@example.com")

    readme = repo / "README.md"
    readme.write_text("hello\n")
    _git(repo, "add", "README.md")
    _git(repo, "commit", "-m", "docs: initial")
    _git(repo, "tag", "v0.1.0")

    app = repo / "app.py"
    app.write_text("print('v2')\n")
    _git(repo, "add", "app.py")
    _git(repo, "commit", "-m", "feat: add app")

    monkeypatch.chdir(repo)
    monkeypatch.setenv("VERLATO_PR_ADAPTER", "none")
    monkeypatch.setenv("VERLATO_MODEL_PROVIDER", "stub")
    monkeypatch.setenv("VERLATO_STATE_PATH", str(repo / ".verlato_state.json"))

    rc = main(["scan", "--from-latest-tag"])
    assert rc == 0
