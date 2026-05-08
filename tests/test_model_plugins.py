from __future__ import annotations

import sys
from pathlib import Path

from verlato.providers.base import ProviderRequest
from verlato.providers.plugin import ExternalModelPluginProvider


def _write_plugin(path: Path, body: str) -> list[str]:
    path.write_text(body)
    return [sys.executable, str(path)]


def test_external_plugin_valid_response_echoes_request_hash(tmp_path: Path):
    command = _write_plugin(
        tmp_path / "plugin.py",
        """
import json
import sys

payload = json.loads(sys.stdin.read())
assert payload["schema"] == "verlato.model_plugin.request.v1"
assert payload["evidence"]["item_id"] == "ci_test"
print(json.dumps({
    "schema": "verlato.model_plugin.response.v1",
    "request_hash": payload["request_hash"],
    "content": "Clarifies plugin-backed release notes for readers",
    "model": "plugin:test-model",
    "metadata": {"saw_schema_hash": payload["schema_hash"]},
}))
""",
    )
    provider = ExternalModelPluginProvider(
        manifest={
            "id": "test-plugin",
            "display_name": "Test plugin",
            "command": command,
            "config_schema": {"type": "object"},
            "capabilities": {"tasks": ["suggest"], "structured_json": True},
        },
    )

    response = provider.summarize(
        ProviderRequest(
            task="suggest",
            prompt="Use this diff evidence.",
            evidence={"item_id": "ci_test"},
            response_format={"type": "object"},
        )
    )

    assert response.content == "Clarifies plugin-backed release notes for readers"
    assert response.model == "plugin:test-model"
    assert response.metadata["audit"]["request_hash"]
    assert response.metadata["saw_schema_hash"] == response.metadata["audit"]["schema_hash"]


def test_external_plugin_rejects_missing_or_mismatched_request_hash(tmp_path: Path):
    command = _write_plugin(
        tmp_path / "plugin.py",
        """
import json
print(json.dumps({"request_hash": "wrong", "content": "Tampered"}))
""",
    )
    provider = ExternalModelPluginProvider(manifest={"id": "bad-hash", "display_name": "Bad hash", "command": command})

    response = provider.summarize(ProviderRequest(task="suggest", prompt="Prompt", evidence={}))

    assert response.content.startswith("[plugin-tamper]")
    assert response.confidence == 0.0


def test_external_plugin_reports_bad_json_nonzero_exit_and_timeout(tmp_path: Path):
    bad_json = ExternalModelPluginProvider(
        manifest={
            "id": "bad-json",
            "display_name": "Bad JSON",
            "command": _write_plugin(tmp_path / "bad_json.py", "print('not json')"),
        },
    )
    assert bad_json.summarize(ProviderRequest(task="suggest", prompt="Prompt")).content.startswith("[plugin-invalid-json]")

    nonzero = ExternalModelPluginProvider(
        manifest={
            "id": "nonzero",
            "display_name": "Nonzero",
            "command": _write_plugin(tmp_path / "nonzero.py", "import sys; print('diagnostic', file=sys.stderr); sys.exit(7)"),
        },
    )
    nonzero_response = nonzero.summarize(ProviderRequest(task="suggest", prompt="Prompt"))
    assert nonzero_response.content == "[plugin-error] command exited 7"
    assert nonzero_response.metadata["stderr"] == "diagnostic"

    timeout = ExternalModelPluginProvider(
        manifest={
            "id": "timeout",
            "display_name": "Timeout",
            "command": _write_plugin(tmp_path / "timeout.py", "import time; time.sleep(2)"),
        },
        timeout_seconds=0.01,
    )
    assert timeout.summarize(ProviderRequest(task="suggest", prompt="Prompt")).content.startswith("[plugin-timeout]")
