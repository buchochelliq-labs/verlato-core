import * as vscode from "vscode";

import { McpServerStatus } from "./client/types";

const MCP_TERMINAL_NAME = "Verlato MCP";
const MCP_CONFIG_PATH = ".vscode/mcp.json";

export class McpServerController implements vscode.Disposable {
  private readonly onDidChangeStatusEmitter = new vscode.EventEmitter<McpServerStatus>();
  readonly onDidChangeStatus = this.onDidChangeStatusEmitter.event;
  private readonly terminalCloseDisposable: vscode.Disposable;
  private terminal: vscode.Terminal | undefined;
  private status: McpServerStatus;

  constructor(private readonly workspaceFolder: vscode.WorkspaceFolder) {
    this.status = notRunningStatus();
    this.terminalCloseDisposable = vscode.window.onDidCloseTerminal((terminal) => {
      if (terminal !== this.terminal) {
        return;
      }
      this.terminal = undefined;
      this.setStatus(notRunningStatus("The Verlato MCP terminal was closed."));
    });
  }

  getStatus(): McpServerStatus {
    return this.status;
  }

  async start(): Promise<McpServerStatus> {
    if (this.terminal) {
      this.terminal.show();
      return this.status;
    }
    const cliPath = vscode.workspace.getConfiguration("verlato").get<string>("cliPath") || "verlato";
    const command = `${quoteShellArg(cliPath)} serve`;
    this.setStatus({
      state: "starting",
      label: "Starting",
      detail: "Opening a local Verlato MCP stdio server terminal...",
      command,
      configPath: MCP_CONFIG_PATH,
    });
    const terminal = vscode.window.createTerminal({
      name: MCP_TERMINAL_NAME,
      cwd: this.workspaceFolder.uri.fsPath,
    });
    this.terminal = terminal;
    terminal.show();
    terminal.sendText(command);
    this.setStatus({
      state: "running",
      label: "Running",
      detail: "Started from the Verlato UI. VS Code/Copilot MCP clients still launch their own attached server from mcp.json.",
      command,
      configPath: MCP_CONFIG_PATH,
      startedAt: new Date().toISOString(),
    });
    return this.status;
  }

  dispose(): void {
    this.terminalCloseDisposable.dispose();
    this.onDidChangeStatusEmitter.dispose();
  }

  private setStatus(status: McpServerStatus): void {
    this.status = status;
    this.onDidChangeStatusEmitter.fire(status);
  }
}

export async function revealMcpConfig(workspaceFolder: vscode.WorkspaceFolder | undefined): Promise<void> {
  if (!workspaceFolder) {
    return;
  }
  const uri = vscode.Uri.joinPath(workspaceFolder.uri, ".vscode", "mcp.json");
  await vscode.window.showTextDocument(uri, { preview: false });
}

export function notRunningStatus(detail = "No Verlato MCP server has been started from this window."): McpServerStatus {
  return {
    state: "not-running",
    label: "Not running",
    detail,
    command: "verlato serve",
    configPath: MCP_CONFIG_PATH,
  };
}

function quoteShellArg(value: string): string {
  if (!/[\s"'&|<>^]/.test(value)) {
    return value;
  }
  return `"${value.replace(/"/g, '\\"')}"`;
}
