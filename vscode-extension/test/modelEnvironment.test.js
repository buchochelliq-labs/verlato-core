const assert = require("node:assert/strict");
const test = require("node:test");

const {
  commandUsesConfiguredModel,
  resolveCommandModelProvider,
  shouldAttachGithubToken,
} = require("../dist/client/modelEnvironment");

test("uses the configured model only for explicit model commands", () => {
  assert.equal(commandUsesConfiguredModel(["draft", "suggest", "--base", "v1.0.0"]), true);
  assert.equal(commandUsesConfiguredModel(["draft", "prompt", "--base", "v1.0.0"]), true);
  assert.equal(commandUsesConfiguredModel(["commit", "conventional", "--sha", "abc1234"]), true);
  assert.equal(commandUsesConfiguredModel(["service"]), true);
  assert.equal(commandUsesConfiguredModel(["doctor", "ollama"]), true);
  assert.equal(commandUsesConfiguredModel(["draft", "json", "--base", "v1.0.0"]), false);
  assert.equal(commandUsesConfiguredModel(["draft", "preview", "--base", "v1.0.0"]), false);
  assert.equal(commandUsesConfiguredModel(["scan", "--base", "v1.0.0"]), false);
});

test("forces scan-like commands onto the stub provider to avoid model fanout", () => {
  assert.equal(resolveCommandModelProvider(["draft", "json"], "ollama"), "stub");
  assert.equal(resolveCommandModelProvider(["draft", "apply"], "github-copilot"), "stub");
  assert.equal(resolveCommandModelProvider(["reconcile"], "ollama"), "stub");
});

test("keeps the selected model provider for suggestion and prompt commands", () => {
  assert.equal(resolveCommandModelProvider(["draft", "suggest"], "ollama"), "ollama");
  assert.equal(resolveCommandModelProvider(["draft", "prompt"], "github-copilot"), "github-copilot");
  assert.equal(resolveCommandModelProvider(["commit", "conventional"], "github-copilot"), "github-copilot");
  assert.equal(resolveCommandModelProvider(["service"], "github-copilot"), "github-copilot");
  assert.equal(resolveCommandModelProvider(["draft", "suggest"], "configured"), "configured");
  assert.equal(resolveCommandModelProvider(["doctor", "ollama"], "configured"), "ollama");
});

test("attaches GitHub auth for GitHub-backed model calls", () => {
  assert.equal(shouldAttachGithubToken(["draft", "suggest"], "github-copilot"), true);
  assert.equal(shouldAttachGithubToken(["commit", "conventional"], "github-copilot"), true);
  assert.equal(shouldAttachGithubToken(["service"], "github-copilot"), true);
  assert.equal(shouldAttachGithubToken(["draft", "prompt"], "github-copilot"), false);
  assert.equal(shouldAttachGithubToken(["draft", "suggest"], "ollama"), false);
});
