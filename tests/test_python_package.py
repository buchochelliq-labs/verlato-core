"""The `verlato` Python package: a thin wrapper over the native implementation."""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

verlato = pytest.importorskip("verlato", reason="the verlato package is not installed (maturin develop)")
native = pytest.importorskip("verlato._native")

ROOT = Path(__file__).resolve().parents[1]


def _cli(*args: str, cwd: Path | None = None, env: dict[str, str] | None = None) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [sys.executable, "-m", "verlato", *args], cwd=cwd, env=env, capture_output=True, text=True, timeout=300
    )


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()
    for args in (("init", "-q"), ("config", "user.email", "t@example.com"), ("config", "user.name", "Tester")):
        subprocess.run(["git", *args], cwd=repo, check=True)
    (repo / "app.py").write_text("def greet(name):\n    return 'hi ' + name\n")
    subprocess.run(["git", "add", "-A"], cwd=repo, check=True)
    subprocess.run(["git", "commit", "-q", "-m", "init"], cwd=repo, check=True)
    subprocess.run(["git", "tag", "v1.0.0"], cwd=repo, check=True)
    (repo / "app.py").write_text("def greet(name, punct='!'):\n    return 'hi ' + name + punct\n")
    subprocess.run(["git", "commit", "-qam", "feat: punctuate greetings"], cwd=repo, check=True)
    return repo


def test_version_agrees_with_cargo():
    workspace_version = tomllib.loads((ROOT / "Cargo.toml").read_text())["workspace"]["package"]["version"]
    assert verlato.__version__ == native.version() == workspace_version
    assert _cli("--version").stdout.strip() == f"verlato {verlato.__version__}"
    assert verlato.engine_version().startswith("intentumdiff-core@")


def test_help_and_usage_errors_exit_like_the_executable():
    help_run = _cli("--help")
    assert help_run.returncode == 0 and "Semantic release notes" in help_run.stdout
    unknown = _cli("no-such-command")
    assert unknown.returncode == 2 and "unrecognized subcommand" in unknown.stderr
    assert native.run(["--version"]) == 0


def test_parser_bundle_is_packaged_and_used(tmp_path: Path):
    bundle = verlato.parser_bundle()
    assert bundle is not None and (bundle / "parser_manifest.json").is_file()
    env = {key: value for key, value in os.environ.items() if "WASM_DIR" not in key}
    info = json.loads(_cli("doctor", "engine", cwd=tmp_path, env=env).stdout)
    assert info["available"] is True
    assert Path(info["parser_bundle"]) == bundle


def test_cli_drafts_release_notes(tmp_path: Path):
    repo = _repo(tmp_path)
    env = {**os.environ, "VERLATO_MODEL_PROVIDER": "stub"}
    result = _cli("draft", "json", "--base", "v1.0.0", "--version", "v1.1.0", cwd=repo, env=env)
    assert result.returncode == 0, result.stderr
    document = json.loads(result.stdout)
    assert document["version"] == "v1.1.0" and document["item_count"] == 1
    failed = _cli("draft", "json", "--base", "no-such-tag", cwd=repo, env=env)
    assert failed.returncode == 1 and failed.stderr.strip()


def test_call_answers_service_requests_in_process(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    repo = _repo(tmp_path)
    monkeypatch.setenv("VERLATO_MODEL_PROVIDER", "stub")
    assert verlato.call("health")["runtime"] == "rust"
    draft = verlato.call("draft.get", {"request": {"base": "v1.0.0", "version": "v1.1.0"}}, repo=repo)
    assert draft["version"] == "v1.1.0"
    with pytest.raises(verlato.ServiceError) as error:
        verlato.call("draft.get", {}, repo=repo)
    assert error.value.code == "invalid_request" and "base" in error.value.message
    with pytest.raises(verlato.ServiceError):
        verlato.call("no.such.method", repo=repo)


def test_mcp_entry_point_serves_the_mcp_protocol(tmp_path: Path):
    script = Path(sys.executable).with_name("verlato-mcp.exe" if os.name == "nt" else "verlato-mcp")
    command = [str(script)] if script.exists() else [sys.executable, "-c", "from verlato._cli import mcp; mcp()"]
    request = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
               "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    result = subprocess.run(command, cwd=_repo(tmp_path), input=json.dumps(request) + "\n", capture_output=True,
                            text=True, timeout=120)
    response = json.loads(result.stdout.splitlines()[0])
    assert response["result"]["serverInfo"]["name"] == "Verlato"
