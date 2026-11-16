// A minimal stand-in for the `vscode` module, so the extension's real client, tree providers
// and webview renderers can run under plain Node for screenshots. Only the API surface those
// modules touch is implemented. Settings come from the extension's package.json defaults,
// overridden by `setConfiguration()`.
"use strict";

const path = require("node:path");

const manifest = require(path.join(__dirname, "..", "..", "vscode-extension", "package.json"));
const defaults = {};
for (const section of [].concat(manifest.contributes.configuration)) {
  for (const [key, schema] of Object.entries(section.properties)) {
    defaults[key] = schema.default;
  }
}
const overrides = {};

class EventEmitter {
  constructor() {
    this.listeners = [];
    this.event = (listener) => {
      this.listeners.push(listener);
      return { dispose: () => (this.listeners = this.listeners.filter((l) => l !== listener)) };
    };
  }
  fire(value) {
    for (const listener of this.listeners) listener(value);
  }
  dispose() {
    this.listeners = [];
  }
}

class TreeItem {
  constructor(label, collapsibleState) {
    this.label = label;
    this.collapsibleState = collapsibleState;
  }
}

class ThemeIcon {
  constructor(id) {
    this.id = id;
  }
}

const panels = [];

module.exports = {
  EventEmitter,
  TreeItem,
  ThemeIcon,
  TreeItemCollapsibleState: { None: 0, Collapsed: 1, Expanded: 2 },
  ConfigurationTarget: { Global: 1, Workspace: 2, WorkspaceFolder: 3 },
  ViewColumn: { Active: -1, Beside: -2, One: 1 },
  Disposable: class {
    constructor(fn) {
      this.dispose = fn || (() => {});
    }
  },
  Uri: {
    file: (fsPath) => ({ fsPath, toString: () => `file://${fsPath}` }),
    joinPath: (base, ...parts) => ({ fsPath: path.join(base.fsPath, ...parts), toString: () => `file://${path.join(base.fsPath, ...parts)}` }),
  },
  workspace: {
    getConfiguration: (section) => ({
      get: (key, fallback) => {
        const full = section ? `${section}.${key}` : key;
        if (full in overrides) return overrides[full];
        if (full in defaults) return defaults[full];
        return fallback;
      },
      update: async (key, value) => {
        overrides[section ? `${section}.${key}` : key] = value;
      },
    }),
    onDidChangeConfiguration: () => ({ dispose() {} }),
    workspaceFolders: [],
  },
  window: {
    createWebviewPanel: (viewType, title) => {
      const panel = {
        viewType,
        title,
        webview: { html: "", options: {} },
        reveal() {},
        onDidDispose() {},
      };
      panels.push(panel);
      return panel;
    },
    createOutputChannel: () => ({ appendLine() {}, append() {}, show() {}, dispose() {} }),
  },
  authentication: { getSession: async () => undefined },
  env: { clipboard: { writeText: async () => {} } },
  commands: { executeCommand: async () => undefined },
  // Test helpers (not part of the VS Code API).
  __setConfiguration(values) {
    Object.assign(overrides, values);
  },
  __panels: panels,
};
