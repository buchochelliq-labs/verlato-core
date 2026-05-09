import * as path from "node:path";

export interface ExecutableResolution {
  executable: string;
  searched: string[];
}

export interface SpawnCommandResolution {
  command: string;
  args: string[];
}

export function resolveVerlatoExecutable(options: {
  configuredPath: string;
  workspacePath: string;
  extensionPath: string;
  existsSync: (candidate: string) => boolean;
  platform?: NodeJS.Platform;
}): ExecutableResolution {
  const configuredPath = normalizeExecutablePath(options.configuredPath);
  if (configuredPath !== "verlato") {
    return { executable: configuredPath, searched: [configuredPath] };
  }

  const platform = options.platform ?? process.platform;
  const workspaceCli = getVenvExecutable(options.workspacePath, platform);
  const extensionCli = getVenvExecutable(options.extensionPath, platform);
  const extensionCheckoutCli = getVenvExecutable(path.dirname(options.extensionPath), platform);
  const candidates = [workspaceCli, extensionCli, extensionCheckoutCli];
  const executable = candidates.find((candidate) => options.existsSync(candidate)) ?? configuredPath;

  return { executable, searched: [...candidates, configuredPath] };
}

export function resolveSpawnCommand(
  command: string,
  args: string[],
  options: { platform?: NodeJS.Platform; comSpec?: string } = {},
): SpawnCommandResolution {
  const executable = normalizeExecutablePath(command);
  if ((options.platform ?? process.platform) === "win32" && isWindowsCommandScript(executable)) {
    return {
      command: options.comSpec || process.env.ComSpec || "cmd.exe",
      args: ["/d", "/s", "/c", executable, ...args],
    };
  }
  return { command: executable, args };
}

export function getVenvExecutable(rootPath: string, platform: NodeJS.Platform = process.platform): string {
  return platform === "win32"
    ? path.join(rootPath, ".venv", "Scripts", "verlato.exe")
    : path.join(rootPath, ".venv", "bin", "verlato");
}

function normalizeExecutablePath(value: string): string {
  const trimmed = (value || "").trim();
  if (!trimmed) {
    return "verlato";
  }
  if (
    trimmed.length >= 2 &&
    ((trimmed.startsWith("\"") && trimmed.endsWith("\"")) || (trimmed.startsWith("'") && trimmed.endsWith("'")))
  ) {
    return trimmed.slice(1, -1);
  }
  return trimmed;
}

function isWindowsCommandScript(value: string): boolean {
  return /\.(?:cmd|bat)$/i.test(value);
}
