# CLI reference

This page is generated from the `--help` output of the `verlato` executable by
`scripts/gen_cli_reference.py`. Do not edit it by hand: change the command definitions in
`crates/verlato/src/cli.rs`, rebuild, and run the script again (CI-style check:
`scripts/gen_cli_reference.py --check`).

The [CLI walkthrough](../guide/cli.md) explains what the commands are for and how they fit
together. Options that every range command shares (`--base`, `--head`, `--from-latest-tag`,
`--from-latest-published`) are described in [Concepts](../guide/concepts.md#ranges).

`verlato-mcp` (from the Python package) is the same as `verlato serve`.

## `verlato`

```text
Semantic release notes powered by IntentumDiff

Usage: verlato [OPTIONS] <COMMAND>

Commands:
  scan        List the change items in a range that are not in state yet (read-only)
  generate    Detect change items in a range and save the new ones to state
  reconcile   List the unpublished change items in a range, with a count (read-only)
  render      Render release notes from the items saved in state
  service     JSON-lines editor service on stdio (used by the VS Code extension)
  serve       MCP server on stdio
  draft       Build, preview, review, apply and improve release-note drafts
  state       Inspect and maintain Verlato's local state
  feedback    Record, export and learn from reviewer feedback
  doctor      Health checks for the semantic engine and model providers
  commit      Draft Conventional Commit messages from commit evidence
  model-logs  Export model telemetry debug logs
  help        Print this message or the help of the given subcommand(s)

Options:
      --diff-engine <DIFF_ENGINE>  Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
  -h, --help                       Print help
  -V, --version                    Print version
```

### `verlato scan`

```text
List the change items in a range that are not in state yet (read-only)

Usage: verlato scan [OPTIONS]

Options:
      --base <BASE>
          Start of the range (exclusive): a tag, branch or commit
      --diff-engine <DIFF_ENGINE>
          Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
      --head <HEAD>
          End of the range (inclusive) [default: HEAD]
      --from-latest-tag
          Use the latest tag reachable from --head as the base
      --from-latest-published
          Use the most recent release recorded with `state mark-published` as the base
      --model-provider <MODEL_PROVIDER>
          Model provider for this run: stub, mock, ollama, litellm, plugin, github-copilot, codex-cli or claude-code (overrides verlato.json and VERLATO_MODEL_PROVIDER)
      --infer-commit-intents
          Infer Conventional Commit type and scope for low-signal commits before grouping
      --no-infer-commit-intents
          Turn commit intent inference off for this run
      --privacy <PRIVACY>
          Privacy mode for model evidence: metadata-only, local-summary-then-ai or trusted-provider-patch
      --allow-remote-patches
          Allow trusted-provider-patch evidence for remote providers
  -h, --help
          Print help
```

### `verlato generate`

```text
Detect change items in a range and save the new ones to state

Usage: verlato generate [OPTIONS]

Options:
      --base <BASE>
          Start of the range (exclusive): a tag, branch or commit
      --diff-engine <DIFF_ENGINE>
          Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
      --head <HEAD>
          End of the range (inclusive) [default: HEAD]
      --from-latest-tag
          Use the latest tag reachable from --head as the base
      --from-latest-published
          Use the most recent release recorded with `state mark-published` as the base
      --model-provider <MODEL_PROVIDER>
          Model provider for this run: stub, mock, ollama, litellm, plugin, github-copilot, codex-cli or claude-code (overrides verlato.json and VERLATO_MODEL_PROVIDER)
      --infer-commit-intents
          Infer Conventional Commit type and scope for low-signal commits before grouping
      --no-infer-commit-intents
          Turn commit intent inference off for this run
      --privacy <PRIVACY>
          Privacy mode for model evidence: metadata-only, local-summary-then-ai or trusted-provider-patch
      --allow-remote-patches
          Allow trusted-provider-patch evidence for remote providers
  -h, --help
          Print help
```

### `verlato reconcile`

```text
List the unpublished change items in a range, with a count (read-only)

Usage: verlato reconcile [OPTIONS]

Options:
      --base <BASE>
          Start of the range (exclusive): a tag, branch or commit
      --diff-engine <DIFF_ENGINE>
          Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
      --head <HEAD>
          End of the range (inclusive) [default: HEAD]
      --from-latest-tag
          Use the latest tag reachable from --head as the base
      --from-latest-published
          Use the most recent release recorded with `state mark-published` as the base
      --model-provider <MODEL_PROVIDER>
          Model provider for this run: stub, mock, ollama, litellm, plugin, github-copilot, codex-cli or claude-code (overrides verlato.json and VERLATO_MODEL_PROVIDER)
      --infer-commit-intents
          Infer Conventional Commit type and scope for low-signal commits before grouping
      --no-infer-commit-intents
          Turn commit intent inference off for this run
      --privacy <PRIVACY>
          Privacy mode for model evidence: metadata-only, local-summary-then-ai or trusted-provider-patch
      --allow-remote-patches
          Allow trusted-provider-patch evidence for remote providers
  -h, --help
          Print help
```

### `verlato render`

```text
Render release notes from the items saved in state

Usage: verlato render [OPTIONS] --format <FORMAT>

Options:
      --diff-engine <DIFF_ENGINE>  Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
      --format <FORMAT>            Output format [possible values: github-release, changelog]
      --version <VERSION>          Release heading used in the rendered notes [default: Unreleased]
      --include-confidence         Show each item's confidence score
      --include-provenance         Show each item's PR and commit provenance
  -h, --help                       Print help
```

### `verlato service`

```text
JSON-lines editor service on stdio (used by the VS Code extension)

Usage: verlato service [OPTIONS]

Options:
      --diff-engine <DIFF_ENGINE>  Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
  -h, --help                       Print help
```

### `verlato serve`

```text
MCP server on stdio

Usage: verlato serve [OPTIONS]

Options:
      --diff-engine <DIFF_ENGINE>  Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
  -h, --help                       Print help
```

### `verlato draft`

```text
Build, preview, review, apply and improve release-note drafts

Usage: verlato draft [OPTIONS] <COMMAND>

Commands:
  json     The draft for a range as a JSON document (items, sections, rendered Markdown)
  preview  The draft for a range rendered as GitHub release notes (Markdown)
  apply    Write the draft into a release-note file (a target from verlato.json, or --file)
  review   Save a reviewer decision for one draft item: status, edited summary, reviewer
  suggest  Model suggestions for draft items
  prompt   The exact prompt, evidence and audit record a suggestion for one item would send
  help     Print this message or the help of the given subcommand(s)

Options:
      --diff-engine <DIFF_ENGINE>  Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
  -h, --help                       Print help
```

#### `verlato draft json`

```text
The draft for a range as a JSON document (items, sections, rendered Markdown)

Usage: verlato draft json [OPTIONS]

Options:
      --base <BASE>
          Start of the range (exclusive): a tag, branch or commit
      --diff-engine <DIFF_ENGINE>
          Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
      --head <HEAD>
          End of the range (inclusive) [default: HEAD]
      --from-latest-tag
          Use the latest tag reachable from --head as the base
      --from-latest-published
          Use the most recent release recorded with `state mark-published` as the base
      --version <VERSION>
          Release heading used in the rendered notes [default: Unreleased]
      --include-confidence
          Show each item's confidence score in the rendered notes
      --include-provenance
          Show each item's PR and commit provenance in the rendered notes
      --model-provider <MODEL_PROVIDER>
          Model provider for this run: stub, mock, ollama, litellm, plugin, github-copilot, codex-cli or claude-code (overrides verlato.json and VERLATO_MODEL_PROVIDER)
      --infer-commit-intents
          Infer Conventional Commit type and scope for low-signal commits before grouping
      --no-infer-commit-intents
          Turn commit intent inference off for this run
      --privacy <PRIVACY>
          Privacy mode for model evidence: metadata-only, local-summary-then-ai or trusted-provider-patch
      --allow-remote-patches
          Allow trusted-provider-patch evidence for remote providers
  -h, --help
          Print help
```

#### `verlato draft preview`

```text
The draft for a range rendered as GitHub release notes (Markdown)

Usage: verlato draft preview [OPTIONS]

Options:
      --base <BASE>
          Start of the range (exclusive): a tag, branch or commit
      --diff-engine <DIFF_ENGINE>
          Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
      --head <HEAD>
          End of the range (inclusive) [default: HEAD]
      --from-latest-tag
          Use the latest tag reachable from --head as the base
      --from-latest-published
          Use the most recent release recorded with `state mark-published` as the base
      --version <VERSION>
          Release heading used in the rendered notes [default: Unreleased]
      --include-confidence
          Show each item's confidence score in the rendered notes
      --include-provenance
          Show each item's PR and commit provenance in the rendered notes
      --model-provider <MODEL_PROVIDER>
          Model provider for this run: stub, mock, ollama, litellm, plugin, github-copilot, codex-cli or claude-code (overrides verlato.json and VERLATO_MODEL_PROVIDER)
      --infer-commit-intents
          Infer Conventional Commit type and scope for low-signal commits before grouping
      --no-infer-commit-intents
          Turn commit intent inference off for this run
      --privacy <PRIVACY>
          Privacy mode for model evidence: metadata-only, local-summary-then-ai or trusted-provider-patch
      --allow-remote-patches
          Allow trusted-provider-patch evidence for remote providers
  -h, --help
          Print help
```

#### `verlato draft apply`

```text
Write the draft into a release-note file (a target from verlato.json, or --file)

Usage: verlato draft apply [OPTIONS]

Options:
      --base <BASE>
          Start of the range (exclusive): a tag, branch or commit
      --diff-engine <DIFF_ENGINE>
          Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
      --head <HEAD>
          End of the range (inclusive) [default: HEAD]
      --from-latest-tag
          Use the latest tag reachable from --head as the base
      --from-latest-published
          Use the most recent release recorded with `state mark-published` as the base
      --version <VERSION>
          Release heading used in the rendered notes [default: Unreleased]
      --include-confidence
          Show each item's confidence score in the rendered notes
      --include-provenance
          Show each item's PR and commit provenance in the rendered notes
      --model-provider <MODEL_PROVIDER>
          Model provider for this run: stub, mock, ollama, litellm, plugin, github-copilot, codex-cli or claude-code (overrides verlato.json and VERLATO_MODEL_PROVIDER)
      --infer-commit-intents
          Infer Conventional Commit type and scope for low-signal commits before grouping
      --no-infer-commit-intents
          Turn commit intent inference off for this run
      --privacy <PRIVACY>
          Privacy mode for model evidence: metadata-only, local-summary-then-ai or trusted-provider-patch
      --allow-remote-patches
          Allow trusted-provider-patch evidence for remote providers
      --target <TARGET>
          Target from verlato.json `targets` [default: changelog] [possible values: changelog, github-release]
      --file <FILE>
          Write to this file instead of the target's configured path
  -h, --help
          Print help
```

#### `verlato draft review`

```text
Save a reviewer decision for one draft item: status, edited summary, reviewer

Usage: verlato draft review [OPTIONS] --id <ID>

Options:
      --base <BASE>
          Start of the range (exclusive): a tag, branch or commit
      --diff-engine <DIFF_ENGINE>
          Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
      --head <HEAD>
          End of the range (inclusive) [default: HEAD]
      --from-latest-tag
          Use the latest tag reachable from --head as the base
      --from-latest-published
          Use the most recent release recorded with `state mark-published` as the base
      --id <ID>
          The draft item ID (`ci_...`)
      --status <STATUS>
          New review status. `hidden` leaves the item out of rendered notes [possible values: needs-review, ready, accepted, edited, hidden]
      --summary <SUMMARY>
          Replacement summary text (marks the item edited)
      --reviewer <REVIEWER>
          Who made the decision (recorded with the item)
      --model-provider <MODEL_PROVIDER>
          Model provider for this run: stub, mock, ollama, litellm, plugin, github-copilot, codex-cli or claude-code (overrides verlato.json and VERLATO_MODEL_PROVIDER)
      --infer-commit-intents
          Infer Conventional Commit type and scope for low-signal commits before grouping
      --no-infer-commit-intents
          Turn commit intent inference off for this run
      --privacy <PRIVACY>
          Privacy mode for model evidence: metadata-only, local-summary-then-ai or trusted-provider-patch
      --allow-remote-patches
          Allow trusted-provider-patch evidence for remote providers
  -h, --help
          Print help
```

#### `verlato draft suggest`

```text
Model suggestions for draft items

Usage: verlato draft suggest [OPTIONS]

Options:
      --base <BASE>
          Start of the range (exclusive): a tag, branch or commit
      --diff-engine <DIFF_ENGINE>
          Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
      --head <HEAD>
          End of the range (inclusive) [default: HEAD]
      --from-latest-tag
          Use the latest tag reachable from --head as the base
      --from-latest-published
          Use the most recent release recorded with `state mark-published` as the base
      --id <ID>
          The draft item to suggest a summary for
      --all-visible
          Suggest for every item that is not hidden (needs --preview)
      --preview
          Return suggestions as JSON without changing state. Without it, the suggestion for --id is applied
      --preflight
          Build planned AI request envelopes without calling the provider
      --count <COUNT>
          Suggestions per item (1-5)
      --parallelism <PARALLELISM>
          Item suggestions to run at once (1-8)
      --batch-size <BATCH_SIZE>
          Uncached items that may share one structured model request (1-5)
      --strategy <STRATEGY>
          How items are sent to the model [possible values: auto, single, paired, full-diff-json, tool-retrieval]
      --prompt-profile <PROMPT_PROFILE>
          Prompt budget for single-item prompts [possible values: conservative, lean, minimal]
      --adaptive-recommendations
          Include recommendation-only tuning advice in the JSON output
      --suggest-model <SUGGEST_MODEL>
          A different (smaller or faster) model for suggestions only
      --no-cache
          Neither read nor write the suggestion cache
      --refresh-cache
          Ignore cached suggestions and overwrite them
      --tone <TONE>
          Wording preset [possible values: release-note, concise, technical, customer-facing]
      --reviewer <REVIEWER>
          Who applied the suggestion (recorded with the item)
      --trace-suggestions
          Emit compact suggestion lifecycle trace logs for this invocation
      --model-provider <MODEL_PROVIDER>
          Model provider for this run: stub, mock, ollama, litellm, plugin, github-copilot, codex-cli or claude-code (overrides verlato.json and VERLATO_MODEL_PROVIDER)
      --infer-commit-intents
          Infer Conventional Commit type and scope for low-signal commits before grouping
      --no-infer-commit-intents
          Turn commit intent inference off for this run
      --privacy <PRIVACY>
          Privacy mode for model evidence: metadata-only, local-summary-then-ai or trusted-provider-patch
      --allow-remote-patches
          Allow trusted-provider-patch evidence for remote providers
  -h, --help
          Print help
```

#### `verlato draft prompt`

```text
The exact prompt, evidence and audit record a suggestion for one item would send

Usage: verlato draft prompt [OPTIONS] --id <ID>

Options:
      --base <BASE>
          Start of the range (exclusive): a tag, branch or commit
      --diff-engine <DIFF_ENGINE>
          Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
      --head <HEAD>
          End of the range (inclusive) [default: HEAD]
      --from-latest-tag
          Use the latest tag reachable from --head as the base
      --from-latest-published
          Use the most recent release recorded with `state mark-published` as the base
      --id <ID>
          The draft item ID (`ci_...`)
      --tone <TONE>
          Wording preset [possible values: release-note, concise, technical, customer-facing]
      --model-provider <MODEL_PROVIDER>
          Model provider for this run: stub, mock, ollama, litellm, plugin, github-copilot, codex-cli or claude-code (overrides verlato.json and VERLATO_MODEL_PROVIDER)
      --infer-commit-intents
          Infer Conventional Commit type and scope for low-signal commits before grouping
      --no-infer-commit-intents
          Turn commit intent inference off for this run
      --privacy <PRIVACY>
          Privacy mode for model evidence: metadata-only, local-summary-then-ai or trusted-provider-patch
      --allow-remote-patches
          Allow trusted-provider-patch evidence for remote providers
  -h, --help
          Print help
```

### `verlato state`

```text
Inspect and maintain Verlato's local state

Usage: verlato state [OPTIONS] <COMMAND>

Commands:
  show                    Print the saved state as JSON
  mark-published          Record that items shipped in a release (the base for --from-latest-published)
  clear-working           Remove unpublished items (and their feedback) from state; published history is kept
  clear-suggestion-cache  Delete the suggestion cache
  help                    Print this message or the help of the given subcommand(s)

Options:
      --diff-engine <DIFF_ENGINE>  Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
  -h, --help                       Print help
```

#### `verlato state show`

```text
Print the saved state as JSON

Usage: verlato state show [OPTIONS]

Options:
      --diff-engine <DIFF_ENGINE>  Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
  -h, --help                       Print help
```

#### `verlato state mark-published`

```text
Record that items shipped in a release (the base for --from-latest-published)

Usage: verlato state mark-published [OPTIONS] --version <VERSION>

Options:
      --diff-engine <DIFF_ENGINE>  Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
      --version <VERSION>          The release or tag the items shipped in
      --ids <IDS>...               Item IDs to mark
      --all-unpublished            Mark every unpublished item
  -h, --help                       Print help
```

#### `verlato state clear-working`

```text
Remove unpublished items (and their feedback) from state; published history is kept

Usage: verlato state clear-working [OPTIONS]

Options:
      --diff-engine <DIFF_ENGINE>  Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
  -h, --help                       Print help
```

#### `verlato state clear-suggestion-cache`

```text
Delete the suggestion cache

Usage: verlato state clear-suggestion-cache [OPTIONS]

Options:
      --diff-engine <DIFF_ENGINE>  Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
  -h, --help                       Print help
```

### `verlato feedback`

```text
Record, export and learn from reviewer feedback

Usage: verlato feedback [OPTIONS] <COMMAND>

Commands:
  record     Record one feedback event for a draft item
  export     Export the recorded feedback events
  recommend  Prompt and style recommendations from aggregate feedback
  help       Print this message or the help of the given subcommand(s)

Options:
      --diff-engine <DIFF_ENGINE>  Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
  -h, --help                       Print help
```

#### `verlato feedback record`

```text
Record one feedback event for a draft item

Usage: verlato feedback record [OPTIONS] --event <EVENT> --item-id <ITEM_ID>

Options:
      --diff-engine <DIFF_ENGINE>      Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
      --event <EVENT>                  Event name, e.g. skip, regenerate, useful or not-useful
      --item-id <ITEM_ID>              The draft item ID
      --metadata-json <METADATA_JSON>  Extra metadata as a JSON object
  -h, --help                           Print help
```

#### `verlato feedback export`

```text
Export the recorded feedback events

Usage: verlato feedback export [OPTIONS]

Options:
      --diff-engine <DIFF_ENGINE>  Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
      --format <FORMAT>            One JSON document, or one event per line [default: json] [possible values: json, jsonl]
      --output <OUTPUT>            Optional output path. Defaults to stdout
      --event <EVENT>              Filter to one feedback event type. Can be provided more than once
  -h, --help                       Print help
```

#### `verlato feedback recommend`

```text
Prompt and style recommendations from aggregate feedback

Usage: verlato feedback recommend [OPTIONS]

Options:
      --diff-engine <DIFF_ENGINE>  Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
  -h, --help                       Print help
```

### `verlato doctor`

```text
Health checks for the semantic engine and model providers

Usage: verlato doctor [OPTIONS] <COMMAND>

Commands:
  engine  Report the IntentumDiff semantic engine status
  ollama  Probe a local Ollama server: load, queue, prompt and generation timing
  model   Probe the configured suggestion model with a tiny structured request
  help    Print this message or the help of the given subcommand(s)

Options:
      --diff-engine <DIFF_ENGINE>  Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
  -h, --help                       Print help
```

#### `verlato doctor engine`

```text
Report the IntentumDiff semantic engine status

Usage: verlato doctor engine [OPTIONS]

Options:
      --diff-engine <DIFF_ENGINE>  Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
  -h, --help                       Print help
```

#### `verlato doctor ollama`

```text
Probe a local Ollama server: load, queue, prompt and generation timing

Usage: verlato doctor ollama [OPTIONS]

Options:
      --diff-engine <DIFF_ENGINE>      Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
      --parallel-probe                 Run two tiny Ollama suggestion probes concurrently to detect server queueing
      --warmup                         Run a tiny suggestion-style warmup request before measuring Ollama health
      --adaptive-recommendations       Include opt-in adaptive suggestion tuning recommendations in the JSON output
      --suggest-model <SUGGEST_MODEL>  Use a specific Ollama model for the suggestion-style health probe
  -h, --help                           Print help
```

#### `verlato doctor model`

```text
Probe the configured suggestion model with a tiny structured request

Usage: verlato doctor model [OPTIONS]

Options:
      --diff-engine <DIFF_ENGINE>
          Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
      --model-provider <MODEL_PROVIDER>
          Override the configured model provider [possible values: stub, mock, ollama, litellm, plugin, github-copilot, codex-cli, claude-code]
      --suggest-model <SUGGEST_MODEL>
          Use a specific model for the suggestion-style health probe
      --timeout-seconds <TIMEOUT_SECONDS>
          Advisory timeout budget used to label slow probes
  -h, --help
          Print help
```

### `verlato commit`

```text
Draft Conventional Commit messages from commit evidence

Usage: verlato commit [OPTIONS] <COMMAND>

Commands:
  conventional  Draft a full Conventional Commits message for one existing commit
  help          Print this message or the help of the given subcommand(s)

Options:
      --diff-engine <DIFF_ENGINE>  Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
  -h, --help                       Print help
```

#### `verlato commit conventional`

```text
Draft a full Conventional Commits message for one existing commit

Usage: verlato commit conventional [OPTIONS] --base <BASE> --sha <SHA>

Options:
      --base <BASE>
          Start of the range that contains the commit (exclusive)
      --diff-engine <DIFF_ENGINE>
          Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
      --head <HEAD>
          End of the range that contains the commit (inclusive) [default: HEAD]
      --sha <SHA>
          The commit to draft a message for
      --model-provider <MODEL_PROVIDER>
          Model provider for this run (see `verlato scan --help`)
      --evidence-mode <EVIDENCE_MODE>
          diff-only hides the authored commit message from the model; subject-hint includes it [default: diff-only] [possible values: diff-only, subject-hint]
      --privacy <PRIVACY>
          Privacy mode for model evidence: metadata-only, local-summary-then-ai or trusted-provider-patch
      --allow-remote-patches
          Allow trusted-provider-patch evidence for remote providers
  -h, --help
          Print help
```

### `verlato model-logs`

```text
Export model telemetry debug logs

Usage: verlato model-logs [OPTIONS] <COMMAND>

Commands:
  export  Normalize a model debug log into a verlato.model_logs.v1 document (redacted by default)
  help    Print this message or the help of the given subcommand(s)

Options:
      --diff-engine <DIFF_ENGINE>  Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
  -h, --help                       Print help
```

#### `verlato model-logs export`

```text
Normalize a model debug log into a verlato.model_logs.v1 document (redacted by default)

Usage: verlato model-logs export [OPTIONS] --file <FILE>

Options:
      --diff-engine <DIFF_ENGINE>  Change-evidence engine: intentumdiff (semantic review, default) or git (legacy text patches) [possible values: intentumdiff, git]
      --file <FILE>                JSONL model debug log produced by VERLATO_MODEL_LOG_PATH
      --format <FORMAT>            Output format: one JSON document, or one record per line [default: json] [possible values: json, jsonl]
      --output <OUTPUT>            Optional output path. Defaults to stdout
      --event <EVENT>              Filter to one event name. Can be provided more than once
  -h, --help                       Print help
```
