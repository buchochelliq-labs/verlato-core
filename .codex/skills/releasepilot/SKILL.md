---
name: releasepilot
scope: workspace
category: workflow

description: |
	Step-by-step workflow and quality criteria for working on ReleasePilot, the PR-first changelog and release-note engine. Use this skill to ensure backend, MCP, and VS Code extension changes follow project conventions and maintain deterministic, reviewable output.

---

# ReleasePilot Skill

## When to Use
- Working on Python backend in `releasepilot/`
- Editing release-note draft generation, classification, or apply logic
- Changing state handling, MCP/editor integration, or review metadata
- Modifying the VS Code extension in `vscode-extension/`

## Workflow Steps
1. **Inspect Entrypoint**: Review the backend service or CLI entrypoint relevant to your change before editing code.
2. **Update Tests**: For any contract, output, or state change, update or add tests (especially for JSON, state, and rendering).
3. **Extension Integration**: If changing the VS Code extension, ensure it calls backend contracts/services—do not duplicate business logic in TypeScript.
4. **Preserve Determinism**: Keep classification/grouping logic deterministic; providers (AI, etc.) are assists, not authorities.
5. **Respect Review Metadata**: Treat `ChangeItem` review metadata (edited summaries, hidden items) as meaningful and persistent product state.
6. **Safe Output Updates**: Make output changes via normal Markdown files and reviewable diffs, not direct file overwrites.
7. **Align Documentation**: Update docs when commands, config, or workflows change.

## Quality Criteria
- All business logic remains in Python services.
- Tests cover new/changed contracts and output.
- Extension is thin, delegating to backend.
- Output is deterministic and reviewable.
- Docs are current with code and workflow changes.

## Example Prompts
- "Draft a release-note for a new feature in ReleasePilot."
- "Update the MCP integration to support new state fields."
- "Refactor the extension to use the backend draft API."
- "Add a test for the new classification heuristic."

## Related Customizations
- Skill for deterministic changelog grouping
- Skill for MCP/VS Code extension integration patterns
- Skill for review-state and metadata handling
