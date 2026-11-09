export type DiffEngine = "intentumdiff" | "git";

export function normalizeDiffEngine(value: unknown): DiffEngine {
  return String(value ?? "").trim().toLowerCase() === "git" ? "git" : "intentumdiff";
}

/** Environment that selects and tunes the IntentumDiff semantic engine for the Verlato CLI/service. */
export function semanticEngineEnvironment(options: {
  diffEngine?: unknown;
  parallelism?: unknown;
  crossFile?: unknown;
}): Record<string, string> {
  const env: Record<string, string> = { VERLATO_DIFF_ENGINE: normalizeDiffEngine(options.diffEngine) };
  const parallelism = Number(options.parallelism);
  if (Number.isFinite(parallelism) && parallelism > 0) {
    env.VERLATO_SEMANTIC_PARALLELISM = String(Math.min(Math.floor(parallelism), 16));
  }
  env.VERLATO_SEMANTIC_CROSS_FILE = options.crossFile === true ? "1" : "0";
  return env;
}

export function commandUsesConfiguredModel(args: string[]): boolean {
  return (
    (args[0] === "draft" && (args[1] === "suggest" || args[1] === "prompt")) ||
    (args[0] === "commit" && args[1] === "conventional") ||
    args[0] === "service" ||
    (args[0] === "doctor" && args[1] === "ollama")
  );
}

export function resolveCommandModelProvider(args: string[], configuredProvider: string): string {
  if (args[0] === "doctor" && args[1] === "ollama") {
    return "ollama";
  }
  if (commandUsesConfiguredModel(args)) {
    return configuredProvider;
  }
  return "stub";
}

export function shouldAttachGithubToken(args: string[], provider: string): boolean {
  return provider === "github-copilot" && (
    (args[0] === "draft" && args[1] === "suggest") ||
    (args[0] === "commit" && args[1] === "conventional") ||
    args[0] === "service"
  );
}
