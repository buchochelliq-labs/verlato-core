# Verlato VS Code Extension

Diff-first release notes. Understands your code changes. Writes your releases.

## Planned workflow

1. Scan the current repository for unpublished changes.
2. Preview generated release notes in a webview panel.
3. Apply a generated changelog or release-note draft back into the repo.

## Current scaffold

- Activity Bar view for Verlato release control
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

5. Open the Run and Debug view, select `Verlato Extension`, and start debugging.
6. In the Extension Development Host window, open the `Verlato` activity bar view and run `Verlato: Scan Repository`.

The default provider is local Ollama with `gemma4:latest`. Change it from `Verlato: Change AI Model` or with these workspace settings:

```json
"verlato.modelProvider": "ollama",
"verlato.ollamaModel": "gemma4:latest",
"verlato.ollamaSuggestionModel": "",
"verlato.ollamaEndpoint": "http://127.0.0.1:11434/api/chat",
"verlato.maxModelItems": 25,
"verlato.suggestionParallelism": 2,
"verlato.suggestionBatchSize": 2,
"verlato.suggestionAutoWarmCache": true,
"verlato.suggestionPromptProfile": "conservative",
"verlato.suggestionAdaptiveRecommendations": false
```

`verlato.maxModelItems` prevents a large comparison range from sending one LLM request per grouped change. Set it to `-1` only when you intentionally want no fanout guard.
`verlato.ollamaSuggestionModel` can point suggestions at a smaller/faster local model while summaries and classification keep using `verlato.ollamaModel`; leave it empty to use the main model.
`verlato.suggestionParallelism` controls how many draft suggestion preview requests may run at once.
`verlato.suggestionBatchSize` controls how many uncached Ollama suggestion items can share one structured model request; non-Ollama providers still run item-by-item.
`verlato.suggestionAutoWarmCache` quietly warms the local suggestion cache after a draft loads when the active provider is Ollama; turn it off if you only want model calls after clicking Suggest.
`verlato.suggestionPromptProfile` chooses the suggestion prompt budget: `conservative`, `lean`, or `minimal`; `conservative` is the default.
`verlato.suggestionAdaptiveRecommendations` shows opt-in suggestion performance advice from preview and doctor telemetry without changing settings.
The Intent Inference panel includes Model health and Warm model actions for Ollama. Model health runs a tiny suggestion-style probe, reads `/api/ps` through the configured endpoint, and optionally enriches the result with `ollama ps` processor details when the CLI is available. Warm model runs the same style of tiny request before larger suggestion runs. If Ollama diagnostics show queue time, set server variables such as `OLLAMA_NUM_PARALLEL`, `OLLAMA_MAX_QUEUE`, and `OLLAMA_CONTEXT_LENGTH` before starting Ollama; the extension can only tune Verlato's client-side request fanout.

If the launch config does not appear, open `.vscode/launch.json` from the repo root and start the `Verlato Extension` configuration from the editor's debug controls.

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
   & "$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd" --install-extension .\vscode-extension\verlato-vscode-0.0.1.vsix --force
   ```

5. Reload VS Code, open a repository, then run `Verlato: Scan Repository`.

The extension looks for the Verlato CLI in this order:

1. The `verlato.cliPath` VS Code setting, if you set it.
2. A workspace-local `.venv\Scripts\verlato.exe` on Windows, or `.venv/bin/verlato` on macOS/Linux.
3. The installed extension's `.venv`, or during extension development, the checked-out Verlato repo beside `vscode-extension/`.
4. `verlato` on VS Code's inherited `PATH`.

On Windows, the repo-level `scripts\install_vscode_extension.ps1` script installs the backend into the Verlato checkout's `.venv` and writes the matching `verlato.cliPath` VS Code user setting. That setting is needed when you open the extension from another repository that does not have Verlato installed in its own `.venv`.

If scanning fails with `spawn verlato ENOENT`, install the backend into the
workspace virtual environment:

```bash
.\.venv\Scripts\python.exe -m pip install -e .
```

Or set `verlato.cliPath` to the full executable path, for example:

```json
"verlato.cliPath": "C:\\Users\\nickn\\OneDrive\\Documents\\GitHub\\verlato\\.venv\\Scripts\\verlato.exe"
```

On Windows, prefer the `code.cmd` command above if `code --list-extensions`
does not print anything. That means PowerShell is resolving `code` to
`Code.exe` instead of the VS Code command-line shim.

To verify the extension is installed:

```bash
& "$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd" --list-extensions --show-versions | Select-String verlato
```

To test a freshly packaged build, uninstall the existing local copy first:

```bash
& "$env:LOCALAPPDATA\Programs\Microsoft VS Code\bin\code.cmd" --uninstall-extension verlato.verlato-vscode
```

For agent-side MCP testing in the same workspace:

- Install the optional MCP dependency:
  `python -m pip install -e .[mcp]`
- Keep `.vscode/mcp.json` enabled so VS Code/Copilot can start the FastMCP server from `scripts/verlato_mcp_server.py:mcp`.
