import { execFile } from "node:child_process";
import * as fs from "node:fs";

import * as vscode from "vscode";

import { resolveVerlatoExecutable } from "./client/cliResolution";
import {
  INSTALL_DOCS_URL,
  RELEASES_URL,
  UnsupportedPlatformError,
  downloadedExecutable,
  installRuntime,
  pathExecutables,
  runtimeBundle,
  runtimeVersion,
  versionMatches,
} from "./client/runtime";

/**
 * Finds the native `verlato` runtime, or downloads it. The order:
 *
 * 1. `verlato.cliPath`, when set;
 * 2. a local install the extension already knows about (an unpacked bundle in the extension's
 *    `bin/`, a source build, or a `verlato` in the workspace's `.venv`);
 * 3. a `verlato` on PATH whose version has the extension's major.minor;
 * 4. a runtime this extension downloaded earlier (global storage, per version);
 * 5. the release bundle for this platform, downloaded and verified against the release's
 *    SHA256SUMS (automatically with `verlato.runtime.autoDownload`, otherwise on request).
 *
 * Returns the executable to use, or undefined when the client's own resolution (1–2) applies.
 */
export class RuntimeManager {
  private warnedVersions = new Set<string>();

  constructor(
    private readonly context: vscode.ExtensionContext,
    private readonly workspacePath: string,
    private readonly output: vscode.OutputChannel,
  ) {}

  /** The runtime version to use and download (package.json `verlatoRuntime`). */
  get version(): string {
    return runtimeVersion(this.context.extension.packageJSON ?? {});
  }

  async prepare(): Promise<string | undefined> {
    const config = vscode.workspace.getConfiguration("verlato");
    const cliPath = (config.get<string>("cliPath") || "").trim();
    if (cliPath && cliPath !== "verlato") {
      return undefined;
    }
    const local = resolveVerlatoExecutable({
      configuredPath: "verlato",
      workspacePath: this.workspacePath,
      extensionPath: this.context.extensionPath,
      existsSync: fs.existsSync,
    });
    if (local.executable !== "verlato") {
      return undefined;
    }

    const onPath = await this.findOnPath();
    if (onPath) {
      return onPath;
    }

    const bundle = runtimeBundle(this.version);
    if (bundle) {
      const downloaded = downloadedExecutable(this.storageDir, this.version, bundle);
      if (fs.existsSync(downloaded)) {
        this.output.appendLine(`Verlato runtime: using the downloaded ${downloaded}`);
        return downloaded;
      }
    }

    if (config.get<boolean>("runtime.autoDownload", true)) {
      return this.download();
    }
    const choice = await vscode.window.showInformationMessage(
      `Verlato needs its native runtime (version ${this.version}). Download it from GitHub Releases?`,
      "Download",
      "Install Instructions",
    );
    if (choice === "Download") {
      return this.download();
    }
    if (choice === "Install Instructions") {
      void vscode.env.openExternal(vscode.Uri.parse(INSTALL_DOCS_URL));
    }
    return undefined;
  }

  /** Download (or re-download) the runtime for this version; returns its executable. */
  async download(): Promise<string | undefined> {
    try {
      const executable = await vscode.window.withProgress(
        { location: vscode.ProgressLocation.Notification, title: "Verlato runtime", cancellable: false },
        async (progress) => {
          let reported = 0;
          return installRuntime({
            version: this.version,
            storageDir: this.storageDir,
            onProgress: (message, fraction) => {
              const increment = fraction === undefined ? undefined : Math.max(0, fraction * 100 - reported);
              if (fraction !== undefined) {
                reported = fraction * 100;
              }
              progress.report({ message, increment });
            },
          });
        },
      );
      this.output.appendLine(`Verlato runtime: installed ${executable}`);
      void vscode.window.showInformationMessage(`Verlato runtime ${this.version} is installed.`);
      return executable;
    } catch (error) {
      const message = error instanceof Error ? error.message : String(error);
      this.output.appendLine(`Verlato runtime: ${message}`);
      const detail =
        error instanceof UnsupportedPlatformError
          ? `${message} Install it with \`pip install verlato-core\`, or build it and set verlato.cliPath.`
          : `Could not download the Verlato runtime: ${message} Install it with \`pip install verlato-core\`, download a bundle from GitHub Releases, or set verlato.cliPath.`;
      const choice = await vscode.window.showErrorMessage(detail, "Install Instructions", "GitHub Releases", "Set CLI Path");
      if (choice === "Install Instructions") {
        void vscode.env.openExternal(vscode.Uri.parse(INSTALL_DOCS_URL));
      } else if (choice === "GitHub Releases") {
        void vscode.env.openExternal(vscode.Uri.parse(`${RELEASES_URL}/tag/v${this.version}`));
      } else if (choice === "Set CLI Path") {
        void vscode.commands.executeCommand("workbench.action.openSettings", "verlato.cliPath");
      }
      return undefined;
    }
  }

  private get storageDir(): string {
    return this.context.globalStorageUri.fsPath;
  }

  private async findOnPath(): Promise<string | undefined> {
    for (const candidate of pathExecutables(process.env.PATH)) {
      if (!fs.existsSync(candidate)) {
        continue;
      }
      const version = await runVersion(candidate);
      if (version !== undefined && versionMatches(version, this.version)) {
        this.output.appendLine(`Verlato runtime: using ${candidate} (${version.trim()})`);
        return candidate;
      }
      if (!this.warnedVersions.has(candidate)) {
        this.warnedVersions.add(candidate);
        this.output.appendLine(
          `Verlato runtime: skipping ${candidate}: ${version === undefined ? "it did not run" : `it is ${version.trim()}`}, the extension needs ${this.version}.`,
        );
      }
    }
    return undefined;
  }
}

function runVersion(executable: string): Promise<string | undefined> {
  return new Promise((resolve) => {
    execFile(executable, ["--version"], { timeout: 10_000, windowsHide: true, shell: /\.(cmd|bat)$/i.test(executable) }, (error, stdout) => {
      resolve(error ? undefined : stdout);
    });
  });
}
