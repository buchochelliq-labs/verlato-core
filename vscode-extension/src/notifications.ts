import * as vscode from "vscode";

export type NotificationAction =
  | "Open Verlato"
  | "View Activity"
  | "Review Suggestions"
  | "Open File"
  | "Preview"
  | "Change Model"
  | "Install IntentumDiff"
  | "Use Git Engine";

// The `verlato` wheel ships the IntentumDiff parser bundle; so does each native release bundle
// (wasm/ beside the executable) on GitHub Releases.
export const INTENTUMDIFF_INSTALL_COMMAND = "pip install --upgrade verlato";

/** True when a Verlato error says the IntentumDiff semantic engine could not be loaded. */
export function isSemanticEngineUnavailable(message: string): boolean {
  return /IntentumDiff/i.test(message) && /could not be loaded|not available|parser bundle was not found|intentumdiff-python/i.test(message);
}

export async function showSuccessNotification(
  message: string,
  ...actions: NotificationAction[]
): Promise<NotificationAction | undefined> {
  if (vscode.workspace.getConfiguration("verlato").get<string>("notifications.successLevel") === "quiet") {
    return undefined;
  }
  return vscode.window.showInformationMessage(message, ...actions);
}

export async function showErrorNotification(
  message: string,
  ...actions: NotificationAction[]
): Promise<NotificationAction | undefined> {
  const engineActions: NotificationAction[] = isSemanticEngineUnavailable(message)
    ? ["Install IntentumDiff", "Use Git Engine"]
    : [];
  return vscode.window.showErrorMessage(message, ...engineActions, ...actions);
}

export async function handleNotificationAction(
  action: NotificationAction | undefined,
  handlers: Partial<Record<NotificationAction, () => void | Thenable<void>>> = {},
): Promise<void> {
  if (!action) {
    return;
  }
  if (handlers[action]) {
    await handlers[action]?.();
    return;
  }
  if (action === "Open Verlato" || action === "Review Suggestions") {
    await vscode.commands.executeCommand("verlato.openControl");
    return;
  }
  if (action === "View Activity") {
    await vscode.commands.executeCommand("verlato.openActivity");
    return;
  }
  if (action === "Change Model") {
    await vscode.commands.executeCommand("verlato.changeModel");
    return;
  }
  if (action === "Install IntentumDiff") {
    const terminal = vscode.window.createTerminal("Verlato: IntentumDiff");
    terminal.show();
    terminal.sendText(INTENTUMDIFF_INSTALL_COMMAND, false);
    return;
  }
  if (action === "Use Git Engine") {
    await vscode.workspace.getConfiguration("verlato").update("diffEngine", "git", vscode.ConfigurationTarget.Workspace);
  }
}
