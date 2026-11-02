import assert from "node:assert/strict";
import { test } from "node:test";
import type * as vscode from "vscode";
import { QqStatus, StateStore } from "../src/monitor";
import { DEFAULT_ROUTE, NotificationRoute } from "../src/channels";
import { PushplusSender } from "../src/qq";
import { QQ_TOKEN_KEY } from "../src/delivery";

type Choice = { label: string; info?: { id: string }; send?: boolean; enable?: boolean };
let pick: (items: Choice[]) => unknown = () => undefined;
let input: string | undefined;
const messages: string[] = [];
const commands = new Map<string, () => Promise<unknown>>();
const loader = require("node:module") as { _load: (id: string, ...args: unknown[]) => unknown };
const original = loader._load;
let register: typeof import("../src/qq-commands").registerQQCommands;
try {
  loader._load = function(this: unknown, id: string, ...args: unknown[]) {
    if (id === "vscode") return { window: {
      showQuickPick: async (items: Choice[]) => pick(items), showInputBox: async () => input,
      showInformationMessage: (text: string) => { messages.push(text); }, showErrorMessage: (text: string) => { messages.push(text); }
    }, commands: { registerCommand: (id: string, run: () => Promise<unknown>) => { commands.set(id, run); return { dispose() {} }; } } };
    return original.call(this, id, ...args);
  };
  register = require("../src/qq-commands").registerQQCommands;
} finally { loader._load = original; }

function fixture(route: NotificationRoute = DEFAULT_ROUTE,
  runInteraction: (action: () => Promise<void>) => Promise<void> = action => action()) {
  pick = () => undefined; input = undefined; messages.length = 0; commands.clear();
  let state: QqStatus = { enabled: true, paused: null, counts: {}, route: { ...route }, routeRevision: 0 };
  const secrets = new Map([[QQ_TOKEN_KEY, "a".repeat(32)]]), savedState = new Map();
  let changes = 0;
  const store = { qqStatus: async () => structuredClone(state),
    qqDisable: async () => { state.enabled = false; },
    qqEnable: async (revision: number) => revision === state.routeRevision ? (state.enabled = true, { enabled: true }) : { updated: false },
    configureRoute: async (next: NotificationRoute, revision: number) => {
      if (revision !== state.routeRevision) return { updated: false };
      state = { ...state, route: next, enabled: false, routeRevision: revision + 1 }; return { updated: true };
    } } as unknown as StateStore;
  const context = { subscriptions: [], secrets: { get: async (key: string) => secrets.get(key),
    store: async (key: string, value: string) => { secrets.set(key, value); }, delete: async (key: string) => { secrets.delete(key); },
    onDidChange: () => ({ dispose() {} }) }, globalState: { update: async (key: string, value: unknown) => { savedState.set(key, value); } } } as unknown as vscode.ExtensionContext;
  const getSender = register(context, () => store, line => messages.push(line), () => { changes++; }, runInteraction);
  const run = (id: string) => commands.get(`codexSessionNotifier.${id}`)!();
  return { get state() { return state; }, secrets, savedState, get changes() { return changes; }, run, getSender };
}

test("existing QQ token is reused and selection cancellation has no effects", async () => {
  const f = fixture();
  assert.ok(await f.getSender(DEFAULT_ROUTE));
  assert.equal(f.secrets.size, 1);
  await f.run("selectChannel");
  assert.equal(f.state.enabled, true);
  assert.equal(f.changes, 0);
  assert.deepEqual([...commands.keys()].map(id => id.split(".")[1]!).sort(),
    ["configureToken", "selectChannel", "testNotification", "removeToken", "enableNotifications", "disableNotifications", "deliveryStatus"].sort());
});

test("notification toggles leave launcher focus before mutating notification state", async () => {
  let complete!: () => void;
  const gate = new Promise<void>(resolve => { complete = resolve; });
  const previous: boolean[] = [];
  const f = fixture(DEFAULT_ROUTE, async action => {
    previous.push(f.state.enabled);
    if (previous.length === 1) await gate;
    await action();
  });
  const pending = f.run("disableNotifications");
  assert.equal(f.state.enabled, true, "the state change waits for focus restoration");
  complete();
  await pending;
  assert.equal(f.state.enabled, false);
  await f.run("enableNotifications");
  assert.deepEqual(previous, [true, false]);
  assert.equal(f.state.enabled, true);
});

test("every notification command uses one interaction boundary across all prompts", async t => {
  let active = false, entered = 0, completed = 0, prompts = 0;
  const f = fixture(DEFAULT_ROUTE, async action => {
    assert.equal(active, false);
    active = true; entered++;
    try { await action(); } finally { active = false; completed++; }
  });
  t.mock.method(PushplusSender.prototype, "sendTest", async () => {
    assert.ok(active, "the boundary spans the asynchronous send"); return "receipt";
  });
  const ids = [...commands.keys()].map(id => id.split(".")[1]!);
  assert.equal(ids.length, 7);
  for (const id of ids) {
    f.secrets.set(QQ_TOKEN_KEY, "a".repeat(32));
    input = "b".repeat(32);
    pick = items => {
      assert.ok(active, "all nested prompts stay inside the command boundary"); prompts++;
      return items.find(row => row.info?.id === "wechat" || row.send || row.enable);
    };
    await f.run(id);
    assert.equal(active, false);
  }
  assert.equal(entered, ids.length);
  assert.equal(completed, ids.length);
  assert.ok(prompts >= 2, "channel selection and test confirmation were exercised");
  assert.equal(f.savedState.size, 0, "commands no longer maintain unused receipt feedback");
});

test("selecting WeChat is shared, disables delivery and sends no request", async () => {
  const f = fixture(); input = ""; pick = items => items.find(row => row.info?.id === "wechat");
  await f.run("selectChannel");
  assert.deepEqual(f.state.route, { channel: "wechat", option: "" });
  assert.equal(f.state.enabled, false);
  assert.equal(f.changes, 1);
  assert.equal(f.secrets.get(QQ_TOKEN_KEY), "a".repeat(32));
  assert.ok(!messages.join().includes("request_accepted"));
});

test("new sender cache follows route as well as token, and token replacement stays secure and disabled", async () => {
  const f = fixture();
  const qq = await f.getSender(DEFAULT_ROUTE), wechat = await f.getSender({ channel: "wechat", option: "" });
  assert.notEqual(qq, wechat);
  input = "b".repeat(32); await f.run("configureToken");
  assert.equal(f.state.enabled, false);
  assert.equal(f.secrets.get(QQ_TOKEN_KEY), input);
  assert.ok(!messages.join().includes(input));
});

test("paid automatic notifications require explicit Quick Pick confirmation", async () => {
  const f = fixture({ channel: "sms", option: "" });
  f.state.enabled = false;
  await f.run("enableNotifications"); assert.equal(f.state.enabled, false);
  pick = items => items.find(row => row.enable);
  await f.run("enableNotifications"); assert.equal(f.state.enabled, true);
});

for (const channel of ["qq", "wechat"] as const) {
  test(`testNotification via ${channel} ends after submission without receipt questions or saved feedback`, async t => {
    const f = fixture({ channel, option: "" });
    let sends = 0, prompts = 0;
    t.mock.method(PushplusSender.prototype, "sendTest", async () => { sends++; return "receipt"; });
    pick = items => {
      assert.equal(++prompts, 1, "only the pre-send review is shown");
      return items.find(row => row.send);
    };
    await f.run("testNotification");
    assert.equal(sends, 1);
    assert.equal(prompts, 1);
    assert.equal(f.savedState.size, 0);
    assert.ok(messages.join().includes("request_accepted; recipient delivery unconfirmed"));
    assert.ok(messages.includes(`Test notification submitted to pushplus. Check ${channel === "qq" ? "QQ" : "WeChat"} for delivery.`));
  });
}

test("cancelling pre-send review sends nothing and ends without another prompt", async t => {
  const f = fixture(); let sends = 0, prompts = 0;
  t.mock.method(PushplusSender.prototype, "sendTest", async () => { sends++; return "receipt"; });
  pick = () => { prompts++; return undefined; };
  await f.run("testNotification");
  assert.equal(sends, 0);
  assert.equal(prompts, 1);
  assert.equal(f.savedState.size, 0);
  assert.equal(messages.length, 0);
});

test("failed test submission reports the error without a receipt question or success message", async t => {
  const f = fixture(); let prompts = 0;
  t.mock.method(PushplusSender.prototype, "sendTest", async () => { throw new Error("qq_timeout"); });
  pick = items => { prompts++; return items.find(row => row.send); };
  await f.run("testNotification");
  assert.equal(prompts, 1);
  assert.equal(f.savedState.size, 0);
  assert.ok(messages.join().includes("qq_timeout"));
  assert.ok(!messages.join().includes("submitted to pushplus"));
});

test("a route change during test confirmation prevents sending the previously reviewed route", async t => {
  const f = fixture(); let sends = 0;
  t.mock.method(PushplusSender.prototype, "sendTest", async () => { sends++; return "receipt"; });
  pick = items => { f.state.routeRevision = 1; return items.find(row => row.send); };
  await f.run("testNotification");
  assert.equal(sends, 0);
  assert.ok(messages.join().includes("configuration_changed"));
});
