import assert from "node:assert/strict";
import { test } from "node:test";
import { completionText, Completion } from "../src/monitor";
import { readerInput } from "../src/probe";
import { Monitor, scanSource, StateStore } from "../src/monitor";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { setImmediate as immediate } from "node:timers/promises";

test("completion preview uses only labels and state, removing control characters", () => {
  const event: Completion = { sessionId: "0123456789", sessionName: "Test\n task\u202e", nameState: "resolved", nameSource: "title",
    turnId: "turn", completedAt: new Date().toISOString(), evidence: "persisted-completion" };
  assert.equal(completionText("machine\r", event), "Codex reply complete | machine | Test task");
  assert.equal(completionText("machine", { ...event, sessionName: null, nameState: "unset" }), "Codex reply complete | machine | Unnamed session · 01234567");
  assert.match(completionText("machine", { ...event, nameState: "unavailable" }), /Name unavailable/);
});

test("large reader state travels in stdin with no user-controlled Python syntax", () => {
  const input = readerInput("print('fixture')", { home: "a'\n__import__('os')", cursors: { s: "x".repeat(20000) } });
  assert.ok(!input.includes("__import__"));
  assert.ok(input.endsWith("print('fixture')"));
  assert.throws(() => readerInput("", { tooLarge: "x".repeat(262144) }), /budget/);
});

test("a missing Codex database produces the specific reader diagnostic", async t => {
  const temporary = await mkdtemp(join(tmpdir(), "csn-monitor-"));
  t.after(() => rm(temporary, { recursive: true, force: true }));
  await assert.rejects(scanSource(resolve(__dirname, "../.."), { kind: "local" },
    { initialized: false, startedAt: Date.now() / 1000, position: 0, cursors: {} }, { home: temporary }), /database_missing/);
});

test("verification diagnostics cross the reader boundary with validated identities and error codes", async () => {
  const probe = require("../src/probe") as typeof import("../src/probe");
  const original = probe.runReader;
  const failure = { sessionId: "session", sessionName: "Test task", turnIds: ["turn"], reason: "windows_list_failed", osError: 5 };
  const response = { schemaVersion: 2, sourceId: "a".repeat(64), hostname: "host", initialized: true,
    position: 0, cursors: {}, events: [], warnings: ["execution_source_unverified"], bytesRead: 0,
    activeSessions: [], activityUnknown: 0, activity: {}, verificationFailures: [failure] };
  const read = () => scanSource("unused", { kind: "local" }, { initialized: true, startedAt: 1, position: 0, cursors: {} });
  try {
    probe.runReader = async () => response;
    assert.deepEqual((await read()).verificationFailures, [failure]);
    failure.reason = "raw unexpected error text";
    await assert.rejects(read(), /invalid_scan_response/);
    failure.reason = "windows_list_failed";
    failure.osError = -1;
    await assert.rejects(read(), /invalid_scan_response/);
  } finally { probe.runReader = original; }
});

test("two windows and a restarted monitor share atomic, durable completion deduplication", async t => {
  const temporary = await mkdtemp(join(tmpdir(), "csn-monitor-"));
  assert.ok(resolve(temporary).startsWith(resolve(tmpdir()) + require("node:path").sep + "csn-monitor-"));
  t.after(() => rm(temporary, { recursive: true, force: true }));
  const database = join(temporary, "state.sqlite");
  let turn = "turn1";
  const observed: string[] = [];
  const reader: typeof scanSource = async (_root, _source, state) => ({ schemaVersion: 2, sourceId: "a".repeat(64), hostname: "test-host",
    initialized: true, cursors: {}, position: 0, bytesRead: 0, warnings: [],
    events: state.initialized ? [{ sessionId: "session", sessionName: "Test", nameSource: "title", nameState: "resolved", turnId: turn,
      completedAt: new Date().toISOString(), evidence: "persisted-completion" }] : [] });
  const create = () => new Monitor(resolve(__dirname, "../.."), { kind: "local" }, database, {},
    (_host, events) => observed.push(...events.map(event => event.turnId)), () => {}, 5000, reader);
  const first = create(), second = create();
  t.after(() => { first.dispose(); second.dispose(); });
  await Promise.all([first.tick(), second.tick()]);
  assert.deepEqual(observed, []);
  await Promise.all([first.tick(), second.tick()]);
  assert.deepEqual(observed, ["turn1"]);
  turn = "turn2";
  await Promise.all([first.tick(), second.tick()]);
  assert.deepEqual(observed, ["turn1", "turn2"]);
  first.dispose(); second.dispose();
  const restarted = create();
  t.after(() => restarted.dispose());
  await restarted.tick();
  assert.deepEqual(observed, ["turn1", "turn2"]);
});

test("connection failures back off and disposal cancels future reads", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  let calls = 0;
  const reader: typeof scanSource = async () => { calls++; throw new Error("probe_timeout"); };
  const statuses: string[] = [];
  const monitor = new Monitor("unused", { kind: "local" }, "unused", {}, () => assert.fail("no events expected"),
    (message, scan) => { statuses.push(message); assert.equal(scan, undefined, "read failures have no activity publication to await"); }, 5000, reader);
  t.after(() => monitor.dispose());
  await monitor.tick();
  assert.equal(calls, 1);
  t.mock.timers.tick(9999);
  await immediate();
  assert.equal(calls, 1);
  t.mock.timers.tick(1);
  await immediate();
  assert.equal(calls, 2);
  t.mock.timers.tick(19999);
  await immediate();
  assert.equal(calls, 2);
  monitor.dispose();
  t.mock.timers.tick(60000);
  await immediate();
  assert.equal(calls, 2);
  assert.deepEqual(statuses, ["probe_timeout", "probe_timeout"]);
});

test("the reader receives committed source identities without persisting them in cursor state", async t => {
  const temporary = await mkdtemp(join(tmpdir(), "csn-known-"));
  t.after(() => rm(temporary, { recursive: true, force: true }));
  const root = resolve(__dirname, "../.."), database = join(temporary, "state.sqlite"), sourceId = "d".repeat(64);
  const store = new StateStore(root, database);
  const state = { initialized: true, startedAt: Date.now() / 1000 - 10, position: 0, cursors: {} };
  await store.commit(sourceId, 0, state, [{ sessionId: "session", turnId: "known", sessionName: "Test",
    nameSource: "title", nameState: "resolved", completedAt: new Date().toISOString(), evidence: "persisted-completion" }]);
  const received: unknown[] = [];
  const reader: typeof scanSource = async (_root, _source, request) => {
    received.push(request.knownCompletions);
    return { schemaVersion: 2, sourceId, hostname: "host", initialized: true, cursors: {}, position: 0, events: [], warnings: [], bytesRead: 0 };
  };
  const monitor = new Monitor(root, { kind: "local" }, database, {}, () => assert.fail("no events expected"), () => {}, 5000, reader);
  t.after(() => monitor.dispose());
  await monitor.tick();
  assert.deepEqual(received.at(-1), [{ sessionId: "session", turnId: "known" }]);
  const saved = await store.load(sourceId);
  assert.equal(saved.state?.knownCompletions, undefined);
});

test("unfinished activity discovery continues promptly then returns to normal polling", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const temporary = await mkdtemp(join(tmpdir(), "csn-monitor-"));
  t.after(() => rm(temporary, { recursive: true, force: true }));
  let calls = 0;
  let completed = 0;
  const scanStatuses: Array<unknown> = [];
  let finished: (() => void) | undefined;
  const reader: typeof scanSource = async () => {
    calls++;
    return { schemaVersion: 2, sourceId: "c".repeat(64), hostname: "test-host", initialized: true,
      cursors: {}, position: 0, bytesRead: 1, warnings: [], events: [], activityBacklog: calls === 1 };
  };
  const monitor = new Monitor(resolve(__dirname, "../.."), { kind: "local" }, join(temporary, "state.sqlite"), {},
    () => {}, (_message, scan) => { scanStatuses.push(scan); }, 5000, reader, async () => { completed++; finished?.(); });
  t.after(() => monitor.dispose());
  await monitor.tick();
  assert.equal(calls, 1);
  t.mock.timers.tick(249);
  await immediate();
  assert.equal(calls, 1);
  const second = new Promise<void>(resolve => { finished = resolve; });
  t.mock.timers.tick(1);
  await second;
  // Finish lease release and timer scheduling before advancing mocked time.
  while ((monitor as unknown as { running: boolean }).running) await immediate();
  assert.equal(completed, 2);
  t.mock.timers.tick(4999);
  await immediate();
  assert.equal(calls, 2);
  const third = new Promise<void>(resolve => { finished = resolve; });
  t.mock.timers.tick(1);
  await third;
  while ((monitor as unknown as { running: boolean }).running) await immediate();
  assert.equal(calls, 3);
  assert.equal(scanStatuses.length, 3);
  assert.ok(scanStatuses.every(scan => scan && typeof scan === "object" && "hostname" in scan), "completed scans expose their publication context");
});
