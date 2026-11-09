"""MCP agents get IntentumDiff's semantic diff, with detail gated by the privacy mode."""

from __future__ import annotations

import importlib.util
import json
import subprocess
from pathlib import Path

import pytest

from verlato.mcp.service import MCPService

requires_engine = pytest.mark.skipif(
    importlib.util.find_spec("intentumdiff") is None, reason="intentumdiff-python is not installed"
)


def _repo(tmp_path: Path) -> Path:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(["git", *args], cwd=repo, check=True, capture_output=True, text=True)

    git("init")
    git("config", "user.email", "t@example.com")
    git("config", "user.name", "Tester")
    (repo / "app.py").write_text("def greet(name):\n    return 'hi ' + name\n\nTIMEOUT = 30\n")
    git("add", ".")
    git("commit", "-m", "init")
    git("tag", "v1")
    (repo / "app.py").write_text("def greet(name, punct='!'):\n    return 'hi ' + name + punct\n\nTIMEOUT = 60\n")
    git("commit", "-am", "update app")
    return repo


@requires_engine
@pytest.mark.parametrize(("privacy", "detailed"), [("metadata-only", False), ("trusted-provider-patch", True)])
def test_mcp_semantic_diff_tools(monkeypatch, tmp_path: Path, privacy: str, detailed: bool):
    repo = _repo(tmp_path)
    monkeypatch.chdir(repo)
    monkeypatch.setenv("VERLATO_STATE_PATH", str(tmp_path / "state.json"))
    monkeypatch.setenv("VERLATO_MODEL_PRIVACY", privacy)
    service = MCPService()
    assert service.uses_semantic_diff() is True
    assert service.semantic_detail_allowed() is detailed

    item = service.list_suggestion_items(base="v1")["items"][0]
    manifest = service.get_suggestion_item(base="v1", item_id=item["item_id"])
    assert manifest["evidence_source"] == "intentumdiff-semantic-diff"
    assert manifest["semantic_review"]["signature_changes"] == ["greet"]

    result = service.get_file_semantic_diff(base="v1", item_id=item["item_id"], path="app.py")
    assert result["semantic_diff_available"] is True
    assert result["detail"] == ("detailed" if detailed else "structure")
    assert "meaningful in greet: addition default_parameter punct" in result["text"]
    assert "@@" not in json.dumps(result) and "+def greet" not in json.dumps(result)
    assert ("30 -> 60" in json.dumps(result)) is detailed

    found = service.search_semantic_diff(base="v1", item_id=item["item_id"], query="punct")
    assert found["results"] and found["results"][0]["path"] == "app.py"


@requires_engine
def test_local_privacy_mode_is_not_detailed_for_remote_agents(monkeypatch, tmp_path: Path):
    monkeypatch.chdir(_repo(tmp_path))
    monkeypatch.setenv("VERLATO_MODEL_PRIVACY", "local-summary-then-ai")
    assert MCPService().semantic_detail_allowed() is False


def test_stdio_service_routes_semantic_diff_methods():
    import io

    from verlato.service_stdio import run_service_stdio

    class _Fake:
        def get_file_semantic_diff(self, **kwargs):
            return {"kind": "file", **kwargs}

        def search_semantic_diff(self, **kwargs):
            return {"kind": "search", **kwargs}

    requests = [
        {"id": 1, "method": "suggestion.semantic_diff", "params": {"base": "v1", "item_id": "ci_1", "path": "a.py"}},
        {"id": 2, "method": "suggestion.semantic_search", "params": {"base": "v1", "item_id": "ci_1", "query": "x"}},
    ]
    out = io.StringIO()
    run_service_stdio(service=_Fake(), input_stream=io.StringIO("".join(json.dumps(r) + "\n" for r in requests)),
                      output_stream=out)
    responses = [json.loads(line) for line in out.getvalue().splitlines()]
    assert responses[0]["result"] == {"kind": "file", "base": "v1", "head": "HEAD", "item_id": "ci_1", "path": "a.py"}
    assert responses[1]["result"]["kind"] == "search" and responses[1]["result"]["limit"] == 5
