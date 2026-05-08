from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path
from time import perf_counter

from verlato.providers.base import ProviderRequest, ProviderResponse, VerlatoProvider
from verlato.providers.model_logging import ModelLogSink, default_model_log_sink, model_log_record

DEFAULT_CLI_AGENT_TIMEOUT_SECONDS = 120.0


class CodexCliProvider(VerlatoProvider):
    """
    Provider backed by a locally authenticated Codex CLI.

    This lets users use their Codex/ChatGPT login as an escape hatch from a
    slow local model without adding an OpenAI API key to Verlato.
    """

    def __init__(
        self,
        *,
        command: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        cwd: str | None = None,
        log_sink: ModelLogSink | None = None,
    ) -> None:
        self.command = command or os.getenv("VERLATO_CODEX_COMMAND") or "codex"
        self.model = _optional_env("VERLATO_CODEX_MODEL", model) or "default"
        self.timeout_seconds = _timeout_seconds("VERLATO_CODEX_TIMEOUT_SECONDS", timeout_seconds)
        self.cwd = cwd or os.getcwd()
        self.log_sink = log_sink or default_model_log_sink()

    def summarize(self, request: ProviderRequest) -> ProviderResponse:
        return self._invoke(task="summarize", request_data=request)

    def classify(self, request: ProviderRequest) -> ProviderResponse:
        return self._invoke(task="classify", request_data=request)

    def _invoke(self, *, task: str, request_data: ProviderRequest) -> ProviderResponse:
        executable = shutil.which(self.command)
        response_model = f"codex-cli:{self.model}"
        if not executable:
            return ProviderResponse(
                content=f"[codex-cli-unavailable] command not found: {self.command}",
                confidence=0.0,
                model=response_model,
            )

        prompt = _agent_prompt(task=task, request_data=request_data, provider="Codex CLI")
        with tempfile.TemporaryDirectory(prefix="verlato-codex-") as tmp_dir:
            output_path = Path(tmp_dir) / "last-message.txt"
            args = [
                executable,
                "exec",
                "--sandbox",
                "read-only",
                "--skip-git-repo-check",
                "--ephemeral",
                "--output-last-message",
                str(output_path),
            ]
            if self.model != "default":
                args.extend(["--model", self.model])
            args.append("-")
            try:
                result, elapsed_ms = self._run(args, prompt)
            except subprocess.TimeoutExpired:
                return _cli_timeout_response(
                    provider="codex-cli",
                    model=response_model,
                    task=task,
                    timeout_seconds=self.timeout_seconds,
                )
            if result.returncode != 0:
                return _cli_error_response(
                    provider="codex-cli",
                    model=response_model,
                    task=task,
                    result=result,
                    elapsed_ms=elapsed_ms,
                )
            try:
                content = output_path.read_text(encoding="utf-8").strip()
            except OSError:
                content = result.stdout.strip()
        return _cli_success_response(
            provider="codex-cli",
            model=response_model,
            task=task,
            content=content,
            elapsed_ms=elapsed_ms,
        )

    def _run(self, args: list[str], prompt: str) -> tuple[subprocess.CompletedProcess[str], int]:
        self._trace(
            f"post provider=codex-cli task=agent command={self.command} model={self.model} "
            f"prompt_chars={len(prompt)} timeout_seconds={self.timeout_seconds:g}"
        )
        start = perf_counter()
        result = subprocess.run(
            args,
            input=prompt,
            cwd=self.cwd,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        elapsed_ms = int((perf_counter() - start) * 1000)
        self._trace(
            f"response provider=codex-cli returncode={result.returncode} stdout_chars={len(result.stdout)} "
            f"stderr_chars={len(result.stderr)} elapsed_ms={elapsed_ms}"
        )
        return result, elapsed_ms

    def _trace(self, message: str, *, error: bool = False) -> None:
        self.log_sink.emit(
            model_log_record(
                message,
                event_name="verlato.model.codex_cli",
                attributes={"provider": "codex-cli", "model": self.model},
                error=error,
            )
        )


class ClaudeCodeProvider(VerlatoProvider):
    """
    Provider backed by a locally authenticated Claude Code CLI.

    Claude Code supports Claude subscription login as well as Console/API
    login, so this is a subscription-friendly remote escape hatch.
    """

    def __init__(
        self,
        *,
        command: str | None = None,
        model: str | None = None,
        timeout_seconds: float | None = None,
        cwd: str | None = None,
        log_sink: ModelLogSink | None = None,
    ) -> None:
        self.command = command or os.getenv("VERLATO_CLAUDE_COMMAND") or "claude"
        self.model = _optional_env("VERLATO_CLAUDE_MODEL", model) or "default"
        self.timeout_seconds = _timeout_seconds("VERLATO_CLAUDE_TIMEOUT_SECONDS", timeout_seconds)
        self.cwd = cwd or os.getcwd()
        self.log_sink = log_sink or default_model_log_sink()

    def summarize(self, request: ProviderRequest) -> ProviderResponse:
        return self._invoke(task="summarize", request_data=request)

    def classify(self, request: ProviderRequest) -> ProviderResponse:
        return self._invoke(task="classify", request_data=request)

    def _invoke(self, *, task: str, request_data: ProviderRequest) -> ProviderResponse:
        executable = shutil.which(self.command)
        response_model = f"claude-code:{self.model}"
        if not executable:
            return ProviderResponse(
                content=f"[claude-code-unavailable] command not found: {self.command}",
                confidence=0.0,
                model=response_model,
            )

        prompt = _agent_prompt(task=task, request_data=request_data, provider="Claude Code")
        args = [
            executable,
            "--print",
            "--output-format",
            "json",
            "--max-turns",
            "1",
            "--permission-mode",
            "plan",
        ]
        if self.model != "default":
            args.extend(["--model", self.model])
        args.append(prompt)
        try:
            result, elapsed_ms = self._run(args)
        except subprocess.TimeoutExpired:
            return _cli_timeout_response(
                provider="claude-code",
                model=response_model,
                task=task,
                timeout_seconds=self.timeout_seconds,
            )
        if result.returncode != 0:
            return _cli_error_response(
                provider="claude-code",
                model=response_model,
                task=task,
                result=result,
                elapsed_ms=elapsed_ms,
            )
        content = _parse_claude_json_result(result.stdout) or result.stdout.strip()
        return _cli_success_response(
            provider="claude-code",
            model=response_model,
            task=task,
            content=content,
            elapsed_ms=elapsed_ms,
        )

    def _run(self, args: list[str]) -> tuple[subprocess.CompletedProcess[str], int]:
        self._trace(
            f"post provider=claude-code task=agent command={self.command} model={self.model} "
            f"timeout_seconds={self.timeout_seconds:g}"
        )
        start = perf_counter()
        result = subprocess.run(
            args,
            cwd=self.cwd,
            capture_output=True,
            text=True,
            timeout=self.timeout_seconds,
            check=False,
        )
        elapsed_ms = int((perf_counter() - start) * 1000)
        self._trace(
            f"response provider=claude-code returncode={result.returncode} stdout_chars={len(result.stdout)} "
            f"stderr_chars={len(result.stderr)} elapsed_ms={elapsed_ms}"
        )
        return result, elapsed_ms

    def _trace(self, message: str, *, error: bool = False) -> None:
        self.log_sink.emit(
            model_log_record(
                message,
                event_name="verlato.model.claude_code",
                attributes={"provider": "claude-code", "model": self.model},
                error=error,
            )
        )


def _agent_prompt(*, task: str, request_data: ProviderRequest, provider: str) -> str:
    if task == "classify":
        output = "Return exactly one line in the form kind/area/audience/semver. Do not include prose."
    else:
        output = "Return only the release-note suggestion text. Do not include Markdown fences or commentary."
    if request_data.response_format is not None:
        output = "Return only JSON matching the requested schema. Do not include Markdown fences or commentary."
    return (
        f"You are Verlato running through {provider}. Use only the supplied prompt and evidence. "
        f"Do not edit files or run tools.\n{output}\n\n"
        f"Task: {task}\n"
        f"Prompt:\n{request_data.prompt}\n\n"
        f"Evidence JSON:\n{json.dumps(request_data.evidence, sort_keys=True, default=str)}"
    )


def _cli_success_response(
    *,
    provider: str,
    model: str,
    task: str,
    content: str,
    elapsed_ms: int,
) -> ProviderResponse:
    normalized = content.strip()
    if not normalized:
        return ProviderResponse(
            content=f"[{provider}-empty] no content",
            confidence=0.0,
            model=model,
            metadata={"elapsed_ms": elapsed_ms, "task": task},
        )
    return ProviderResponse(
        content=normalized,
        confidence=0.75,
        model=model,
        metadata={"elapsed_ms": elapsed_ms, "task": task},
    )


def _cli_error_response(
    *,
    provider: str,
    model: str,
    task: str,
    result: subprocess.CompletedProcess[str],
    elapsed_ms: int,
) -> ProviderResponse:
    stderr = _compact_cli_error(result.stderr or result.stdout)
    return ProviderResponse(
        content=f"[{provider}-error] command failed ({result.returncode}): {stderr}",
        confidence=0.0,
        model=model,
        metadata={"elapsed_ms": elapsed_ms, "task": task, "returncode": result.returncode},
    )


def _cli_timeout_response(
    *,
    provider: str,
    model: str,
    task: str,
    timeout_seconds: float,
) -> ProviderResponse:
    return ProviderResponse(
        content=f"[{provider}-timeout] command exceeded {timeout_seconds:g}s",
        confidence=0.0,
        model=model,
        metadata={"elapsed_ms": int(timeout_seconds * 1000), "task": task, "timeout": True},
    )


def _compact_cli_error(value: str) -> str:
    normalized = " ".join((value or "").strip().split())
    if not normalized:
        return "no error output"
    return normalized[:500]


def _parse_claude_json_result(value: str) -> str:
    try:
        parsed = json.loads(value)
    except json.JSONDecodeError:
        return ""
    if not isinstance(parsed, dict):
        return ""
    for key in ("result", "response", "content", "message"):
        raw = parsed.get(key)
        if isinstance(raw, str) and raw.strip():
            return raw.strip()
    return ""


def _optional_env(name: str, configured: str | None) -> str | None:
    raw = os.getenv(name)
    value = raw if raw is not None else configured
    if value is None:
        return None
    normalized = str(value).strip()
    return normalized or None


def _timeout_seconds(name: str, configured: float | None) -> float:
    raw = os.getenv(name)
    if raw is not None and raw.strip():
        return float(raw)
    if configured is not None:
        return float(configured)
    return DEFAULT_CLI_AGENT_TIMEOUT_SECONDS
