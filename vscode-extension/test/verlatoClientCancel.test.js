const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const Module = require("node:module");
const path = require("node:path");
const test = require("node:test");

function loadClientWithSpawnMock(spawnMock, configValues = {}) {
  const originalLoad = Module._load;
  delete require.cache[require.resolve("../dist/client/verlatoClient")];
  Module._load = function patchedLoad(request, parent, isMain) {
    if (request === "vscode") {
      return {
        EventEmitter: class {
          constructor() {
            this.fire = () => {};
            this.event = () => ({ dispose() {} });
          }
          dispose() {}
        },
        workspace: {
          getConfiguration() {
            return {
              get(key, fallback) {
                if (Object.prototype.hasOwnProperty.call(configValues, key)) {
                  return configValues[key];
                }
                return fallback;
              },
              update() {
                return Promise.resolve();
              },
            };
          },
        },
        authentication: {
          getSession() {
            return Promise.resolve(undefined);
          },
        },
      };
    }
    if (request === "node:child_process") {
      return { spawn: spawnMock };
    }
    return originalLoad.call(this, request, parent, isMain);
  };
  try {
    return require("../dist/client/verlatoClient");
  } finally {
    Module._load = originalLoad;
  }
}

function makeClient(spawnMock, configValues = {}) {
  const { VerlatoClient } = loadClientWithSpawnMock(spawnMock, configValues);
  return new VerlatoClient(
    { uri: { fsPath: path.join("C:", "repos", "consumer") } },
    path.join("C:", "repos", "release-pilot", "vscode-extension"),
    { appendLine() {} },
  );
}

function makeChild(pid = 12345) {
  const child = new EventEmitter();
  const stdout = new EventEmitter();
  const stderr = new EventEmitter();
  stdout.setEncoding = () => {};
  stderr.setEncoding = () => {};
  child.stdout = stdout;
  child.stderr = stderr;
  child.pid = pid;
  child.killed = false;
  child.kill = () => {
    child.killed = true;
  };
  return child;
}

async function waitForSpawn(spawnCalls) {
  for (let index = 0; index < 20; index += 1) {
    if (spawnCalls.length > 0) {
      return;
    }
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.fail("Timed out waiting for spawn.");
}

test("cancelCommand kills the tracked Windows process tree before aborting", () => {
  const spawnCalls = [];
  const client = makeClient((command, args, options) => {
    spawnCalls.push({ command, args, options });
    return new EventEmitter();
  });
  let aborted = false;
  let childKillCalled = false;
  client.activeCommandControllers.set(7, { abort: () => { aborted = true; } });
  client.activeCommandChildren.set(7, {
    pid: 12345,
    killed: false,
    kill: () => { childKillCalled = true; },
  });

  client.cancelCommand(7);

  assert.equal(aborted, true);
  assert.equal(childKillCalled, false);
  assert.equal(spawnCalls.length, process.platform === "win32" ? 1 : 0);
  if (process.platform === "win32") {
    assert.deepEqual(spawnCalls[0], {
      command: "taskkill",
      args: ["/pid", "12345", "/T", "/F"],
      options: { windowsHide: true, stdio: "ignore" },
    });
  }
});

test("optional enrichment processes are visible and cancellable", async () => {
  const spawnCalls = [];
  const child = makeChild(333);
  const client = makeClient((command, args, options) => {
    spawnCalls.push({ command, args, options });
    return command === "taskkill" ? new EventEmitter() : child;
  });

  const pending = client.runOptionalProcess("gh", ["pr", "view", "12"]);
  const running = client.getCommandRequests()[0];
  assert.equal(running.status, "running");
  assert.equal(running.command, "gh");
  assert.deepEqual(running.args, ["pr", "view", "12"]);

  client.cancelActiveCommands();
  child.emit("close", null);
  await assert.rejects(pending, /Verlato command cancelled/);

  const cancelled = client.getCommandRequests()[0];
  assert.equal(cancelled.status, "cancelled");
  assert.equal(client.activeCommandControllers.size, 0);
  assert.equal(client.activeCommandChildren.size, 0);
  if (process.platform === "win32") {
    assert.deepEqual(spawnCalls[1].args, ["/pid", "333", "/T", "/F"]);
  } else {
    assert.equal(child.killed, true);
  }
});

test("preview suggestions passes configured parallelism to the CLI and environment", async () => {
  const spawnCalls = [];
  const child = makeChild(555);
  const client = makeClient((command, args, options) => {
    spawnCalls.push({ command, args, options });
    return child;
  }, { suggestionParallelism: 4 });

  const pending = client.previewDraftSuggestions({ base: "v1.0.0", head: "HEAD" });
  await waitForSpawn(spawnCalls);
  child.stdout.emit("data", JSON.stringify({
    base_ref: "v1.0.0",
    head_ref: "HEAD",
    provider: "mock",
    count: 1,
    items: [],
  }));
  child.emit("close", 0);
  await pending;

  assert.deepEqual(spawnCalls[0].args, [
    "draft",
    "suggest",
    "--base",
    "v1.0.0",
    "--head",
    "HEAD",
    "--preview",
    "--count",
    "1",
    "--parallelism",
    "4",
    "--tone",
    "release-note",
    "--all-visible",
  ]);
  assert.equal(spawnCalls[0].options.env.VERLATO_SUGGESTION_PARALLELISM, "4");
});

test("optional enrichment failures complete without failed request status", async () => {
  const child = makeChild(444);
  const client = makeClient(() => child);

  const pending = client.runOptionalProcess("gh", ["pr", "view", "13"]);
  child.stderr.emit("data", "not authenticated");
  child.emit("close", 1);
  const result = await pending;

  assert.equal(result, undefined);
  const request = client.getCommandRequests()[0];
  assert.equal(request.status, "completed");
  assert.equal(request.exitCode, 1);
  assert.equal(request.error, "not authenticated");
});

test("cancelActiveCommands cancels every tracked request", () => {
  const spawnCalls = [];
  const client = makeClient((command, args, options) => {
    spawnCalls.push({ command, args, options });
    return new EventEmitter();
  });
  const aborted = [];
  client.activeCommandControllers.set(1, { abort: () => { aborted.push(1); } });
  client.activeCommandControllers.set(2, { abort: () => { aborted.push(2); } });
  client.activeCommandChildren.set(1, { pid: 111, killed: false, kill() {} });
  client.activeCommandChildren.set(2, { pid: 222, killed: false, kill() {} });

  client.cancelActiveCommands();

  assert.deepEqual(aborted, [1, 2]);
  if (process.platform === "win32") {
    assert.deepEqual(spawnCalls.map((call) => call.args), [
      ["/pid", "111", "/T", "/F"],
      ["/pid", "222", "/T", "/F"],
    ]);
  }
});
