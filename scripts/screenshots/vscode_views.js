#!/usr/bin/env node
// Screenshots of the VS Code extension's views, rendered from its real code.
//
// The extension's own client (`VerlatoClient`) runs the real `verlato` executable against a
// demo repository, and the views' real renderers (`buildReleaseControlHtml`,
// `buildReleaseReleasesHtml`, `buildReleaseActivityHtml`, `PreviewPanel`,
// `ReleaseSourcesProvider`) turn the results into HTML, which Playwright captures with VS Code's
// Dark Modern theme variables. Nothing is mocked except the `vscode` module itself.
//
// Usage: node scripts/screenshots/vscode_views.js --repo DIR [--out docs/assets/screenshots]
// Needs: `npm ci && npm run build` in vscode-extension/, and `verlato` on PATH.
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const Module = require("node:module");

const ROOT = path.resolve(__dirname, "..", "..");
const fakeVscode = require("./fake-vscode");
const { themed, sideBarFrame } = require("./theme");

// Route `require("vscode")` to the stand-in.
const originalResolve = Module._resolveFilename;
Module._resolveFilename = function (request, ...rest) {
  if (request === "vscode") return path.join(__dirname, "fake-vscode.js");
  return originalResolve.call(this, request, ...rest);
};

const dist = path.join(ROOT, "vscode-extension", "dist");
const { VerlatoClient } = require(path.join(dist, "client", "verlatoClient.js"));
const markup = require(path.join(dist, "webview", "releaseControlMarkup.js"));
const { PreviewPanel } = require(path.join(dist, "webview", "previewPanel.js"));
const { ReleaseSourcesProvider } = require(path.join(dist, "tree", "releaseSourcesProvider.js"));

function args() {
  const out = { repo: null, out: path.join(ROOT, "docs", "assets", "screenshots") };
  const argv = process.argv.slice(2);
  for (let i = 0; i < argv.length; i += 1) {
    if (argv[i] === "--repo") out.repo = path.resolve(argv[++i]);
    else if (argv[i] === "--out") out.out = path.resolve(argv[++i]);
  }
  if (!out.repo) throw new Error("--repo DIR is required");
  return out;
}

const ICONS = {
  "git-pull-request": '<circle cx="4" cy="3.5" r="1.6"/><circle cx="4" cy="12.5" r="1.6"/><circle cx="12" cy="12.5" r="1.6"/><path d="M4 5v6M12 11V6.5c0-1.5-1-2.5-2.5-2.5H7m1.5-2L7 4l1.5 2"/>',
  tag: '<path d="M2 2h6l6 6-6 6-6-6z"/><circle cx="5.5" cy="5.5" r="1.2"/>',
  "git-branch": '<circle cx="4" cy="3.5" r="1.6"/><circle cx="4" cy="12.5" r="1.6"/><circle cx="12" cy="5" r="1.6"/><path d="M4 5v6M12 6.6c0 3-3 3.4-6.6 4.6"/>',
  "git-commit": '<circle cx="8" cy="8" r="2.6"/><path d="M1 8h4.4M10.6 8H15"/>',
  package: '<path d="M2 4.5 8 2l6 2.5v7L8 14l-6-2.5z"/><path d="M2 4.5 8 7l6-2.5M8 7v7"/>',
  "circle-slash": '<circle cx="8" cy="8" r="6"/><path d="m3.8 12.2 8.4-8.4"/>',
  info: '<circle cx="8" cy="8" r="6"/><path d="M8 7v4M8 5v.5"/>',
};

function icon(id) {
  const body = ICONS[id] || ICONS.info;
  return `<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.2" stroke-linecap="round" stroke-linejoin="round">${body}</svg>`;
}

function escapeHtml(value) {
  return String(value ?? "").replace(/[&<>"]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[c]);
}

/** Render the Sources tree the way VS Code's tree view draws it (expanded groups). */
function renderTree(provider) {
  const rows = [];
  const walk = (node, depth) => {
    const item = provider.getTreeItem(node);
    const expandable = item.collapsibleState !== 0;
    const expanded = item.collapsibleState === 2;
    rows.push(`<div class="row" style="padding-left:${8 + depth * 16}px">
      <span class="twistie">${expandable ? (expanded ? "&#x25BE;" : "&#x25B8;") : ""}</span>
      <span class="icon">${icon(item.iconPath && item.iconPath.id)}</span>
      <span class="label">${escapeHtml(item.label)}</span>
      <span class="description">${escapeHtml(item.description)}</span></div>`);
    if (expanded) {
      for (const child of provider.getChildren(node)) walk(child, depth + 1);
    }
  };
  for (const node of provider.getChildren()) walk(node, 0);
  return `<style>
    .tree { padding: 4px 0; font-size: 13px; }
    .row { display: flex; align-items: center; gap: 4px; height: 22px; white-space: nowrap; overflow: hidden; }
    .row:hover { background: var(--vscode-list-hoverBackground); }
    .twistie { width: 14px; text-align: center; color: var(--vscode-icon-foreground); }
    .icon { display: inline-flex; color: var(--vscode-icon-foreground); }
    .label { color: var(--vscode-foreground); }
    .description { color: var(--vscode-descriptionForeground); font-size: 12px; margin-left: 6px; overflow: hidden; text-overflow: ellipsis; }
  </style><div class="tree">${rows.join("")}</div>`;
}

async function main() {
  const options = args();
  fs.mkdirSync(options.out, { recursive: true });
  fakeVscode.__setConfiguration({
    "verlato.modelProvider": "mock",
    "verlato.suggestionParallelism": 1,
  });

  const workspaceFolder = { uri: { fsPath: options.repo }, name: path.basename(options.repo), index: 0 };
  // An empty extension directory, so the client resolves `verlato` from PATH.
  const extensionDir = fs.mkdtempSync(path.join(os.tmpdir(), "verlato-ext-"));
  const output = { appendLine() {}, append() {}, show() {}, dispose() {} };
  const client = new VerlatoClient(workspaceFolder, extensionDir, output);
  let requests = [];
  client.onDidChangeRequests((snapshot) => (requests = snapshot));

  const range = { base: "v1.0.0", head: "HEAD", version: "v1.1.0" };
  const modelSettings = client.getModelSettings();
  const document = await client.getDraft(range);
  const overview = await client.getRepositoryOverview();
  const suggestions = await client.previewDraftSuggestions(range, undefined, undefined, "no-cache");
  await client.getEngineStatus();
  await new Promise((resolve) => setTimeout(resolve, 300)); // let the request snapshot settle
  requests = client.getCommandRequests();
  client.dispose();

  const vendor = path.join(ROOT, "vscode-extension", "media", "vendor", "vscode-elements.bundled.js");
  const pages = {
    "vscode-control": themed(markup.buildReleaseControlHtml({
      document, overview, modelSettings, range, busy: false, suggestions, requests,
      statusMessage: `Loaded ${document.item_count} release-note items for ${range.base}..${range.head}.`,
    }, { webviewElementsScriptUri: `file://${vendor}` })),
    "vscode-releases": themed(markup.buildReleaseReleasesHtml({ document, overview, suggestions })),
    "vscode-activity": themed(markup.buildReleaseActivityHtml({ requests })),
  };

  const previewPanel = new PreviewPanel();
  previewPanel.show(document);
  pages["vscode-preview"] = themed(fakeVscode.__panels[fakeVscode.__panels.length - 1].webview.html, "#1f1f1f");

  const sources = new ReleaseSourcesProvider();
  sources.refresh(overview);
  pages["vscode-sources"] = sideBarFrame("Sources", renderTree(sources));

  const { chromium } = require("playwright");
  const browser = await chromium.launch();
  // `page` is the HTML to load; `prepare` opens details before the capture; `element` clips the
  // capture to one element instead of the viewport.
  const shots = [
    { name: "vscode-control", width: 520, height: 1240 },
    {
      name: "vscode-semantic-review",
      page: "vscode-control",
      width: 520,
      height: 1400,
      prepare: () => {
        const item = document.querySelectorAll("article.item")[1];
        item.querySelector("details.semantic-evidence").open = true;
        item.querySelector("details.changeset-commits").open = false;
        item.setAttribute("data-screenshot", "semantic");
      },
      element: 'article.item[data-screenshot="semantic"]',
    },
    { name: "vscode-releases", width: 520, height: 760 },
    {
      name: "vscode-activity",
      width: 520,
      height: 720,
      prepare: () => {
        const suggest = [...document.querySelectorAll("details.request")]
          .find((request) => request.querySelector("summary").textContent.includes("draft suggest"));
        if (suggest) suggest.open = true;
      },
    },
    { name: "vscode-preview", width: 900, height: 900 },
    { name: "vscode-sources", width: 420, height: 520 },
  ];
  const staging = fs.mkdtempSync(path.join(os.tmpdir(), "verlato-shots-"));
  for (const shot of shots) {
    const file = path.join(staging, `${shot.name}.html`);
    fs.writeFileSync(file, pages[shot.page || shot.name]);
    const page = await browser.newPage({ viewport: { width: shot.width, height: shot.height }, deviceScaleFactor: 2 });
    await page.goto(`file://${file}`);
    if (shot.prepare) await page.evaluate(shot.prepare);
    await page.waitForTimeout(600);
    const target = path.join(options.out, `${shot.name}.png`);
    if (shot.element) await page.locator(shot.element).screenshot({ path: target });
    else await page.screenshot({ path: target });
    await page.close();
    console.log(`wrote ${path.relative(ROOT, target)}`);
  }
  await browser.close();
  fs.writeFileSync(path.join(staging, "data.json"), JSON.stringify({ document, overview, suggestions, requests }, null, 2));
  console.log(`page sources and data: ${staging}`);
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
