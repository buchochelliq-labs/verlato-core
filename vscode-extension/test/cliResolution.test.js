const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");

const { getVenvExecutable, resolveReleasePilotExecutable } = require("../dist/client/cliResolution");

const workspacePath = path.join("C:", "repos", "consumer-project");
const extensionPath = path.join("C:", "repos", "release-pilot", "vscode-extension");

test("uses configured releasepilot CLI path without probing fallback locations", () => {
  const configuredPath = path.join("C:", "tools", "releasepilot.exe");
  const seen = [];

  const resolution = resolveReleasePilotExecutable({
    configuredPath,
    workspacePath,
    extensionPath,
    existsSync(candidate) {
      seen.push(candidate);
      return false;
    },
    platform: "win32",
  });

  assert.deepEqual(resolution, {
    executable: configuredPath,
    searched: [configuredPath],
  });
  assert.deepEqual(seen, []);
});

test("prefers a workspace-local releasepilot CLI when no explicit path is configured", () => {
  const workspaceCli = getVenvExecutable(workspacePath, "win32");

  const resolution = resolveReleasePilotExecutable({
    configuredPath: "releasepilot",
    workspacePath,
    extensionPath,
    existsSync: (candidate) => candidate === workspaceCli,
    platform: "win32",
  });

  assert.equal(resolution.executable, workspaceCli);
  assert.deepEqual(resolution.searched, [
    workspaceCli,
    getVenvExecutable(extensionPath, "win32"),
    getVenvExecutable(path.dirname(extensionPath), "win32"),
    "releasepilot",
  ]);
});

test("falls back to the extension checkout venv for packaged extension paths", () => {
  const checkoutCli = getVenvExecutable(path.dirname(extensionPath), "win32");

  const resolution = resolveReleasePilotExecutable({
    configuredPath: "releasepilot",
    workspacePath,
    extensionPath,
    existsSync: (candidate) => candidate === checkoutCli,
    platform: "win32",
  });

  assert.equal(resolution.executable, checkoutCli);
});

test("prefers the extension venv before the extension checkout venv", () => {
  const extensionCli = getVenvExecutable(extensionPath, "win32");
  const checkoutCli = getVenvExecutable(path.dirname(extensionPath), "win32");

  const resolution = resolveReleasePilotExecutable({
    configuredPath: "releasepilot",
    workspacePath,
    extensionPath,
    existsSync: (candidate) => candidate === extensionCli || candidate === checkoutCli,
    platform: "win32",
  });

  assert.equal(resolution.executable, extensionCli);
});

test("falls back to the releasepilot command when no local venv executable exists", () => {
  const resolution = resolveReleasePilotExecutable({
    configuredPath: "",
    workspacePath,
    extensionPath,
    existsSync: () => false,
    platform: "linux",
  });

  assert.equal(resolution.executable, "releasepilot");
  assert.deepEqual(resolution.searched, [
    getVenvExecutable(workspacePath, "linux"),
    getVenvExecutable(extensionPath, "linux"),
    getVenvExecutable(path.dirname(extensionPath), "linux"),
    "releasepilot",
  ]);
});

test("builds the platform-specific releasepilot executable path", () => {
  assert.equal(
    getVenvExecutable("/workspace/project", "linux"),
    path.join("/workspace/project", ".venv", "bin", "releasepilot"),
  );
  assert.equal(
    getVenvExecutable(workspacePath, "win32"),
    path.join(workspacePath, ".venv", "Scripts", "releasepilot.exe"),
  );
});
