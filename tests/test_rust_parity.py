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
_ENGINE_DERIVED = {"engine_version", "json_chars", "evidence_chars", "estimated_tokens"}


def _strip_engine_identity(value):
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


def test_commit_intent_inference_is_still_delegated(tmp_path: Path):
    _, rs_repo, env = _pair(tmp_path, "git")
    result = subprocess.run(
        _rust("scan", "--base", "v1.0.0", "--infer-commit-intents"),
        cwd=rs_repo,
        env=_native_only(env),
        capture_output=True,
        text=True,
    )
    assert result.returncode != 0
    assert "commit intent inference" in result.stderr


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
