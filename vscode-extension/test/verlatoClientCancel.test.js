const assert = require("node:assert/strict");
const { EventEmitter } = require("node:events");
const Module = require("node:module");
const path = require("node:path");
const test = require("node:test");

function loadClientWithSpawnMock(spawnMock, configValues = {}) {
  const originalLoad = Module._load;
  delete require.cache[require.resolve("../dist/client/verlatoClient")];
  delete require.cache[require.resolve("../dist/client/verlatoServiceClient")];
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

function makeChild(pid = 12345, onStdinWrite) {
  const child = new EventEmitter();
  const stdout = new EventEmitter();
  const stderr = new EventEmitter();
  stdout.setEncoding = () => {};
  stderr.setEncoding = () => {};
  child.stdout = stdout;
  child.stderr = stderr;
  child.stdin = {
    write(data, callback) {
      if (onStdinWrite) {
        onStdinWrite(String(data));
      }
      if (callback) {
        callback();
      }
      return true;
    },
    end() {},
  };
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

async function waitForSpawnCount(spawnCalls, count) {
  for (let index = 0; index < 30; index += 1) {
    if (spawnCalls.length >= count) {
      return;
    }
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.fail(`Timed out waiting for ${count} spawn calls.`);
}

async function waitForWriteCount(writes, count) {
  for (let index = 0; index < 30; index += 1) {
    if (writes.length >= count) {
      return;
    }
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.fail(`Timed out waiting for ${count} service writes.`);
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

test("preview suggestions passes configured parallelism and batch size to the CLI and environment", async () => {
  const spawnCalls = [];
  const child = makeChild(555);
  const client = makeClient((command, args, options) => {
    spawnCalls.push({ command, args, options });
    return child;
  }, { suggestionParallelism: 4, suggestionBatchSize: 2, commitIntentInference: true });

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
    "--batch-size",
    "2",
    "--strategy",
    "auto",
    "--prompt-profile",
    "conservative",
    "--tone",
    "release-note",
    "--all-visible",
  ]);
  assert.equal(spawnCalls[0].options.env.VERLATO_SUGGESTION_PARALLELISM, "4");
  assert.equal(spawnCalls[0].options.env.VERLATO_SUGGESTION_BATCH_SIZE, "2");
  assert.equal(spawnCalls[0].options.env.VERLATO_SUGGESTION_STRATEGY, "auto");
  assert.equal(spawnCalls[0].options.env.VERLATO_COMMIT_INTENT_INFERENCE, "low-signal");
  assert.equal(spawnCalls[0].options.env.VERLATO_SUGGESTION_PROMPT_PROFILE, "conservative");
  assert.equal(spawnCalls[0].options.env.VERLATO_SUGGESTION_ADAPTIVE_RECOMMENDATIONS, "0");
  assert.equal(spawnCalls[0].options.env.VERLATO_OLLAMA_SUGGEST_MODEL, "");
  assert.equal(spawnCalls[0].options.env.VERLATO_MODEL_PRIVACY, "metadata-only");
  assert.equal(spawnCalls[0].options.env.VERLATO_ALLOW_REMOTE_PATCHES, "0");
});

test("preview suggestions passes explicit privacy mode and remote patch opt-in", async () => {
  const spawnCalls = [];
  const child = makeChild(555);
  const client = makeClient((command, args, options) => {
    spawnCalls.push({ command, args, options });
    return child;
  }, {
    modelPrivacy: "trusted-provider-patch",
    allowRemotePatches: true,
  });

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

  assert.ok(spawnCalls[0].args.includes("--privacy"));
  assert.equal(spawnCalls[0].args[spawnCalls[0].args.indexOf("--privacy") + 1], "trusted-provider-patch");
  assert.ok(spawnCalls[0].args.includes("--allow-remote-patches"));
  assert.equal(spawnCalls[0].options.env.VERLATO_MODEL_PRIVACY, "trusted-provider-patch");
  assert.equal(spawnCalls[0].options.env.VERLATO_ALLOW_REMOTE_PATCHES, "1");
});

test("preview suggestions can refresh the persistent suggestion cache", async () => {
  const spawnCalls = [];
  const child = makeChild(556);
  const client = makeClient((command, args, options) => {
    spawnCalls.push({ command, args, options });
    return child;
  });

  const pending = client.previewDraftSuggestions({ base: "v1.0.0", head: "HEAD" }, undefined, undefined, "refresh-cache");
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

  assert.ok(spawnCalls[0].args.includes("--refresh-cache"));
  assert.ok(!spawnCalls[0].args.includes("--no-cache"));
});

test("preview suggestions passes prompt profile and adaptive recommendation settings", async () => {
  const spawnCalls = [];
  const child = makeChild(562);
  const client = makeClient((command, args, options) => {
    spawnCalls.push({ command, args, options });
    return child;
  }, { suggestionPromptProfile: "lean", suggestionAdaptiveRecommendations: true });

  const pending = client.previewDraftSuggestions({ base: "v1.0.0", head: "HEAD" });
  await waitForSpawn(spawnCalls);
  child.stdout.emit("data", JSON.stringify({
    base_ref: "v1.0.0",
    head_ref: "HEAD",
    provider: "mock",
    count: 1,
    adaptive_recommendations: {
      schema_version: "verlato.suggestion_adaptive.v1",
      recommendations: [],
    },
    items: [],
  }));
  child.emit("close", 0);
  await pending;

  assert.ok(spawnCalls[0].args.includes("--prompt-profile"));
  assert.equal(spawnCalls[0].args[spawnCalls[0].args.indexOf("--prompt-profile") + 1], "lean");
  assert.ok(spawnCalls[0].args.includes("--adaptive-recommendations"));
  assert.equal(spawnCalls[0].options.env.VERLATO_SUGGESTION_PROMPT_PROFILE, "lean");
  assert.equal(spawnCalls[0].options.env.VERLATO_SUGGESTION_ADAPTIVE_RECOMMENDATIONS, "1");
});

test("preview suggestions passes optional Ollama suggestion model", async () => {
  const spawnCalls = [];
  const child = makeChild(563);
  const client = makeClient((command, args, options) => {
    spawnCalls.push({ command, args, options });
    return child;
  }, { modelProvider: "ollama", ollamaSuggestionModel: "gemma3:1b" });

  const pending = client.previewDraftSuggestions({ base: "v1.0.0", head: "HEAD" });
  await waitForSpawn(spawnCalls);
  child.stdout.emit("data", JSON.stringify({
    base_ref: "v1.0.0",
    head_ref: "HEAD",
    provider: "ollama",
    model: "gemma4:latest",
    suggest_model: "gemma3:1b",
    effective_suggest_model: "gemma3:1b",
    count: 1,
    items: [],
  }));
  child.emit("close", 0);
  const result = await pending;

  assert.ok(spawnCalls[0].args.includes("--suggest-model"));
  assert.equal(spawnCalls[0].args[spawnCalls[0].args.indexOf("--suggest-model") + 1], "gemma3:1b");
  assert.equal(spawnCalls[0].options.env.VERLATO_OLLAMA_SUGGEST_MODEL, "gemma3:1b");
  assert.equal(result.effective_suggest_model, "gemma3:1b");
});

test("warm suggestion cache invokes all-visible preview without changing visible state", async () => {
  const spawnCalls = [];
  const child = makeChild(561);
  const client = makeClient((command, args, options) => {
    spawnCalls.push({ command, args, options });
    return child;
  }, { suggestionParallelism: 3, suggestionBatchSize: 2, suggestionStrategy: "auto" });

  const pending = client.warmDraftSuggestionCache({ base: "v1.0.0", head: "HEAD" });
  await waitForSpawn(spawnCalls);
  child.stdout.emit("data", JSON.stringify({
    base_ref: "v1.0.0",
    head_ref: "HEAD",
    provider: "ollama",
    count: 1,
    items: [],
  }));
  child.emit("close", 0);
  const result = await pending;

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
    "3",
    "--batch-size",
    "2",
    "--strategy",
    "auto",
    "--prompt-profile",
    "conservative",
    "--tone",
    "release-note",
    "--all-visible",
  ]);
  assert.ok(!spawnCalls[0].args.includes("--refresh-cache"));
  assert.ok(!spawnCalls[0].args.includes("--no-cache"));
  assert.equal(result.provider, "ollama");
});

test("clear suggestion cache invokes the state command", async () => {
  const spawnCalls = [];
  const child = makeChild(557);
  const client = makeClient((command, args, options) => {
    spawnCalls.push({ command, args, options });
    return child;
  }, { serviceMode: "cli" });

  const pending = client.clearSuggestionCache();
  await waitForSpawn(spawnCalls);
  child.stdout.emit("data", JSON.stringify({ removed_entries: 2, path: ".verlato_suggestion_cache.json" }));
  child.emit("close", 0);
  const result = await pending;

  assert.deepEqual(spawnCalls[0].args, ["state", "clear-suggestion-cache"]);
  assert.equal(result.removed_entries, 2);
});

test("configured Windows command shim is launched through cmd.exe for CLI calls", { skip: process.platform !== "win32" }, async () => {
  const originalComSpec = process.env.ComSpec;
  process.env.ComSpec = "C:\\Windows\\System32\\cmd.exe";
  const spawnCalls = [];
  const child = makeChild(570);
  const client = makeClient((command, args, options) => {
    spawnCalls.push({ command, args, options });
    return child;
  }, { serviceMode: "cli", cliPath: "C:\\tools\\verlato.cmd" });

  try {
    const pending = client.clearSuggestionCache();
    await waitForSpawn(spawnCalls);
    child.stdout.emit("data", JSON.stringify({ removed_entries: 2, path: ".verlato_suggestion_cache.json" }));
    child.emit("close", 0);
    await pending;
  } finally {
    if (originalComSpec === undefined) {
      delete process.env.ComSpec;
    } else {
      process.env.ComSpec = originalComSpec;
    }
  }

  assert.equal(spawnCalls[0].command, "C:\\Windows\\System32\\cmd.exe");
  assert.deepEqual(spawnCalls[0].args, [
    "/d",
    "/s",
    "/c",
    "C:\\tools\\verlato.cmd",
    "state",
    "clear-suggestion-cache",
  ]);
});

test("service mode auto uses stdio service for core flows", async () => {
  const spawnCalls = [];
  const writes = [];
  let serviceChild;
  serviceChild = makeChild(600, (data) => {
    writes.push(data);
    const request = JSON.parse(data);
    const result = request.method === "health"
      ? { status: "ok", version: "0.1.0" }
      : { removed_entries: 3, path: ".verlato_suggestion_cache.json" };
    serviceChild.stdout.emit("data", `${JSON.stringify({ id: request.id, ok: true, result })}\n`);
  });
  const client = makeClient((command, args, options) => {
    spawnCalls.push({ command, args, options });
    return serviceChild;
  }, { serviceMode: "auto" });

  const result = await client.clearSuggestionCache();

  assert.deepEqual(spawnCalls.map((call) => call.args), [["service"]]);
  assert.equal(JSON.parse(writes[0]).method, "health");
  assert.equal(JSON.parse(writes[1]).method, "state.clear_suggestion_cache");
  assert.equal(result.removed_entries, 3);
  assert.equal(client.getCommandRequests()[0].command, "verlato-service");
  assert.equal(client.getCommandRequests()[0].status, "completed");
  assert.equal(client.lastStatus.label, "Verlato: Service");
});

test("configured Windows command shim is launched through cmd.exe for service mode", { skip: process.platform !== "win32" }, async () => {
  const originalComSpec = process.env.ComSpec;
  process.env.ComSpec = "C:\\Windows\\System32\\cmd.exe";
  const spawnCalls = [];
  const writes = [];
  let serviceChild;
  serviceChild = makeChild(607, (data) => {
    writes.push(data);
    const request = JSON.parse(data);
    const result = request.method === "health"
      ? { status: "ok", version: "0.1.0" }
      : { removed_entries: 3, path: ".verlato_suggestion_cache.json" };
    serviceChild.stdout.emit("data", `${JSON.stringify({ id: request.id, ok: true, result })}\n`);
  });
  const client = makeClient((command, args, options) => {
    spawnCalls.push({ command, args, options });
    return serviceChild;
  }, { serviceMode: "auto", cliPath: "C:\\tools\\verlato.cmd" });

  try {
    const result = await client.clearSuggestionCache();
    assert.equal(result.removed_entries, 3);
  } finally {
    if (originalComSpec === undefined) {
      delete process.env.ComSpec;
    } else {
      process.env.ComSpec = originalComSpec;
    }
  }

  assert.equal(spawnCalls[0].command, "C:\\Windows\\System32\\cmd.exe");
  assert.deepEqual(spawnCalls[0].args.slice(0, 5), ["/d", "/s", "/c", "C:\\tools\\verlato.cmd", "service"]);
  assert.equal(JSON.parse(writes[0]).method, "health");
  assert.equal(JSON.parse(writes[1]).method, "state.clear_suggestion_cache");
});

test("service start failure falls back to CLI", async () => {
  const spawnCalls = [];
  const serviceChild = makeChild(601);
  const cliChild = makeChild(602);
  const client = makeClient((command, args, options) => {
    spawnCalls.push({ command, args, options });
    if (args[0] === "service") {
      setImmediate(() => serviceChild.emit("error", new Error("service unavailable")));
      return serviceChild;
    }
    return cliChild;
  }, { serviceMode: "auto" });

  const pending = client.clearSuggestionCache();
  await waitForSpawnCount(spawnCalls, 2);
  cliChild.stdout.emit("data", JSON.stringify({ removed_entries: 1, path: ".verlato_suggestion_cache.json" }));
  cliChild.emit("close", 0);
  const result = await pending;

  assert.deepEqual(spawnCalls.map((call) => call.args), [["service"], ["state", "clear-suggestion-cache"]]);
  assert.equal(result.removed_entries, 1);
  assert.equal(client.lastStatus.label, "Verlato: CLI fallback");
});

test("service request failure retries through CLI once", async () => {
  const spawnCalls = [];
  const writes = [];
  let serviceChild;
  const cliChild = makeChild(604);
  serviceChild = makeChild(603, (data) => {
    writes.push(data);
    const request = JSON.parse(data);
    const response = request.method === "health"
      ? { id: request.id, ok: true, result: { status: "ok" } }
      : { id: request.id, ok: false, error: { code: "handler_error", message: "service handler failed" } };
    serviceChild.stdout.emit("data", `${JSON.stringify(response)}\n`);
  });
  const client = makeClient((command, args, options) => {
    spawnCalls.push({ command, args, options });
    return args[0] === "service" ? serviceChild : cliChild;
  }, { serviceMode: "auto" });

  const pending = client.clearWorkingState();
  await waitForSpawnCount(spawnCalls, 2);
  cliChild.stdout.emit("data", JSON.stringify({
    removed_working_items: 1,
    removed_feedback_events: 0,
    preserved_published_items: 2,
  }));
  cliChild.emit("close", 0);
  const result = await pending;

  assert.equal(JSON.parse(writes[1]).method, "state.clear_working");
  assert.deepEqual(spawnCalls.map((call) => call.args), [["service"], ["state", "clear-working"]]);
  assert.equal(result.preserved_published_items, 2);
  assert.equal(serviceChild.killed, true);
  assert.equal(client.lastStatus.label, "Verlato: CLI fallback");
});

test("cancelling a service-backed request stops service and marks request cancelled", async () => {
  const spawnCalls = [];
  const writes = [];
  let serviceChild;
  serviceChild = makeChild(605, (data) => {
    writes.push(data);
    const request = JSON.parse(data);
    if (request.method === "health") {
      serviceChild.stdout.emit("data", `${JSON.stringify({ id: request.id, ok: true, result: { status: "ok" } })}\n`);
    }
  });
  const client = makeClient((command, args, options) => {
    spawnCalls.push({ command, args, options });
    return serviceChild;
  }, { serviceMode: "auto" });

  const pending = client.getDraft({ base: "v1.0.0", head: "HEAD" });
  await waitForWriteCount(writes, 2);
  client.cancelActiveCommands();
  await assert.rejects(pending, /cancelled/);

  const request = client.getCommandRequests()[0];
  assert.equal(serviceChild.killed, true);
  assert.equal(request.command, "verlato-service");
  assert.equal(request.status, "cancelled");
});

test("diagnose Ollama invokes doctor command with parallel probe when configured", async () => {
  const spawnCalls = [];
  const child = makeChild(559);
  const client = makeClient((command, args, options) => {
    spawnCalls.push({ command, args, options });
    return child;
  }, { suggestionParallelism: 3, ollamaSuggestionModel: "gemma3:1b" });

  const pending = client.diagnoseOllama();
  await waitForSpawn(spawnCalls);
  child.stdout.emit("data", JSON.stringify({
    schema: "ollama_doctor.v1",
    provider: "ollama",
    config: { model: "gemma4:latest" },
    probes: [],
    summary: {
      status: "ok",
      bottleneck: "healthy",
      diagnostics: [],
      recommendations: [],
    },
  }));
  child.emit("close", 0);
  const result = await pending;

  assert.deepEqual(spawnCalls[0].args, ["doctor", "ollama", "--parallel-probe", "--suggest-model", "gemma3:1b"]);
  assert.equal(spawnCalls[0].options.env.VERLATO_MODEL_PROVIDER, "ollama");
  assert.equal(spawnCalls[0].options.env.VERLATO_OLLAMA_SUGGEST_MODEL, "gemma3:1b");
  assert.equal(result.schema, "ollama_doctor.v1");
});

test("warmup Ollama invokes doctor warmup command", async () => {
  const spawnCalls = [];
  const child = makeChild(560);
  const client = makeClient((command, args, options) => {
    spawnCalls.push({ command, args, options });
    return child;
  });

  const pending = client.warmupOllama();
  await waitForSpawn(spawnCalls);
  child.stdout.emit("data", JSON.stringify({
    schema: "ollama_doctor.v1",
    provider: "ollama",
    config: { model: "gemma4:latest" },
    probes: [{ name: "warmup", purpose: "warmup", ok: true, elapsed_ms: 120, prompt_chars: 90 }],
    summary: {
      status: "ok",
      bottleneck: "healthy",
      warmed_up: true,
      diagnostics: ["Warmup completed in 120ms."],
      recommendations: [],
    },
  }));
  child.emit("close", 0);
  const result = await pending;

  assert.deepEqual(spawnCalls[0].args, ["doctor", "ollama", "--warmup"]);
  assert.equal(spawnCalls[0].options.env.VERLATO_MODEL_PROVIDER, "ollama");
  assert.equal(result.summary.warmed_up, true);
});

test("preview suggestions streams partial items before final stdout resolves", async () => {
  const spawnCalls = [];
  const child = makeChild(558);
  const client = makeClient((command, args, options) => {
    spawnCalls.push({ command, args, options });
    return child;
  });
  const firstItem = {
    item_id: "ci_a",
    title: "First change",
    current_summary: "Old first",
    provider: "mock-local",
    prompt_budget: { profile: "suggestion-conservative", prompt_chars: 1100 },
    suggestions: ["First streamed suggestion"],
  };
  const secondItem = {
    item_id: "ci_b",
    title: "Second change",
    current_summary: "Old second",
    provider: "mock-local",
    prompt_budget: { profile: "suggestion-conservative", prompt_chars: 900 },
    suggestions: ["Second streamed suggestion"],
  };
  const provisionalSecondItem = {
    ...secondItem,
    provider: "mock-local",
    provisional: { source: "model", state: "provisional" },
    suggestions: ["Prepared second suggestion"],
  };
  const partials = [];

  const pending = client.previewDraftSuggestions({ base: "v1.0.0", head: "HEAD" }, undefined, (preview) => {
    partials.push(preview);
  });
  await waitForSpawn(spawnCalls);

  child.stderr.emit("data", `[verlato:suggestion] json ${JSON.stringify({
    event: "suggestion_preview_started",
    total: 2,
    preview: {
      base_ref: "v1.0.0",
      head_ref: "HEAD",
      provider: "mock",
      count: 1,
      performance: { parallelism: 2 },
      items: [],
    },
  })}\n`);
  child.stderr.emit("data", `[verlato:suggestion] json ${JSON.stringify({
    event: "suggestion_item_provisional",
    order: 1,
    item_id: "ci_b",
    item: provisionalSecondItem,
  })}\n`);
  child.stderr.emit("data", `[verlato:suggestion] json ${JSON.stringify({
    event: "suggestion_item_result",
    order: 1,
    item_id: "ci_b",
    item: secondItem,
  })}\n`);
  child.stderr.emit("data", `[verlato:suggestion] json ${JSON.stringify({
    event: "suggestion_item_result",
    order: 0,
    item_id: "ci_a",
    item: firstItem,
  })}\n`);
  child.stdout.emit("data", JSON.stringify({
    base_ref: "v1.0.0",
    head_ref: "HEAD",
    provider: "mock",
    count: 1,
    items: [firstItem, secondItem],
  }));
  child.emit("close", 0);
  const final = await pending;

  assert.deepEqual(partials.map((preview) => preview.items.map((item) => item.item_id)), [
    [],
    ["ci_b"],
    ["ci_b"],
    ["ci_a", "ci_b"],
  ]);
  assert.equal(partials[1].items[0].provider, "mock-local");
  assert.equal(partials[2].items[0].provider, "mock-local");
  assert.deepEqual(final.items.map((item) => item.item_id), ["ci_a", "ci_b"]);
  assert.equal(partials[3].items[0].prompt_budget.prompt_chars, 1100);
  assert.equal(client.getCommandRequests()[0].activity.some((entry) => entry.category === "suggestion"), true);
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
