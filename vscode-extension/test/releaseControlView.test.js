const assert = require("node:assert/strict");
const Module = require("node:module");
const test = require("node:test");

const modelSettings = {
  provider: "mock",
  model: "mock-model",
  endpoint: "http://127.0.0.1:11434/api/chat",
  ollamaTimeoutSeconds: 120,
  maxModelItems: 25,
  suggestionCount: 1,
  suggestionParallelism: 2,
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
        suggestions: [text],
      },
    ],
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
    await new Promise((resolve) => setImmediate(resolve));
  }
  assert.fail("Timed out waiting for async view update.");
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
