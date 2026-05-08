const assert = require("node:assert/strict");
const Module = require("node:module");
const test = require("node:test");

const modelSettings = {
  provider: "mock",
  model: "mock-model",
  suggestionModel: "",
  endpoint: "http://127.0.0.1:11434/api/chat",
  ollamaTimeoutSeconds: 120,
  maxModelItems: 25,
  suggestionCount: 1,
  suggestionParallelism: 2,
  suggestionBatchSize: 2,
  suggestionStrategy: "auto",
  suggestionAutoWarmCache: false,
  suggestionPromptProfile: "conservative",
  suggestionAdaptiveRecommendations: false,
  suggestionTone: "release-note",
  styleGuidance: "",
  projectVocabulary: [],
  bannedPhrases: [],
  specificityFloor: 0,
  feedbackIncludeText: false,
  includePromptPayloads: false,
  includeReasoningPayloads: false,
  redactPayloads: true,
  debugModelLogPath: "",
  ollamaStreamResponses: true,
};

function loadControlViewWithVscodeMock(vscodeMock) {
  const originalLoad = Module._load;
  for (const modulePath of [
    "../dist/webview/releaseControlView",
    "../dist/notifications",
  ]) {
    delete require.cache[require.resolve(modulePath)];
  }
  Module._load = function patchedLoad(request, parent, isMain) {
    if (request === "vscode") {
      return vscodeMock;
    }
    return originalLoad.call(this, request, parent, isMain);
  };
  try {
    return require("../dist/webview/releaseControlView");
  } finally {
    Module._load = originalLoad;
  }
}

function makeVscodeMock(overrides = {}) {
  return {
    workspace: {
      getConfiguration() {
        return {
          get(_key, fallback) {
            return fallback;
          },
        };
      },
      openTextDocument() {
        return Promise.resolve({});
      },
    },
    window: {
      showInformationMessage() {
        return Promise.resolve(undefined);
      },
      showErrorMessage() {
        return Promise.resolve(undefined);
      },
      showWarningMessage() {
        return Promise.resolve(undefined);
      },
      showTextDocument() {
        return Promise.resolve(undefined);
      },
    },
    commands: {
      executeCommand() {
        return Promise.resolve(undefined);
      },
    },
    env: {
      clipboard: {
        writeText() {
          return Promise.resolve(undefined);
        },
      },
    },
    Uri: {
      joinPath() {
        return {};
      },
      file(path) {
        return { fsPath: path };
      },
    },
    ViewColumn: { Beside: 2 },
    ...overrides,
  };
}

function makeClient(overrides = {}) {
  return {
    getModelSettings() {
      return modelSettings;
    },
    getCommandRequests() {
      return [];
    },
    onDidChangeRequests() {
      return { dispose() {} };
    },
    getDraft() {
      return Promise.resolve({
        version: "Unreleased",
        generated_on: "2026-05-03",
        base_ref: "v1.0.0",
        head_ref: "HEAD",
        targets: [],
        sections: [],
        rendered: {},
        item_count: 1,
      });
    },
    getRepositoryOverview() {
      return Promise.resolve({
        branches: [],
        tags: [],
        commits: [],
        releases: [],
        unpublishedCount: 0,
      });
    },
    previewDraftSuggestions() {
      return Promise.resolve({
        base_ref: "v1.0.0",
        head_ref: "HEAD",
        provider: "mock",
        count: 1,
        items: [],
      });
    },
    warmDraftSuggestionCache() {
      return Promise.resolve({
        base_ref: "v1.0.0",
        head_ref: "HEAD",
        provider: "mock",
        count: 1,
        items: [],
      });
    },
    recordFeedback() {
      return Promise.resolve(undefined);
    },
    clearSuggestionCache() {
      return Promise.resolve({ removed_entries: 0, path: ".verlato_suggestion_cache.json" });
    },
    diagnoseOllama() {
      return Promise.resolve({
        schema: "ollama_doctor.v1",
        provider: "ollama",
        config: { model: "mock-model" },
        probes: [],
        summary: {
          status: "ok",
          bottleneck: "healthy",
          diagnostics: ["Ollama probe completed in 100ms with no obvious bottleneck."],
          recommendations: ["No obvious Ollama bottleneck was detected by the probe."],
        },
      });
    },
    warmupOllama() {
      return Promise.resolve({
        schema: "ollama_doctor.v1",
        provider: "ollama",
        config: { model: "mock-model" },
        probes: [],
        summary: {
          status: "ok",
          bottleneck: "healthy",
          warmed_up: true,
          diagnostics: ["Warmup completed in 100ms."],
          recommendations: ["No obvious Ollama bottleneck was detected by the probe."],
        },
      });
    },
    ...overrides,
  };
}

function suggestionPreview(text, itemId = "ci_1") {
  return {
    base_ref: "v1.0.0",
    head_ref: "HEAD",
    provider: "mock",
    count: 1,
    items: [
      {
        item_id: itemId,
        title: "Improve API",
        current_summary: "Old API summary",
        provider: "mock-local",
        prompt_budget: {
          profile: "suggestion-conservative",
          prompt_chars: 1200,
          files_included: 4,
          files_total: 6,
          hunks_included: 1,
          hunks_total: 2,
        },
        suggestion_details: [
          {
            summary: text,
            similarity_to_current: 0.25,
            specificity_score: 0.7,
            comparison: {
              current: "Old API summary",
              suggested: text,
            },
          },
        ],
        suggestions: [text],
      },
    ],
  };
}

function draftDocument(itemCount = 1) {
  return {
    version: "Unreleased",
    generated_on: "2026-05-03",
    base_ref: "v1.0.0",
    head_ref: "HEAD",
    targets: [],
    sections: [],
    rendered: {},
    item_count: itemCount,
  };
}

function makeProvider(ReleaseControlViewProvider, client) {
  const htmlSnapshots = [];
  const webview = {
    options: {},
    onDidReceiveMessage() {
      return { dispose() {} };
    },
    asWebviewUri() {
      return { toString: () => "vscode-resource:/asset.js" };
    },
  };
  Object.defineProperty(webview, "html", {
    get() {
      return htmlSnapshots.at(-1) ?? "";
    },
    set(value) {
      htmlSnapshots.push(value);
    },
  });
  const provider = new ReleaseControlViewProvider({}, client, { show() {} });
  provider.view = { webview };
  return { provider, htmlSnapshots };
}

async function waitFor(predicate) {
  for (let index = 0; index < 20; index += 1) {
    if (predicate()) {
      return;
    }
    await sleep(10);
  }
  assert.fail("Timed out waiting for async view update.");
}

function sleep(ms) {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

test("success notifications do not keep the control view busy", async () => {
  let resolveNotification;
  let notificationCalls = 0;
  const notificationPromise = new Promise((resolve) => {
    resolveNotification = resolve;
  });
  const vscodeMock = makeVscodeMock({
    window: {
      ...makeVscodeMock().window,
      showInformationMessage() {
        notificationCalls += 1;
        return notificationPromise;
      },
    },
  });
  const { ReleaseControlViewProvider } = loadControlViewWithVscodeMock(vscodeMock);
  const { provider, htmlSnapshots } = makeProvider(ReleaseControlViewProvider, makeClient());

  const scanPromise = provider.handleMessage({ type: "scan" });
  await waitFor(() => notificationCalls === 1);

  try {
    const html = htmlSnapshots.at(-1);
    assert.match(html, /Loaded 1 draft item\(s\) from v1\.0\.0\.\.HEAD/);
    assert.match(html, />Generate release notes<\/button>/);
    assert.doesNotMatch(html, /Running Verlato/);
    assert.doesNotMatch(html, /class="stop" data-action="cancel"/);
  } finally {
    resolveNotification(undefined);
    await scanPromise;
  }
});

test("applying a model suggestion records model provenance", async () => {
  let reviewUpdate;
  const client = makeClient({
    reviewDraftItem(_range, _itemId, update) {
      reviewUpdate = update;
      return Promise.resolve({});
    },
  });
  const { ReleaseControlViewProvider } = loadControlViewWithVscodeMock(makeVscodeMock());
  const { provider } = makeProvider(ReleaseControlViewProvider, client);
  provider.suggestions = suggestionPreview("Model API wording");

  await provider.handleMessage({ type: "applySuggestion", itemId: "ci_1", suggestionIndex: 0 });

  assert.equal(reviewUpdate.summary, "Model API wording");
  assert.equal(reviewUpdate.summaryProvenance.source, "model-suggestion");
  assert.equal(reviewUpdate.summaryProvenance.model, "mock-local");
  assert.equal(reviewUpdate.summaryProvenance.prompt_profile, "suggestion-conservative");
  assert.equal(reviewUpdate.summaryProvenance.similarity_to_current, 0.25);
  assert.equal(reviewUpdate.summaryProvenance.specificity_score, 0.7);
  assert.equal(reviewUpdate.summaryProvenance.source_evidence_count, 0);
});

test("suggestion feedback includes model source metadata", async () => {
  const feedbackCalls = [];
  const client = makeClient({
    recordFeedback(event, itemId, metadata) {
      feedbackCalls.push({ event, itemId, metadata });
      return Promise.resolve(undefined);
    },
  });
  const { ReleaseControlViewProvider } = loadControlViewWithVscodeMock(makeVscodeMock());
  const { provider } = makeProvider(ReleaseControlViewProvider, client);
  provider.suggestions = suggestionPreview("Model API wording");

  await provider.handleMessage({ type: "feedbackPositive", itemId: "ci_1" });
  await provider.handleMessage({ type: "skipSuggestion", itemId: "ci_1" });

  assert.equal(feedbackCalls[0].event, "suggestion_marked_useful");
  assert.equal(feedbackCalls[0].metadata.source, "model-suggestion");
  assert.equal(feedbackCalls[0].metadata.specificity_score, 0.7);
  assert.equal(feedbackCalls[1].event, "suggestion_skipped");
  assert.equal(feedbackCalls[1].metadata.source, "model-suggestion");
});

test("error notifications do not keep the control view busy", async () => {
  let resolveNotification;
  let notificationCalls = 0;
  const notificationPromise = new Promise((resolve) => {
    resolveNotification = resolve;
  });
  const vscodeMock = makeVscodeMock({
    window: {
      ...makeVscodeMock().window,
      showErrorMessage() {
        notificationCalls += 1;
        return notificationPromise;
      },
    },
  });
  const { ReleaseControlViewProvider } = loadControlViewWithVscodeMock(vscodeMock);
  const client = makeClient({
    previewDraftSuggestions() {
      return Promise.reject(new Error("model down"));
    },
  });
  const { provider, htmlSnapshots } = makeProvider(ReleaseControlViewProvider, client);

  const suggestPromise = provider.handleMessage({ type: "suggestAll" });
  await waitFor(() => notificationCalls === 1);

  try {
    const html = htmlSnapshots.at(-1);
    assert.match(html, /model down/);
    assert.match(html, />Generate release notes<\/button>/);
    assert.doesNotMatch(html, /Running Verlato/);
    assert.doesNotMatch(html, /class="stop" data-action="cancel"/);
  } finally {
    resolveNotification(undefined);
    await suggestPromise;
  }
});

test("draft load schedules quiet Ollama suggestion cache warming", async () => {
  const { ReleaseControlViewProvider } = loadControlViewWithVscodeMock(makeVscodeMock());
  const warmRequests = [];
  const client = makeClient({
    getModelSettings() {
      return { ...modelSettings, provider: "ollama", model: "gemma4:latest", suggestionStrategy: "auto", suggestionAutoWarmCache: true };
    },
    getDraft() {
      return Promise.resolve(draftDocument(2));
    },
    warmDraftSuggestionCache(request) {
      warmRequests.push(request);
      return Promise.resolve({
        base_ref: request.base,
        head_ref: request.head,
        provider: "ollama",
        count: 1,
        items: [{ item_id: "ci_1", title: "One", current_summary: "", provider: "ollama", suggestions: ["Warmed suggestion"] }],
      });
    },
  });
  const { provider, htmlSnapshots } = makeProvider(ReleaseControlViewProvider, client);

  await provider.handleMessage({ type: "scan" });
  await waitFor(() => warmRequests.length === 1);

  assert.deepEqual(warmRequests[0], { base: "v1.0.0", head: "HEAD", version: "Unreleased" });
  assert.match(htmlSnapshots.at(-1), /Suggestion cache warmed for 1 item\(s\)/);
  assert.doesNotMatch(htmlSnapshots.at(-1), /Warmed suggestion/);
});

test("draft load does not warm suggestion cache for non-Ollama providers", async () => {
  const { ReleaseControlViewProvider } = loadControlViewWithVscodeMock(makeVscodeMock());
  let warmCalls = 0;
  const client = makeClient({
    warmDraftSuggestionCache() {
      warmCalls += 1;
      return Promise.resolve(suggestionPreview("Warmed suggestion"));
    },
  });
  const { provider } = makeProvider(ReleaseControlViewProvider, client);

  await provider.handleMessage({ type: "scan" });
  await sleep(150);

  assert.equal(warmCalls, 0);
});

test("draft load does not warm suggestion cache when auto warm is disabled", async () => {
  const { ReleaseControlViewProvider } = loadControlViewWithVscodeMock(makeVscodeMock());
  let warmCalls = 0;
  const client = makeClient({
    getModelSettings() {
      return { ...modelSettings, provider: "ollama", suggestionAutoWarmCache: false };
    },
    warmDraftSuggestionCache() {
      warmCalls += 1;
      return Promise.resolve(suggestionPreview("Warmed suggestion"));
    },
  });
  const { provider } = makeProvider(ReleaseControlViewProvider, client);

  await provider.handleMessage({ type: "scan" });
  await sleep(150);

  assert.equal(warmCalls, 0);
});

test("foreground suggest does not wait for a matching active cache warm", async () => {
  let resolveWarm;
  let previewCalls = 0;
  const { ReleaseControlViewProvider } = loadControlViewWithVscodeMock(makeVscodeMock());
  const client = makeClient({
    getModelSettings() {
      return { ...modelSettings, provider: "ollama", model: "gemma4:latest", suggestionStrategy: "auto", suggestionAutoWarmCache: true };
    },
    warmDraftSuggestionCache() {
      return new Promise((resolve) => {
        resolveWarm = resolve;
      });
    },
    previewDraftSuggestions() {
      previewCalls += 1;
      return Promise.resolve(suggestionPreview("Foreground suggestion"));
    },
  });
  const { provider, htmlSnapshots } = makeProvider(ReleaseControlViewProvider, client);

  await provider.handleMessage({ type: "scan" });
  await waitFor(() => Boolean(resolveWarm));

  const suggestPromise = provider.handleMessage({ type: "suggestAll" });
  const foregroundResult = await Promise.race([
    suggestPromise.then(() => "foreground"),
    sleep(25).then(() => "waiting"),
  ]);
  assert.equal(foregroundResult, "foreground");
  assert.equal(previewCalls, 1);
  assert.match(htmlSnapshots.at(-1), /Foreground suggestion/);

  resolveWarm(suggestionPreview("Warm-only suggestion"));

  assert.match(htmlSnapshots.at(-1), /Foreground suggestion/);
  assert.doesNotMatch(htmlSnapshots.at(-1), /Warm-only suggestion/);
});

test("failed background cache warm does not clear visible suggestions or show an error banner", async () => {
  const { ReleaseControlViewProvider } = loadControlViewWithVscodeMock(makeVscodeMock());
  const client = makeClient({
    getModelSettings() {
      return { ...modelSettings, provider: "ollama", model: "gemma4:latest", suggestionStrategy: "auto", suggestionAutoWarmCache: true };
    },
    warmDraftSuggestionCache() {
      return Promise.reject(new Error("ollama busy"));
    },
  });
  const { provider, htmlSnapshots } = makeProvider(ReleaseControlViewProvider, client);
  provider.suggestions = suggestionPreview("Existing visible suggestion");
  provider.refresh(draftDocument(1), undefined, client.getModelSettings());

  await waitFor(() => /did not complete/.test(htmlSnapshots.at(-1) ?? ""));

  assert.match(htmlSnapshots.at(-1), /Existing visible suggestion/);
  assert.doesNotMatch(htmlSnapshots.at(-1), /ollama busy/);
});

test("control view renders partial suggestions while the request is running", async () => {
  let resolveSuggest;
  const { ReleaseControlViewProvider } = loadControlViewWithVscodeMock(makeVscodeMock());
  const client = makeClient({
    previewDraftSuggestions(_request, _itemId, onPartial) {
      onPartial(suggestionPreview("First streamed suggestion"));
      return new Promise((resolve) => {
        resolveSuggest = resolve;
      });
    },
  });
  const { provider, htmlSnapshots } = makeProvider(ReleaseControlViewProvider, client);

  const suggestPromise = provider.handleMessage({ type: "suggestAll" });
  await waitFor(() => /First streamed suggestion/.test(htmlSnapshots.at(-1) ?? ""));

  resolveSuggest(suggestionPreview("Final suggestion"));
  await suggestPromise;

  assert.match(htmlSnapshots.at(-1), /Final suggestion/);
});

test("control view refreshes cache for regenerate requests", async () => {
  let receivedMode;
  const { ReleaseControlViewProvider } = loadControlViewWithVscodeMock(makeVscodeMock());
  const client = makeClient({
    previewDraftSuggestions(_request, _itemId, _onPartial, cacheMode) {
      receivedMode = cacheMode;
      return Promise.resolve(suggestionPreview("Refreshed suggestion"));
    },
  });
  const { provider } = makeProvider(ReleaseControlViewProvider, client);

  await provider.handleMessage({ type: "regenerateAll" });

  assert.equal(receivedMode, "refresh-cache");
});

test("control view clears the persistent suggestion cache", async () => {
  const vscodeMock = makeVscodeMock({
    window: {
      ...makeVscodeMock().window,
      showWarningMessage() {
        return Promise.resolve("Clear Suggestion Cache");
      },
    },
  });
  let clearCalls = 0;
  const { ReleaseControlViewProvider } = loadControlViewWithVscodeMock(vscodeMock);
  const client = makeClient({
    clearSuggestionCache() {
      clearCalls += 1;
      return Promise.resolve({ removed_entries: 3, path: ".verlato_suggestion_cache.json" });
    },
  });
  const { provider, htmlSnapshots } = makeProvider(ReleaseControlViewProvider, client);

  await provider.handleMessage({ type: "clearSuggestionCache" });

  assert.equal(clearCalls, 1);
  assert.match(htmlSnapshots.at(-1), /Cleared 3 cached suggestion entries/);
});

test("control view runs and renders Ollama doctor diagnostics", async () => {
  let doctorCalls = 0;
  const { ReleaseControlViewProvider } = loadControlViewWithVscodeMock(makeVscodeMock());
  const client = makeClient({
    diagnoseOllama() {
      doctorCalls += 1;
      return Promise.resolve({
        schema: "ollama_doctor.v1",
        provider: "ollama",
        config: { model: "gemma4:latest" },
        probes: [
          {
            name: "single",
            ok: true,
            elapsed_ms: 1500,
            prompt_chars: 120,
            timings: { model_ms: 1500, prompt_eval_ms: 900, eval_ms: 300 },
          },
        ],
        summary: {
          status: "warning",
          bottleneck: "prompt_eval",
          diagnostics: ["Ollama prompt evaluation reached 900ms."],
          recommendations: ["Reduce prompt budget further or inspect prompt budget telemetry for files/hunks that dominate."],
        },
      });
    },
  });
  const { provider, htmlSnapshots } = makeProvider(ReleaseControlViewProvider, client);

  await provider.handleMessage({ type: "diagnoseOllama" });

  assert.equal(doctorCalls, 1);
  const html = htmlSnapshots.at(-1);
  assert.match(html, /Ollama health/);
  assert.match(html, /warning \| prompt eval/);
  assert.match(html, /prompt 900ms/);
  assert.match(html, /Reduce prompt budget further/);
});

test("control view runs and renders Ollama warmup", async () => {
  let warmupCalls = 0;
  const { ReleaseControlViewProvider } = loadControlViewWithVscodeMock(makeVscodeMock());
  const client = makeClient({
    warmupOllama() {
      warmupCalls += 1;
      return Promise.resolve({
        schema: "ollama_doctor.v1",
        provider: "ollama",
        config: { model: "gemma4:latest" },
        probes: [
          {
            name: "warmup",
            purpose: "warmup",
            ok: true,
            elapsed_ms: 220,
            prompt_chars: 120,
            timings: { model_ms: 220, load_ms: 180 },
          },
          {
            name: "single",
            ok: true,
            elapsed_ms: 80,
            prompt_chars: 120,
            timings: { model_ms: 80, eval_ms: 30 },
          },
        ],
        summary: {
          status: "ok",
          bottleneck: "healthy",
          warmed_up: true,
          diagnostics: ["Ollama probe completed in 80ms with no obvious bottleneck.", "Warmup completed in 220ms and absorbed 180ms of model load time."],
          recommendations: ["No obvious Ollama bottleneck was detected by the probe."],
        },
      });
    },
  });
  const { provider, htmlSnapshots } = makeProvider(ReleaseControlViewProvider, client);

  await provider.handleMessage({ type: "warmupOllama" });

  assert.equal(warmupCalls, 1);
  const html = htmlSnapshots.at(-1);
  assert.match(html, /Ollama warmup completed/);
  assert.match(html, /ok \| healthy \| warmed/);
  assert.match(html, /absorbed 180ms/);
});

test("control view keeps partial suggestions visible when a later item fails", async () => {
  let resolveNotification;
  let notificationCalls = 0;
  const notificationPromise = new Promise((resolve) => {
    resolveNotification = resolve;
  });
  const vscodeMock = makeVscodeMock({
    window: {
      ...makeVscodeMock().window,
      showErrorMessage() {
        notificationCalls += 1;
        return notificationPromise;
      },
    },
  });
  const { ReleaseControlViewProvider } = loadControlViewWithVscodeMock(vscodeMock);
  const client = makeClient({
    previewDraftSuggestions(_request, _itemId, onPartial) {
      onPartial(suggestionPreview("Completed before failure"));
      return Promise.reject(new Error("No model suggestions were produced for draft item ci_2."));
    },
  });
  const { provider, htmlSnapshots } = makeProvider(ReleaseControlViewProvider, client);

  const suggestPromise = provider.handleMessage({ type: "suggestAll" });
  await waitFor(() => notificationCalls === 1);

  try {
    const html = htmlSnapshots.at(-1);
    assert.match(html, /Completed before failure/);
    assert.match(html, /No model suggestions were produced for draft item ci_2/);
  } finally {
    resolveNotification(undefined);
    await suggestPromise;
  }
});
