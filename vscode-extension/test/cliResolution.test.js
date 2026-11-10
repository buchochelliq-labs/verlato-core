const assert = require("node:assert/strict");
const path = require("node:path");
const test = require("node:test");

const {
  getNativeExecutables,
  getVenvExecutable,
  resolveSpawnCommand,
  resolveVerlatoExecutable,
} = require("../dist/client/cliResolution");

const workspacePath = path.join("C:", "repos", "consumer-project");
const extensionPath = path.join("C:", "repos", "verlato", "vscode-extension");

test("uses configured verlato CLI path without probing fallback locations", () => {
  const configuredPath = path.join("C:", "tools", "verlato.exe");
  const seen = [];

  const resolution = resolveVerlatoExecutable({
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

test("normalizes configured CLI path whitespace and quotes", () => {
  const configuredPath = `"${path.join("C:", "tools", "verlato.exe")}"`;

  const resolution = resolveVerlatoExecutable({
    configuredPath: `  ${configuredPath}  `,
    workspacePath,
    extensionPath,
    existsSync: () => false,
    platform: "win32",
  });

  assert.equal(resolution.executable, path.join("C:", "tools", "verlato.exe"));
});

test("prefers a workspace-local verlato CLI when no explicit path is configured", () => {
  const workspaceCli = getVenvExecutable(workspacePath, "win32");

  const resolution = resolveVerlatoExecutable({
    configuredPath: "verlato",
    workspacePath,
    extensionPath,
    existsSync: (candidate) => candidate === workspaceCli,
    platform: "win32",
  });

  assert.equal(resolution.executable, workspaceCli);
  assert.deepEqual(resolution.searched, [
    ...getNativeExecutables(extensionPath, "win32"),
    workspaceCli,
    getVenvExecutable(extensionPath, "win32"),
    getVenvExecutable(path.dirname(extensionPath), "win32"),
    "verlato",
  ]);
});

test("falls back to the extension checkout venv for packaged extension paths", () => {
  const checkoutCli = getVenvExecutable(path.dirname(extensionPath), "win32");

  const resolution = resolveVerlatoExecutable({
    configuredPath: "verlato",
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

  const resolution = resolveVerlatoExecutable({
    configuredPath: "verlato",
    workspacePath,
    extensionPath,
    existsSync: (candidate) => candidate === extensionCli || candidate === checkoutCli,
    platform: "win32",
  });

  assert.equal(resolution.executable, extensionCli);
});

test("falls back to the verlato command when no local venv executable exists", () => {
  const resolution = resolveVerlatoExecutable({
    configuredPath: "",
    workspacePath,
    extensionPath,
    existsSync: () => false,
    platform: "linux",
  });

  assert.equal(resolution.executable, "verlato");
  assert.deepEqual(resolution.searched, [
    ...getNativeExecutables(extensionPath, "linux"),
    getVenvExecutable(workspacePath, "linux"),
    getVenvExecutable(extensionPath, "linux"),
    getVenvExecutable(path.dirname(extensionPath), "linux"),
    "verlato",
  ]);
});

test("builds the platform-specific verlato executable path", () => {
  assert.equal(
    getVenvExecutable("/workspace/project", "linux"),
    path.join("/workspace/project", ".venv", "bin", "verlato"),
  );
  assert.equal(
    getVenvExecutable(workspacePath, "win32"),
    path.join(workspacePath, ".venv", "Scripts", "verlato.exe"),
  );
});

test("wraps Windows command shims through cmd.exe", () => {
  const shimPath = path.join("C:", "tools", "verlato.cmd");

  const resolution = resolveSpawnCommand(shimPath, ["state", "show"], {
    platform: "win32",
    comSpec: "C:\\Windows\\System32\\cmd.exe",
  });

  assert.deepEqual(resolution, {
    command: "C:\\Windows\\System32\\cmd.exe",
    args: ["/d", "/s", "/c", shimPath, "state", "show"],
  });
});

test("spawns native executables directly", () => {
  const exePath = path.join("C:", "tools", "verlato.exe");

  assert.deepEqual(resolveSpawnCommand(exePath, ["state", "show"], { platform: "win32" }), {
    command: exePath,
    args: ["state", "show"],
  });
});

test("prefers the native Rust binary bundled with the extension or built in the checkout", () => {
  const [bundled, checkoutBuild] = getNativeExecutables(extensionPath, "win32");
  assert.equal(bundled, path.join(extensionPath, "bin", "verlato.exe"));
  assert.equal(checkoutBuild, path.join(path.dirname(extensionPath), "target", "release", "verlato.exe"));

  const workspaceCli = getVenvExecutable(workspacePath, "win32");
  const resolution = resolveVerlatoExecutable({
    configuredPath: "verlato",
    workspacePath,
    extensionPath,
    existsSync: (candidate) => candidate === checkoutBuild || candidate === workspaceCli,
    platform: "win32",
  });
  assert.equal(resolution.executable, checkoutBuild);
});

test("falls back to a verlato on PATH, such as a pip install or an unpacked release bundle", () => {
  const resolution = resolveVerlatoExecutable({
    configuredPath: "",
    workspacePath,
    extensionPath,
    existsSync: () => false,
    platform: "linux",
  });
  assert.equal(resolution.executable, "verlato");
  assert.equal(resolution.searched.at(-1), "verlato");
});

test("uses a configured release bundle executable as given", () => {
  const bundled = "/opt/verlato-v0.2.0-x86_64-unknown-linux-gnu/verlato";
  const resolution = resolveVerlatoExecutable({
    configuredPath: bundled,
    workspacePath,
    extensionPath,
    existsSync: () => false,
    platform: "linux",
  });
  assert.equal(resolution.executable, bundled);
});
