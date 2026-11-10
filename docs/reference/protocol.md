# Service and MCP protocol

Verlato has two long-running modes on stdio: the **editor service** (`verlato service`), a
small JSON-lines protocol used by the VS Code extension, and the **MCP server**
(`verlato serve`) for MCP clients. Both work on the repository in their working directory.

## The editor service

`verlato service` reads one JSON request per line on stdin and writes one JSON response per
line on stdout, in order.

```text
→ {"id": 1, "method": "health"}
← {"id": 1, "ok": true, "result": {"status": "ok", "version": "0.2.0", "runtime": "rust"}}
→ {"id": 2, "method": "draft.preview", "params": {"request": {"base": "v1.0.0", "version": "v1.1.0"}}}
← {"id": 2, "ok": true, "result": "## v1.1.0 - 2026-11-10\n\n### Added\n- ..."}
→ {"id": 3, "method": "nope"}
← {"id": 3, "ok": false, "error": {"code": "invalid_request", "message": "Unknown service method: nope"}}
→ {"id": 4, "method": "shutdown"}
← {"id": 4, "ok": true, "result": {"shutdown": true}}
```

A request is an object with `method`, optional `params` (an object) and an optional `id`,
which is echoed back. A response has `ok` and either `result` or `error` (`code` and
`message`). Error codes: `invalid_json` (the line is not JSON), `invalid_request` (bad shape,
unknown method or missing parameter), `handler_error` (the command failed), and typed codes
from the pipeline. Parameter names also accept camelCase (`includeConfidence`).

### Methods

| Method | Params | Result |
|---|---|---|
| `health` | | status, version, runtime |
| `info` | | version, diff engine status, state path, schema version, tracked items, targets |
| `draft.get` | `request: {base, head?, version?, include_confidence?, include_provenance?, model_provider?}` | the [draft document](output-formats.md#draft-documents) |
| `draft.preview` | as `draft.get` | the rendered GitHub release notes (a string) |
| `draft.apply` | as `draft.get`, plus `target?` (any configured target, default `changelog`) and `file?` | what was written |
| `draft.review` | as `draft.get`, plus `item_id`, `status?`, `summary?`, `reviewer?`, `summary_provenance?` | the updated item |
| `feedback.record` | `event`, `item_id`, `metadata?` | the recorded event |
| `state.show` | | the state |
| `state.clear_working` | | what was removed |
| `state.clear_suggestion_cache` | | what was removed |
| `suggestion.items` | `base`, `head?`, `include_files?` | compact suggestion items |
| `suggestion.item` | `base`, `head?`, `item_id` | one item's metadata, commits, files and semantic review |
| `suggestion.semantic_diff` | `base`, `head?`, `item_id`, `path` | one file's IntentumDiff semantic diff |
| `suggestion.semantic_search` | `base`, `head?`, `item_id`, `query`, `limit?` (5) | matches in the item's semantic diff |
| `suggestion.patch` | `base`, `head?`, `item_id`, `path`, `offset?`, `limit?` (12000) | a bounded raw patch chunk |
| `suggestion.search` | `base`, `head?`, `item_id`, `query`, `limit?` (5) | matches in the item's patch text |
| `commit.conventional` | `base`, `head?`, `sha`, `model_provider?`, `model_privacy?`, `allow_remote_patches?`, `evidence_mode?` | a Conventional Commit draft |
| `commit.conventional_many` | as above, with `shas?` (default: every commit in the range) | drafts for several commits |
| `shutdown` | | stops the service |

`draft.*` parameters may also be given at the top level of `params` instead of inside
`request`. The same methods are available in-process from Python with
[`verlato.call`](python.md). Embedding binaries can add methods; see
[Extension points](../technical/extending.md).

## The MCP server

`verlato serve` implements the Model Context Protocol (protocol version `2025-06-18`) over
stdio, as newline-delimited JSON-RPC 2.0. It supports `initialize`, `ping`, `tools/list`,
`tools/call`, `resources/list`, `resources/templates/list`, `resources/read`, `prompts/list`
and `prompts/get`.

- **Tools:** listed in [The MCP server](../guide/mcp.md#tools). Each tool's input schema is
  returned by `tools/list`. Tool results carry structured content plus a text rendering;
  failures are results with `isError: true`.
- **Resources:** `verlato://overview`, `verlato://sample-config` and
  `verlato://guidance/{name}` (`readme`, `agent`, `architecture`, `copilot`, `skill`).
- **Prompts:** `verlato_review_request` (arguments `base`, `head`, `version`).

The catalog (tool descriptions and schemas, resources, prompts and server instructions) is
[`crates/verlato/src/mcp_catalog.json`](https://github.com/buchochelliq-labs/verlato-core/blob/main/crates/verlato/src/mcp_catalog.json).
