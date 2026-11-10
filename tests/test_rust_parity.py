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
import re
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


@pytest.mark.parametrize("engine", ["git", "intentumdiff"])
@pytest.mark.parametrize("privacy", ["metadata-only", "local-summary-then-ai", "trusted-provider-patch"])
def test_draft_prompt_matches(tmp_path: Path, engine: str, privacy: str):
    py_repo, rs_repo, env = _pair(tmp_path, engine)
    env = {**env, "VERLATO_SUGGESTION_TONE": "technical"}
    compare = (lambda value: value) if engine == "git" else _strip_engine_identity
    items = json.loads(_run(_python("scan", "--base", "v1.0.0"), py_repo, env))
    for item in items:
        args = ("draft", "prompt", "--base", "v1.0.0", "--id", item["id"], "--privacy", privacy)
        python_out = json.loads(_run(_python(*args), py_repo, env))
        rust_out = json.loads(_run(_rust(*args), rs_repo, _native_only(env)))
        assert compare(rust_out) == compare(python_out), item["title"]
    assert python_out["provider"] == "ollama"


def test_unported_commands_are_delegated_to_python(tmp_path: Path):
    py_repo, rs_repo, env = _pair(tmp_path, "git")
    env = {**env, "VERLATO_PYTHON_CLI": f"{sys.executable} -m verlato.cli"}
    # A command path the native binary does not know is handed to Python unchanged.
    args = ("no-such-command",)
    delegated = subprocess.run(_rust(*args), cwd=rs_repo, env=env, capture_output=True, text=True)
    python = subprocess.run(_python(*args), cwd=py_repo, env=env, capture_output=True, text=True)
    assert (delegated.returncode, delegated.stdout) == (python.returncode, python.stdout)
    assert "invalid choice" in delegated.stderr

    result = subprocess.run(_rust(*args), cwd=rs_repo, env=_native_only(env), capture_output=True, text=True)
    assert result.returncode != 0
    assert "served by the Python implementation" in result.stderr


# ---------------------------------------------------------------------------
# native model providers
# ---------------------------------------------------------------------------

_PARITY_PLUGIN = r'''
import json, os, sys
request = json.load(sys.stdin)
with open(os.environ["PARITY_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(request, sort_keys=True) + "\n")
if request["task"] == "classify":
    content, confidence = "fixed/backend/external/patch", 0.9
else:
    content = f"Explains {request['evidence']['title']} using {len(request['prompt'])} prompt characters of evidence."
    confidence = 0.8
print(json.dumps({"request_hash": request["request_hash"], "content": content, "confidence": confidence, "model": "parity"}))
'''

# Fields derived from the engine build identity, which differs between runtimes under IntentumDiff.
_ENGINE_DERIVED = {"engine_version", "json_chars", "evidence_chars", "prompt_chars", "estimated_tokens"}
# Prompts that embed evidence JSON (conventional-commit derivation) carry the version in their text.
_ENGINE_VERSION_TEXT = re.compile(r'"engine_version":( ?)"[^"]*"')


def _strip_engine_identity(value):
    if isinstance(value, str):
        return _ENGINE_VERSION_TEXT.sub(r'"engine_version":\1"<engine>"', value)
    if isinstance(value, dict):
        return {
            key: _strip_engine_identity(item)
            for key, item in value.items()
            if key not in _ENGINE_DERIVED and not key.endswith("_hash") and not key.startswith("verlato_")
        }
    if isinstance(value, list):
        return [_strip_engine_identity(item) for item in value]
    return value


def _native_only(env: dict[str, str]) -> dict[str, str]:
    """Run the Rust binary where no Python delegate can be found, so a delegation would fail."""
    return {**env, "VERLATO_PYTHON_CLI": "", "PATH": os.pathsep.join(["/usr/bin", "/bin"])}


def _read_log(path: Path) -> list[dict]:
    return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []


@pytest.mark.parametrize("engine", ["git", "intentumdiff"])
def test_plugin_provider_scan_matches(tmp_path: Path, engine: str):
    """A scan with the external-command plugin runs natively and sends Python's exact requests."""
    py_repo, rs_repo, env = _pair(tmp_path, engine)
    plugin = tmp_path / "plugin.py"
    plugin.write_text(_PARITY_PLUGIN)
    env.pop("VERLATO_MODEL_PROVIDER")
    for repo in (py_repo, rs_repo):
        (repo / "verlato.json").write_text(json.dumps({
            "provider": {
                "name": "plugin",
                "plugin_manifest": {"id": "parity", "display_name": "Parity plugin"},
                "plugin_command": [sys.executable, str(plugin)],
                "plugin_env": ["PARITY_LOG"],
                "plugin_config": {"style": "test"},
            },
            "suggestion_style": {"guidance": "Name the user-visible effect.", "project_vocabulary": ["export"]},
        }))
    logs = {name: tmp_path / f"{name}.jsonl" for name in ("py", "rs")}

    python_out = json.loads(_run(_python("scan", "--base", "v1.0.0"), py_repo, {**env, "PARITY_LOG": str(logs["py"])}))
    rust_out = json.loads(
        _run(_rust("scan", "--base", "v1.0.0"), rs_repo, _native_only({**env, "PARITY_LOG": str(logs["rs"])}))
    )
    python_requests, rust_requests = _read_log(logs["py"]), _read_log(logs["rs"])

    assert rust_out and any("provider-summary:parity" in item["evidence"]["commit_signals"] for item in rust_out)
    assert any("provider-classification:parity" in item["evidence"]["commit_signals"] for item in rust_out)
    assert {request["task"] for request in rust_requests} == {"summarize", "classify"}
    if engine == "git":
        assert rust_out == python_out
        assert rust_requests == python_requests  # byte-identical prompts, evidence and hashes
    else:
        assert _normalize(rust_out) == _normalize(python_out)
        assert _strip_engine_identity(rust_requests) == _strip_engine_identity(python_requests)


def test_litellm_provider_scan_matches(tmp_path: Path):
    """LiteLLM requests (messages, audit metadata, output cap) are identical across runtimes."""
    import threading
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    bodies: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server API
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            bodies.append(body)
            prompt = body["messages"][-1]["content"]
            content = (
                "added/api/external/minor"
                if prompt.startswith("Classify")
                else f"Summarises {len(prompt)} characters of semantic evidence for reviewers."
            )
            payload = json.dumps({"choices": [{"message": {"content": content}}], "usage": {"prompt_tokens": 7}}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        py_repo, rs_repo, env = _pair(tmp_path, "git")
        env = {
            **env,
            "VERLATO_MODEL_PROVIDER": "litellm",
            "VERLATO_LITELLM_ENDPOINT": f"http://127.0.0.1:{server.server_address[1]}/v1/chat/completions",
            "VERLATO_LITELLM_MODEL": "parity-model",
        }
        python_out = json.loads(_run(_python("scan", "--base", "v1.0.0"), py_repo, env))
        python_bodies, bodies[:] = list(bodies), []
        rust_out = json.loads(_run(_rust("scan", "--base", "v1.0.0"), rs_repo, _native_only(env)))
    finally:
        server.shutdown()
    assert rust_out == python_out
    assert any("provider-summary:litellm:parity-model" in item["evidence"]["commit_signals"] for item in rust_out)
    assert bodies == python_bodies
    assert all(body["max_tokens"] == 1024 for body in bodies)


def _serve(handler_factory):
    import threading
    from http.server import ThreadingHTTPServer

    server = ThreadingHTTPServer(("127.0.0.1", 0), handler_factory)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    return server


def _model_lines(stderr: str) -> list[str]:
    return [line for line in stderr.splitlines() if line.startswith("[verlato:model]")]


def _otel_records(path: Path) -> list[dict]:
    records = _read_log(path)
    for record in records:
        record.pop("Timestamp")
        record.pop("ObservedTimestamp")
    return records


@pytest.mark.parametrize("stream", [False, True])
def test_ollama_provider_scan_matches(tmp_path: Path, stream: bool):
    """The default Ollama provider runs natively: same requests, items, traces and telemetry."""
    from http.server import BaseHTTPRequestHandler

    bodies: list[dict] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server API
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            bodies.append(body)
            prompt = body["messages"][-1]["content"]
            content = (
                "fixed/backend/external/patch"
                if prompt.startswith("Classify")
                else f"Clarifies {len(prompt)} characters of change evidence for the release (token=abc123)."
            )
            stats = {"done": True, "done_reason": "stop", "eval_count": 9, "prompt_eval_count": 40}
            if body["stream"]:
                half = len(content) // 2
                chunks = [
                    {"message": {"role": "assistant", "content": content[:half]}, "done": False},
                    {"message": {"role": "assistant", "content": content[half:]}, "done": False},
                    {"message": {"role": "assistant", "content": ""}, **stats},
                ]
                payload = "".join(json.dumps(chunk) + "\n" for chunk in chunks).encode()
                content_type = "application/x-ndjson"
            else:
                payload = json.dumps({"message": {"role": "assistant", "content": content}, **stats}).encode()
                content_type = "application/json"
            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    try:
        py_repo, rs_repo, env = _pair(tmp_path, "git")
        env = {
            **env,
            "VERLATO_MODEL_PROVIDER": "ollama",
            "VERLATO_OLLAMA_ENDPOINT": f"http://127.0.0.1:{server.server_address[1]}/api/chat",
            "VERLATO_OLLAMA_MODEL": "parity-model",
            "VERLATO_OLLAMA_STREAM": "1" if stream else "0",
            "VERLATO_TRACE_MODEL_CALLS": "1",
        }
        results = {}
        for name, command, repo in (("py", _python, py_repo), ("rs", _rust, rs_repo)):
            log = tmp_path / f"{name}-model.jsonl"
            run_env = {**env, "VERLATO_MODEL_LOG_PATH": str(log)}
            if name == "rs":
                run_env = _native_only(run_env)
            bodies.clear()
            result = subprocess.run(
                command("scan", "--base", "v1.0.0"), cwd=repo, env=run_env, capture_output=True, text=True, timeout=600
            )
            assert result.returncode == 0, result.stderr[-2000:]
            results[name] = (json.loads(result.stdout), list(bodies), _model_lines(result.stderr), _otel_records(log))
    finally:
        server.shutdown()
    (py_items, py_bodies, py_lines, py_otel), (rs_items, rs_bodies, rs_lines, rs_otel) = results["py"], results["rs"]
    assert rs_items == py_items
    assert any("provider-summary:ollama:parity-model" in item["evidence"]["commit_signals"] for item in rs_items)
    assert rs_bodies == py_bodies
    assert rs_bodies[0]["options"] == {"temperature": 0.1, "num_predict": 180}
    assert rs_lines == py_lines
    assert rs_otel == py_otel
    if stream:
        chunk = next(line for line in rs_lines if '"stream_chunk"' in line)
        assert '"redacted": true' in chunk or '"redacted": false' in chunk


@pytest.mark.parametrize("engine", ["git", "intentumdiff"])
def test_mock_provider_scan_and_draft_match(tmp_path: Path, engine: str):
    """The deterministic mock provider (Python's Mersenne Twister) gives identical results."""
    py_repo, rs_repo, env = _pair(tmp_path, engine)
    env = {**env, "VERLATO_MODEL_PROVIDER": "mock"}
    compare = (lambda value: value) if engine == "git" else _normalize
    for args in (
        ("scan", "--base", "v1.0.0"),
        ("draft", "json", "--base", "v1.0.0", "--model-provider", "mock", "--include-confidence"),
    ):
        python_out = json.loads(_run(_python(*args), py_repo, env))
        rust_out = json.loads(_run(_rust(*args), rs_repo, _native_only(env)))
        assert compare(rust_out) == compare(python_out), args
        if args[0] == "scan":
            signals = [signal for item in rust_out for signal in item["evidence"]["commit_signals"]]
            assert "provider-classification:mock-local" in signals


def test_github_models_provider_scan_matches(tmp_path: Path):
    from http.server import BaseHTTPRequestHandler

    requests: list[tuple[dict, dict]] = []

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self):  # noqa: N802 - http.server API
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            headers = {key: self.headers[key] for key in ("Authorization", "Accept", "X-GitHub-Api-Version")}
            requests.append((headers, body))
            prompt = body["messages"][-1]["content"]
            if "Title: Add more tests" in prompt and not prompt.startswith("Classify"):
                payload = json.dumps({"error": {"code": "RateLimit"}, "message": "slow down"}).encode()
                self.send_response(429)
            else:
                content = f"Documents {len(prompt)} characters of reviewed evidence for release readers."
                payload = json.dumps({"id": "r1", "choices": [{"message": {"content": content}}],
                                      "usage": {"total_tokens": 11}}).encode()
                self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    try:
        py_repo, rs_repo, env = _pair(tmp_path, "git")
        env = {
            **env,
            "VERLATO_MODEL_PROVIDER": "github-copilot",
            "VERLATO_GITHUB_MODELS_ENDPOINT": f"http://127.0.0.1:{server.server_address[1]}/chat/completions",
            "VERLATO_GITHUB_MODEL": "gpt-4.1-mini",
            "GITHUB_TOKEN": "test-token",
        }
        python_out = json.loads(_run(_python("scan", "--base", "v1.0.0"), py_repo, env))
        python_requests, requests[:] = list(requests), []
        rust_out = json.loads(_run(_rust("scan", "--base", "v1.0.0"), rs_repo, _native_only(env)))
    finally:
        server.shutdown()
    assert rust_out == python_out
    assert requests == python_requests
    assert requests[0][0]["Authorization"] == "Bearer test-token"
    assert requests[0][1]["model"] == "openai/gpt-4.1-mini"
    signals = [signal for item in rust_out for signal in item["evidence"]["commit_signals"]]
    assert "provider-summary:github-copilot:openai/gpt-4.1-mini" in signals
    assert any(signal.startswith("provider-error:github-copilot:openai/gpt-4.1-mini:[github-copilot-error] "
                                 "GitHub Models rate limit exceeded (429 Too Many Requests): slow down")
               for signal in signals)


_FAKE_AGENT = r'''#!{python}
import json, os, sys
argv = sys.argv[1:]
prompt = sys.stdin.read() if "-" in argv else argv[-1]
with open(os.environ["AGENT_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps({{"argv": [a for a in argv if not a.startswith("/")], "prompt": prompt}}) + "\n")
answer = "fixed/backend/external/patch" if "Task: classify" in prompt else f"Explains {{len(prompt)}} prompt characters for reviewers."
if "--output-last-message" in argv:
    with open(argv[argv.index("--output-last-message") + 1], "w", encoding="utf-8") as out:
        out.write(answer)
else:
    print(json.dumps({{"result": answer}}))
'''


@pytest.mark.parametrize("agent", ["codex-cli", "claude-code"])
def test_cli_agent_provider_scan_matches(tmp_path: Path, agent: str):
    script = tmp_path / "agent"
    script.write_text(_FAKE_AGENT.format(python=sys.executable))
    script.chmod(0o755)
    py_repo, rs_repo, env = _pair(tmp_path, "git")
    command_env = "VERLATO_CODEX_COMMAND" if agent == "codex-cli" else "VERLATO_CLAUDE_COMMAND"
    env = {**env, "VERLATO_MODEL_PROVIDER": agent, command_env: str(script), "VERLATO_CODEX_MODEL": "",
           "VERLATO_CLAUDE_MODEL": ""}
    logs = {name: tmp_path / f"{name}.jsonl" for name in ("py", "rs")}
    python_out = json.loads(_run(_python("scan", "--base", "v1.0.0"), py_repo, {**env, "AGENT_LOG": str(logs["py"])}))
    rust_out = json.loads(
        _run(_rust("scan", "--base", "v1.0.0"), rs_repo, _native_only({**env, "AGENT_LOG": str(logs["rs"])}))
    )
    assert rust_out == python_out
    assert _read_log(logs["rs"]) == _read_log(logs["py"])
    assert any(f"provider-summary:{agent}:default" in item["evidence"]["commit_signals"] for item in rust_out)


_INTENT_PLUGIN = r"""
import json, os, sys
request = json.load(sys.stdin)
with open(os.environ["PARITY_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(request, sort_keys=True) + "\n")
evidence = request["evidence"]
paths = [f.get("path", "") for f in evidence.get("files", [])]
scope = "api" if any("api/" in p for p in paths) else "docs" if any(p.endswith(".md") for p in paths) else "core"
if request["task"] in ("derive-conventional", "draft-conventional"):
    body = {"type": "feat" if evidence.get("initial_commit") is False else "chore", "scope": scope,
            "description": f"update {len(paths)} file(s) from {request['task']}", "breaking": False,
            "confidence": 0.9, "rationale": "parity"}
    if request["task"] == "draft-conventional":
        body["body"] = [f"- touches {p}" for p in paths[:3]]
    content = json.dumps(body)
elif request["task"] == "classify":
    content = "changed/general/external/patch"
else:
    content = f"Explains {evidence.get('title', 'change')} using {len(request['prompt'])} prompt characters."
print(json.dumps({"request_hash": request["request_hash"], "content": content, "confidence": 0.8, "model": "parity"}))
"""


def _intent_setup(tmp_path: Path, engine: str, provider: str):
    py_repo, rs_repo, env = _pair(tmp_path, engine)
    env = {**env, "VERLATO_TRACE_PROGRESS": "1"}
    if provider == "plugin":
        plugin = tmp_path / "intent_plugin.py"
        plugin.write_text(_INTENT_PLUGIN)
        env.pop("VERLATO_MODEL_PROVIDER")
        config = {"provider": {"name": "plugin", "plugin_manifest": {"id": "parity"},
                               "plugin_command": [sys.executable, str(plugin)], "plugin_env": ["PARITY_LOG"]}}
        for repo in (py_repo, rs_repo):
            (repo / "verlato.json").write_text(json.dumps(config))
    else:
        env["VERLATO_MODEL_PROVIDER"] = "mock"
    return py_repo, rs_repo, env


def _progress_events(stderr: str, stage: str) -> list[dict]:
    prefix = "[verlato:progress] json "
    events = [json.loads(line[len(prefix):]) for line in stderr.splitlines() if line.startswith(prefix)]
    return [{k: v for k, v in event.items() if v not in ("", None)} for event in events if event.get("stage") == stage]


@pytest.mark.parametrize("engine", ["git", "intentumdiff"])
@pytest.mark.parametrize("provider", ["mock", "plugin"])
def test_commit_intent_inference_matches(tmp_path: Path, engine: str, provider: str):
    """Low-signal commits are grouped by model-derived intents natively, as in Python."""
    py_repo, rs_repo, env = _intent_setup(tmp_path, engine, provider)
    compare = (lambda value: value) if engine == "git" else _strip_engine_identity
    commands = [
        ("scan", "--base", "v1.0.0", "--infer-commit-intents"),
        ("draft", "json", "--base", "v1.0.0", "--infer-commit-intents", "--model-provider", provider),
        ("draft", "json", "--base", "v1.0.0", "--infer-commit-intents"),
    ]
    for index, args in enumerate(commands):
        logs = {name: tmp_path / f"{name}-{index}.jsonl" for name in ("py", "rs")}
        python = subprocess.run(_python(*args), cwd=py_repo, env={**env, "PARITY_LOG": str(logs["py"])},
                                capture_output=True, text=True, timeout=600)
        rust = subprocess.run(_rust(*args), cwd=rs_repo, env=_native_only({**env, "PARITY_LOG": str(logs["rs"])}),
                              capture_output=True, text=True, timeout=600)
        assert python.returncode == 0 and rust.returncode == 0, (python.stderr[-800:], rust.stderr[-800:])
        assert compare(json.loads(rust.stdout)) == compare(json.loads(python.stdout)), args
        assert compare(_read_log(logs["rs"])) == compare(_read_log(logs["py"])), args
        assert _progress_events(rust.stderr, "inferring_commit_intents") == _progress_events(
            python.stderr, "inferring_commit_intents"
        ), args
    items = json.loads(rust.stdout)
    signals = [signal for section in items["sections"] for item in section["items"] for signal in item["commit_signals"]]
    assert any(signal.startswith("inferred-conventional:") for signal in signals)


def test_commit_intent_inference_config_and_prompt_match(tmp_path: Path):
    py_repo, rs_repo, env = _intent_setup(tmp_path, "git", "mock")
    for repo in (py_repo, rs_repo):
        (repo / "verlato.json").write_text(json.dumps({"commit_intent_inference": "low-signal"}))
    items = json.loads(_run(_python("scan", "--base", "v1.0.0"), py_repo, env))
    assert json.loads(_run(_rust("scan", "--base", "v1.0.0"), rs_repo, _native_only(env))) == items
    for item in items[:3]:
        args = ("draft", "prompt", "--base", "v1.0.0", "--id", item["id"], "--model-provider", "mock")
        assert json.loads(_run(_rust(*args), rs_repo, _native_only(env))) == json.loads(_run(_python(*args), py_repo, env))
    # Fan-out guard: no inference (and no model calls) when the range has too many commits.
    guarded = {**env, "VERLATO_MODEL_MAX_ITEMS": "2"}
    assert json.loads(_run(_rust("scan", "--base", "v1.0.0"), rs_repo, _native_only(guarded))) == json.loads(
        _run(_python("scan", "--base", "v1.0.0"), py_repo, guarded)
    )


def _commit_shas(repo: Path) -> list[str]:
    out = subprocess.run(["git", "log", "--format=%H", "v1.0.0..HEAD"], cwd=repo, capture_output=True, text=True, check=True)
    return out.stdout.split()


@pytest.mark.parametrize("engine", ["git", "intentumdiff"])
@pytest.mark.parametrize("provider", ["mock", "plugin"])
def test_commit_conventional_matches(tmp_path: Path, engine: str, provider: str):
    py_repo, rs_repo, env = _intent_setup(tmp_path, engine, provider)
    compare = (lambda value: value) if engine == "git" else _strip_engine_identity
    shas = _commit_shas(py_repo)
    logs = {name: tmp_path / f"conventional-{name}.jsonl" for name in ("py", "rs")}
    for sha in shas[:4]:
        for mode in ("diff-only", "subject-hint"):
            for privacy in ("metadata-only", "trusted-provider-patch"):
                args = ("commit", "conventional", "--base", "v1.0.0", "--sha", sha[:10], "--evidence-mode", mode,
                        "--privacy", privacy)
                python_out = json.loads(_run(_python(*args), py_repo, {**env, "PARITY_LOG": str(logs["py"])}))
                rust_out = json.loads(_run(_rust(*args), rs_repo, _native_only({**env, "PARITY_LOG": str(logs["rs"])})))
                assert compare(rust_out) == compare(python_out), (sha, mode, privacy)
    assert compare(_read_log(logs["rs"])) == compare(_read_log(logs["py"]))
    missing = subprocess.run(_rust("commit", "conventional", "--base", "v1.0.0", "--sha", "deadbeef"), cwd=rs_repo,
                             env=_native_only(env), capture_output=True, text=True)
    assert missing.returncode != 0 and "deadbeef was not found" in missing.stderr


def test_commit_service_methods_match(tmp_path: Path):
    py_repo, rs_repo, env = _intent_setup(tmp_path, "intentumdiff", "plugin")
    shas = _commit_shas(py_repo)
    requests = [
        {"id": 1, "method": "commit.conventional", "params": {"base": "v1.0.0", "sha": shas[0][:8]}},
        {"id": 2, "method": "commit.conventional",
         "params": {"base": "v1.0.0", "sha": shas[1], "evidenceMode": "subject-hint", "modelPrivacy": "trusted-provider-patch"}},
        {"id": 3, "method": "commit.conventional", "params": {"base": "v1.0.0", "sha": "deadbeef"}},
        {"id": 4, "method": "commit.conventional_many", "params": {"base": "v1.0.0", "shas": [shas[2], "deadbeef", shas[3][:7]]}},
        {"id": 5, "method": "commit.conventional_many", "params": {"base": "v1.0.0"}},
        {"id": 6, "method": "commit.conventional_many", "params": {"base": "v1.0.0", "shas": "nope"}},
        {"id": 7, "method": "commit.conventional", "params": {"sha": "x"}},
        {"id": 8, "method": "draft.get", "params": {"request": {"base": "v1.0.0", "model_provider": "plugin"}}},
        {"id": 9, "method": "shutdown"},
    ]
    payload = "".join(json.dumps(request) + "\n" for request in requests)
    for repo in (py_repo, rs_repo):
        config = json.loads((repo / "verlato.json").read_text())
        config["commit_intent_inference"] = "low-signal"
        (repo / "verlato.json").write_text(json.dumps(config))

    def serve(cmd: list[str], repo: Path, run_env: dict[str, str]) -> list[dict]:
        result = subprocess.run(cmd, cwd=repo, env=run_env, input=payload, capture_output=True, text=True, timeout=600)
        return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

    logs = {name: tmp_path / f"service-{name}.jsonl" for name in ("py", "rs")}
    python_responses = serve(_python("service"), py_repo, {**env, "PARITY_LOG": str(logs["py"])})
    rust_responses = serve(_rust("service"), rs_repo, _native_only({**env, "PARITY_LOG": str(logs["rs"])}))
    assert len(rust_responses) == len(python_responses) == len(requests)
    for python_response, rust_response in zip(python_responses, rust_responses, strict=True):
        assert _strip_engine_identity(rust_response) == _strip_engine_identity(python_response), python_response["id"]
    assert _strip_engine_identity(_read_log(logs["rs"])) == _strip_engine_identity(_read_log(logs["py"]))


# ---------------------------------------------------------------------------
# draft suggest --preview (single strategy)
# ---------------------------------------------------------------------------

_SUGGEST_PLUGIN = r'''
import json, os, sys
request = json.load(sys.stdin)
with open(os.environ["PARITY_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(request, sort_keys=True) + "\n")
evidence = request["evidence"]
count = int(evidence.get("suggestion_count") or 1)
title = evidence.get("title", "change")
if count > 1:
    content = "\n".join(
        f"{i}. Option {i} explains how {title} changes behaviour across {len(request['prompt'])} prompt characters."
        for i in range(1, count + 1)
    )
else:
    content = f"Explains how {title} changes behaviour, based on {len(request['prompt'])} prompt characters."
print(json.dumps({"request_hash": request["request_hash"], "content": content, "confidence": 0.8, "model": "parity"}))
'''

_TIMING_KEYS = {"age_seconds", "created_at", "updated_at"}


def _strip_timings(value, *, engine_identity: bool = False):
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key.endswith("_ms") or key in _TIMING_KEYS or key == "tokens_per_second":
                continue
            if engine_identity and (
                key in _ENGINE_DERIVED or key.endswith("_hash") or key.startswith("verlato_") or key == "audit"
            ):
                continue
            out[key] = _strip_timings(item, engine_identity=engine_identity)
        return out
    if isinstance(value, list):
        return [_strip_timings(item, engine_identity=engine_identity) for item in value]
    return value


def _trace_records(stderr: str, *, engine_identity: bool, skip_model_prefixes: tuple[str, ...] = ()) -> list:
    import re

    records: list = []
    for line in stderr.splitlines():
        if line.startswith(("[verlato:suggestion] json ", "[verlato:progress] json ")):
            prefix, _, payload = line.partition(" json ")
            event = json.loads(payload)
            if event.get("stage") == "semantic_review":
                continue  # commits are reviewed in parallel: completion order varies
            records.append((prefix, _strip_timings(event, engine_identity=engine_identity)))
        elif line.startswith("[verlato:model]"):
            if any(line.startswith(f"[verlato:model] {prefix}") for prefix in skip_model_prefixes):
                continue
            records.append(("model", re.sub(r"\b(\w+_ms|tokens_per_second)=\S+", r"\1=*", line)))
    return records


def _suggest_setup(tmp_path: Path, engine: str, provider: str, server_port: int | None):
    py_repo, rs_repo, env = _pair(tmp_path, engine)
    env = {**env, "VERLATO_TRACE_PROGRESS": "1", "VERLATO_TRACE_MODEL_CALLS": "1"}
    env.pop("VERLATO_MODEL_PROVIDER")
    if provider == "plugin":
        plugin = tmp_path / "suggest_plugin.py"
        plugin.write_text(_SUGGEST_PLUGIN)
        config = {
            "provider": {
                "name": "plugin",
                "plugin_manifest": {"id": "parity", "display_name": "Parity plugin"},
                "plugin_command": [sys.executable, str(plugin)],
                "plugin_env": ["PARITY_LOG"],
            },
            "suggestion_style": {"guidance": "Name the user-visible effect.", "banned_phrases": ["synergy"]},
        }
    else:
        env.update({
            "VERLATO_OLLAMA_ENDPOINT": f"http://127.0.0.1:{server_port}/api/chat",
            "VERLATO_OLLAMA_MODEL": "parity-model",
        })
        config = {"provider": {"name": "ollama"}}
    for repo in (py_repo, rs_repo):
        (repo / "verlato.json").write_text(json.dumps(config))
    return py_repo, rs_repo, env


def _ollama_suggest_handler():
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        bodies: list[dict] = []

        def do_POST(self):  # noqa: N802 - http.server API
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            Handler.bodies.append(body)
            prompt = body["messages"][-1]["content"]
            count = 3 if "Return exactly 3 distinct" in prompt else 1
            title = prompt.split("Title: ", 1)[1].splitlines()[0]
            if count > 1:
                content = "\n".join(
                    f"{i}. Variant {i} clarifies how {title} affects release readers ({len(prompt)} chars)."
                    for i in range(1, count + 1)
                )
            else:
                content = f"Clarifies how {title} affects release readers ({len(prompt)} chars of evidence)."
            payload = json.dumps({
                "message": {"role": "assistant", "content": content},
                "done": True, "done_reason": "stop", "eval_count": 12, "prompt_eval_count": 50,
                "total_duration": 9_000_000, "eval_duration": 4_000_000,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    return Handler


def _run_full(cmd: list[str], repo: Path, env: dict[str, str]) -> tuple[dict, str]:
    result = subprocess.run(cmd, cwd=repo, env=env, capture_output=True, text=True, timeout=600)
    assert result.returncode == 0, f"{cmd} failed: {result.stderr[-3000:]}"
    return json.loads(result.stdout), result.stderr


def _cache_file(repo: Path, *, engine_identity: bool) -> dict:
    return _strip_timings(json.loads((repo / ".verlato_suggestion_cache.json").read_text()), engine_identity=engine_identity)


@pytest.mark.parametrize("engine", ["git", "intentumdiff"])
@pytest.mark.parametrize("provider", ["plugin", "ollama"])
@pytest.mark.parametrize("count", [1, 3])
def test_draft_suggest_preview_matches(tmp_path: Path, engine: str, provider: str, count: int):
    handler = _ollama_suggest_handler()
    server = _serve(handler) if provider == "ollama" else None
    try:
        py_repo, rs_repo, env = _suggest_setup(
            tmp_path, engine, provider, server.server_address[1] if server else None
        )
        identity = engine != "git"
        logs = {name: tmp_path / f"{name}.jsonl" for name in ("py", "rs")}
        item_id = json.loads(_run(_python("scan", "--base", "v1.0.0"), py_repo, env))[0]["id"]
        skip = ("response-detail",) if provider == "plugin" else ()
        for args in (
            ("draft", "suggest", "--base", "v1.0.0", "--all-visible", "--preview", "--count", str(count)),
            ("draft", "suggest", "--base", "v1.0.0", "--id", item_id, "--preview", "--count", str(count)),
            ("draft", "suggest", "--base", "v1.0.0", "--all-visible", "--preview", "--count", str(count)),  # cache hits
        ):
            outputs = {}
            handler.bodies.clear()
            py_out, py_err = _run_full(_python(*args), py_repo, {**env, "PARITY_LOG": str(logs["py"])})
            py_bodies = list(handler.bodies)
            handler.bodies.clear()
            rs_out, rs_err = _run_full(_rust(*args), rs_repo, _native_only({**env, "PARITY_LOG": str(logs["rs"])}))
            outputs = (_strip_timings(rs_out, engine_identity=identity), _strip_timings(py_out, engine_identity=identity))
            assert outputs[0] == outputs[1], args
            assert _trace_records(rs_err, engine_identity=identity, skip_model_prefixes=skip) == _trace_records(
                py_err, engine_identity=identity, skip_model_prefixes=skip
            ), args
            assert handler.bodies == py_bodies
        assert rs_out["items"] and all(item["cache"]["hit"] for item in rs_out["items"])
        assert all(len(item["suggestions"]) == count for item in rs_out["items"])
        compare = _strip_engine_identity if identity else (lambda value: value)
        assert compare(_read_log(logs["rs"])) == compare(_read_log(logs["py"]))
        assert _cache_file(rs_repo, engine_identity=identity) == _cache_file(py_repo, engine_identity=identity)
    finally:
        if server:
            server.shutdown()


def test_draft_suggest_preflight_and_parallelism_match(tmp_path: Path):
    handler = _ollama_suggest_handler()
    server = _serve(handler)
    try:
        py_repo, rs_repo, env = _suggest_setup(tmp_path, "git", "ollama", server.server_address[1])
        for args in (
            ("draft", "suggest", "--base", "v1.0.0", "--all-visible", "--preflight"),
            ("draft", "suggest", "--base", "v1.0.0", "--all-visible", "--preview", "--parallelism", "3", "--no-cache"),
            ("draft", "suggest", "--base", "v1.0.0", "--all-visible", "--preview", "--tone", "concise",
             "--prompt-profile", "lean", "--suggest-model", "other-model", "--refresh-cache"),
        ):
            py_out, _ = _run_full(_python(*args), py_repo, env)
            rs_out, _ = _run_full(_rust(*args), rs_repo, _native_only(env))
            assert _strip_timings(rs_out) == _strip_timings(py_out), args
        assert rs_out["effective_suggest_model"] == "other-model"
    finally:
        server.shutdown()


_BATCH_PLUGIN = r'''
import json, os, sys
request = json.load(sys.stdin)
with open(os.environ["PARITY_LOG"], "a", encoding="utf-8") as log:
    log.write(json.dumps(request, sort_keys=True) + "\n")
evidence = request["evidence"]
count = int(evidence.get("suggestion_count") or 1)
mode = os.environ.get("PARITY_BATCH_MODE", "ok")
if evidence.get("batch"):
    payload = json.loads(request["prompt"].split("Input JSON:\n", 1)[1])
    items = payload["items"]
    if mode == "garbage":
        content = "not json at all"
    else:
        if mode == "partial":
            items = items[1:]  # the first item of every batch falls back to a single call
        content = json.dumps({"items": [
            {"item_id": item["item_id"], "suggestions": [
                f"Option {i} explains how {item['title']} changes behaviour for release readers."
                for i in range(1, count + 1)
            ]}
            for item in items
        ]})
else:
    title = evidence.get("title", "change")
    content = "\n".join(
        f"{i}. Single option {i} explains how {title} changes behaviour across {len(request['prompt'])} characters."
        for i in range(1, count + 1)
    )
print(json.dumps({"request_hash": request["request_hash"], "content": content, "confidence": 0.8, "model": "parity"}))
'''


def _batch_setup(tmp_path: Path, engine: str):
    py_repo, rs_repo, env = _suggest_setup(tmp_path, engine, "plugin", None)
    env = {**env, "PARITY_LOG": str(tmp_path / "unused.jsonl")}
    plugin = tmp_path / "batch_plugin.py"
    plugin.write_text(_BATCH_PLUGIN)
    for repo in (py_repo, rs_repo):
        config = json.loads((repo / "verlato.json").read_text())
        config["provider"]["plugin_command"] = [sys.executable, str(plugin)]
        config["model_privacy"] = "trusted-provider-patch"
        config["provider"]["plugin_env"] = ["PARITY_LOG", "PARITY_BATCH_MODE"]
        (repo / "verlato.json").write_text(json.dumps(config))
    return py_repo, rs_repo, env


def _mask_structured_hashes(value):
    """Structured strategies send the full evidence JSON, which embeds the engine build under
    IntentumDiff, so their prompt sizes, prompt hashes and cache keys differ between runtimes."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key in {"prompt_sha", "full_diff_prompt_chars", "batch_prompt_chars"} or (
                key == "key" and str(item).startswith("suggestion-v1:")
            ):
                continue
            out[key] = _mask_structured_hashes(item)
        return out
    if isinstance(value, (list, tuple)):
        return [_mask_structured_hashes(item) for item in value]
    if isinstance(value, str):
        value = _ENGINE_VERSION_TEXT.sub(r'"engine_version":\1"<engine>"', value)
        value = re.sub(r'"(estimated_tokens|json_chars|evidence_chars)":( ?)\d+', r'"\1":\2*', value)
        return re.sub(r"(prompt_sha|chars)=\w+", r"\1=*", value)
    return value


def _cache_entries(repo: Path, *, engine_identity: bool) -> list:
    entries = _cache_file(repo, engine_identity=engine_identity)["entries"].values()
    masked = [_mask_structured_hashes(entry) for entry in entries]
    return sorted(masked, key=lambda entry: json.dumps(entry, sort_keys=True))


def _compare_suggest_runs(cases, py_repo, rs_repo, env, *, identity: bool, logs: dict[str, Path] | None = None):
    rs_out: dict = {}
    for args, extra_env in cases:
        run_env = {**env, **extra_env}
        if logs:
            py_env = {**run_env, "PARITY_LOG": str(logs["py"])}
            rs_env = {**run_env, "PARITY_LOG": str(logs["rs"])}
        else:
            py_env = rs_env = run_env
        py_out, py_err = _run_full(_python(*args), py_repo, py_env)
        rs_out, rs_err = _run_full(_rust(*args), rs_repo, _native_only(rs_env))
        mask = _mask_structured_hashes if identity else (lambda value: value)
        assert mask(_strip_timings(rs_out, engine_identity=identity)) == mask(
            _strip_timings(py_out, engine_identity=identity)
        ), args
        skip = ("response-detail",)
        assert mask(_trace_records(rs_err, engine_identity=identity, skip_model_prefixes=skip)) == mask(
            _trace_records(py_err, engine_identity=identity, skip_model_prefixes=skip)
        ), args
    return rs_out


@pytest.mark.parametrize("engine", ["git", "intentumdiff"])
@pytest.mark.parametrize("strategy", ["paired", "full-diff-json"])
def test_draft_suggest_batched_strategies_match(tmp_path: Path, engine: str, strategy: str):
    py_repo, rs_repo, env = _batch_setup(tmp_path, engine)
    identity = engine != "git"
    logs = {name: tmp_path / f"{name}.jsonl" for name in ("py", "rs")}
    base = ("draft", "suggest", "--base", "v1.0.0", "--all-visible", "--strategy", strategy)
    cases = [
        ((*base, "--preflight"), {}),
        ((*base, "--preview", "--count", "2"), {}),
        ((*base, "--preview", "--count", "2"), {}),  # cache hits
        ((*base, "--preview", "--refresh-cache", "--batch-size", "3"), {"PARITY_BATCH_MODE": "partial"}),
        ((*base, "--preview", "--no-cache"), {"PARITY_BATCH_MODE": "garbage"}),
    ]
    rs_out = _compare_suggest_runs(cases, py_repo, rs_repo, env, identity=identity, logs=logs)
    performance = rs_out["performance"]
    assert performance["strategy"] == strategy
    assert performance["batched_model_calls"] >= 1 and performance["single_model_calls"] >= 1
    compare = (lambda value: _mask_structured_hashes(_strip_engine_identity(value))) if identity else (lambda value: value)
    assert compare(_read_log(logs["rs"])) == compare(_read_log(logs["py"]))
    assert _cache_entries(rs_repo, engine_identity=identity) == _cache_entries(py_repo, engine_identity=identity)


def test_draft_suggest_batched_parallel_chunks_match(tmp_path: Path):
    py_repo, rs_repo, env = _batch_setup(tmp_path, "git")
    for args in (
        ("draft", "suggest", "--base", "v1.0.0", "--all-visible", "--preview", "--no-cache",
         "--strategy", "full-diff-json", "--batch-size", "1", "--parallelism", "3"),
        ("draft", "suggest", "--base", "v1.0.0", "--all-visible", "--preview", "--no-cache",
         "--strategy", "paired", "--parallelism", "2"),
    ):
        py_out, _ = _run_full(_python(*args), py_repo, env)
        rs_out, _ = _run_full(_rust(*args), rs_repo, _native_only(env))
        assert _strip_timings(rs_out) == _strip_timings(py_out), args


def _ollama_tool_handler():
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        bodies: list[dict] = []

        def do_POST(self):  # noqa: N802 - http.server API
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            Handler.bodies.append(json.loads(json.dumps(body)))
            messages = body["messages"]
            prompt = next(m["content"] for m in messages if m["role"] == "user")
            manifest = json.loads(prompt.split("Compact input JSON:\n", 1)[1])["item"]
            tool_names = [tool["function"]["name"] for tool in body.get("tools", [])]
            rounds = sum(1 for m in messages if m["role"] == "assistant")
            if rounds == 0:
                path = manifest["files"][0]["path"]
                file_tool = "verlato_get_file_semantic_diff" if "verlato_get_file_semantic_diff" in tool_names else "verlato_get_file_patch"
                calls = [
                    {"function": {"name": "verlato_get_suggestion_item", "arguments": {"item_id": manifest["item_id"]}}},
                    {"function": {"name": file_tool, "arguments": json.dumps({"item_id": manifest["item_id"], "path": path})}},
                    {"function": {"name": "verlato_get_suggestion_item", "arguments": {"item_id": "missing"}}},
                    {"function": {"name": "verlato_search_item_diff", "arguments": {"item_id": manifest["item_id"]}}},
                    {"function": {"name": "verlato_unknown_tool", "arguments": "not json"}},
                ]
                message = {"role": "assistant", "content": "", "tool_calls": calls}
            else:
                count = int(prompt.split("Return exactly ", 1)[1].split(" ", 1)[0])
                lines = [
                    f"{i}. Variant {i} clarifies how {manifest['title']} affects release readers after {rounds} tool round(s)."
                    for i in range(1, count + 1)
                ]
                message = {"role": "assistant", "content": "\n".join(lines) if count > 1 else lines[0][3:]}
            payload = json.dumps({
                "message": message, "done": True, "done_reason": "stop", "eval_count": 12,
                "prompt_eval_count": 50, "total_duration": 9_000_000, "eval_duration": 4_000_000,
            }).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def log_message(self, *args):
            pass

    return Handler


@pytest.mark.parametrize("engine", ["git", "intentumdiff"])
def test_draft_suggest_tool_retrieval_matches(tmp_path: Path, engine: str):
    handler = _ollama_tool_handler()
    server = _serve(handler)
    try:
        py_repo, rs_repo, env = _suggest_setup(tmp_path, engine, "ollama", server.server_address[1])
        env = {**env, "VERLATO_MODEL_PRIVACY": "trusted-provider-patch"}
        identity = engine != "git"
        compare = (lambda value: _mask_structured_hashes(_strip_engine_identity(value))) if identity else (lambda v: v)
        base = ("draft", "suggest", "--base", "v1.0.0", "--all-visible", "--privacy", "trusted-provider-patch")
        for args in (
            (*base, "--preflight"),
            (*base, "--preview"),
            (*base, "--preview", "--count", "3", "--refresh-cache"),
            (*base, "--preview", "--count", "3"),  # cache hits
        ):
            handler.bodies.clear()
            py_out, py_err = _run_full(_python(*args), py_repo, env)
            py_bodies = list(handler.bodies)
            handler.bodies.clear()
            rs_out, rs_err = _run_full(_rust(*args), rs_repo, _native_only(env))
            mask = _mask_structured_hashes if identity else (lambda value: value)
            assert mask(_strip_timings(rs_out, engine_identity=identity)) == mask(
                _strip_timings(py_out, engine_identity=identity)
            ), args
            assert mask(_trace_records(rs_err, engine_identity=identity)) == mask(
                _trace_records(py_err, engine_identity=identity)
            ), args
            assert compare(handler.bodies) == compare(py_bodies), args
        assert rs_out["performance"]["strategy"] == "tool-retrieval"
        assert _cache_entries(rs_repo, engine_identity=identity) == _cache_entries(py_repo, engine_identity=identity)
    finally:
        server.shutdown()


def test_draft_suggest_tool_retrieval_calls_tools(tmp_path: Path):
    handler = _ollama_tool_handler()
    server = _serve(handler)
    try:
        _, rs_repo, env = _suggest_setup(tmp_path, "git", "ollama", server.server_address[1])
        rs_out, rs_err = _run_full(
            _rust("draft", "suggest", "--base", "v1.0.0", "--all-visible", "--preview", "--no-cache",
                  "--privacy", "trusted-provider-patch"),
            rs_repo,
            _native_only(env),
        )
        item = rs_out["items"][0]
        assert item["provider_metadata"]["tool_rounds"] == 1
        assert item["provider_metadata"]["tool_call_count"] == 5
        assert "tool-call provider=ollama task=suggest tool=verlato_unknown_tool args=0" in rs_err
        tool_messages = [m for m in handler.bodies[1]["messages"] if m["role"] == "tool"]
        assert json.loads(tool_messages[2]["content"]) == {
            "error": "'Unknown suggestion item id: missing'", "tool": "verlato_get_suggestion_item",
        }
        assert json.loads(tool_messages[3]["content"])["error"] == "Missing required string argument: query"
    finally:
        server.shutdown()


@pytest.mark.parametrize("strategy", ["single", "full-diff-json"])
def test_draft_suggest_adaptive_recommendations_match(tmp_path: Path, strategy: str):
    py_repo, rs_repo, env = _batch_setup(tmp_path, "git")
    item_id = json.loads(_run(_python("scan", "--base", "v1.0.0"), py_repo, env))[0]["id"]
    for repo, runner in ((py_repo, _python), (rs_repo, _rust)):
        for event, source in (("suggestion_applied", "model-suggestion"), ("suggestion_regenerated", "model-suggestion"),
                              ("suggestion_rejected", "other")):
            _run(runner("feedback", "record", "--event", event, "--item-id", item_id,
                        "--metadata-json", json.dumps({"source": source})), repo, _native_only(env) if runner is _rust else env)
    base = ("draft", "suggest", "--base", "v1.0.0", "--strategy", strategy, "--adaptive-recommendations")
    cases = [
        ((*base, "--all-visible", "--preview"), {}),
        ((*base, "--all-visible", "--preview"), {}),  # cache hits
        ((*base, "--all-visible", "--preview", "--no-cache"), {"VERLATO_MODEL_MAX_ITEMS": "0"}),
    ]
    rs_out = _compare_suggest_runs(cases, py_repo, rs_repo, env, identity=False)
    assert rs_out["adaptive_recommendations"]["schema_version"] == "verlato.suggestion_adaptive.v1"


def test_draft_suggest_cache_backends_match(tmp_path: Path):
    py_repo, rs_repo, env = _batch_setup(tmp_path, "git")
    env = {**env, "VERLATO_SUGGESTION_CACHE_BACKEND": "Redis"}
    args = ("draft", "suggest", "--base", "v1.0.0", "--all-visible", "--preview")
    python = subprocess.run(_python(*args), cwd=py_repo, env=env, capture_output=True, text=True)
    rust = subprocess.run(_rust(*args), cwd=rs_repo, env=_native_only(env), capture_output=True, text=True)
    message = "Unknown suggestion cache backend 'redis'; available: json"
    assert python.returncode != 0 and message in python.stderr
    assert rust.returncode != 0 and message in rust.stderr, rust.stderr
    _compare_suggest_runs([((*args, "--no-cache"), {})], py_repo, rs_repo, env, identity=False)

@pytest.mark.parametrize("engine", ["git", "intentumdiff"])
def test_draft_suggest_apply_matches(tmp_path: Path, engine: str):
    """`draft suggest --id` (no preview) records the model summary in review state natively."""
    py_repo, rs_repo, env = _suggest_setup(tmp_path, engine, "plugin", None)
    compare = (lambda value: value) if engine == "git" else _strip_engine_identity
    items = json.loads(_run(_python("scan", "--base", "v1.0.0"), py_repo, env))
    logs = {name: tmp_path / f"apply-{name}.jsonl" for name in ("py", "rs")}
    for item in items[:3]:
        args = ("draft", "suggest", "--base", "v1.0.0", "--id", item["id"], "--reviewer", "alice", "--tone", "concise")
        python_out = json.loads(_run(_python(*args), py_repo, {**env, "PARITY_LOG": str(logs["py"])}))
        rust_out = json.loads(_run(_rust(*args), rs_repo, _native_only({**env, "PARITY_LOG": str(logs["rs"])})))
        assert compare(rust_out) == compare(python_out), item["title"]
        assert rust_out["status"] == "edited"
        assert rust_out["summary_provenance"]["source"] == "model-generated"
    assert compare(_read_log(logs["rs"])) == compare(_read_log(logs["py"]))
    state_name = ".verlato_state.json"
    assert compare(json.loads((rs_repo / state_name).read_text())) == compare(
        json.loads((py_repo / state_name).read_text())
    )


def test_draft_suggest_apply_errors_match(tmp_path: Path):
    py_repo, rs_repo, env = _suggest_setup(tmp_path, "git", "plugin", None)
    empty_plugin = tmp_path / "empty_plugin.py"
    empty_plugin.write_text(
        "import json, sys\nrequest = json.load(sys.stdin)\n"
        "print(json.dumps({'request_hash': request['request_hash'], 'content': ''}))\n"
    )
    env = {**env, "VERLATO_PLUGIN_COMMAND": f"{sys.executable} {empty_plugin}"}
    item_id = json.loads(_run(_python("scan", "--base", "v1.0.0"), py_repo, env))[0]["id"]
    for args in (
        ("--id", item_id),
        ("--all-visible",),
        (),
        ("--id", "ci_missing"),
    ):
        cmd = ("draft", "suggest", "--base", "v1.0.0", *args)
        python = subprocess.run(_python(*cmd), cwd=py_repo, env=env, capture_output=True, text=True)
        rust = subprocess.run(_rust(*cmd), cwd=rs_repo, env=_native_only(env), capture_output=True, text=True)
        assert (rust.returncode != 0) == (python.returncode != 0) and rust.returncode != 0, args
        assert python.stderr.strip().splitlines()[-1] in rust.stderr, (args, python.stderr, rust.stderr)


def test_github_pr_adapter_matches(tmp_path: Path):
    """The GitHub PR adapter runs natively: same API requests, PR mapping and outputs."""
    from http.server import BaseHTTPRequestHandler
    from urllib.parse import parse_qs, urlparse

    py_repo, rs_repo, env = _pair(tmp_path, "git")
    shas = subprocess.run(
        ["git", "log", "--format=%H %s", "v1.0.0..HEAD"], cwd=py_repo, capture_output=True, text=True, check=True
    ).stdout.splitlines()
    by_subject = {line.split(" ", 1)[1]: line.split(" ", 1)[0] for line in shas}
    pr_commits = {by_subject["feat(api): add export endpoint"], by_subject["fix(api): handle empty export"]}
    failing = by_subject["Update app helpers"]
    files = [{"filename": f"api/file_{i}.py", "status": "modified", "patch": f"@@ -1 +1 @@\n-a\n+b{i}"} for i in range(101)]
    requests: list[str] = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):  # noqa: N802 - http.server API
            url = urlparse(self.path)
            requests.append(self.path)
            assert self.headers["Authorization"] == "Bearer gh-test"
            parts = url.path.strip("/").split("/")
            status, payload = 200, []
            if parts[3:4] == ["commits"]:
                sha = parts[4]
                if sha == failing:
                    status, payload = 500, {"message": "boom"}
                else:
                    payload = [{"number": 7}] if sha in pr_commits else []
            elif parts[-1] == "files":
                page = int(parse_qs(url.query)["page"][0])
                payload = files[(page - 1) * 100 : page * 100]
            else:
                payload = {
                    "title": "  Export API (#7)  ",
                    "body": "Adds CSV export.",
                    "labels": [{"name": "feature"}, {"name": ""}],
                    "base": {"ref": "main"},
                    "head": {"ref": "export"},
                }
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = _serve(Handler)
    try:
        env = {
            **env,
            "VERLATO_PR_ADAPTER": "github",
            "VERLATO_GITHUB_REPO": "acme/demo",
            "GITHUB_TOKEN": "gh-test",
            "VERLATO_GITHUB_API_URL": f"http://127.0.0.1:{server.server_address[1]}",
        }
        for args in (("scan", "--base", "v1.0.0"), ("draft", "json", "--base", "v1.0.0", "--version", "v1.1.0")):
            requests.clear()
            python_out = json.loads(_run(_python(*args), py_repo, env))
            python_requests = list(requests)
            requests.clear()
            rust_out = json.loads(_run(_rust(*args), rs_repo, _native_only(env)))
            assert rust_out == python_out, args
            assert requests == python_requests, args
            if args[0] == "scan":
                pr_items = [item for item in rust_out if item["source"]["pr_number"] == 7]
                assert pr_items and pr_items[0]["content"]["title"] == "Export API (#7)"
                assert any(len(item["evidence"]["files"]) >= 101 for item in pr_items)
                assert any("files?page=2&per_page=100" in path for path in requests)
    finally:
        server.shutdown()


# ---------------------------------------------------------------------------
# MCP server (`verlato serve`)
# ---------------------------------------------------------------------------

_PYDANTIC_DOCS = re.compile(r"errors\.pydantic\.dev/[0-9.]+/")


class _McpClient:
    """A JSON-RPC client over a server's stdio; answers are read one request at a time."""

    def __init__(self, cmd: list[str], repo: Path, env: dict[str, str]) -> None:
        self.process = subprocess.Popen(
            cmd, cwd=repo, env=env, stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True
        )
        self.next_id = 0

    def send(self, message: dict) -> None:
        assert self.process.stdin is not None
        self.process.stdin.write(json.dumps(message) + "\n")
        self.process.stdin.flush()

    def request(self, method: str, params: dict | None = None) -> dict:
        self.next_id += 1
        message: dict = {"jsonrpc": "2.0", "id": self.next_id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        assert self.process.stdout is not None
        line = self.process.stdout.readline()
        assert line, f"{method}: the server closed stdout"
        response = json.loads(line)
        assert response["id"] == self.next_id, response
        return response

    def close(self) -> str:
        assert self.process.stdin is not None and self.process.stderr is not None
        self.process.stdin.close()
        self.process.wait(timeout=60)
        return self.process.stderr.read()


def _mcp_normalize(value, repo: Path, versions: set[str], *, engine_identity: bool):
    """Runtime-neutral MCP payloads: versions, repo paths, pydantic doc links and (for the
    IntentumDiff engine) engine build identity are masked; JSON text content is parsed."""
    if isinstance(value, dict):
        out = {}
        for key, item in value.items():
            if key == "diff_engine" and isinstance(item, dict):
                item = {k: item.get(k) for k in ("engine", "configured")}
            out[key] = _mcp_normalize(item, repo, versions, engine_identity=engine_identity)
        return _strip_engine_identity(out) if engine_identity else out
    if isinstance(value, list):
        return [_mcp_normalize(item, repo, versions, engine_identity=engine_identity) for item in value]
    if isinstance(value, str):
        if value in versions:
            return "<version>"
        if value[:1] in "[{":
            try:
                parsed = json.loads(value)
            except ValueError:
                parsed = None
            if isinstance(parsed, (dict, list)):
                return {"<json>": _mcp_normalize(parsed, repo, versions, engine_identity=engine_identity)}
        text = _PYDANTIC_DOCS.sub("errors.pydantic.dev/<v>/", value.replace(str(repo), "<repo>"))
        for version in versions:
            text = text.replace(f'"version": "{version}"', '"version": "<version>"')
        return _strip_engine_identity(text) if engine_identity else text
    return value


def _mcp_pair(tmp_path: Path, engine: str, privacy: str | None = None):
    pytest.importorskip("fastmcp")
    py_repo, rs_repo, env = _pair(tmp_path, engine)
    if privacy is not None:
        for repo in (py_repo, rs_repo):
            (repo / "verlato.json").write_text(json.dumps({"model_privacy": privacy}))
    return py_repo, rs_repo, env


def _mcp_versions() -> set[str]:
    from verlato import __version__

    rust_version = _run(_rust("--version"), ROOT, dict(os.environ)).split()[-1]
    return {__version__, rust_version}


def _mcp_compare(python: dict, rust: dict, py_repo: Path, rs_repo: Path, *, engine_identity: bool, label) -> None:
    versions = _mcp_versions()
    expected = _mcp_normalize(python, py_repo, versions, engine_identity=engine_identity)
    actual = _mcp_normalize(rust, rs_repo, versions, engine_identity=engine_identity)
    assert actual == expected, label


_MCP_INIT = {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "parity", "version": "1"}}


@pytest.mark.parametrize("engine", ["git", "intentumdiff"])
@pytest.mark.parametrize("privacy", ["metadata-only", "trusted-provider-patch"])
def test_mcp_server_matches(tmp_path: Path, engine: str, privacy: str):
    """`verlato serve` runs natively and answers an MCP session exactly like the FastMCP server."""
    py_repo, rs_repo, env = _mcp_pair(tmp_path, engine, privacy)
    engine_identity = engine != "git"
    python = _McpClient(_python("serve"), py_repo, env)
    rust = _McpClient(_rust("serve"), rs_repo, _native_only(env))

    def both(method: str, params: dict | None = None) -> dict:
        python_response = python.request(method, params)
        rust_response = rust.request(method, params)
        _mcp_compare(
            python_response, rust_response, py_repo, rs_repo, engine_identity=engine_identity, label=(method, params)
        )
        return python_response

    def call(name: str, **arguments) -> dict:
        return both("tools/call", {"name": name, "arguments": arguments})["result"]

    try:
        assert both("initialize", _MCP_INIT)["result"]["protocolVersion"] == "2025-06-18"
        for client in (python, rust):
            client.send({"jsonrpc": "2.0", "method": "notifications/initialized"})
        tools = [tool["name"] for tool in both("tools/list")["result"]["tools"]]
        legacy = {"verlato_get_file_patch", "verlato_search_item_diff"}
        assert legacy <= set(tools) if engine == "git" else not legacy & set(tools)
        for method in ("resources/list", "resources/templates/list", "prompts/list", "ping"):
            both(method)

        call("verlato_service_health")
        call("verlato_service_info")
        scan = call("verlato_scan_unpublished_changes", base="v1.0.0")
        assert scan["structuredContent"]["result"]
        call("verlato_reconcile_preview", base="v1.0.0")
        call("verlato_get_draft_document", base="v1.0.0", version="v1.1.0", include_confidence=True)
        call("verlato_preview_release_notes", base="v1.0.0", version="v1.1.0", include_provenance="yes")
        listing = call("verlato_list_suggestion_items", base="v1.0.0", include_files=True)["structuredContent"]
        for item in listing["items"][:4]:
            item_id = item["item_id"]
            detail = call("verlato_get_suggestion_item", base="v1.0.0", item_id=item_id)
            assert not detail["isError"]
            for changed in item["files"]:
                semantic = call("verlato_get_file_semantic_diff", base="v1.0.0", item_id=item_id, path=changed["path"])
                assert not semantic["isError"]
            for query in ("export", "rows", "zzz-none"):
                call("verlato_search_semantic_diff", base="v1.0.0", item_id=item_id, query=query, limit=2)
            path = item["files"][0]["path"]
            call("verlato_get_file_patch", base="v1.0.0", item_id=item_id, path=path, offset=10, limit=40)
            call("verlato_search_item_diff", base="v1.0.0", item_id=item_id, query="return", limit="3")

        item_id = scan["structuredContent"]["result"][0]["id"]
        updated = call("verlato_update_review_state", base="v1.0.0", item_id=item_id, status="ready", reviewer="alice")
        assert updated["structuredContent"]["status"] == "ready"
        call("verlato_update_review_state", base="v1.0.0", item_id=item_id, summary="Adds the export endpoint.")
        applied = call("verlato_apply_draft", base="v1.0.0", version="v1.1.0")
        assert not applied["isError"]
        assert (rs_repo / "CHANGELOG.md").read_text() == (py_repo / "CHANGELOG.md").read_text()
        state = [json.loads((repo / ".verlato_state.json").read_text()) for repo in (py_repo, rs_repo)]
        compare = _strip_engine_identity if engine_identity else (lambda value: value)
        assert compare(state[1]) == compare(state[0])

        shas = _commit_shas(py_repo)
        call("verlato_draft_conventional_commit", base="v1.0.0", sha=shas[0][:10])
        call("verlato_draft_conventional_commits", base="v1.0.0", shas=[shas[1][:8], "deadbeef"])

        # Errors: unknown tools and items, failed validation, service errors.
        for name, arguments in (
            ("nope", {}),
            ("verlato_scan_unpublished_changes", {}),
            ("verlato_service_health", {"extra": 1}),
            ("verlato_get_draft_document", {"base": 5, "model_provider": "bad", "include_confidence": "maybe"}),
            ("verlato_apply_draft", {"base": "v1.0.0", "target": "wiki"}),
            ("verlato_draft_conventional_commits", {"base": "v1.0.0", "shas": ["a", 3]}),
            ("verlato_get_suggestion_item", {"base": "v1.0.0", "item_id": "ci_missing"}),
            ("verlato_get_file_semantic_diff", {"base": "v1.0.0", "item_id": item_id, "path": "nope.py"}),
            ("verlato_update_review_state", {"base": "v1.0.0", "item_id": item_id}),
            ("verlato_update_review_state", {"base": "v1.0.0", "item_id": "ci_missing", "status": "ready"}),
            ("verlato_draft_conventional_commit", {"base": "v1.0.0", "sha": "deadbeef"}),
        ):
            assert both("tools/call", {"name": name, "arguments": arguments})["result"]["isError"], name

        for uri in (
            "verlato://overview",
            "verlato://guidance/readme",
            "verlato://guidance/skill",
            "verlato://sample-config",
            "verlato://guidance/zzz",
            "verlato://nope",
        ):
            both("resources/read", {"uri": uri})
        both("prompts/get", {"name": "verlato_review_request", "arguments": {"base": "v1.0.0", "head": "main"}})
        both("prompts/get", {"name": "verlato_review_request", "arguments": {}})
        both("prompts/get", {"name": "nope"})
        both("bogus/method")
        both("tools/call", {})
        assert both("initialize", {**_MCP_INIT, "protocolVersion": "1999-01-01"})["result"]["protocolVersion"]
    finally:
        python.close()
        rust_stderr = rust.close()
    assert "not available in the Rust build" not in rust_stderr


def test_mcp_request_model_input_matches(tmp_path: Path):
    """`verlato_request_model_input` asks the provider natively and records the same summary."""
    pytest.importorskip("fastmcp")
    py_repo, rs_repo, env = _suggest_setup(tmp_path, "git", "plugin", None)
    logs = {name: tmp_path / f"mcp-{name}.jsonl" for name in ("py", "rs")}
    python = _McpClient(_python("serve"), py_repo, {**env, "PARITY_LOG": str(logs["py"])})
    rust = _McpClient(_rust("serve"), rs_repo, _native_only({**env, "PARITY_LOG": str(logs["rs"])}))
    try:
        for client in (python, rust):
            client.request("initialize", _MCP_INIT)
        item_id = json.loads(_run(_python("scan", "--base", "v1.0.0"), py_repo, env))[0]["id"]
        for arguments in (
            {"base": "v1.0.0", "item_id": item_id, "reviewer": "alice"},
            {"base": "v1.0.0", "item_id": "ci_missing"},
        ):
            params = {"name": "verlato_request_model_input", "arguments": arguments}
            python_response, rust_response = python.request("tools/call", params), rust.request("tools/call", params)
            _mcp_compare(python_response, rust_response, py_repo, rs_repo, engine_identity=False, label=arguments)
        assert python_response["result"]["isError"]
    finally:
        python.close()
        rust.close()
    assert _read_log(logs["rs"]) == _read_log(logs["py"])
    assert json.loads((rs_repo / ".verlato_state.json").read_text()) == json.loads(
        (py_repo / ".verlato_state.json").read_text()
    )


# ---------------------------------------------------------------------------
# maintenance commands
# ---------------------------------------------------------------------------

_MODEL_LOG_LINES = [
    {
        "Timestamp": "2026-01-02T03:04:05.000006Z",
        "ObservedTimestamp": "2026-01-02T03:04:05.000007Z",
        "SeverityText": "INFO",
        "SeverityNumber": 9,
        "EventName": "verlato.model.request",
        "Body": "post http://x/api/chat token=sk-abcdefghijklmnopqrstuvwx é",
        "Attributes": {"provider": "ollama", "nested": {"api_key": "AKIAABCDEFGHIJKLMNOP"}, "ratio": 0.5, "n": 1e-05},
        "Resource": {"service.name": "verlato"},
        "InstrumentationScope": {"name": "verlato.providers"},
    },
    {"EventName": "", "Body": 7, "Attributes": [1], "InstrumentationScope": "bad"},
    {
        "event": "verlato.model.response",
        "message": "done",
        "payload": "Bearer abcdefghijklmnopqrstuvwxyz",
        "attributes": {"redacted": 1},
    },
    {"message": None, "attributes": {"tags": ["a", "ghp_" + "a" * 36]}},
]


def _maintenance_setup(tmp_path: Path):
    py_repo, rs_repo, env = _pair(tmp_path, "git")
    log = tmp_path / "model-log.jsonl"
    log.write_text("\n".join(json.dumps(line) for line in _MODEL_LOG_LINES) + "\n\n")
    events = [
        ("suggestion_applied", "a", {"tone": "concise", "specificity_score": 0.3}),
        ("suggestion_applied", "b", {"tone": "concise", "specificity_score": "0.2"}),
        ("summary_edited", "c", {"tone": "technical", "specificity_score": 0.41}),
        ("suggestion_regenerated", "a", {"tone": "technical"}),
        ("suggestion_regenerated", "b", {}),
        ("suggestion_skipped", "c", {"tone": "é"}),
        ("suggestion_rejected", "d", {}),
    ]
    for repo in (py_repo, rs_repo):
        for event, item_id, metadata in events:
            args = ("feedback", "record", "--event", event, "--item-id", item_id, "--metadata-json", json.dumps(metadata))
            _run(_python(*args), repo, env)
    return py_repo, rs_repo, env, log


def test_maintenance_commands_match(tmp_path: Path):
    """`model-logs export`, `feedback export|recommend` and `state clear-suggestion-cache`."""
    py_repo, rs_repo, env, log = _maintenance_setup(tmp_path)
    rs_env = _native_only(env)
    for args in (
        ("model-logs", "export", "--file", str(log)),
        ("model-logs", "export", "--file", str(log), "--format", "jsonl"),
        ("model-logs", "export", "--file", str(log), "--event", "verlato.model.response", "--event", "verlato.model.log"),
        ("feedback", "export"),
        ("feedback", "export", "--format", "jsonl", "--event", "suggestion_applied"),
        ("feedback", "export", "--event", "nothing"),
        ("feedback", "export", "--event", "nothing", "--format", "jsonl"),
        ("feedback", "recommend"),
    ):
        assert _run(_rust(*args), rs_repo, rs_env) == _run(_python(*args), py_repo, env), args
    for redact in ("0", "1"):
        args = ("model-logs", "export", "--file", str(log))
        redact_env = {**env, "VERLATO_REDACT_MODEL_PAYLOADS": redact}
        assert _run(_rust(*args), rs_repo, _native_only(redact_env)) == _run(_python(*args), py_repo, redact_env)

    for name, repo, command, run_env in (("py", py_repo, _python, env), ("rs", rs_repo, _rust, rs_env)):
        out = tmp_path / f"{name}-feedback.json"
        _run(command("feedback", "export", "--output", str(out)), repo, run_env)
        _run(command("model-logs", "export", "--file", str(log), "--format", "jsonl", "--output", f"{out}l"), repo, run_env)
    for suffix in ("json", "jsonl"):
        assert (tmp_path / f"rs-feedback.{suffix}").read_text() == (tmp_path / f"py-feedback.{suffix}").read_text()

    empty_py, empty_rs = tmp_path / "empty-py", tmp_path / "empty-rs"
    empty_py.mkdir()
    empty_rs.mkdir()
    assert _run(_rust("feedback", "recommend"), empty_rs, rs_env) == _run(_python("feedback", "recommend"), empty_py, env)

    entry = {"created_at": "2099-01-01T00:00:00Z", "updated_at": "2099-01-01T00:00:00Z", "item": {"summary": "x"}}
    cache = {
        "schema_version": "v1",
        "created_at": "2026-01-01T00:00:00Z",
        "updated_at": "2099-01-01T00:00:00Z",
        "entries": {"k1": entry, "k2": entry},
    }
    args = ("state", "clear-suggestion-cache")
    for cache_env in ({}, {"VERLATO_SUGGESTION_CACHE_PATH": "./cache//sub/c.json"}):
        relative = cache_env.get("VERLATO_SUGGESTION_CACHE_PATH", ".verlato_suggestion_cache.json")
        for repo in (py_repo, rs_repo):
            (repo / relative).parent.mkdir(parents=True, exist_ok=True)
            (repo / relative).write_text(json.dumps(cache))
        rust_out = _run(_rust(*args), rs_repo, {**rs_env, **cache_env})
        assert rust_out == _run(_python(*args), py_repo, {**env, **cache_env})
        assert json.loads(rust_out)["removed_entries"] == 2
        assert not (rs_repo / relative).exists()
        # Clearing again finds nothing.
        assert _run(_rust(*args), rs_repo, {**rs_env, **cache_env}) == _run(_python(*args), py_repo, {**env, **cache_env})


def test_maintenance_errors_match(tmp_path: Path):
    py_repo, rs_repo, env, _ = _maintenance_setup(tmp_path)
    for args, extra in (
        (("model-logs", "export", "--file", "missing.jsonl"), {}),
        (("state", "clear-suggestion-cache"), {"VERLATO_SUGGESTION_CACHE_BACKEND": "Redis"}),
    ):
        python = subprocess.run(_python(*args), cwd=py_repo, env={**env, **extra}, capture_output=True, text=True)
        rust = subprocess.run(
            _rust(*args), cwd=rs_repo, env=_native_only({**env, **extra}), capture_output=True, text=True
        )
        assert python.returncode != 0 and rust.returncode != 0, args
        assert rust.stderr.strip().splitlines()[-1] in python.stderr, (rust.stderr, python.stderr)


def _doctor_ollama_handler(*, ps_status: int = 200, content: str = "Adds a model health check."):
    from http.server import BaseHTTPRequestHandler

    class Handler(BaseHTTPRequestHandler):
        bodies: list[dict] = []

        def _send(self, status: int, payload: dict) -> None:
            data = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

        def do_GET(self):  # noqa: N802 - http.server API
            if ps_status != 200:
                self._send(ps_status, {"error": "nope"})
                return
            self._send(200, {"models": [
                {"name": "other:7b", "model": "other:7b", "size_vram": 1},
                {
                    "name": "parity-model:latest", "model": "parity-model:latest", "size": 5,
                    "size_vram": 0, "context_length": 16384, "expires_at": "2999-01-01T00:00:00.123456Z",
                },
            ]})

        def do_POST(self):  # noqa: N802 - http.server API
            body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
            Handler.bodies.append(body)
            answer = content
            if "Input JSON" in body["messages"][-1]["content"]:
                answer = json.dumps({"items": [{"item_id": "doctor_model", "suggestions": [" Clarifies notes. ", 3]}]})
            self._send(200, {
                "message": {"role": "assistant", "content": answer},
                "done": True, "done_reason": "stop", "eval_count": 12, "prompt_eval_count": 50,
                "total_duration": 1_000_000_000_000, "load_duration": 900_000_000_000,
                "prompt_eval_duration": 30_000_000, "eval_duration": 40_000_000,
            })

        def log_message(self, *args):
            pass

    return Handler


_ELAPSED = re.compile(r'"(elapsed_ms|model_ms|queue_ms)": \d+')
_WARMUP_ELAPSED = re.compile(r"Warmup completed in \d+ms")


def _doctor_pair(tmp_path: Path, handler, args: tuple[str, ...], config: dict, runtime_env=None):
    """Run a doctor command in both runtimes against a fake Ollama; returns (stdout, request bodies)."""
    server = _serve(handler)
    try:
        py_repo, rs_repo, env = _pair(tmp_path, "git")
        env.pop("VERLATO_MODEL_PROVIDER")
        env.update({
            "VERLATO_OLLAMA_ENDPOINT": f"http://127.0.0.1:{server.server_address[1]}/api/chat",
            "VERLATO_OLLAMA_MODEL": "parity-model",
        })
        outputs = []
        for name, repo, command in (("py", py_repo, _python), ("rs", rs_repo, _rust)):
            run_env = {**env, **(runtime_env(name) if runtime_env else {})}
            if name == "rs":
                run_env = _native_only(run_env)
            (repo / "verlato.json").write_text(json.dumps(config))
            handler.bodies.clear()
            out = _run(command(*args), repo, run_env)
            out = _WARMUP_ELAPSED.sub("Warmup completed in 0ms", _ELAPSED.sub(r'"\1": 0', out))
            outputs.append((out, list(handler.bodies)))
    finally:
        server.shutdown()
    return outputs


@pytest.mark.parametrize("ps_status", [200, 404])
def test_doctor_ollama_matches(tmp_path: Path, ps_status: int):
    handler = _doctor_ollama_handler(ps_status=ps_status)
    config = {"provider": {"name": "ollama", "num_ctx": 8192}, "suggestion_parallelism": 3}
    args = ("doctor", "ollama", "--warmup", "--parallel-probe", "--suggest-model", " ")
    (py_out, py_bodies), (rs_out, rs_bodies) = _doctor_pair(tmp_path, handler, args, config)
    assert rs_out == py_out
    assert rs_bodies == py_bodies and len(rs_bodies) == 4
    report = json.loads(rs_out)
    assert report["summary"]["bottleneck"] == "load"
    assert [probe["name"] for probe in report["probes"]] == ["warmup", "single", "parallel-a", "parallel-b"]
    assert report["runtime"]["selected_model"]["loaded"] is (True if ps_status == 200 else None)


def test_doctor_ollama_failures_match(tmp_path: Path):
    handler = _doctor_ollama_handler(content="")
    (py_out, _), (rs_out, _) = _doctor_pair(tmp_path, handler, ("doctor", "ollama"), {"provider": {"name": "ollama"}})
    assert rs_out == py_out
    assert json.loads(rs_out)["summary"]["status"] == "error"


def _adaptive_doctor_view(report: dict) -> dict:
    """Doctor adaptive output without the parts that depend on real queueing time."""
    adaptive = report["adaptive_recommendations"]
    signals = {
        key: value
        for key, value in adaptive["signals"].items()
        if not key.endswith(("_ms", "_share")) and key != "dominant_bottleneck"
    }
    kinds = [item["kind"] for item in adaptive["recommendations"] if item["kind"] != "reduce-queueing"]
    return {"current": adaptive["current"], "signals": signals, "kinds": kinds, "schema": adaptive["schema_version"]}


def test_doctor_ollama_adaptive_matches(tmp_path: Path):
    handler = _doctor_ollama_handler()
    config = {
        "provider": {"name": "ollama"},
        "suggestion_parallelism": 2,
        "suggestion_prompt_profile": "lean",
        "suggestion_batch_size": 3,
        "suggestion_strategy": "paired",
        "suggestion_count": 2,
    }
    args = ("doctor", "ollama", "--parallel-probe", "--adaptive-recommendations")
    (py_out, py_bodies), (rs_out, rs_bodies) = _doctor_pair(tmp_path, handler, args, config)
    py_report, rs_report = json.loads(py_out), json.loads(rs_out)
    assert list(rs_report) == list(py_report)
    assert list(rs_report["adaptive_recommendations"]) == list(py_report["adaptive_recommendations"])
    assert _adaptive_doctor_view(rs_report) == _adaptive_doctor_view(py_report)
    assert rs_bodies == py_bodies
    assert rs_report["adaptive_recommendations"]["signals"]["source"] == "doctor"


@pytest.mark.parametrize("provider", ["ollama", "plugin"])
def test_doctor_model_matches(tmp_path: Path, provider: str):
    handler = _doctor_ollama_handler()
    logs = {name: tmp_path / f"{name}.jsonl" for name in ("py", "rs")}
    if provider == "plugin":
        plugin = tmp_path / "plugin.py"
        plugin.write_text(_PARITY_PLUGIN)
        config = {
            "provider": {"name": "plugin", "plugin_command": [sys.executable, str(plugin)], "plugin_env": ["PARITY_LOG"]},
            "suggestion_tone": "technical",
            "suggestion_style": {"guidance": "Name the user-visible effect.", "specificity_floor": 0.3},
        }
        args = ("doctor", "model", "--timeout-seconds", "0.000001")
    else:
        config = {"provider": {"name": "ollama", "timeout_seconds": 30}}
        args = ("doctor", "model", "--model-provider", "ollama", "--suggest-model", "tiny")
    (py_out, py_bodies), (rs_out, rs_bodies) = _doctor_pair(
        tmp_path, handler, args, config, lambda name: {"PARITY_LOG": str(logs[name])}
    )
    assert rs_out == py_out
    assert rs_bodies == py_bodies
    if provider == "plugin":
        assert _read_log(logs["rs"]) == _read_log(logs["py"]) != []
        assert json.loads(rs_out)["status"] == "error"
    else:
        assert json.loads(rs_out)["status"] == "ok"
        assert rs_bodies[0]["model"] == "tiny" and rs_bodies[0]["format"]["required"] == ["items"]


@pytest.mark.parametrize("engine", ["git", "intentumdiff"])
def test_suggestion_service_methods_match(tmp_path: Path, engine: str):
    py_repo, rs_repo, env = _pair(tmp_path, engine)
    items = json.loads(_run(_python("scan", "--base", "v1.0.0"), py_repo, env))
    item = next(entry for entry in items if entry["evidence"]["files"])
    item_id, path = item["id"], item["evidence"]["files"][0]
    base = {"base": "v1.0.0", "item_id": item_id}
    requests = [
        {"id": 1, "method": "suggestion.items", "params": {"base": "v1.0.0", "include_files": True}},
        {"id": 2, "method": "suggestion.items", "params": {"base": "v1.0.0"}},
        {"id": 3, "method": "suggestion.item", "params": base},
        {"id": 4, "method": "suggestion.item", "params": {"base": "v1.0.0", "item_id": "missing"}},
        {"id": 5, "method": "suggestion.patch", "params": {**base, "path": path, "offset": "3", "limit": 2.9}},
        {"id": 6, "method": "suggestion.patch", "params": base},
        {"id": 7, "method": "suggestion.semantic_diff", "params": {**base, "path": path}},
        {"id": 8, "method": "suggestion.semantic_search", "params": {**base, "query": "export", "limit": "x"}},
        {"id": 9, "method": "suggestion.search", "params": {**base, "query": "return"}},
        {"id": 10, "method": "suggestion.items", "params": {}},
        {"id": 11, "method": "state.clear_suggestion_cache"},
        {"id": 12, "method": "shutdown"},
    ]
    for repo in (py_repo, rs_repo):
        (repo / ".verlato_suggestion_cache.json").write_text(json.dumps({"schema_version": "v1", "entries": {}}))
    payload = "".join(json.dumps(request) + "\n" for request in requests)

    def serve(cmd: list[str], repo: Path, run_env: dict[str, str]) -> list[dict]:
        result = subprocess.run(cmd, cwd=repo, env=run_env, input=payload, capture_output=True, text=True, timeout=600)
        return [json.loads(line) for line in result.stdout.splitlines() if line.strip()]

    python_responses = serve(_python("service"), py_repo, env)
    rust_responses = serve(_rust("service"), rs_repo, _native_only(env))
    compare = (lambda value: value) if engine == "git" else _strip_engine_identity
    assert len(rust_responses) == len(python_responses) == len(requests)
    for python_response, rust_response in zip(python_responses, rust_responses, strict=True):
        assert compare(rust_response) == compare(python_response), python_response["id"]
    assert rust_responses[0]["ok"] and rust_responses[3]["error"]["code"] == "KeyError"
