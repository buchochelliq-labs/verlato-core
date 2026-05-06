import * as vscode from "vscode";

export type NotificationAction =
  | "Open Verlato"
  | "View Activity"
  | "Review Suggestions"
  | "Open File"
  | "Preview"
  | "Change Model";

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
  return vscode.window.showErrorMessage(message, ...actions);
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
  }
}
