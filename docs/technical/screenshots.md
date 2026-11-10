# Screenshots

The screenshots in this documentation and the READMEs (`docs/assets/screenshots/`) are
generated, so they always show the current product:

```sh
cargo build --release -p verlato
scripts/fetch_intentumdiff_parsers.sh
(cd vscode-extension && npm ci)
scripts/screenshots/capture.sh
```

`capture.sh` needs Node 22+ and Playwright with Chromium. It:

1. creates the **taskboard** demo repository with `scripts/screenshots/make_demo_repo.sh`: a
   tiny Python project with a tagged release, a merged pull request, a fix, a rename, a vague
   `wip` commit, a formatting-only commit and a docs commit, with a fixed author and fixed
   dates so commit and item IDs never change;
2. records a few review decisions with the real CLI (one item accepted with an edited summary,
   one hidden);
3. runs `scripts/screenshots/vscode_views.js`, which loads the **extension's own code** under
   Node with a stand-in for the `vscode` module (`fake-vscode.js`): the real `VerlatoClient`
   runs the real `verlato` executable against the demo repository, and the real view
   renderers (`buildReleaseControlHtml`, `buildReleaseReleasesHtml`,
   `buildReleaseActivityHtml`, `PreviewPanel`, `ReleaseSourcesProvider`) produce the HTML.
   Playwright captures it with VS Code's Dark Modern theme variables (`theme.js`) at twice
   the resolution;
4. runs `scripts/screenshots/cli.js`, which runs `verlato --help`, `verlato doctor engine` and
   `verlato draft preview` in the demo repository and captures their output in a terminal
   frame;
5. copies the semantic review capture to `vscode-extension/media/semantic-review.png` for the
   Marketplace page.

The demo repository lives at `/srv/demo/taskboard` (override with `DEMO_DIR`) and the parser
bundle is linked at `/opt/verlato/wasm`, so no machine-specific path appears in an image. The
model provider is the deterministic `mock`, so the captures need no model and are
reproducible. Look at every image before committing it.

| File | Shows |
|---|---|
| `vscode-control.png` | the Control view |
| `vscode-semantic-review.png` | one changeset with its semantic review expanded |
| `vscode-releases.png` | the Releases view |
| `vscode-activity.png` | the Activity view with a suggestion run expanded |
| `vscode-preview.png` | the preview panel |
| `vscode-sources.png` | the Sources view |
| `cli-help.png`, `cli-doctor-engine.png`, `cli-draft-preview.png` | terminal sessions |
