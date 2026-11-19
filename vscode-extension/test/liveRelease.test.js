// Live check of the runtime download against a real GitHub release: download, SHA256SUMS
// verification and extraction for this platform, then run the unpacked `verlato`.
//
// Skipped unless VERLATO_LIVE_RELEASE_TEST=1, so `npm test` stays offline. Run it with:
//   VERLATO_LIVE_RELEASE_TEST=1 npm test
// VERLATO_LIVE_RELEASE_VERSION picks another release (default: package.json's verlatoRuntime).
// Behind a proxy, run Node with NODE_USE_ENV_PROXY=1 (VS Code routes the extension's https itself).
const assert = require("node:assert/strict");
const { execFileSync } = require("node:child_process");
const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const test = require("node:test");

const { installRuntime, runtimeVersion } = require("../dist/client/runtime");

const live = process.env.VERLATO_LIVE_RELEASE_TEST === "1";
const version =
  process.env.VERLATO_LIVE_RELEASE_VERSION || runtimeVersion(require("../package.json"));

test(
  `downloads, verifies and runs the published v${version} runtime`,
  { skip: !live && "set VERLATO_LIVE_RELEASE_TEST=1 to run against the real release", timeout: 600_000 },
  async () => {
    const storageDir = fs.mkdtempSync(path.join(os.tmpdir(), "verlato-live-"));
    try {
      const messages = [];
      const executable = await installRuntime({
        version,
        storageDir,
        onProgress: (message) => messages.push(message),
      });
      assert.ok(fs.existsSync(executable), executable);
      assert.ok(messages.some((m) => m.startsWith("Downloading")), messages.join("\n"));

      const reported = execFileSync(executable, ["--version"], { encoding: "utf8" }).trim();
      assert.equal(reported, `verlato ${version}`);

      // The bundle's parser components sit next to the executable, where it looks for them.
      const env = { ...process.env };
      delete env.VERLATO_INTENTUMDIFF_WASM_DIR;
      delete env.INTENTUMDIFF_WASM_DIR;
      const engine = JSON.parse(execFileSync(executable, ["doctor", "engine"], { encoding: "utf8", env }));
      assert.equal(engine.available, true, JSON.stringify(engine));
      console.log(`${reported}; engine ${engine.version}; from ${executable}`);
    } finally {
      fs.rmSync(storageDir, { recursive: true, force: true });
    }
  },
);
