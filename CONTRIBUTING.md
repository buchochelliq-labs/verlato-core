# Contributing to Verlato

## Development workflow

1. Create a branch for your change.
2. Install dependencies:
   ```bash
   python -m pip install -e .[dev]
   ```
3. Run local checks before committing:
   ```bash
   ruff check .
   mypy verlato
   pytest
   ```
4. Update `ARCHITECTURE.md` and `BACKLOG.md` for any structural changes.

## Decision log

When landing notable architecture changes, capture:

- **Context**: what problem was observed?
- **Decision**: what was chosen?
- **Consequences**: what trade-offs were accepted?

Keep entries in pull request descriptions or in an ADR if introduced later.
