"""Parity between the native Rust `verlato` binary and the Python implementation.

Both implementations run the same commands against the same generated repositories; their
JSON/Markdown outputs and state files must match. Runs only when the Rust binary is available
(``VERLATO_RUST_BIN`` or ``target/release/verlato``).

With ``--diff-engine git`` the pipelines must match exactly. With the IntentumDiff engine the
two runtimes link different engine builds (the Python wheel vs the pinned Rust core), so the
comparison ignores engine version strings but still requires identical classifications,
verdicts and symbol findings.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
RUST_BIN = Path(os.getenv("VERLATO_RUST_BIN", ROOT / "target" / "release" / "verlato"))

pytestmark = pytest.mark.skipif(not RUST_BIN.exists(), reason="native verlato binary not built")


def _git(repo: Path, *args: str) -> None:
    subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)


def _commit(repo: Path, message: str, files: dict[str, str | None]) -> None:
    for path, content in files.items():
        target = repo / path
        if content is None:
            target.unlink()
            continue
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", message)


def _build_repo(root: Path) -> Path:
    repo = root / "repo"
    repo.mkdir(parents=True)
    _git(repo, "init", "-q")
    _git(repo, "config", "user.email", "t@example.com")
    _git(repo, "config", "user.name", "Tester")
    _commit(repo, "init", {
        "src/app.py": "def greet(name):\n    return 'hi ' + name\n\n\ndef gone(x):\n    return x\n",
        "web/ui.ts": "export function render(a: number): number {\n  return a;\n}\n",
        "README.md": "# Demo\n",
    })
    _git(repo, "tag", "v1.0.0")
    _commit(repo, "feat(api): add export endpoint", {"api/export.py": "def export(rows):\n    return list(rows)\n"})
    _commit(repo, "fix(api): handle empty export", {
        "api/export.py": "def export(rows):\n    return list(rows or [])\n",
    })
    _commit(repo, "Update app helpers", {
        "src/app.py": (
            "def greet(name, punct='!'):\n    return 'hi ' + name + punct\n\n\ndef fresh(y):\n    return y\n"
        ),
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
    return repo


def _env(tmp: Path, engine: str) -> dict[str, str]:
    env = dict(os.environ)
    env.update({
        "VERLATO_MODEL_PROVIDER": "stub",
        "VERLATO_DIFF_ENGINE": engine,
        "VERLATO_SEMANTIC_PARALLELISM": "2",
        "VERLATO_SEMANTIC_CACHE_PATH": "off",
        "VERLATO_PYTHON_CLI": "",
    })
    env.pop("VERLATO_STATE_PATH", None)
    return env


def _run(cmd: list[str], repo: Path, env: dict[str, str]) -> str:
    result = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, f"{cmd} failed: {result.stderr[-2000:]}"
    return result.stdout


def _python(*args: str) -> list[str]:
    return [sys.executable, "-m", "verlato.cli", *args]


def _rust(*args: str) -> list[str]:
    return [str(RUST_BIN), *args]


def _normalize(value):
    """Drop fields that legitimately differ between runtimes (engine build identity)."""
    if isinstance(value, dict):
        return {
            key: _normalize(item)
            for key, item in value.items()
            if key not in {"engine_version", "repository_url"}
        }
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    if isinstance(value, str) and value.startswith("IntentumDiff semantic review"):
        return value
    return value


def _pair(tmp_path: Path, engine: str) -> tuple[Path, Path, dict[str, str]]:
    base = _build_repo(tmp_path / "base")
    py_repo = tmp_path / "py"
    rs_repo = tmp_path / "rs"
    shutil.copytree(base, py_repo)
    shutil.copytree(base, rs_repo)
    return py_repo, rs_repo, _env(tmp_path, engine)


@pytest.mark.parametrize("engine", ["git", "intentumdiff"])
def test_draft_scan_and_render_match(tmp_path: Path, engine: str):
    py_repo, rs_repo, env = _pair(tmp_path, engine)
    for args in (
        ("draft", "json", "--base", "v1.0.0", "--version", "v1.1.0", "--include-confidence"),
        ("scan", "--base", "v1.0.0"),
        ("reconcile", "--from-latest-tag"),
    ):
        python_out = json.loads(_run(_python(*args), py_repo, env))
        rust_out = json.loads(_run(_rust(*args), rs_repo, env))
        assert _normalize(rust_out) == _normalize(python_out), args

    for args in (("draft", "preview", "--base", "v1.0.0", "--version", "v1.1.0"),):
        assert _run(_rust(*args), rs_repo, env) == _run(_python(*args), py_repo, env)


def test_state_workflow_matches(tmp_path: Path):
    py_repo, rs_repo, env = _pair(tmp_path, "git")

    def both(*args: str) -> tuple[str, str]:
        python_out = _run(_python(*args), py_repo, env).replace(str(py_repo), "<repo>")
        rust_out = _run(_rust(*args), rs_repo, env).replace(str(rs_repo), "<repo>")
        return python_out, rust_out

    def state(repo: Path) -> dict:
        return json.loads((repo / ".verlato_state.json").read_text())

    py_items = json.loads(both("generate", "--base", "v1.0.0")[0])
    item_id = py_items[0]["id"]
    for args in (
        ("draft", "review", "--base", "v1.0.0", "--id", item_id, "--status", "ready", "--reviewer", "alice"),
        ("draft", "review", "--base", "v1.0.0", "--id", item_id, "--summary", "Adds the export endpoint."),
        ("feedback", "record", "--event", "suggestion_viewed", "--item-id", item_id, "--metadata-json", '{"n": 1}'),
        ("draft", "apply", "--base", "v1.0.0", "--version", "v1.1.0"),
        ("render", "--format", "changelog", "--version", "v1.1.0", "--include-provenance"),
        ("state", "mark-published", "--version", "v1.1.0", "--ids", item_id),
        ("state", "show",),
        ("state", "clear-working",),
    ):
        python_out, rust_out = both(*args)
        if args[0] == "render":
            assert rust_out == python_out
        else:
            assert json.loads(rust_out) == json.loads(python_out), args
        assert state(rs_repo) == state(py_repo), args
    assert (rs_repo / "CHANGELOG.md").read_text() == (py_repo / "CHANGELOG.md").read_text()


def test_service_protocol_matches(tmp_path: Path):
    py_repo, rs_repo, env = _pair(tmp_path, "git")
    requests = [
        {"id": 1, "method": "health"},
        {"id": 2, "method": "draft.get", "params": {"request": {"base": "v1.0.0", "version": "v1.1.0"}}},
        {"id": 3, "method": "draft.preview", "params": {"base": "v1.0.0"}},
        {"id": 4, "method": "nope"},
        {"id": 5, "method": "draft.get", "params": {}},
        {"id": 6, "method": "state.show"},
        {"id": 7, "method": "shutdown"},
    ]
    payload = "".join(json.dumps(request) + "\n" for request in requests) + "not json\n"

    def serve(cmd: list[str], repo: Path) -> list[dict]:
        result = subprocess.run(cmd, cwd=repo, env=env, input=payload, capture_output=True, text=True, timeout=600)
        return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

    python_responses = serve(_python("service"), py_repo)
    rust_responses = serve(_rust("service"), rs_repo)
    assert len(rust_responses) == len(python_responses) == 7
    for python_response, rust_response in zip(python_responses, rust_responses, strict=True):
        if python_response["id"] == 1:
            assert rust_response["result"]["status"] == "ok"
            continue
        if python_response["id"] == 4:
            # Unknown methods: Python rejects; Rust tries the Python delegate (disabled here).
            assert rust_response["ok"] is False and python_response["ok"] is False
            continue
        assert _normalize(rust_response) == _normalize(python_response), python_response["id"]


def test_model_backed_commands_are_delegated_to_python(tmp_path: Path):
    py_repo, rs_repo, env = _pair(tmp_path, "git")
    env = {**env, "VERLATO_PYTHON_CLI": f"{sys.executable} -m verlato.cli"}
    prompt = ("draft", "prompt", "--base", "v1.0.0", "--id")
    item_id = json.loads(_run(_python("scan", "--base", "v1.0.0"), py_repo, env))[0]["id"]
    assert _run(_rust(*prompt, item_id), rs_repo, env) == _run(_python(*prompt, item_id), py_repo, env)

    without_python = {**env, "VERLATO_PYTHON_CLI": "", "PATH": "/usr/bin:/bin"}
    result = subprocess.run(_rust(*prompt, item_id), cwd=rs_repo, env=without_python, capture_output=True, text=True)
    assert result.returncode != 0
    assert "served by the Python implementation" in result.stderr
