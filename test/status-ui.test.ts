import assert from "node:assert/strict";
import { test } from "node:test";
import { setImmediate as immediate } from "node:timers/promises";
import type { ActivitySource, QqStatus, StateStore } from "../src/monitor";

class Markdown {
  isTrusted: unknown;
  constructor(public value: string) {}
}

class Item {
  name = "";
  command = "";
  text = "";
  accessibilityInformation?: { label: string };
  private value?: Markdown;
  shown = 0;
  disposed = false;
  hovers: string[] = [];
  get tooltip() { return this.value; }
  set tooltip(value: Markdown | undefined) {
    assert.equal(this.disposed, false, "disposed items cannot be redrawn");
    this.value = value;
    if (value) this.hovers.push(value.value);
  }
  show() { this.shown++; }
  dispose() { this.disposed = true; }
}

// A small VS Code API double for this isolated Node test worker. Production
// uses only the public VS Code API; no extra test runtime ships in the VSIX.
const moduleLoader = require("node:module") as { _load: (id: string, ...args: unknown[]) => unknown };
const originalLoad = moduleLoader._load;
const menuLabels: string[][] = [];
let chooseItem: (items: Array<{ command?: string }>) => Promise<{ command?: string } | undefined> = async () => undefined;
let runCommand: (command: string) => Promise<void> = async () => {};
let StatusUI: typeof import("../src/status-ui").StatusUI;
try {
  moduleLoader._load = function (this: unknown, id: string, ...args: unknown[]) {
    if (id === "vscode") return {
      window: { createStatusBarItem: () => new Item(), showQuickPick: async (items: { label: string; command?: string }[]) => {
        menuLabels.push(items.map(item => item.label)); return chooseItem(items);
      } },
      commands: { executeCommand: (command: string) => runCommand(command) },
      StatusBarAlignment: { Right: 2 }, QuickPickItemKind: { Separator: -1 }, MarkdownString: Markdown
    };
    return originalLoad.call(this, id, ...args);
  };
  StatusUI = require("../src/status-ui").StatusUI;
} finally { moduleLoader._load = originalLoad; }

type Overview = { qq: QqStatus; sources: ActivitySource[] };
function deferred<T>() {
  let resolve!: (value: T) => void;
  const promise = new Promise<T>(complete => { resolve = complete; });
  return { promise, resolve };
}
const now = Date.parse("2026-11-02T01:00:00Z");
const ready: Overview = { qq: { enabled: true, paused: null, counts: {} }, sources: [{
  sourceId: "a".repeat(64), machine: "GPU server", runningCount: 1, unknown: 0, updatedAt: now / 1000,
  sessions: [{ sessionId: "s1", turnId: "t1", sessionName: "Train model", nameState: "resolved",
    startedAt: new Date(now - 120000).toISOString() }]
}] };
const store = (read: () => Promise<Overview>) => () => ({ activityOverview: read } as unknown as StateStore);

test("the first hover contains real saved settings and sessions, without default-state flashes", async t => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval", "Date"], now });
  const gate = deferred<Overview>();
  const ui = new StatusUI(store(() => gate.promise), async () => {});
  const item = ui.item as unknown as Item;
  t.after(() => ui.dispose());
  ui.setMonitor("monitoring");
  t.mock.timers.tick(500);
  assert.equal(item.hovers.length, 0, "a pending read must not render fake Disabled/empty state");
  gate.resolve(ready);
  await ui.refresh();
  t.mock.timers.tick(100);
  t.mock.timers.tick(2000);
  assert.equal(item.hovers.length, 1);
  assert.ok(item.hovers[0]!.includes("**Enabled**"));
  assert.ok(item.hovers[0]!.includes("Train model"));
  assert.ok(!item.hovers[0]!.includes("No confirmed running sessions"));
  assert.equal(item.shown, 1);
  ui.setMonitor("read_backlog");
  await ui.refresh();
  t.mock.timers.tick(100);
  assert.equal(item.hovers.length, 1, "the same visible snapshot must not be reassigned");
});

test("startup waits for source initialization and batches status with its published snapshot", async t => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval", "Date"], now });
  let overview: Overview = { qq: ready.qq, sources: [] };
  const ui = new StatusUI(store(async () => overview), async () => {});
  const item = ui.item as unknown as Item;
  t.after(() => ui.dispose());
  await ui.refresh();
  t.mock.timers.tick(100);
  assert.equal(item.hovers.length, 0, "starting is not a final empty session snapshot");
  ui.setMonitor("monitored_in_another_window");
  t.mock.timers.tick(100);
  assert.equal(item.hovers.length, 0, "shared-source startup waits for its owner's first snapshot");
  ui.setMonitor("monitoring");
  overview = ready;
  await ui.refresh();
  t.mock.timers.tick(100);
  t.mock.timers.tick(2000);
  assert.equal(item.hovers.length, 1);
  assert.ok(item.hovers[0]!.includes("Train model"));
  assert.ok(item.hovers[0]!.includes("Current window: Monitoring"));
});

test("overlapping refresh callers wait for the newer snapshot without an intermediate redraw", async t => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval", "Date"], now });
  const first = deferred<Overview>(), second = deferred<Overview>();
  let reads = 0;
  const ui = new StatusUI(store(() => ++reads === 1 ? first.promise : second.promise), async () => {});
  const item = ui.item as unknown as Item;
  t.after(() => ui.dispose());
  ui.setMonitor("monitoring");
  const a = ui.refresh(), b = ui.refresh();
  assert.equal(a, b);
  first.resolve({ qq: { enabled: false, paused: null, counts: {} }, sources: [] });
  await immediate();
  t.mock.timers.tick(100);
  assert.equal(reads, 2);
  assert.equal(item.hovers.length, 0);
  second.resolve(ready);
  await Promise.all([a, b]);
  t.mock.timers.tick(100);
  t.mock.timers.tick(2000);
  assert.equal(item.hovers.length, 1);
  assert.ok(item.hovers[0]!.includes("**Enabled**"));
});

test("read errors remain visible, recovery works, and disposal cancels a pending redraw", async t => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval", "Date"], now });
  let fail = true;
  const ui = new StatusUI(store(async () => { if (fail) throw new Error("fixture"); return ready; }), async () => {});
  const item = ui.item as unknown as Item;
  await ui.refresh();
  t.mock.timers.tick(100);
  assert.ok(item.hovers[0]!.includes("Session status is unavailable"));
  assert.ok(!item.hovers[0]!.includes("Disabled"));
  fail = false;
  ui.setMonitor("monitoring");
  await ui.refresh();
  t.mock.timers.tick(100);
  assert.ok(item.hovers[1]!.includes("Train model"));
  ui.setMonitor("probe_timeout");
  ui.dispose();
  t.mock.timers.tick(10000);
  assert.equal(item.hovers.length, 2);
});

test("the menu uses persisted settings during startup and updates toggles without waiting for source discovery", async t => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval", "Date"], now });
  menuLabels.length = 0;
  let overview = ready;
  const ui = new StatusUI(store(async () => overview), async () => {});
  const item = ui.item as unknown as Item;
  t.after(() => ui.dispose());
  await ui.showMenu();
  assert.ok(menuLabels[0]![0]!.includes("Disable notifications"));
  assert.equal(item.hovers.length, 0);
  overview = { ...ready, qq: { ...ready.qq, enabled: false } };
  await ui.showMenu();
  assert.ok(menuLabels[1]![0]!.includes("Enable notifications"));
  ui.setMonitor("monitoring");
  t.mock.timers.tick(100);
  t.mock.timers.tick(2000);
  assert.ok(item.hovers[0]!.includes("**Disabled**"));
  assert.ok(item.hovers[0]!.includes("Train model"));
});

test("initial discovery and transient diagnostics settle into one complete first card", async t => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval", "Date"], now });
  let overview: Overview = { qq: ready.qq, sources: [{ ...ready.sources[0]!, sessions: [], runningCount: 0, unknown: 3 }] };
  const ui = new StatusUI(store(async () => overview), async () => {});
  const item = ui.item as unknown as Item;
  t.after(() => ui.dispose());
  ui.setMonitor("monitoring");
  await ui.refresh();
  t.mock.timers.tick(100);
  assert.equal(item.hovers.length, 0);
  overview = ready;
  ui.setMonitor("oversized_record_skipped");
  await ui.refresh();
  t.mock.timers.tick(1000);
  ui.setMonitor("monitoring");
  t.mock.timers.tick(1000);
  assert.equal(item.hovers.length, 0);
  t.mock.timers.tick(2000);
  assert.equal(item.hovers.length, 1);
  assert.ok(item.hovers[0]!.includes("Train model"));
  assert.ok(item.hovers[0]!.includes("Current window: Monitoring"));
  assert.ok(!item.hovers[0]!.includes("still being checked"));
});

test("continuous startup changes cannot postpone the first card indefinitely", async t => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval", "Date"], now });
  let generation = 0;
  const ui = new StatusUI(store(async () => ({ ...ready, sources: [{ ...ready.sources[0]!,
    sessions: [{ ...ready.sources[0]!.sessions[0]!, sessionName: `Latest task ${generation}` }] }] })), async () => {});
  const item = ui.item as unknown as Item;
  t.after(() => ui.dispose());
  ui.setMonitor("monitoring");
  await ui.refresh();
  t.mock.timers.tick(100);
  while (!item.hovers.length && Date.now() - now < 9000) {
    generation++;
    await ui.refresh();
    t.mock.timers.tick(500);
    await immediate();
  }
  assert.equal(item.hovers.length, 1);
  assert.ok(Date.now() - now <= 8500, "the initial settling window has a bounded maximum");
  assert.ok(item.hovers[0]!.includes(`Latest task ${generation}`));
});


test("reload checks old snapshots neutrally but a startup timeout still exposes delay", async t => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval", "Date"], now });
  const old = { ...ready, sources: [{ ...ready.sources[0]!, updatedAt: now / 1000 - 40 }] };
  const ui = new StatusUI(store(async () => old), async () => {});
  t.after(() => ui.dispose());
  ui.setMonitor("monitored_in_another_window");
  await ui.refresh();
  t.mock.timers.tick(100);
  t.mock.timers.tick(2000);
  assert.ok(ui.snapshot().tooltip.includes("Checking source status"));
  assert.ok(!ui.snapshot().tooltip.includes("Source updates delayed"));
  t.mock.timers.tick(30000);
  await ui.refresh();
  t.mock.timers.tick(100);
  assert.ok(ui.snapshot().tooltip.includes("Source updates delayed"));
  assert.ok(!ui.snapshot().tooltip.includes("Checking source status"));
});

const menuCommands = ["enableNotifications", "disableNotifications", "selectChannel", "configureToken",
  "testNotification", "deliveryStatus", "removeToken", "refreshStatus", "showStatus", "openSettings"];
for (const id of menuCommands) test(`menu ${id} transfers focus before the picker and spans the whole action`, async t => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval", "Date"], now });
  let overview: Overview = ready;
  const selection = deferred<{ command?: string } | undefined>(), action = deferred<void>();
  const calls: string[] = [];
  let pickerCount = 0;
  const execute = async () => {
    assert.equal(ui.snapshot().tooltip, "");
    // The selected command has its own registration boundary. It must neither
    // restore the tooltip nor refocus while nested inside the menu boundary.
    await ui.runInteraction(async () => {
      calls.push(id);
      await action.promise;
      overview = { ...overview, qq: { ...overview.qq, enabled: false } };
    });
  };
  const ui = new StatusUI(store(async () => overview), execute);
  const item = ui.item as unknown as Item;
  t.after(() => { chooseItem = async () => undefined; runCommand = async () => {}; ui.dispose(); });
  ui.setMonitor("monitoring"); await ui.refresh();
  t.mock.timers.tick(100); t.mock.timers.tick(2000);
  assert.ok(item.tooltip);
  runCommand = async command => {
    assert.equal(ui.snapshot().tooltip, "");
    if (command === "workbench.action.focusActiveEditorGroup") calls.push("focus");
    else { assert.equal(command, `codexSessionNotifier.${id}`); await execute(); }
  };
  chooseItem = async () => {
    assert.deepEqual(calls, ["focus"], "the picker return target is set before it opens");
    pickerCount++; return selection.promise;
  };
  const menu = ui.showMenu();
  await immediate();
  await ui.showMenu();
  assert.equal(pickerCount, 1);
  overview = { ...overview, sources: [{ ...ready.sources[0]!,
    sessions: [{ ...ready.sources[0]!.sessions[0]!, sessionName: "Latest session" }] }] };
  await ui.refresh(); t.mock.timers.tick(100);
  assert.equal(ui.snapshot().tooltip, "", "background updates cannot restore an active interaction's hover");
  selection.resolve({ command: `codexSessionNotifier.${id}` });
  await immediate();
  assert.deepEqual(calls, ["focus", id]);
  assert.equal(ui.snapshot().tooltip, "", "nested configuration/send steps cannot restore it early");
  action.resolve(undefined); await menu;
  assert.ok(ui.snapshot().tooltip.includes("**Disabled**"));
  assert.ok(ui.snapshot().tooltip.includes("Latest session"));
  assert.deepEqual(calls, ["focus", id], "settings and logs keep their destination focus");
  const count = item.hovers.length;
  await ui.refresh(); t.mock.timers.tick(100);
  assert.equal(item.hovers.length, count);
});

test("menu cancellation and command errors restore the latest hover with a safe return target", async t => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval", "Date"], now });
  const ui = new StatusUI(store(async () => ready), async () => {});
  const calls: string[] = [];
  t.after(() => { chooseItem = async () => undefined; runCommand = async () => {}; ui.dispose(); });
  ui.setMonitor("monitoring"); await ui.refresh();
  t.mock.timers.tick(100); t.mock.timers.tick(2000);
  runCommand = async command => {
    calls.push(command);
    if (command !== "workbench.action.focusActiveEditorGroup") throw new Error("command_failed");
  };
  await ui.showMenu();
  assert.deepEqual(calls, ["workbench.action.focusActiveEditorGroup"]);
  assert.ok(ui.snapshot().tooltip.includes("Train model"));
  chooseItem = async () => ({ command: "codexSessionNotifier.showStatus" });
  await assert.rejects(ui.showMenu(), /command_failed/);
  assert.deepEqual(calls.slice(1), ["workbench.action.focusActiveEditorGroup", "codexSessionNotifier.showStatus"]);
  assert.ok(ui.snapshot().tooltip.includes("Train model"));
  chooseItem = async () => undefined;
  await ui.showMenu();
  assert.ok(ui.snapshot().tooltip);
});

test("closing a menu after disposal cannot restore its tooltip", async t => {
  const ui = new StatusUI(store(async () => ready), async () => {});
  const selection = deferred<{ command?: string } | undefined>();
  chooseItem = async () => selection.promise;
  t.after(() => { chooseItem = async () => undefined; ui.dispose(); });
  const menu = ui.showMenu(); await immediate();
  ui.dispose(); selection.resolve(undefined); await menu;
  assert.equal(ui.item.tooltip, undefined);
});

test("direct commands and overlapping interactions keep the hover suppressed until the last action finishes", async t => {
  t.mock.timers.enable({ apis: ["setTimeout", "setInterval", "Date"], now });
  const ui = new StatusUI(store(async () => ready), async () => {});
  const focus = deferred<void>(), firstAction = deferred<void>(), secondAction = deferred<void>();
  let focused = 0;
  t.after(() => { runCommand = async () => {}; ui.dispose(); });
  ui.setMonitor("monitoring"); await ui.refresh();
  t.mock.timers.tick(100); t.mock.timers.tick(2000);
  runCommand = async command => {
    assert.equal(command, "workbench.action.focusActiveEditorGroup");
    assert.equal(ui.snapshot().tooltip, ""); focused++; await focus.promise;
  };
  const first = ui.runInteraction(async () => { await firstAction.promise; return 42; });
  let secondStarted = false;
  const second = ui.runInteraction(async () => { secondStarted = true; await secondAction.promise; });
  await ui.refresh(); t.mock.timers.tick(100);
  assert.equal(ui.snapshot().tooltip, "");
  assert.equal(secondStarted, false, "overlapping commands also wait for the common focus transition");
  focus.resolve(undefined); await immediate();
  assert.equal(secondStarted, true);
  firstAction.resolve(undefined); assert.equal(await first, 42);
  assert.equal(ui.snapshot().tooltip, "", "an outstanding interaction still owns suppression");
  secondAction.resolve(undefined); await second;
  assert.equal(focused, 1);
  assert.ok(ui.snapshot().tooltip.includes("Train model"));
  await assert.rejects(ui.runInteraction(async () => { throw new Error("fixture"); }), /fixture/);
  assert.ok(ui.snapshot().tooltip.includes("Train model"), "errors release suppression");
});
