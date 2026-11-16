# Copilot Instructions For Verlato

Read [AGENT.md](../AGENT.md) first for the project mission and constraints, and
[docs/technical/architecture.md](../docs/technical/architecture.md) for the module map.

When working in this repo:

- Treat Verlato as a release-note engine plus editor workflow, not just a Markdown renderer.
- Prefer the existing CLI and service contracts (implemented in `crates/verlato`) instead of duplicating logic in TypeScript or Python.
- Preserve the PR-first grouping and deterministic classification model.
- Respect review metadata such as edited summaries and hidden items.
- When changing the VS Code extension, keep it thin and use backend commands or services for business logic.
- Update the documentation in `docs/` with any user-visible change.

Additional agent-facing guidance lives in `.codex/skills/verlato/SKILL.md`.
