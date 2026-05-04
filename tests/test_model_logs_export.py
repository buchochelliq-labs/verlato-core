import json

from releasepilot.cli.main import main


def test_model_logs_export_outputs_normalized_json_with_redaction(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("RELEASEPILOT_REDACT_MODEL_PAYLOADS", raising=False)
    source = tmp_path / "model.jsonl"
    source.write_text(
        "\n".join(
            [
                json.dumps(
                    {
                        "event": "prompt_preview",
                        "message": "prompt token=ghp_abcdefghijklmnopqrstuvwxyz123456",
                        "attributes": {"chars": 12},
                        "payload": "Use api_key=plain-secret-value",
                    }
                ),
                json.dumps(
                    {
                        "Timestamp": "2026-05-04T10:00:00+00:00",
                        "ObservedTimestamp": "2026-05-04T10:00:00+00:00",
                        "SeverityText": "INFO",
                        "SeverityNumber": 9,
                        "Body": "stream token=ghp_abcdefghijklmnopqrstuvwxyz123456",
                        "Attributes": {"provider": "ollama"},
                        "EventName": "releasepilot.model.stream",
                        "Resource": {"service.name": "releasepilot"},
                        "InstrumentationScope": {"name": "releasepilot.providers"},
                    }
                ),
            ]
        )
    )

    rc = main(["model-logs", "export", "--file", str(source)])

    assert rc == 0
    exported = json.loads(capsys.readouterr().out)
    assert exported["schema_version"] == "releasepilot.model_logs.v1"
    assert exported["record_count"] == 2
    cli_record = exported["records"][0]
    assert cli_record["raw_format"] == "cli"
    assert cli_record["event"] == "prompt_preview"
    assert cli_record["attributes"]["redacted"] is True
    assert "ghp_abcdefghijklmnopqrstuvwxyz" not in cli_record["message"]
    assert "plain-secret-value" not in cli_record["payload"]
    assert "[REDACTED]" in cli_record["payload"]
    otel_record = exported["records"][1]
    assert otel_record["raw_format"] == "otel"
    assert otel_record["event"] == "releasepilot.model.stream"
    assert otel_record["attributes"]["redacted"] is True
    assert "ghp_abcdefghijklmnopqrstuvwxyz" not in otel_record["body"]


def test_model_logs_export_filters_events_and_writes_jsonl(tmp_path, capsys):
    source = tmp_path / "model.jsonl"
    output = tmp_path / "export.jsonl"
    source.write_text(
        "\n".join(
            [
                json.dumps({"event": "prompt_preview", "message": "prompt", "attributes": {}}),
                json.dumps({"event": "response_preview", "message": "response", "attributes": {}}),
            ]
        )
    )

    rc = main(
        [
            "model-logs",
            "export",
            "--file",
            str(source),
            "--event",
            "response_preview",
            "--format",
            "jsonl",
            "--output",
            str(output),
        ]
    )

    assert rc == 0
    assert capsys.readouterr().out == ""
    lines = output.read_text().splitlines()
    assert len(lines) == 1
    exported = json.loads(lines[0])
    assert exported["event"] == "response_preview"
    assert exported["message"] == "response"
