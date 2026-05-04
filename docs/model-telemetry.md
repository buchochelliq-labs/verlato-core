# Model Telemetry

ReleasePilot emits model telemetry as realtime stderr activity for editor clients and,
optionally, JSONL debug records for local investigation.

## Controls

- `RELEASEPILOT_TRACE_MODEL_CALLS=1` enables model lifecycle logs on stderr.
- `RELEASEPILOT_TRACE_MODEL_PAYLOADS=1` includes full prompt and response payload events.
- `RELEASEPILOT_TRACE_MODEL_REASONING=1` includes full provider-exposed reasoning text in collapsed details and streamed reasoning chunks.
- `RELEASEPILOT_REDACT_MODEL_PAYLOADS=0` disables payload redaction for deep local debugging. Redaction is on by default.
- `RELEASEPILOT_MODEL_LOG_PATH=<path>` appends JSONL debug records.
- VS Code mirrors these with `releasepilot.telemetry.includePromptPayloads`, `releasepilot.telemetry.includeReasoningPayloads`, `releasepilot.telemetry.redactPayloads`, and `releasepilot.telemetry.debugModelLogPath`.

Payload telemetry is off by default because prompts can include PR text and diff snippets. Reasoning payloads are separately off by default because provider-exposed reasoning can be large. Clients may still show `reasoning_exposed` and `reasoning_chars` without showing the reasoning text. When payloads are enabled, ReleasePilot redacts common secret shapes by default and sets `redacted=true` on affected telemetry attributes.

## Stderr Events

Human-readable lines use this prefix:

```text
[releasepilot:model] <message>
```

Payload events also emit a structured companion line:

```text
[releasepilot:model] json {"event":"prompt_preview","attributes":{"chars":123},"message":"...","payload":"..."}
```

Editor clients should coalesce `stream_chunk` events by request, task, and `kind`
before rendering. ReleasePilot's VS Code view shows reasoning chunks as one live
reasoning block and content chunks as one live response block. Reasoning detail
is rendered collapsed by default when full reasoning payloads are enabled.

Fields:

- `event`: stable snake_case event name.
- `message`: compact human-readable message.
- `attributes`: structured metadata for filtering and future export.
- `payload`: optional full text, emitted only when payload telemetry is enabled.
- `attributes.redacted`: whether the payload text was modified by redaction.

Current event names:

- `suggestion_plan`
- `target_selection`
- `evidence_built`
- `evidence_detail`
- `prompt_built`
- `prompt_budget`
- `prompt_preview`
- `request_started`
- `stream_chunk`
- `stream_complete`
- `request_finished`
- `response_detail`
- `response_preview`
- `reasoning_preview`
- `suggestion_parse`
- `suggestion_accept`
- `suggestion_reject`
- `suggestion_raw_response`
- `suggestion_decision`

## JSONL Debug Sink

Provider logs written by `RELEASEPILOT_MODEL_LOG_PATH` use an OpenTelemetry-style record shape with fields such as `Timestamp`, `SeverityText`, `Body`, `Resource`, `InstrumentationScope`, `Attributes`, and `EventName`.

CLI lifecycle events written to the same path use the lighter stderr event shape above. This keeps local debugging readable while leaving the provider boundary ready for JSON/OTLP export.

## Export

Use `releasepilot model-logs export --file <model-debug.jsonl>` to normalize a debug JSONL file into a `releasepilot.model_logs.v1` JSON document:

```text
releasepilot model-logs export --file model-debug.jsonl --output model-export.json
```

Use `--format jsonl` to emit one normalized record per line, and `--event <name>` to filter to one or more event names. Export redacts likely secrets by default, even if older logs were captured before redaction existed. Set `RELEASEPILOT_REDACT_MODEL_PAYLOADS=0` only when exporting raw local debugging data intentionally.
