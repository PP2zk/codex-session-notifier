import assert from "node:assert/strict";
import * as vscode from "vscode";
import { inspectSource } from "../src/probe";
import { validateSshHost } from "../src/source";
import { StateStore } from "../src/monitor";
import { existsSync, readFileSync, writeFileSync } from "node:fs";
import { join, resolve } from "node:path";

export async function run(): Promise<void> {
  const testStarted = Date.now();
  const manifest = JSON.parse(readFileSync(join(__dirname, "../../package.json"), "utf8"));
  const extension = vscode.extensions.getExtension(`${manifest.publisher}.${manifest.name}`);
  assert.ok(extension, "development extension must be discoverable");
  const deadline = Date.now() + 5000;
  while (!extension.isActive && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 50));
  assert.ok(extension.isActive, "must activate automatically on startup");
  const api = extension.exports;
  const startupTrace: Array<{ elapsedMs: number; text: string; tooltip: string }> = [];
  const capture = () => {
    const snapshot = api.getUI();
    if (startupTrace.length < 64 && (!startupTrace.length || startupTrace[startupTrace.length - 1]!.tooltip !== snapshot.tooltip)) {
      startupTrace.push({ elapsedMs: Date.now() - testStarted, text: snapshot.text, tooltip: snapshot.tooltip });
    }
  };
  if (process.env.CSN_UI_STARTUP_TRACE) capture();
  const traceTimer = process.env.CSN_UI_STARTUP_TRACE ? setInterval(capture, 20) : undefined;
  assert.equal(api.hostKind, vscode.ExtensionKind.UI, "must run in the local UI host");
  assert.equal(api.prototype, "P2");
  const commands = await vscode.commands.getCommands();
  assert.ok(commands.includes("codexSessionNotifier.inspect"));
  assert.ok(commands.includes("codexSessionNotifier.showStatus"));
  for (const id of ["configureQQ", "testQQ", "removeQQ", "enableQQ", "disableQQ", "qqStatus"]) {
    assert.ok(!commands.includes(`codexSessionNotifier.${id}`), `obsolete command ${id} must not be registered`);
  }
  for (const id of ["selectChannel", "configureToken", "testNotification", "enableNotifications", "disableNotifications", "deliveryStatus", "removeToken"]) {
    assert.ok(commands.includes(`codexSessionNotifier.${id}`), `generic command ${id}`);
  }
  assert.ok(commands.includes("codexSessionNotifier.openMenu"));
  assert.ok(commands.includes("codexSessionNotifier.refreshStatus"));
  assert.equal(api.getUI().command, "codexSessionNotifier.openMenu");
  const local = await api.inspect();
  assert.equal(local.schemaVersion, 1);
  assert.ok(local.databaseExists, "real local Codex metadata must be readable");
  const monitoringDeadline = Date.now() + 10000;
  while (api.getStatus() === "starting" && Date.now() < monitoringDeadline) await new Promise(resolve => setTimeout(resolve, 50));
  assert.ok(["monitoring", "read_backlog"].includes(api.getStatus()),
    `automatic monitor must commit a real local scan; a bounded backlog is expected on resume, actual: ${api.getStatus()}`);
  const hoverDeadline = Date.now() + 12000;
  while (!api.getUI().tooltip.includes("Running sessions") && Date.now() < hoverDeadline) await new Promise(resolve => setTimeout(resolve, 50));
  assert.ok(api.getUI().tooltip.includes("Running sessions"), "the initial hover waits for real persisted status");
  console.log(`P2 extension host: automatic monitor and local SQLite scan passed (${api.getStatus()})`);
  console.log("P0 extension host: automatic local UI activation and real local metadata passed");
  if (process.env.CSN_TEST_EXPECT_SESSION) {
    const database = join(process.env.CSN_TEST_USER_DATA_DIR!, "User", "globalStorage", extension.id.toLowerCase(), "monitor.sqlite");
    const store = new StateStore(extension.extensionPath, resolve(database));
    let matched;
    const discoveryDeadline = Date.now() + 45000;
    while (Date.now() < discoveryDeadline) {
      const overview = await store.activityOverview();
      matched = overview.sources.flatMap(source => source.sessions).find(session => session.sessionId === process.env.CSN_TEST_EXPECT_SESSION);
      if (matched) break;
      await new Promise(resolve => setTimeout(resolve, 500));
    }
    assert.ok(matched, "the independently known running Codex session must appear through automatic monitoring");
    await vscode.commands.executeCommand("codexSessionNotifier.refreshStatus");
    // The snapshot writer also starts an asynchronous UI refresh. A command
    // invoked during that refresh returns before the existing draw completes.
    const uiDeadline = Date.now() + 5000;
    while (!api.getUI().tooltip.includes(matched.sessionName || matched.sessionId.slice(0, 8)) && Date.now() < uiDeadline) {
      await new Promise(resolve => setTimeout(resolve, 50));
    }
    assert.ok(api.getUI().tooltip.includes(matched.sessionName || matched.sessionId.slice(0, 8)));
    assert.ok(!api.getUI().tooltip.includes("$(play-circle)"));
    console.log(`Real running Codex session: automatically detected with backend binding and displayed in the hover (${Date.now() - testStarted}ms from test start)`);
  }
  if (process.env.CSN_TEST_SSH_HOST) {
    const remote = await inspectSource(extension.extensionPath,
      { kind: "ssh", host: validateSshHost(process.env.CSN_TEST_SSH_HOST) }, { ssh: process.env.CSN_TEST_SSH_EXECUTABLE });
    assert.equal(remote.platform, "Linux");
    assert.ok(remote.databaseExists);
    assert.ok(remote.samples.some(sample => sample.hasName));
    console.log("P0 extension host: authorized SSH metadata read from the local UI process passed");
  }
  if (process.env.CSN_TEST_CHANNELS) {
    const database = join(process.env.CSN_TEST_USER_DATA_DIR!, "User", "globalStorage", extension.id.toLowerCase(), "monitor.sqlite");
    const store = new StateStore(extension.extensionPath, database);
    const previous = await store.qqStatus();
    assert.equal(previous.enabled, false);
    assert.ok((await store.configureRoute({ channel: "wechat", option: "" }, previous.routeRevision || 0)).updated);
    await vscode.commands.executeCommand("codexSessionNotifier.refreshStatus");
    const deadline = Date.now() + 5000;
    while (!api.getUI().tooltip.includes("WeChat notifications") && Date.now() < deadline) await new Promise(resolve => setTimeout(resolve, 50));
    assert.ok(api.getUI().tooltip.includes("WeChat notifications **Disabled**"));
    const current = await store.qqStatus();
    assert.ok((await store.configureRoute(previous.route!, current.routeRevision!)).updated);
    assert.equal((await store.qqStatus()).enabled, false);
    console.log("pushplus channels: generic commands and shared route-to-hover update passed; no token or external sends");
  }
  if (process.env.CSN_UI_STARTUP_TRACE) {
    await new Promise(resolve => setTimeout(resolve, 12000));
    if (traceTimer) clearInterval(traceTimer);
    capture();
    writeFileSync(process.env.CSN_UI_STARTUP_TRACE, JSON.stringify(startupTrace, null, 2));
    assert.ok(startupTrace.filter(entry => entry.tooltip).every(entry => !entry.tooltip.includes("Waiting for a fresh source snapshot") && !entry.tooltip.includes("Current window: starting")),
      "startup renders real source snapshots without intermediate empty cards");
    console.log(`Startup UI trace: ${startupTrace.filter(entry => entry.tooltip).length} real hover snapshots; no default empty/starting card`);
  }
  api.stop();
  if (process.env.CSN_UI_PREVIEW_DONE) {
    const storage = join(process.env.CSN_TEST_USER_DATA_DIR!, "User", "globalStorage", extension.id.toLowerCase(), "monitor.sqlite");
    const store = new StateStore(extension.extensionPath, resolve(storage));
    assert.equal((await store.qqStatus()).enabled, false, "UI preview must never enable QQ delivery");
    const otherSources = (await store.activityOverview()).sources.filter(source => source.sourceId !== "f".repeat(64));
    for (const source of otherSources) await store.publishActivity(source.sourceId, "Local PC", [], 0);
    const previewSessions = [
      { sessionId: "preview-1", turnId: "preview-turn-1", sessionName: "Train the recommendation model", nameState: "resolved", startedAt: new Date(Date.now() - 9 * 60000).toISOString() },
      { sessionId: "preview-2", turnId: "preview-turn-2", sessionName: "Fix training checkpoint recovery", nameState: "resolved", startedAt: new Date(Date.now() - 2 * 60000).toISOString() }
    ] as const;
    await store.publishActivity("f".repeat(64), "GPU server", [...previewSessions], 0);
    await vscode.commands.executeCommand("codexSessionNotifier.refreshStatus");
    void vscode.commands.executeCommand("codexSessionNotifier.openMenu");
    console.log("CSN_UI_READY: isolated Quick Pick and running-session hover preview; no external sends");
    const reviewDeadline = Date.now() + 120000;
    let heartbeat = Date.now();
    while (!existsSync(process.env.CSN_UI_PREVIEW_DONE) && Date.now() < reviewDeadline) {
      if (Date.now() - heartbeat > 10000) {
        for (const source of otherSources) await store.publishActivity(source.sourceId, "Local PC", [], 0);
        await store.publishActivity("f".repeat(64), "GPU server", [...previewSessions], 0);
        heartbeat = Date.now();
      }
      await new Promise(resolve => setTimeout(resolve, 250));
    }
    assert.ok(existsSync(process.env.CSN_UI_PREVIEW_DONE), "finish the UI review using its local completion marker");
  }
}
