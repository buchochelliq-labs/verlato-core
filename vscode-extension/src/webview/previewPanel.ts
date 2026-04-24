import * as vscode from "vscode";

import { DraftDocument } from "../client/types";

export class PreviewPanel {
  private panel: vscode.WebviewPanel | undefined;

  show(document: DraftDocument): void {
    if (!this.panel) {
      this.panel = vscode.window.createWebviewPanel(
        "releasepilot.preview",
        "ReleasePilot Preview",
        vscode.ViewColumn.Beside,
        {
          enableScripts: false,
        },
      );
      this.panel.onDidDispose(() => {
        this.panel = undefined;
      });
    }

    this.panel.title = `ReleasePilot Preview: ${document.version}`;
    this.panel.webview.html = this.renderHtml(document);
    this.panel.reveal(vscode.ViewColumn.Beside);
  }

  private renderHtml(document: DraftDocument): string {
    const sections = document.sections
      .map(
        (section) => `
          <section>
            <h2>${escapeHtml(section.title)}</h2>
            <ul>
              ${section.items
                .map(
                  (item) => `
                    <li>
                      <strong>${escapeHtml(item.title)}</strong>
                      <div>${escapeHtml(item.summary)}</div>
                      <small>${escapeHtml(`${item.kind} | ${item.area} | ${item.status} | confidence ${item.confidence.toFixed(2)}`)}</small>
                    </li>
                  `,
                )
                .join("")}
            </ul>
          </section>
        `,
      )
      .join("");

    return `<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <style>
      body { font-family: var(--vscode-font-family); padding: 16px; color: var(--vscode-editor-foreground); }
      h1, h2 { font-weight: 600; }
      pre { white-space: pre-wrap; background: var(--vscode-textBlockQuote-background); padding: 12px; border-radius: 6px; }
      ul { padding-left: 20px; }
      li { margin-bottom: 12px; }
      small { opacity: 0.8; }
    </style>
  </head>
  <body>
    <h1>${escapeHtml(document.version)}</h1>
    <p>${escapeHtml(`${document.base_ref}..${document.head_ref} | ${document.item_count} items`)}</p>
    <p>${escapeHtml("Hidden items stay in the draft data but are excluded from rendered output.")}</p>
    ${sections}
    <h2>Rendered GitHub Release</h2>
    <pre>${escapeHtml(document.rendered["github-release"])}</pre>
  </body>
</html>`;
  }
}

function escapeHtml(value: string): string {
  return value
    .replace(/&/g, "&amp;")
    .replace(/</g, "&lt;")
    .replace(/>/g, "&gt;");
}
