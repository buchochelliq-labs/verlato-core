# Privacy modes

A privacy mode decides what a model provider may see. It applies to every model call:
summaries, suggestions, commit intent inference, Conventional Commit drafting and the MCP
evidence tools.

| Mode | Local providers (`ollama`, `mock`, `stub`) | Remote providers |
|---|---|---|
| `metadata-only` (default) | structure only | structure only |
| `local-summary-then-ai` | structure and source content | structure only |
| `trusted-provider-patch` | structure and source content | structure and source content |

**Structure** means the change summary, PR and commit metadata, file paths and statuses,
deterministic diff facts, and IntentumDiff's semantic diff with identifier names: which
definitions changed, were added, removed, renamed, moved or refactored, and which edits were
formatting only. **Source content** adds IntentumDiff's change descriptions, which can quote
the changed statement before and after (bounded to 120 characters per change). With the
legacy `git` engine, source content means raw patch excerpts.

Under `local-summary-then-ai`, source content goes only to local providers, and only if every
provider in the [fallback chain](models.md#fallback-chains) is local too.
`trusted-provider-patch` sends source content to any provider, local or remote: choose it
only for providers your team trusts with your code. `--allow-remote-patches` and the
`verlato.allowRemotePatches` setting are explicit opt-ins that select it (for scans and
drafts, so does `VERLATO_ALLOW_REMOTE_PATCHES=1`).

## Setting the mode

In order of precedence: `--allow-remote-patches` (selects `trusted-provider-patch`), then
`--privacy <mode>`, then `VERLATO_MODEL_PRIVACY` (the `verlato.modelPrivacy` setting), then
`model_privacy` in `verlato.json`. Unknown values fall back to `metadata-only`.

## Seeing what is sent

Every model call records what it sent:

```sh
verlato draft prompt --from-latest-tag --id ci_715c52ad2c02
```

returns the exact prompt and evidence a suggestion would send, without calling a model, plus
its privacy record:

```json
"privacy": {
  "mode": "metadata-only",
  "provider": "ollama",
  "raw_patch_chars_available": 659,
  "raw_patch_chars_sent": 0,
  "will_send": ["change summary", "PR metadata", "commit metadata", "file paths and statuses", "deterministic diff facts"],
  "will_not_send": ["raw patches", "patch excerpts", "representative hunks"]
}
```

Model answers carry an audit record too (`ai_request`), with the SHA-256 hashes of the
request, prompt and evidence, the prompt and evidence sizes, and what was omitted and why.
Applied suggestions keep this provenance with the item.

## Suggestion strategies and privacy

The batched (`paired`, `full-diff-json`) and `tool-retrieval` suggestion strategies exist to
show models more of each change, so they only run when the mode allows source content for
the provider. Otherwise Verlato quietly uses the `single` strategy.

## MCP clients

MCP clients are treated as remote models: IntentumDiff's change descriptions are only
included in MCP answers under `trusted-provider-patch`. See [The MCP server](mcp.md).
