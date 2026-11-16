// VS Code "Dark Modern" theme variables used by Verlato's webviews, plus the frame around each
// view, so screenshots look like the views do inside VS Code.
"use strict";

const variables = {
  "font-family": "Inter, 'Segoe UI', system-ui, sans-serif",
  "font-size": "13px",
  "editor-font-family": "'JetBrains Mono', 'DejaVu Sans Mono', monospace",
  foreground: "#cccccc",
  descriptionForeground: "#9d9d9d",
  "editor-background": "#1f1f1f",
  "editor-foreground": "#cccccc",
  "sideBar-background": "#181818",
  "sideBarTitle-foreground": "#cccccc",
  "sideBarSectionHeader-border": "#2b2b2b",
  "input-background": "#313131",
  "input-foreground": "#cccccc",
  "input-border": "#3c3c3c",
  focusBorder: "#0078d4",
  "button-background": "#0078d4",
  "button-foreground": "#ffffff",
  "button-hoverBackground": "#026ec1",
  "button-secondaryBackground": "#313131",
  "button-secondaryForeground": "#cccccc",
  "button-secondaryHoverBackground": "#3c3c3c",
  "badge-background": "#616161",
  "badge-foreground": "#f8f8f8",
  "list-hoverBackground": "#2a2d2e",
  "list-activeSelectionBackground": "#04395e",
  "textLink-foreground": "#4daafc",
  "charts-green": "#89d185",
  "charts-blue": "#3794ff",
  "charts-yellow": "#cca700",
  errorForeground: "#f85149",
  "textBlockQuote-background": "#2b2b2b",
  "progressBar-background": "#0078d4",
  "icon-foreground": "#cccccc",
  "editorWidget-border": "#313131",
  "editorWidget-background": "#202020",
  "dropdown-background": "#313131",
  "dropdown-foreground": "#cccccc",
  "dropdown-border": "#3c3c3c",
};

function themeStyle(background) {
  const lines = Object.entries(variables).map(([name, value]) => `  --vscode-${name}: ${value};`);
  return `<style id="screenshot-theme">
:root {
${lines.join("\n")}
}
html, body { background: ${background}; color: var(--vscode-foreground); font-family: var(--vscode-font-family); font-size: var(--vscode-font-size); margin: 0; }
body { padding: 0; }
</style>`;
}

/** Insert the theme and a stub of the webview API into a webview's HTML. */
function themed(html, background = variables["sideBar-background"]) {
  const stub = "<script>window.acquireVsCodeApi = () => ({ postMessage() {}, getState() { return undefined; }, setState() {} });</script>";
  return html.replace(/<head>/i, `<head>\n${themeStyle(background)}\n${stub}`);
}

/** A VS Code side bar view title above a view's body. */
function sideBarFrame(title, bodyHtml) {
  return `<!DOCTYPE html><html><head><meta charset="utf-8">${themeStyle(variables["sideBar-background"])}
<style>
  body { padding: 0; }
  .view-title { height: 35px; display: flex; align-items: center; padding: 0 12px; font-size: 11px; font-weight: 600;
    letter-spacing: .04em; text-transform: uppercase; color: var(--vscode-sideBarTitle-foreground); border-bottom: 1px solid #2b2b2b; }
</style></head><body><div class="view-title">${title}</div>${bodyHtml}</body></html>`;
}

module.exports = { variables, themed, sideBarFrame, themeStyle };
