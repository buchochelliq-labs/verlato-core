"""Model-free summaries: `summary_style: prose` (the default) against `evidence`.

The parity suite compares the evidence style, which is what the retired Python implementation
wrote; these tests pin the prose style's sentences with reviewed expected values, end to end
through the installed package, on the parity suite's fixture repository.
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


def _commit(repo: Path, message: str, files: dict[str, str]) -> None:
    for path, content in files.items():
        (repo / path).parent.mkdir(parents=True, exist_ok=True)
        (repo / path).write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """The parity suite's fixture history, plus a vague `wip` commit that renames a function."""
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-q")
    _commit(repo, "init", {
        "src/app.py": "def greet(name):\n    return 'hi ' + name\n\n\ndef gone(x):\n    return x\n",
        "web/ui.ts": "export function render(a: number): number {\n  return a;\n}\n",
        "README.md": "# Demo\n",
    })
    _git(repo, "tag", "v1.0.0")
    _commit(repo, "feat(api): add export endpoint", {"api/export.py": "def export(rows):\n    return list(rows)\n"})
    _commit(repo, "fix(api): handle empty export", {"api/export.py": "def export(rows):\n    return list(rows or [])\n"})
    _commit(repo, "Update app helpers", {
        "src/app.py": "def greet(name, punct='!'):\n    return 'hi ' + name + punct\n\n\ndef fresh(y):\n    return y\n",
    })
    _commit(repo, "Tidy formatting", {
        "src/app.py": "def greet(name, punct='!'):\n    return 'hi '+name+punct\n\n\ndef fresh(y):\n    return y\n",
    })
    _commit(repo, "Big change across areas (#12)", {
        "web/ui.ts": "export function render(a: number, b: number): number {\n  return a + b;\n}\n",
        "server/handler.py": "def handle(req):\n    return req\n",
    })
    _commit(repo, "docs: explain export", {"docs/export.md": "# Export\n\nUse it.\n"})
    _commit(repo, "Add more tests", {"tests/test_export.py": "def test_export():\n    assert True\n"})
    _commit(repo, "refactor!: drop legacy greet", {"src/legacy.py": "VALUE = 1\n"})
    _commit(repo, "wip", {
        "src/app.py": "def greet(name, punct='!'):\n    return 'hi '+name+punct\n\n\ndef renamed_fresh(y):\n    return y\n",
    })
    return repo


def _verlato(repo: Path, engine: str, *args: str, **env: str) -> str:
    run_env = {key: value for key, value in os.environ.items() if not key.startswith("VERLATO_")}
    run_env.update({
        "VERLATO_DIFF_ENGINE": engine,
        "VERLATO_MODEL_PROVIDER": "stub",
        "VERLATO_SEMANTIC_CACHE_PATH": "off",
        "VERLATO_STATE_PATH": str(repo.parent / f"state-{engine}.json"),
        **env,
    })
    result = subprocess.run(
        [sys.executable, "-m", "verlato", *args], cwd=repo, env=run_env, capture_output=True, text=True, timeout=600
    )
    assert result.returncode == 0, result.stderr[-2000:]
    return result.stdout


def _summaries(repo: Path, engine: str, **env: str) -> dict[str, dict]:
    draft = json.loads(_verlato(repo, engine, "draft", "json", "--base", "v1.0.0", "--version", "v1.1.0", **env))
    return {item["title"]: item for section in draft["sections"] for item in section["items"]}


# The `fix(api)` commit touches the same file as `feat(api)`, so the grouper puts both in the
# `feat` changeset; the `fix` sentence ("Fixed handling of empty export.") is a Rust unit test.
_PROSE = {
    "intentumdiff": {
        "feat(api): add export endpoint": "Added export endpoint.",
        "Update app helpers": (
            "Updated app helpers. Removed `gone`; added optional parameters to `greet`; added `fresh` "
            "(possible breaking change)."
        ),
        "Big change across areas (#12) [backend]": "Big change across areas (backend). Added `handle`.",
        "Big change across areas (#12) [frontend]": (
            "Big change across areas (frontend). Changed the signature of `render` (possible breaking change)."
        ),
        "docs: explain export": "Explained export.",
        "Add more tests": "Added more tests.",
        "refactor!: drop legacy greet": "Dropped legacy greet (breaking change).",
        "wip": "Renamed `fresh` to `renamed_fresh` (possible breaking change).",
    },
    # Without semantic evidence the sentence comes from the title, or the changed files when the
    # title says nothing.
    "git": {
        "feat(api): add export endpoint": "Added export endpoint.",
        "Update app helpers": "Updated app helpers.",
        "Big change across areas (#12) [backend]": "Big change across areas (backend).",
        "Big change across areas (#12) [frontend]": "Big change across areas (frontend).",
        "docs: explain export": "Explained export.",
        "Add more tests": "Added more tests.",
        "refactor!: drop legacy greet": "Dropped legacy greet (breaking change).",
        "wip": "Updated `src/app.py`.",
    },
}


@pytest.mark.parametrize("engine", ["intentumdiff", "git"])
def test_prose_summaries(repo: Path, engine: str):
    items = _summaries(repo, engine)
    expected = _PROSE[engine]
    found = {title: items[title]["summary"] for title in expected if title in items}
    assert found == expected
    # The diagnostic summary is still the item's evidence.
    for title in found:
        assert items[title]["evidence_summary"] != items[title]["summary"]
    if engine == "intentumdiff":
        assert items["wip"]["evidence_summary"].startswith("IntentumDiff semantic review:")


def test_prose_preview_puts_the_pr_inside_the_sentence(repo: Path):
    preview = _verlato(repo, "intentumdiff", "draft", "preview", "--base", "v1.0.0", "--version", "v1.1.0")
    assert "- Big change across areas (backend). Added `handle` (PR #12).\n" in preview
    assert "- Dropped legacy greet (breaking change).\n" in preview
    assert "IntentumDiff semantic review" not in preview


@pytest.mark.parametrize("via", ["env", "config"])
def test_evidence_style_restores_the_diagnostic_summary(repo: Path, via: str):
    env = {}
    if via == "env":
        env["VERLATO_SUMMARY_STYLE"] = "evidence"
    else:
        (repo / "verlato.json").write_text(json.dumps({"summary_style": "evidence"}))
    items = _summaries(repo, "intentumdiff", **env)
    assert items["wip"]["summary"].startswith("IntentumDiff semantic review:")
    preview = _verlato(repo, "intentumdiff", "draft", "preview", "--base", "v1.0.0", "--version", "v1.1.0", **env)
    assert "- wip: IntentumDiff semantic review:" in preview


def test_environment_overrides_the_config(repo: Path):
    (repo / "verlato.json").write_text(json.dumps({"summary_style": "evidence"}))
    items = _summaries(repo, "intentumdiff", VERLATO_SUMMARY_STYLE="prose")
    assert items["wip"]["summary"] == _PROSE["intentumdiff"]["wip"]


def test_an_unknown_style_is_a_config_error(repo: Path):
    (repo / "verlato.json").write_text(json.dumps({"summary_style": "poetry"}))
    run_env = {key: value for key, value in os.environ.items() if not key.startswith("VERLATO_")}
    result = subprocess.run(
        [sys.executable, "-m", "verlato", "draft", "json", "--base", "v1.0.0"],
        cwd=repo, env=run_env, capture_output=True, text=True, timeout=600,
    )
    assert result.returncode != 0
    assert 'Invalid summary_style "poetry"' in result.stderr
