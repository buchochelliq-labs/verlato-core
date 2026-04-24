# ReleasePilot VS Code Extension

This extension is the first UI scaffold for ReleasePilot.

## Planned workflow

1. Scan the current repository for unpublished changes.
2. Preview generated release notes in a webview panel.
3. Apply a generated changelog or release-note draft back into the repo.

## Current scaffold

- Activity Bar view for ReleasePilot items
- Tree view for draft sections and items
- CLI-backed client abstraction
- Preview panel for rendered notes
- Apply commands for changelog and GitHub release-note targets

## Development

```bash
npm install
npm run build
```

Then launch the extension with the VS Code Extension Development Host.
