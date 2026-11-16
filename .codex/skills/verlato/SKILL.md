---
name: verlato
scope: workspace
category: workflow

description: |
  Step-by-step workflow and quality criteria for working on Verlato, the diff-first changelog and release-note engine. Use this skill to ensure backend, MCP, and VS Code extension changes follow project conventions and maintain deterministic, reviewable output.

---

# Verlato Skill

## When to Use
- Working on the Rust backend in `crates/verlato/` (CLI, service, MCP server, pipeline)
- Editing release-note draft generation, classification, or apply logic
- Changing state handling, MCP/editor integration, or review metadata
- Modifying the VS Code extension in `vscode-extension/`

## Workflow Steps
1. **Inspect Entrypoint**: Review the backend service or CLI entrypoint relevant to your change before editing code.
2. **Update Tests**: For any contract, output, or state change, update or add tests (especially for JSON, state, and rendering).
3. **Extension Integration**: If changing the VS Code extension, ensure it calls backend contracts/services—do not duplicate business logic in TypeScript.
4. **Preserve Determinism**: Keep classification/grouping logic deterministic; providers (AI, etc.) are assists, not authorities. When determinism conflicts with backend-only logic, keep the logic in the backend but design it deterministically — for example, if a backend service receives non-deterministic AI suggestions, apply a deterministic rule inside that same service to produce the final classification rather than surfacing the raw AI output.
5. **Respect Review Metadata**: Preserve `ChangeItem` review metadata (edited summaries, hidden items) across edits by default. Only three actions qualify as explicit user updates that override a preserved value: (1) saving an edited summary via the UI editor, (2) toggling an item's hidden flag via the UI, or (3) sending a PATCH or edit API request that targets that specific metadata field. Every other operation must leave existing metadata unchanged.
6. **Safe Output Updates**: Make output changes via normal Markdown files and reviewable diffs, not direct file overwrites.
7. **Align Documentation**: Update `docs/` when commands, config, or workflows change, and regenerate `docs/reference/cli.md` with `scripts/gen_cli_reference.py` when commands or flags change.

## Quality Criteria
> **Constraint priority** (highest to lowest): determinism → backend-only logic → test coverage → documentation alignment. When constraints conflict, apply this order. For example, if achieving determinism requires refactoring backend logic, refactor the backend rather than compromising determinism.

- All business logic remains in the Rust crate (`crates/verlato`); the Python package and the extension stay thin.
- Tests cover new/changed contracts and output.
- Extension is thin, delegating to backend.
- Output is deterministic and reviewable.
- Docs are current with code and workflow changes.

## Example Prompts
- "Draft a release-note for a new feature in Verlato."
- "Update the MCP integration to support new state fields."
- "Refactor the extension to use the backend draft API."
- "Add a test for the new classification heuristic."

## Related Customizations
- Skill for deterministic changelog grouping
- Skill for MCP/VS Code extension integration patterns
- Skill for review-state and metadata handling
