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
    args = ("state", "clear-suggestion-cache")
    assert json.loads(_run(_rust(*args), rs_repo, env)) == json.loads(_run(_python(*args), py_repo, env))

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


def test_draft_suggest_batched_strategy_is_delegated(tmp_path: Path):
    _, rs_repo, env = _pair(tmp_path, "git")
    for args, reason in (
        (("--strategy", "full-diff-json", "--model-provider", "litellm", "--privacy", "trusted-provider-patch"),
         "full-diff-json suggestion strategy"),
        (("--model-provider", "ollama", "--allow-remote-patches"), "tool-retrieval suggestion strategy"),
        (("--model-provider", "mock", "--adaptive-recommendations"), "adaptive suggestion recommendations"),
    ):
        result = subprocess.run(
            _rust("draft", "suggest", "--base", "v1.0.0", "--all-visible", "--preview", *args),
            cwd=rs_repo,
            env=_native_only(env),
            capture_output=True,
            text=True,
        )
        assert result.returncode != 0, args
        assert "not available in the Rust build yet" in result.stderr and reason in result.stderr, result.stderr


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
