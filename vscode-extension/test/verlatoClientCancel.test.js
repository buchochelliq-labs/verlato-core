const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const Module = require("node:module");
const path = require("node:path");
const test = require("node:test");

function loadClientWithSpawnMock(spawnMock) {
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
              get(_key, fallback) {
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

function makeClient(spawnMock) {
  const { VerlatoClient } = loadClientWithSpawnMock(spawnMock);
  return new VerlatoClient(
    { uri: { fsPath: path.join("C:", "repos", "consumer") } },
    path.join("C:", "repos", "release-pilot", "vscode-extension"),
    { appendLine() {} },
  );
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
