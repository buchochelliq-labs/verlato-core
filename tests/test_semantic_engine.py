"""IntentumDiff 0.0.3 behaviours Verlato relies on, end to end through the installed package.

Each test commits a small change to a scratch repository and checks the semantic evidence the
native runtime derives from the engine's review: signature changes (core #297), cross-file
moves (core #298/#250), and the size limits for very large files (core #301).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytest.importorskip("verlato._native", reason="the verlato package is not installed (maturin develop)")

_GIT_DATE = "2026-01-01T12:00:00+00:00"


def _git(repo: Path, *args: str) -> None:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    env.update({
        "GIT_CONFIG_GLOBAL": os.devnull,
        "GIT_CONFIG_NOSYSTEM": "1",
        "GIT_AUTHOR_NAME": "Tester",
        "GIT_AUTHOR_EMAIL": "t@example.com",
        "GIT_COMMITTER_NAME": "Tester",
        "GIT_COMMITTER_EMAIL": "t@example.com",
        "GIT_AUTHOR_DATE": _GIT_DATE,
        "GIT_COMMITTER_DATE": _GIT_DATE,
    })
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, env=env)


def _repo(tmp_path: Path, before: dict[str, str], after: dict[str, str | None]) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    for path, content in before.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "tag", "v1.0.0")
    for path, content in after.items():
        if content is None:
            (repo / path).unlink()
        else:
            (repo / path).parent.mkdir(parents=True, exist_ok=True)
            (repo / path).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "change")
    return repo


def _semantic(repo: Path, **env: str) -> dict:
    run_env = {key: value for key, value in os.environ.items() if not key.startswith("VERLATO_")}
    run_env.update({
        "VERLATO_DIFF_ENGINE": "intentumdiff",
        "VERLATO_MODEL_PROVIDER": "stub",
        "VERLATO_SEMANTIC_CACHE_PATH": "off",
        **env,
    })
    result = subprocess.run(
        [sys.executable, "-m", "verlato", "scan", "--base", "v1.0.0"],
        cwd=repo, env=run_env, capture_output=True, text=True, timeout=300,
    )
    assert result.returncode == 0, result.stderr[-2000:]
    items = json.loads(result.stdout)
    assert len(items) == 1, items
    return items[0]["evidence"]["diff_summary"]["semantic"]


_FETCH = "def fetch(limit, page=1):\n    return [limit, page]\n"


@pytest.mark.parametrize(
    ("signature", "verdict", "extended", "changed"),
    [
        ("def fetch(limit, page=1, offset=0):", "additive", ["fetch in api.py"], []),
        ("def fetch(limit, page=1, **options):", "additive", ["fetch in api.py"], []),
        ("def fetch(limit, page, offset=0):", "behavioral", [], ["fetch in api.py"]),
        ("def fetch(offset=0, limit=10, page=1):", "behavioral", [], ["fetch in api.py"]),
    ],
)
def test_signature_changes_come_from_the_engine(tmp_path: Path, signature, verdict, extended, changed):
    new = _FETCH.replace("def fetch(limit, page=1):", signature)
    semantic = _semantic(_repo(tmp_path, {"api.py": _FETCH}, {"api.py": new}))
    assert semantic["refactorings"] == ["CHANGE_SIGNATURE"]
    assert semantic["extended_signatures"] == extended
    assert semantic["signature_changes"] == changed
    # A parameter-shape change is never reported as a behavior-preserving refactoring.
    assert semantic["verdict"] == verdict
    assert semantic["meaningful_files"] == ["api.py"] and semantic["refactor_only_files"] == []


_UTILS = 'def parse_date(s):\n    return s.split("-")\n\n\ndef slug(s):\n    return s.lower()\n'
_DATES = 'def today():\n    return "2026"\n'


@pytest.mark.parametrize("enabled", ["0", "1"])
def test_cross_file_moves_are_opt_in(tmp_path: Path, enabled: str):
    repo = _repo(
        tmp_path,
        {"shop/utils.py": _UTILS, "shop/dates.py": _DATES},
        {"shop/utils.py": "def slug(s):\n    return s.lower()\n",
         "shop/dates.py": _DATES + '\n\ndef parse_date(s):\n    return s.split("-")\n'},
    )
    semantic = _semantic(repo, VERLATO_SEMANTIC_CROSS_FILE=enabled)
    if enabled == "0":
        assert semantic["cross_file"] == []
        return
    assert [(c["change_type"], c["symbol_name"], c["symbol_kind"], c["old_file"], c["new_file"])
            for c in semantic["cross_file"]] == [
        # Top-level names lose the engine's synthetic `module.` qualifier (core #311).
        ("MOVE_TO_MODULE", "parse_date", "function", "shop/utils.py", "shop/dates.py"),
    ]
    assert "cross-file: parse_date (move to module)" in semantic["summary"]


def test_large_files_are_too_large_until_the_limit_is_raised(tmp_path: Path):
    functions = [f"def f{i}(a, b):\n    if a > b:\n        return a * {i} + b\n    return b - {i}\n" for i in range(2500)]
    before = "".join(functions)
    functions[17] = functions[17].replace("a * 17", "a * 71")
    repo = _repo(tmp_path, {"big.py": before}, {"big.py": "".join(functions)})

    default = _semantic(repo)
    assert default["files_unreviewed"] == 1 and not default["complete"]
    assert default["unreviewed"][0]["kind"] == "too_large"

    raised = _semantic(repo, VERLATO_SEMANTIC_MAX_NODES="200000")
    assert raised["complete"] and raised["files_reviewed"] == 1
    assert raised["meaningful_files"] == ["big.py"]
