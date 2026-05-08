import json
import subprocess
from pathlib import Path

from verlato.providers.base import ProviderRequest
from verlato.providers.cli_agents import ClaudeCodeProvider, CodexCliProvider


def test_codex_cli_provider_uses_stdin_and_last_message_file(monkeypatch, tmp_path: Path):
    calls = []

    def fake_run(args, *, input, cwd, capture_output, text, timeout, check):
        calls.append(
            {
                "args": args,
                "input": input,
                "cwd": cwd,
                "capture_output": capture_output,
                "text": text,
                "timeout": timeout,
                "check": check,
            }
        )
        output_path = Path(args[args.index("--output-last-message") + 1])
        output_path.write_text("Clarifies login flow updates", encoding="utf-8")
        return subprocess.CompletedProcess(args, 0, stdout="", stderr="")

    monkeypatch.setattr("shutil.which", lambda command: f"/bin/{command}")
    monkeypatch.setattr("subprocess.run", fake_run)

    provider = CodexCliProvider(command="codex", model="default", timeout_seconds=7, cwd=str(tmp_path))
    response = provider.summarize(
        ProviderRequest(task="suggest", prompt="Suggest a release note", evidence={"title": "Improve login"})
    )

    assert response.content == "Clarifies login flow updates"
    assert response.model == "codex-cli:default"
    assert calls[0]["args"][:2] == ["/bin/codex", "exec"]
    assert "--ask-for-approval" not in calls[0]["args"]
    assert calls[0]["args"][-1] == "-"
    assert "Suggest a release note" in calls[0]["input"]
    assert '"title": "Improve login"' in calls[0]["input"]
    assert calls[0]["cwd"] == str(tmp_path)
    assert calls[0]["timeout"] == 7


def test_claude_code_provider_parses_json_result(monkeypatch, tmp_path: Path):
    calls = []

    def fake_run(args, *, cwd, capture_output, text, timeout, check):
        calls.append({"args": args, "cwd": cwd, "timeout": timeout})
        return subprocess.CompletedProcess(args, 0, stdout=json.dumps({"result": "Highlights dashboard updates"}), stderr="")

    monkeypatch.setattr("shutil.which", lambda command: f"/bin/{command}")
    monkeypatch.setattr("subprocess.run", fake_run)

    provider = ClaudeCodeProvider(command="claude", model="sonnet", timeout_seconds=9, cwd=str(tmp_path))
    response = provider.summarize(
        ProviderRequest(task="suggest", prompt="Suggest a release note", evidence={"title": "Tune dashboard"})
    )

    assert response.content == "Highlights dashboard updates"
    assert response.model == "claude-code:sonnet"
    assert calls[0]["args"][:5] == ["/bin/claude", "--print", "--output-format", "json", "--max-turns"]
    assert calls[0]["args"][-1].startswith("You are Verlato running through Claude Code")
    assert calls[0]["cwd"] == str(tmp_path)
    assert calls[0]["timeout"] == 9


def test_cli_agent_provider_reports_unavailable_command(monkeypatch):
    monkeypatch.setattr("shutil.which", lambda command: None)

    response = CodexCliProvider(command="missing-codex").summarize(
        ProviderRequest(task="suggest", prompt="Suggest", evidence={})
    )

    assert response.content.startswith("[codex-cli-unavailable]")
    assert response.confidence == 0.0
