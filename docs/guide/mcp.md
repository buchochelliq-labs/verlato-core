# The MCP server

`verlato serve` runs a [Model Context Protocol](https://modelcontextprotocol.io) server on
stdio, so agents such as GitHub Copilot, Claude and other MCP clients can review and write
release notes with Verlato's tools. `verlato-mcp`, installed by the Python package, is the
same server. It is part of the native program and needs nothing else installed.

The server works on the repository in its **working directory**, so start it from (or
configure its `cwd` as) the repository root.

## Connecting a client

### VS Code (Copilot agent mode)

`.vscode/mcp.json` in the repository:

```json
{
  "servers": {
    "verlato": {
      "type": "stdio",
      "command": "verlato",
      "args": ["serve"]
    }
  }
}
```

VS Code starts the server in the workspace folder. If `verlato` is not on `PATH`, give its
full path (for example a release bundle's executable, or `${workspaceFolder}/.venv/bin/verlato-mcp`).

### Claude Code

```sh
claude mcp add verlato -- verlato serve
```

### Other clients

Any client that launches stdio servers can use this shape:

```json
{
  "mcpServers": {
    "verlato": {
      "command": "verlato",
      "args": ["serve"],
      "cwd": "/path/to/your/repository"
    }
  }
}
```

The extension's **Verlato: Start MCP Server** command runs `verlato serve` in a terminal and
shows its status in the Control view. Clients that launch their own server (all of the above)
do not need it; to check a connected client, call `verlato_service_health`.

## Tools

Every range tool takes `base` (required) and `head` (default `HEAD`).

| Tool | What it does |
|---|---|
| `verlato_service_health` | backend health and version |
| `verlato_service_info` | state statistics, configured targets and the diff engine |
| `verlato_scan_unpublished_changes` | unpublished change items in a range (read-only) |
| `verlato_reconcile_preview` | a lightweight preview of unpublished changes |
| `verlato_get_draft_document` | the structured draft (`version`, `include_confidence`, `include_provenance`, `model_provider`) |
| `verlato_preview_release_notes` | the rendered GitHub release notes |
| `verlato_apply_draft` | write the draft to a `target` or `file` |
| `verlato_update_review_state` | set an item's `status`, `summary` or `reviewer` |
| `verlato_request_model_input` | ask the configured model for a summary for one item |
| `verlato_list_suggestion_items` | compact items with their changed files (`include_files`) |
| `verlato_get_suggestion_item` | one item's metadata, commits, files and semantic review |
| `verlato_get_file_semantic_diff` | the IntentumDiff semantic diff of one file in one item |
| `verlato_search_semantic_diff` | search one item's semantic diff for a symbol or term |
| `verlato_draft_conventional_commit` | draft a Conventional Commit message for one commit (`sha`) |
| `verlato_draft_conventional_commits` | the same for several commits (`shas`, or the whole range) |

With the legacy `git` diff engine, two raw-patch tools replace the semantic ones:
`verlato_get_file_patch` and `verlato_search_item_diff`.

Agents get IntentumDiff's semantic diff, not raw patches: each file's meaningful changes with
their enclosing symbols, refactorings, moved code and ignored formatting. The server's
instructions and its prompt tell agents to base release notes on it and never to describe
moved code, refactorings or formatting as new behaviour. MCP clients count as remote models,
so change descriptions (which can quote source) are only included under the
`trusted-provider-patch` [privacy mode](privacy.md).

## Resources and prompts

| Name | What it is |
|---|---|
| `verlato://overview` | a structured overview of the server and its capabilities |
| `verlato://sample-config` | a sample `verlato.json` |
| `verlato://guidance/{name}` | workspace guidance: `readme`, `agent`, `architecture`, `copilot`, `skill` |
| prompt `verlato_review_request` | walks an agent through reviewing and writing release notes for a range (`base`, `head`, `version`) |

## An agent session

A typical exchange: the agent calls `verlato_list_suggestion_items` for `v1.0.0..HEAD`,
reads `verlato_get_suggestion_item` and `verlato_get_file_semantic_diff` for each item that
needs review, writes better summaries with `verlato_update_review_state`, and finally calls
`verlato_apply_draft` with `target: "changelog"`. The protocol details are in
[Service and MCP protocol](../reference/protocol.md#the-mcp-server).
