import * as vscode from "vscode";
import { QQ_TEST_MESSAGE, PushplusSender, validQQToken } from "./qq";
import { QqStatus, StateStore } from "./monitor";
import { QQ_TOKEN_KEY } from "./delivery";
import { CHANNELS, DEFAULT_ROUTE, NotificationRoute, channelInfo, validOption, validateRoute } from "./channels";

// Retain the existing secret key for upgrades. No token migration is needed.
export function registerQQCommands(context: vscode.ExtensionContext, store: () => StateStore, append: (line: string) => void,
  onChange: () => void = () => {}, runInteraction: (action: () => Promise<void>) => Promise<void> = action => action()): (route: NotificationRoute) => Promise<PushplusSender | undefined> {
  let sender: PushplusSender | undefined;
  let loadedKey: string | undefined;
  const getSender = async (route: NotificationRoute) => {
    const token = await context.secrets.get(QQ_TOKEN_KEY);
    if (!token) return undefined;
    validateRoute(route);
    const key = JSON.stringify([token, route.channel, route.option]);
    if (!sender || loadedKey !== key) { sender = new PushplusSender(token, undefined, route); loadedKey = key; }
    return sender;
  };
  const sameRevision = async (state: QqStatus) => (await store().qqStatus()).routeRevision === state.routeRevision;
  const selectChannel = async () => {
    try {
      const current = await store().qqStatus();
      const selected = await vscode.window.showQuickPick(CHANNELS.map(info => ({
        label: info.label, description: `${info.id}${"paid" in info ? " · Paid" : ""}${info.id === current.route?.channel ? " · Current" : ""}`,
        detail: info.detail, info
      })), { title: "Select pushplus channel", placeHolder: "One channel for all monitored windows on this computer", ignoreFocusOut: true });
      if (!selected) return;
      const info = selected.info;
      const option = await vscode.window.showInputBox({ title: `Configure ${info.label}`, ignoreFocusOut: true,
        value: current.route?.channel === info.id ? current.route.option : "",
        prompt: `${"requiredOption" in info ? "Required" : "Optional"} pushplus channel configuration code (option). ${info.detail}`,
        validateInput: value => validOption(value.trim()) && (!("requiredOption" in info) || !!value.trim()) ? undefined :
          "Enter a channel configuration code, not a URL. This channel may require a nonempty code." });
      if (option === undefined) return;
      const route: NotificationRoute = { channel: info.id, option: option.trim() };
      validateRoute(route);
      if (!(await store().configureRoute(route, current.routeRevision || 0)).updated) {
        void vscode.window.showInformationMessage("Notification settings changed in another window. Select the channel again."); return;
      }
      sender = undefined; loadedKey = undefined; onChange();
      const changed = JSON.stringify(route) !== JSON.stringify(current.route || DEFAULT_ROUTE);
      void vscode.window.showInformationMessage(`${info.label} selected.${changed ? " Automatic delivery is off. Review your pushplus binding, then enable notifications." : ""}`);
    } catch { void vscode.window.showErrorMessage("Could not save the pushplus channel configuration."); }
  };
  const configure = async () => {
    const token = await vscode.window.showInputBox({ title: "Configure pushplus token", password: true,
      prompt: "Enter your pushplus user token or message token. Stored only in local secure storage.",
      ignoreFocusOut: true, validateInput: value => validQQToken(value.trim()) ? undefined : "Enter a valid pushplus token" });
    if (!token) return;
    try {
      await store().qqDisable(); await context.secrets.store(QQ_TOKEN_KEY, token.trim());
      sender = undefined; loadedKey = undefined;
      onChange();
      void vscode.window.showInformationMessage("pushplus token saved. Automatic delivery is off. Select a channel, optionally send a test, then enable notifications.");
    } catch { void vscode.window.showErrorMessage("Could not save the pushplus token to local secure storage."); }
  };
  const test = async () => {
    let label = "pushplus";
    try {
      const state = await store().qqStatus(), route = state.route || DEFAULT_ROUTE, info = channelInfo(route.channel);
      label = info.label;
      if (!(await context.secrets.get(QQ_TOKEN_KEY))) { void vscode.window.showInformationMessage("Run Configure pushplus Token first."); return; }
      const { title, content } = QQ_TEST_MESSAGE;
      const choice = await vscode.window.showQuickPick([
        { label: "$(send) Send test", description: `${info.label}${"paid" in info ? " · Paid request" : ""}`,
          detail: `Title: ${title} | Details: ${content}${route.option ? " | Uses your configured channel code" : ""}`, send: true },
        { label: "Cancel", send: false }
      ], { title: "Send test notification", placeHolder: "Review the message and recipient binding before sending", ignoreFocusOut: true });
      if (!choice?.send) return;
      if (!(await sameRevision(state))) throw new Error("qq_configuration_changed");
      const active = await getSender(route);
      if (!active) return;
      await active.sendTest(title, content);
      append(`${label}: request_accepted; recipient delivery unconfirmed`);
      void vscode.window.showInformationMessage(`Test notification submitted to pushplus. Check ${label} for delivery.`);
    } catch (error) {
      const code = error instanceof Error && /^qq_[a-z_0-9]+$/.test(error.message) ? error.message : "qq_delivery_unknown";
      append(`${label}: ${code}`);
      void vscode.window.showErrorMessage(`${label} test result: ${code}. Check the recipient or your pushplus records before sending again.`);
    }
  };
  const remove = async () => {
    await store().qqDisable(); await context.secrets.delete(QQ_TOKEN_KEY);
    sender = undefined; loadedKey = undefined; onChange();
    void vscode.window.showInformationMessage("The locally stored pushplus token has been removed.");
  };
  const enable = async () => {
    try {
      const state = await store().qqStatus(), route = state.route || DEFAULT_ROUTE, info = channelInfo(route.channel);
      if (!(await getSender(route))) { void vscode.window.showInformationMessage("Run Configure pushplus Token first."); return; }
      if ("paid" in info) {
        const choice = await vscode.window.showQuickPick([{ label: "Enable paid notifications", description: info.label, enable: true },
          { label: "Cancel", enable: false }], { title: "This pushplus channel charges for messages", placeHolder: "Future reply completions may consume pushplus credits", ignoreFocusOut: true });
        if (!choice?.enable) return;
      }
      const result = await store().qqEnable(state.routeRevision || 0);
      if (!result.enabled) { void vscode.window.showInformationMessage("Channel changed in another window. Review it before enabling notifications."); return; }
      onChange(); append(`${info.label}: automatic delivery enabled for new completions`);
      void vscode.window.showInformationMessage(`Automatic ${info.label} notifications are enabled and will stay enabled after restarting VS Code.`);
    } catch { void vscode.window.showErrorMessage("Could not enable notifications. Check your channel, local Python and secure storage configuration."); }
  };
  const disable = async () => {
    try {
      await store().qqDisable(); onChange(); append("pushplus: automatic delivery disabled");
      void vscode.window.showInformationMessage("Automatic notifications are off. A request already submitted may still arrive.");
    } catch { void vscode.window.showErrorMessage("Could not save the disabled notification state."); }
  };
  const showStatus = async () => {
    try {
      const state = await store().qqStatus(), label = channelInfo(state.route?.channel).label;
      append(`pushplus: ${JSON.stringify({ channel: state.route?.channel || "qq", enabled: state.enabled, paused: state.paused, counts: state.counts })}`);
      void vscode.window.showInformationMessage(`Automatic ${label} notifications: ${state.enabled ? state.paused ? `paused (${state.paused})` : "enabled" : "disabled"}. Provider acceptance does not confirm recipient delivery. Review uncertain requests in pushplus before resuming.`);
    } catch { void vscode.window.showErrorMessage("Could not read the local notification delivery status."); }
  };
  const actions = { configureToken: configure, selectChannel, testNotification: test, removeToken: remove,
    enableNotifications: enable, disableNotifications: disable, deliveryStatus: showStatus };
  for (const [id, action] of Object.entries(actions)) context.subscriptions.push(
    vscode.commands.registerCommand(`codexSessionNotifier.${id}`, () => runInteraction(action)));
  context.subscriptions.push(context.secrets.onDidChange(event => {
    if (event.key === QQ_TOKEN_KEY) { sender = undefined; loadedKey = undefined; }
  }));
  return getSender;
}
