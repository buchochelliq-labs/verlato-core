export function commandUsesConfiguredModel(args: string[]): boolean {
  return (args[0] === "draft" && (args[1] === "suggest" || args[1] === "prompt")) || (args[0] === "doctor" && args[1] === "ollama");
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
  return provider === "github-copilot" && args[0] === "draft" && args[1] === "suggest";
}
