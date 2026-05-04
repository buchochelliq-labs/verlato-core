import * as path from "node:path";

export interface ExecutableResolution {
  executable: string;
  searched: string[];
}

export function resolveReleasePilotExecutable(options: {
  configuredPath: string;
  workspacePath: string;
  extensionPath: string;
  existsSync: (candidate: string) => boolean;
  platform?: NodeJS.Platform;
}): ExecutableResolution {
  const configuredPath = options.configuredPath || "releasepilot";
  if (configuredPath !== "releasepilot") {
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

export function getVenvExecutable(rootPath: string, platform: NodeJS.Platform = process.platform): string {
  return platform === "win32"
    ? path.join(rootPath, ".venv", "Scripts", "releasepilot.exe")
    : path.join(rootPath, ".venv", "bin", "releasepilot");
}
