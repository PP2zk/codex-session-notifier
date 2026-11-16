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

test("local process startup uses the shared process birth rather than query time", async t => {
  const temporary = await mkdtemp(join(tmpdir(), "csn-process-start-"));
  t.after(() => rm(temporary, { recursive: true, force: true }));
  const store = new StateStore(resolve(__dirname, "../.."), join(temporary, "state.sqlite"));
  const result = await store.processStart(process.pid);
  assert.equal(typeof result.startedAt, "number");
  assert.ok(Math.abs(result.startedAt! - (Date.now() / 1000 - process.uptime())) < 2);
  assert.equal((await store.processStart(-1)).startedAt, null);
});

test("a source startup raises the cutoff while live peers preserve its monitoring interval", async t => {
  const temporary = await mkdtemp(join(tmpdir(), "csn-startup-"));
  t.after(() => rm(temporary, { recursive: true, force: true }));
  const database = join(temporary, "state.sqlite"), sourceId = "d".repeat(64);
  const root = resolve(__dirname, "../.."), store = new StateStore(root, database);
  const appStart = Math.floor(Date.now() / 1000) - 30;
  const cursors = { session: { offset: 12345, parser: { activeTurn: "already-running" } } };
  const activity = { session: { fileKey: "a".repeat(64), observedSize: 12345, activityKnown: true, activeTurn: "already-running" } };
  await store.commit(sourceId, 0, { initialized: true, startedAt: appStart - 86400, position: 0, cursors, activity, notificationBacklog: true }, []);
  const observed: Array<import("../src/monitor").ScanState> = [];
  const reader: typeof scanSource = async (_root, _source, state) => {
    if (state.initialized) observed.push(state);
    return { schemaVersion: 2, sourceId, hostname: "host", initialized: true, position: 0,
      cursors: state.cursors, activity: state.activity || {}, events: [], warnings: [], bytesRead: 0 };
  };
  const make = (start: number) => new Monitor(root, { kind: "local" }, database, {}, () => {}, () => {}, 5000, reader,
    undefined, undefined, Promise.resolve(start));
  const first = make(appStart), peer = make(appStart + 5);
  t.after(async () => { await Promise.all([first.dispose(), peer.dispose()]); });
  await first.tick();
  await peer.tick();
  assert.equal(observed.length, 2);
  assert.ok(observed.every(state => state.startedAt === appStart && state.initialized));
  assert.deepEqual(observed[0]?.cursors, cursors);
  assert.deepEqual(observed[0]?.activity, activity);
  assert.equal(observed[0]?.notificationBacklog, true);
  assert.equal((await store.load(sourceId)).state?.startedAt, appStart);
  await Promise.all([first.dispose(), peer.dispose()]);
  const laterStart = appStart + 10, restarted = make(laterStart);
  t.after(() => restarted.dispose());
  await restarted.tick();
  assert.equal((await store.load(sourceId)).state?.startedAt, laterStart);
  assert.deepEqual((await store.load(sourceId)).state?.cursors, cursors);
});

test("disposal waits for a pending presence join before removing its owner", async t => {
  let entered!: () => void, finish!: (value: { startedAt: number; diagnostics: [] }) => void;
  const entering = new Promise<void>(resolve => { entered = resolve; });
  const joining = new Promise<{ startedAt: number; diagnostics: [] }>(resolve => { finish = resolve; });
  const operations: string[] = [];
  t.mock.method(StateStore.prototype, "monitorJoin", () => { operations.push("join"); entered(); return joining; });
  t.mock.method(StateStore.prototype, "monitorLeave", async () => { operations.push("leave"); });
  const reader: typeof scanSource = async () => ({ schemaVersion: 2, sourceId: "a".repeat(64), hostname: "host",
    initialized: true, cursors: {}, position: 0, bytesRead: 0, warnings: [], events: [] });
  const monitor = new Monitor("unused", { kind: "local" }, "unused", {}, () => assert.fail("no notifications"), () => {}, 5000, reader);
  const ticking = monitor.tick();
  await entering;
  const disposing = monitor.dispose();
  assert.deepEqual(operations, ["join"]);
  finish({ startedAt: Math.floor(Date.now() / 1000), diagnostics: [] });
  await Promise.all([ticking, disposing]);
  assert.deepEqual(operations, ["join", "leave"]);
});

test("verification diagnostics cross the reader boundary with validated identities and error codes", async () => {
  const probe = require("../src/probe") as typeof import("../src/probe");
  const original = probe.runReader;
  const failure = { sessionId: "session", sessionName: "Test task", turnIds: ["turn"], reason: "process_image_failed", osError: 5,
    operation: "OpenProcess", processId: 4321 };
  const response = { schemaVersion: 2, sourceId: "a".repeat(64), hostname: "host", initialized: true,
    position: 0, cursors: {}, events: [], warnings: ["execution_source_unverified"], bytesRead: 0,
    activeSessions: [], activityUnknown: 0, activity: {}, verificationFailures: [failure] };
  const read = () => scanSource("unused", { kind: "local" }, { initialized: true, startedAt: 1, position: 0, cursors: {} });
  try {
    probe.runReader = async () => response;
    assert.deepEqual((await read()).verificationFailures, [failure]);
    failure.operation = "untrusted raw path or API text";
    await assert.rejects(read(), /invalid_scan_response/);
    failure.operation = "OpenProcess";
    failure.processId = -1;
    await assert.rejects(read(), /invalid_scan_response/);
    failure.processId = 4321;
    failure.reason = "raw unexpected error text";
    await assert.rejects(read(), /invalid_scan_response/);
    failure.reason = "windows_list_failed";
    failure.osError = -1;
    await assert.rejects(read(), /invalid_scan_response/);
  } finally { probe.runReader = original; }
});

test("optional diagnostic Unicode and invalid detail never reject a valid scan", async () => {
  const probe = require("../src/probe") as typeof import("../src/probe");
  const original = probe.runReader;
  const diagnostic = { kind: "activity", sessionId: "session", sessionName: "\u{1F600}".repeat(96),
    state: "running", reason: "task_started", fileBytes: 10 };
  const response = { schemaVersion: 2, sourceId: "a".repeat(64), hostname: "host", initialized: true,
    position: 0, cursors: {}, events: [], warnings: [], bytesRead: 0,
    activeSessions: [], activityUnknown: 0, activity: {}, diagnostics: [diagnostic] };
  try {
    probe.runReader = async () => response;
    const read = () => scanSource("unused", { kind: "local" }, { initialized: true, startedAt: 1, position: 0, cursors: {} });
    assert.equal((await read()).diagnostics?.[0]?.sessionName, diagnostic.sessionName);
    diagnostic.fileBytes = -1;
    response.diagnostics = [diagnostic];
    assert.deepEqual((await read()).diagnostics, []);
  } finally { probe.runReader = original; }
});

test("two windows and a restarted monitor share atomic, durable completion deduplication", async t => {
  const temporary = await mkdtemp(join(tmpdir(), "csn-monitor-"));
  assert.ok(resolve(temporary).startsWith(resolve(tmpdir()) + require("node:path").sep + "csn-monitor-"));
  t.after(() => rm(temporary, { recursive: true, force: true }));
  const database = join(temporary, "state.sqlite");
  let turn = "turn1";
  const observed: string[] = [];
  const diagnosticTurns: Array<string | null | undefined> = [];
  const reader: typeof scanSource = async (_root, _source, state) => ({ schemaVersion: 2, sourceId: "a".repeat(64), hostname: "test-host",
    initialized: true, cursors: {}, position: 0, bytesRead: 0, warnings: [],
    diagnostics: [{ kind: "activity", sessionId: "session", turnId: turn, state: "running", reason: "task_started" }],
    events: state.initialized ? [{ sessionId: "session", sessionName: "Test", nameSource: "title", nameState: "resolved", turnId: turn,
      completedAt: new Date().toISOString(), evidence: "persisted-completion" }] : [] });
  const create = () => new Monitor(resolve(__dirname, "../.."), { kind: "local" }, database, {},
    (_host, events) => observed.push(...events.map(event => event.turnId)), () => {}, 5000, reader, undefined,
    entries => diagnosticTurns.push(...entries.filter(entry => entry.kind === "activity").map(entry => entry.turnId)));
  const first = create(), second = create();
  t.after(async () => { await Promise.all([first.dispose(), second.dispose()]); });
  await Promise.all([first.tick(), second.tick()]);
  assert.deepEqual(observed, []);
  await Promise.all([first.tick(), second.tick()]);
  assert.deepEqual(observed, ["turn1"]);
  turn = "turn2";
  await Promise.all([first.tick(), second.tick()]);
  assert.deepEqual(observed, ["turn1", "turn2"]);
  assert.deepEqual(diagnosticTurns, ["turn1", "turn2"]);
  await Promise.all([first.dispose(), second.dispose()]);
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

for (const backlog of ["activityBacklog", "notificationBacklog"] as const) {
test(`unfinished ${backlog} continues promptly then returns to normal polling`, async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const temporary = await mkdtemp(join(tmpdir(), "csn-monitor-"));
  t.after(() => rm(temporary, { recursive: true, force: true }));
  let calls = 0;
  let completed = 0;
  const scanStatuses: Array<unknown> = [];
  let finished: (() => void) | undefined;
  const flags: Array<boolean | undefined> = [];
  const reader: typeof scanSource = async (_root, _source, state) => {
    calls++;
    flags.push(state.notificationBacklog);
    return { schemaVersion: 2, sourceId: "c".repeat(64), hostname: "test-host", initialized: true,
      cursors: {}, position: 0, bytesRead: 1, warnings: [], events: [],
      activityBacklog: backlog === "activityBacklog" && calls === 1,
      notificationBacklog: backlog === "notificationBacklog" && calls === 1 };
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
  assert.equal(flags[1], backlog === "notificationBacklog");
  t.mock.timers.tick(4999);
  await immediate();
  assert.equal(calls, 2);
  const third = new Promise<void>(resolve => { finished = resolve; });
  t.mock.timers.tick(1);
  await third;
  while ((monitor as unknown as { running: boolean }).running) await immediate();
  assert.equal(calls, 3);
  assert.equal(flags[2], false);
  assert.equal(scanStatuses.length, 3);
  assert.ok(scanStatuses.every(scan => scan && typeof scan === "object" && "hostname" in scan), "completed scans expose their publication context");
});
}
