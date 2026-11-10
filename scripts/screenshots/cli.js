#!/usr/bin/env node
// Screenshots of real `verlato` terminal sessions.
//
// Runs each command in the demo repository, converts its output (ANSI colours included, if
// any) to HTML, frames it as a dark terminal window and captures it with Playwright.
//
// Usage: node scripts/screenshots/cli.js --repo DIR [--out docs/assets/screenshots]
// Needs: `verlato` on PATH (and its parser bundle).
"use strict";

const fs = require("node:fs");
const os = require("node:os");
const path = require("node:path");
const { spawnSync } = require("node:child_process");

const ROOT = path.resolve(__dirname, "..", "..");

const SESSIONS = [
  { name: "cli-help", commands: [["verlato", "--help"]], width: 820 },
  { name: "cli-doctor-engine", commands: [["verlato", "doctor", "engine"]], width: 820 },
  {
    name: "cli-draft-preview",
    commands: [["verlato", "draft", "preview", "--from-latest-tag", "--version", "v1.1.0"]],
    width: 980,
  },
];

const ANSI_COLOURS = {
  30: "#545454", 31: "#f14c4c", 32: "#23d18b", 33: "#f5f543", 34: "#3b8eea", 35: "#d670d6", 36: "#29b8db", 37: "#e5e5e5",
  90: "#666666", 91: "#f14c4c", 92: "#23d18b", 93: "#f5f543", 94: "#3b8eea", 95: "#d670d6", 96: "#29b8db", 97: "#ffffff",
};

function escapeHtml(text) {
  return text.replace(/[&<>]/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;" })[c]);
}

/** SGR colour and bold codes to <span>s; everything else is dropped. */
function ansiToHtml(text) {
  let open = 0;
  const html = escapeHtml(text).replace(/\x1b\[([0-9;]*)m/g, (_, codes) => {
    let out = "";
    for (const code of (codes || "0").split(";").map(Number)) {
      if (code === 0) {
        out += "</span>".repeat(open);
        open = 0;
      } else if (code === 1) {
        out += '<span style="font-weight:700">';
        open += 1;
      } else if (ANSI_COLOURS[code]) {
        out += `<span style="color:${ANSI_COLOURS[code]}">`;
        open += 1;
      }
    }
    return out;
  });
  return html.replace(/\x1b\[[0-9;?]*[A-Za-z]/g, "") + "</span>".repeat(open);
}

/** Light Markdown/JSON highlighting for uncoloured output, so the captures stay readable. */
function highlight(text) {
  return text
    .split("\n")
    .map((line) => {
      if (/^#{2,3} /.test(line)) return `<span class="heading">${line}</span>`;
      return line
        .replace(/^(\s*)(&quot;|")([^"]+)("):/, '$1<span class="key">"$3"</span>:')
        .replace(/^(\S[^:\n]*:)$/, '<span class="section">$1</span>');
    })
    .join("\n");
}

function render(session, repo) {
  const blocks = [];
  for (const argv of session.commands) {
    const result = spawnSync(argv[0], argv.slice(1), {
      cwd: repo,
      encoding: "utf8",
      env: { ...process.env, COLUMNS: "96", CLICOLOR_FORCE: "1" },
    });
    if (result.status !== 0) {
      throw new Error(`${argv.join(" ")} failed (${result.status}): ${result.stderr}`);
    }
    const output = result.stdout.replace(/\s+$/, "");
    const body = output.includes("\x1b[") ? ansiToHtml(output) : highlight(escapeHtml(output));
    blocks.push(`<div><span class="prompt">${path.basename(repo)} $</span> ${escapeHtml(argv.join(" "))}</div><div>${body}</div>`);
  }
  return `<!DOCTYPE html><html><head><meta charset="utf-8"><style>
  html, body { margin: 0; background: transparent; }
  .window { margin: 16px; border-radius: 10px; overflow: hidden; background: #181818; box-shadow: 0 8px 28px rgba(0,0,0,.45); border: 1px solid #2b2b2b; }
  .bar { height: 30px; display: flex; align-items: center; gap: 8px; padding: 0 12px; background: #252526; }
  .dot { width: 12px; height: 12px; border-radius: 50%; }
  .title { flex: 1; text-align: center; color: #9d9d9d; font: 12px Inter, system-ui, sans-serif; margin-right: 52px; }
  pre { margin: 0; padding: 14px 16px 18px; color: #cccccc; font: 13px/1.45 'JetBrains Mono', 'DejaVu Sans Mono', monospace; white-space: pre-wrap; word-break: break-word; }
  .prompt { color: #23d18b; }
  .heading { color: #3b8eea; font-weight: 700; }
  .key { color: #9cdcfe; }
  .section { color: #cca700; }
</style></head><body><div class="window"><div class="bar"><span class="dot" style="background:#ff5f57"></span><span class="dot" style="background:#febc2e"></span><span class="dot" style="background:#28c840"></span><span class="title">Terminal</span></div><pre>${blocks.join("\n")}</pre></div></body></html>`;
}

async function main() {
  const argv = process.argv.slice(2);
  const repo = path.resolve(argv[argv.indexOf("--repo") + 1]);
  const outIndex = argv.indexOf("--out");
  const out = outIndex >= 0 ? path.resolve(argv[outIndex + 1]) : path.join(ROOT, "docs", "assets", "screenshots");
  fs.mkdirSync(out, { recursive: true });
  const { chromium } = require("playwright");
  const browser = await chromium.launch();
  const staging = fs.mkdtempSync(path.join(os.tmpdir(), "verlato-cli-shots-"));
  for (const session of SESSIONS) {
    const file = path.join(staging, `${session.name}.html`);
    fs.writeFileSync(file, render(session, repo));
    const page = await browser.newPage({ viewport: { width: session.width, height: 400 }, deviceScaleFactor: 2 });
    await page.goto(`file://${file}`);
    const target = path.join(out, `${session.name}.png`);
    await page.locator(".window").screenshot({ path: target, omitBackground: true });
    await page.close();
    console.log(`wrote ${path.relative(ROOT, target)}`);
  }
  await browser.close();
}

main().catch((error) => {
  console.error(error);
  process.exit(1);
});
