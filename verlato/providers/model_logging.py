from __future__ import annotations

import os
import sys
import json
import re
import threading
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


OTEL_INFO_SEVERITY_NUMBER = 9
OTEL_ERROR_SEVERITY_NUMBER = 17
REDACTION_TEXT = "[REDACTED]"
_SENSITIVE_PATTERNS = [
    re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}\b"),
    re.compile(r"(?i)\b(bearer\s+)[A-Za-z0-9._~+/=-]{16,}"),
    re.compile(r"(?i)\b(api[_-]?key|token|secret|password|authorization)\s*[:=]\s*['\"]?[^'\"\s]+"),
    re.compile(r"(?m)^([A-Z0-9_]*(?:TOKEN|SECRET|PASSWORD|API_KEY|AUTH)[A-Z0-9_]*\s*=\s*).+$"),
    re.compile(r"\b[A-Fa-f0-9]{40,}\b"),
    re.compile(r"\b[A-Za-z0-9+/]{48,}={0,2}\b"),
]
_LOG_LOCK = threading.Lock()


def print_stderr_line(line: str) -> None:
    with _LOG_LOCK:
        print(line, file=sys.stderr, flush=True)


def print_stderr_lines(lines: list[str]) -> None:
    with _LOG_LOCK:
        for line in lines:
            print(line, file=sys.stderr, flush=True)


def append_log_line(path: str | Path, line: str) -> None:
    output_path = Path(path)
    with _LOG_LOCK:
        output_path.parent.mkdir(parents=True, exist_ok=True)
        with output_path.open("a", encoding="utf-8") as handle:
            handle.write(line + "\n")


@dataclass
class ModelLogRecord:
    """
    OpenTelemetry-style log record for model/provider activity.

    The field names intentionally mirror the OpenTelemetry Logs Data Model:
    timestamp, observed timestamp, severity, body, resource, instrumentation
    scope, attributes, and event name.
    """

    body: str
    event_name: str = "verlato.model.log"
    severity_text: str = "INFO"
    severity_number: int = OTEL_INFO_SEVERITY_NUMBER
    timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    observed_timestamp: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    attributes: dict[str, Any] = field(default_factory=dict)
    resource: dict[str, Any] = field(default_factory=lambda: {"service.name": "verlato"})
    instrumentation_scope: dict[str, Any] = field(
        default_factory=lambda: {"name": "verlato.providers", "version": "0.1.0"}
    )

    def to_otel_dict(self) -> dict[str, Any]:
        return {
            "Timestamp": self.timestamp,
            "ObservedTimestamp": self.observed_timestamp,
            "SeverityText": self.severity_text,
            "SeverityNumber": self.severity_number,
            "Body": self.body,
            "Resource": self.resource,
            "InstrumentationScope": self.instrumentation_scope,
            "Attributes": self.attributes,
            "EventName": self.event_name,
        }


class ModelLogSink(ABC):
    @abstractmethod
    def emit(self, record: ModelLogRecord) -> None:
        raise NotImplementedError


class NullModelLogSink(ModelLogSink):
    def emit(self, record: ModelLogRecord) -> None:
        return


class StderrModelLogSink(ModelLogSink):
    def emit(self, record: ModelLogRecord) -> None:
        print_stderr_line(f"[verlato:model] {record.body}")


class JsonlModelLogSink(ModelLogSink):
    def __init__(self, path: str) -> None:
        self.path = Path(path)

    def emit(self, record: ModelLogRecord) -> None:
        append_log_line(self.path, json.dumps(record.to_otel_dict(), sort_keys=True))


class CompositeModelLogSink(ModelLogSink):
    def __init__(self, sinks: list[ModelLogSink]) -> None:
        self.sinks = sinks

    def emit(self, record: ModelLogRecord) -> None:
        for sink in self.sinks:
            sink.emit(record)


def default_model_log_sink() -> ModelLogSink:
    sinks: list[ModelLogSink] = []
    if os.getenv("VERLATO_TRACE_MODEL_CALLS") == "1":
        sinks.append(StderrModelLogSink())
    if os.getenv("VERLATO_MODEL_LOG_PATH"):
        sinks.append(JsonlModelLogSink(os.environ["VERLATO_MODEL_LOG_PATH"]))
    if not sinks:
        return NullModelLogSink()
    if len(sinks) == 1:
        return sinks[0]
    return CompositeModelLogSink(sinks)


def model_log_record(
    body: str,
    *,
    event_name: str = "verlato.model.log",
    attributes: dict[str, Any] | None = None,
    error: bool = False,
) -> ModelLogRecord:
    return ModelLogRecord(
        body=body,
        event_name=event_name,
        severity_text="ERROR" if error else "INFO",
        severity_number=OTEL_ERROR_SEVERITY_NUMBER if error else OTEL_INFO_SEVERITY_NUMBER,
        attributes=attributes or {},
    )


def redact_model_payload(value: str) -> tuple[str, bool]:
    redacted = value
    for pattern in _SENSITIVE_PATTERNS:
        redacted = pattern.sub(_redaction_replacement, redacted)
    return redacted, redacted != value


def model_payload_redaction_enabled() -> bool:
    raw = os.getenv("VERLATO_REDACT_MODEL_PAYLOADS")
    if raw is None:
        return True
    return raw.strip().lower() not in {"0", "false", "no", "off"}


def _redaction_replacement(match: re.Match[str]) -> str:
    if match.lastindex:
        prefix = match.group(1)
        if prefix:
            return f"{prefix}{REDACTION_TEXT}"
    text = match.group(0)
    if ":" in text:
        key = text.split(":", 1)[0]
        return f"{key}: {REDACTION_TEXT}"
    if "=" in text:
        key = text.split("=", 1)[0]
        return f"{key}={REDACTION_TEXT}"
    return REDACTION_TEXT
