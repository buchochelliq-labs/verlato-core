# ReleasePilot VS Code Extension

This extension is the first UI scaffold for ReleasePilot.

## Planned workflow

1. Scan the current repository for unpublished changes.
2. Preview generated release notes in a webview panel.
3. Apply a generated changelog or release-note draft back into the repo.

## Current scaffold

- Activity Bar view for ReleasePilot release control
- Webview interface for draft sections, repository activity, and release actions
- Repository activity view for branches, tags, recent commits, and release associations
- CLI-backed client abstraction
- Configurable AI provider/model controls with an LLM fanout guard
- Preview panel for rendered notes
- Apply commands for changelog and GitHub release-note targets

## Development

```bash
npm install
npm run build
```

## Local testing

### Option 1: Run in an Extension Development Host

1. From the repo root, install the backend package:

   ```bash
   python -m pip install -e .
   ```

2. In `vscode-extension/`, install dependencies:

   ```bash
   npm install
   ```

3. Build the extension:

   ```bash
   npm run build
   ```

4. Open the repo root in VS Code:

   ```bash
   code .
   ```

5. Open the Run and Debug view, select `ReleasePilot Extension`, and start debugging.
6. In the Extension Development Host window, open the `ReleasePilot` activity bar view and run `ReleasePilot: Scan Repository`.

The default provider is local Ollama with `gemma4:31b`. Change it from `ReleasePilot: Change AI Model` or with these workspace settings:

```json
"releasepilot.modelProvider": "ollama",
"releasepilot.ollamaModel": "gemma4:31b",
"releasepilot.ollamaEndpoint": "http://127.0.0.1:11434/api/chat",
"releasepilot.maxModelItems": 25
```

`releasepilot.maxModelItems` prevents a large comparison range from sending one LLM request per grouped change. Set it to `-1` only when you intentionally want no fanout guard.

If the launch config does not appear, open `.vscode/launch.json` from the repo root and start the `ReleasePilot Extension` configuration from the editor's debug controls.

### Option 2: Install a local VSIX

Use this path when you want to test the extension as an installed extension instead of through the debugger.

1. From the repo root, install the backend package:

   ```bash
   python -m pip install -e .
   ```

2. Install dependencies and build the extension:

   ```bash
   cd vscode-extension
   npm install
   npm run build
   ```

3. Package the extension:

   ```bash
   npx @vscode/vsce package
   ```

4. Return to the repo root and install the generated `.vsix`:

   ```bash
   cd ..
   & "$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd" --install-extension .\vscode-extension\releasepilot-vscode-0.0.1.vsix --force
   ```

5. Reload VS Code, open a repository, then run `ReleasePilot: Scan Repository`.

The extension looks for the ReleasePilot CLI in this order:

1. The `releasepilot.cliPath` VS Code setting, if you set it.
2. A workspace-local `.venv\Scripts\releasepilot.exe` on Windows, or `.venv/bin/releasepilot` on macOS/Linux.
3. The installed extension's `.venv`, or during extension development, the checked-out ReleasePilot repo beside `vscode-extension/`.
4. `releasepilot` on VS Code's inherited `PATH`.

On Windows, the repo-level `scripts\install_vscode_extension.ps1` script installs the backend into the ReleasePilot checkout's `.venv` and writes the matching `releasepilot.cliPath` VS Code user setting. That setting is needed when you open the extension from another repository that does not have ReleasePilot installed in its own `.venv`.

If scanning fails with `spawn releasepilot ENOENT`, install the backend into the
workspace virtual environment:

```bash
.\.venv\Scripts\python.exe -m pip install -e .
```

Or set `releasepilot.cliPath` to the full executable path, for example:

```json
"releasepilot.cliPath": "C:\\Users\\nickn\\OneDrive\\Documents\\GitHub\\release-pilot\\.venv\\Scripts\\releasepilot.exe"
```

On Windows, prefer the `code.cmd` command above if `code --list-extensions`
does not print anything. That means PowerShell is resolving `code` to
`Code.exe` instead of the VS Code command-line shim.

To verify the extension is installed:

```bash
& "$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd" --list-extensions --show-versions | Select-String releasepilot
```

To test a freshly packaged build, uninstall the existing local copy first:

```bash
& "$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd" --uninstall-extension releasepilot.releasepilot-vscode
```

For agent-side MCP testing in the same workspace:

- Install the optional MCP dependency:
  `python -m pip install -e .[mcp]`
- Keep `.vscode/mcp.json` enabled so VS Code/Copilot can start the FastMCP server from `scripts/releasepilot_mcp_server.py:mcp`.
