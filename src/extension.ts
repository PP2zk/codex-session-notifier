import * as vscode from "vscode";
import { hostname } from "node:os";
import { join } from "node:path";
import { inspectSource } from "./probe";
import { discoverSource } from "./source";
import { completionText, Monitor, ScanResult, StateStore } from "./monitor";
import { Source } from "./source";
import { registerQQCommands } from "./qq-commands";
import { QqDelivery, resolveQueued } from "./delivery";
import { StatusUI } from "./status-ui";
import { plainLabel } from "./presentation";

function sourceSettings(settings: vscode.WorkspaceConfiguration, source: Source) {
  const overrides = settings.get<Record<string, { codexHome?: string; machineLabel?: string }>>("sources", {});
  const selected = overrides[source.kind === "local" ? "local" : source.host];
  return { home: selected?.codexHome ?? settings.get<string>("codexHome", ""),
    label: selected?.machineLabel ?? settings.get<string>("machineLabel", "") };
}

export function activate(context: vscode.ExtensionContext) {
  const output = vscode.window.createOutputChannel("Codex Session Notifier");
  let ui: StatusUI | undefined;
  let monitor: Monitor | undefined;
  let lastStatus = "";
  let lastVerification = "";
  let outputBytes = 0;
  const append = (text: string) => {
    outputBytes += Buffer.byteLength(text, "utf8");
    if (outputBytes > 1048576) { output.clear(); outputBytes = Buffer.byteLength(text, "utf8"); }
    output.appendLine(text);
  };
  const setStatus = (message: string, scan?: ScanResult) => {
    // A successful scan publishes its activity before updating the hover.
    // Errors and shared-source ownership have no publication to wait for.
    if (!scan) ui?.setMonitor(message);
    if (lastStatus !== message) { append(`Monitor: ${message}`); lastStatus = message; }
  };
  const database = join(context.globalStorageUri.fsPath, "monitor.sqlite");
  const store = () => new StateStore(context.extensionPath, database,
    vscode.workspace.getConfiguration("codexSessionNotifier").get<string>("pythonExecutable", ""));
  const getSender = registerQQCommands(context, store, append, () => { void ui?.refresh(); },
    action => ui ? ui.runInteraction(action) : action());
  ui = new StatusUI(store, async () => { await monitor?.tick(); });
  const startMonitor = () => {
    monitor?.dispose();
    monitor = undefined;
    lastVerification = "";
    const settings = vscode.workspace.getConfiguration("codexSessionNotifier");
    if (!settings.get<boolean>("enabled", true)) { setStatus("disabled"); return; }
    try {
      const authorities = (vscode.workspace.workspaceFolders || []).map(folder => folder.uri.authority);
      const source = discoverSource(vscode.env.remoteName, authorities, settings.get<string>("sshHost", ""));
      const selected = sourceSettings(settings, source);
      const options = {
        home: selected.home, python: settings.get<string>("pythonExecutable", ""),
        ssh: settings.get<string>("sshExecutable", "") };
      const delivery = new QqDelivery(store(), getSender, append);
      monitor = new Monitor(context.extensionPath, source, database, options, (host, events) => {
        const label = selected.label.trim() || host;
        for (const event of events) append(completionText(label, event));
      }, setStatus, 5000, undefined, async (sourceId, host, events, signal, scan) => {
        await store().publishActivity(sourceId, selected.label.trim() || host, scan.activeSessions || [], scan.activityUnknown || 0)
          .catch(() => append("Monitor UI: activity_snapshot_failed"));
        if (signal.aborted) return;
        const verification = JSON.stringify(scan.verificationFailures || []);
        if (verification !== lastVerification) {
          for (const failure of scan.verificationFailures || []) {
            append(`${new Date().toISOString()} Completion withheld: source not verified | ${JSON.stringify({
              machine: plainLabel(selected.label.trim() || host), sourceId,
              ...failure, sessionName: failure.sessionName === null ? null : plainLabel(failure.sessionName)
            })}`);
          }
          lastVerification = verification;
        }
        ui?.setMonitor(scan.warnings.length ? scan.warnings.join(", ") : "monitoring");
        void ui?.refresh();
        await delivery.drain(sourceId, host, events, selected.label,
          item => resolveQueued(context.extensionPath, source, sourceId, item, { ...options, signal }), signal);
      });
      setStatus("starting");
      monitor.start();
    } catch (error) {
      const message = error instanceof Error ? error.message : "source_not_identified";
      setStatus(/^[a-z_]+$/.test(message) ? message : "source_not_identified");
    }
  };
  let busy = false;
  const inspect = async () => {
    if (busy) return;
    busy = true;
    try {
      const settings = vscode.workspace.getConfiguration("codexSessionNotifier");
      const authorities = (vscode.workspace.workspaceFolders || []).map(folder => folder.uri.authority);
      const source = discoverSource(vscode.env.remoteName, authorities, settings.get<string>("sshHost", ""));
      const report = await inspectSource(context.extensionPath, source, {
        home: sourceSettings(settings, source).home, python: settings.get<string>("pythonExecutable", ""),
        ssh: settings.get<string>("sshExecutable", "") });
      output.clear();
      output.appendLine(JSON.stringify({ prototype: "P2", localHost: hostname(), extensionKind: context.extension.extensionKind,
        sourceKind: source.kind, ...report }, null, 2));
      output.show(true);
      return report;
    } catch (error) {
      const code = error instanceof Error ? error.message : "probe_failed";
      output.appendLine(`P0: ${/^[a-z_]+$/.test(code) ? code : "probe_failed"}`);
      output.show(true);
    } finally { busy = false; }
  };
  const register = <T>(id: string, action: () => T | PromiseLike<T>) =>
    vscode.commands.registerCommand(`codexSessionNotifier.${id}`, () => ui!.runInteraction(action));
  context.subscriptions.push(output, ui, { dispose: () => monitor?.dispose() },
    register("inspect", inspect),
    register("showStatus", () => output.show(true)),
    register("openMenu", () => ui!.showMenu()),
    register("refreshStatus", async () => { await monitor?.tick(); await ui?.refresh(); }),
    register("openSettings", () => vscode.commands.executeCommand("workbench.action.openSettings", `@ext:${context.extension.id}`)),
    vscode.workspace.onDidChangeConfiguration(event => { if (event.affectsConfiguration("codexSessionNotifier")) startMonitor(); }));
  startMonitor();
  return { inspect, stop: () => monitor?.dispose(), getStatus: () => lastStatus,
    getUI: () => ui?.snapshot(),
    hostKind: context.extension.extensionKind, localHost: hostname(), prototype: "P2" as const };
}

export function deactivate() {}
