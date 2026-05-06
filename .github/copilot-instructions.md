# Copilot Instructions For Verlato

Read [AGENT.md](../AGENT.md) first for the project mission and constraints.

When working in this repo:

- Treat Verlato as a release-note engine plus editor workflow, not just a Markdown renderer.
- Prefer using existing Python services and CLI contracts instead of duplicating logic in TypeScript.
- Preserve the PR-first grouping and deterministic classification model.
- Respect review metadata such as edited summaries and hidden items.
- When changing the VS Code extension, keep it thin and use backend commands or services for business logic.

Additional agent-facing guidance lives in `.codex/skills/verlato/SKILL.md`.
