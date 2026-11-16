import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm, mkdir, readFile, writeFile } from "node:fs/promises";
import { execFile } from "node:child_process";
import { promisify } from "node:util";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { Completion, StateStore } from "../src/monitor";
import { QqDelivery, qqCompletionMessage, DeliveryGate, sourceDeliveryGate } from "../src/delivery";
import { PushplusSender } from "../src/qq";

const allow: DeliveryGate = async () => ({ acquired: true, release: async () => {} });

const sample = (): Completion => ({ sessionId: "0123456789", sessionName: "Test task", nameState: "resolved", nameSource: "title",
  turnId: "turn1", completedAt: new Date(Date.now() + 1000).toISOString(), evidence: "persisted-completion" });

test("QQ titles are session names; English details retain machine, full name and distinct completion time", () => {
  const event = { ...sample(), sessionName: "\u68c0\u67e5\u4efb\u52a1\n\u202e", prompt: "PRIVATE", reply: "PRIVATE" };
  const result = qqCompletionMessage("GPU\rhost", event);
  assert.equal(result.title, "\u68c0\u67e5\u4efb\u52a1");
  assert.equal(result.content, `Codex reply complete | Machine: GPU host | Session: \u68c0\u67e5\u4efb\u52a1 | Completed: ${event.completedAt}`);
  assert.ok(!JSON.stringify(result).includes("PRIVATE"));
  assert.match(qqCompletionMessage("host", { ...sample(), sessionName: null, nameState: "unset" }).title, /^Unnamed session/);
  const long = qqCompletionMessage("host", { ...sample(), sessionName: "a".repeat(500) });
  assert.ok(long.title.length <= 100);
  assert.ok(long.content.includes("a".repeat(500)));
});

for (const outcome of ["accepted", "unknown"] as const) {
  test(`real local outbox permits one ${outcome} send across windows and restart`, async t => {
    const dir = await mkdtemp(join(tmpdir(), "csn-delivery-"));
    t.after(() => rm(dir, { recursive: true, force: true }));
    const store = new StateStore(resolve(__dirname, "../.."), join(dir, "state.sqlite"));
    const sourceId = "a".repeat(64);
    await store.qqEnable();
    const event = sample();
    await store.commit(sourceId, 0, { initialized: true, startedAt: Date.now() / 1000, position: 0, cursors: {} }, [event]);
    let calls = 0;
    const sender = async () => ({ send: async (title: string, content: string) => {
      calls++;
      assert.equal(title, event.sessionName);
      assert.ok(content.includes("Machine: machine"));
      if (outcome === "unknown") throw new Error("PRIVATE NETWORK ERROR");
      return "receipt";
    } });
    const logs: string[] = [];
    const create = () => new QqDelivery(store, sender, line => logs.push(line), allow);
    const drain = (delivery: QqDelivery) => delivery.drain(sourceId, "machine", outcome === "accepted" ? [event] : [], "",
      async item => { assert.deepEqual(item, { sessionId: event.sessionId, turnId: event.turnId, completedAt: event.completedAt }); return { host: "machine", event }; }, new AbortController().signal);
    await Promise.all([drain(create()), drain(create())]);
    await drain(create());
    assert.equal(calls, 1);
    assert.equal((await store.qqStatus()).counts[outcome], 1);
    assert.ok(!logs.join().includes("PRIVATE"));
  });
}

test("a route switch after claim blocks the old prepared request before HTTP begins", async t => {
  const dir = await mkdtemp(join(tmpdir(), "csn-route-delivery-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const store = new StateStore(resolve(__dirname, "../.."), join(dir, "state.sqlite"));
  await store.configureRoute({ channel: "wechat", option: "" }, 0);
  await store.qqEnable(1);
  const event = sample(), sourceId = "a".repeat(64);
  await store.commit(sourceId, 0, { initialized: true, startedAt: Date.now() / 1000, position: 0, cursors: {} }, [event]);
  let sends = 0;
  const delivery = new QqDelivery(store, async route => {
    assert.equal(route.channel, "wechat");
    return { send: async () => { sends++; return "receipt"; } };
  }, () => {}, allow);
  await delivery.drain(sourceId, "machine", [], "", async () => {
    await store.configureRoute({ channel: "mail", option: "" }, 1);
    await store.qqEnable(2);
    return { host: "machine", event };
  }, new AbortController().signal);
  assert.equal(sends, 0);
  assert.equal((await store.qqStatus()).route?.channel, "mail");
  assert.equal((await store.qqStatus()).counts.accepted, undefined);
});

test("app startup cutoff prevents a claimed old completion from reaching HTTP", async t => {
  const dir = await mkdtemp(join(tmpdir(), "csn-startup-delivery-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const root = resolve(__dirname, "../.."), store = new StateStore(root, join(dir, "state.sqlite"));
  await store.qqEnable();
  const event = { ...sample(), completedAt: new Date().toISOString() }, sourceId = "a".repeat(64);
  const state = { initialized: true as const, startedAt: Date.now() / 1000 - 600, position: 0, cursors: {} };
  await store.commit(sourceId, 0, state, [event]);
  let sends = 0, releases = 0;
  const delivery = new QqDelivery(store, async () => ({ send: async () => { sends++; return "receipt"; } }), () => {},
    async () => ({ acquired: true, release: async () => { releases++; } }));
  await delivery.drain(sourceId, "machine", [], "", async () => {
    await store.commit(sourceId, 1, { ...state, startedAt: Date.now() / 1000 }, []);
    return { host: "machine", event };
  }, new AbortController().signal);
  assert.equal(sends, 0);
  assert.equal(releases, 1);
  assert.equal((await store.qqStatus()).counts.preparing, undefined);
  assert.equal((await store.qqClaim(sourceId, "b".repeat(36))).item, null);
});

for (const outcome of ["accepted", "unknown"] as const) test(`independent client databases share one ${outcome} attempt through the real reader`, async t => {
  const dir = await mkdtemp(join(tmpdir(), "csn-clients-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const root = resolve(__dirname, "../.."), fixture = join(dir, "reader"), home = join(dir, "backend-user");
  await mkdir(join(fixture, "resources"), { recursive: true });
  await mkdir(home);
  // Redirect only the isolated test subprocess's user home. Production has no
  // client-supplied coordination path and uses the same local/SSH Python source.
  const script = await readFile(join(root, "resources", "probe.py"), "utf8");
  const prefix = `import pathlib\npathlib.Path.home = classmethod(lambda cls: cls(${JSON.stringify(home)}))\n`;
  await writeFile(join(fixture, "resources", "probe.py"), prefix + script);
  const python = process.platform === "win32" ? "python" : "python3";
  const code = `import runpy\np = runpy.run_path(${JSON.stringify(join(fixture, "resources", "probe.py"))})\nprint(p['source_identity'](p['Path'](${JSON.stringify(home)})))`;
  const sourceId = (await promisify(execFile)(python, ["-I", "-c", code])).stdout.trim();
  const stores = [0, 1, 2].map(i => new StateStore(root, join(dir, `client${i}.sqlite`)));
  await Promise.all(stores.map(store => store.qqEnable()));
  const event = sample();
  await Promise.all(stores.map(store => store.commit(sourceId, 0,
    { initialized: true, startedAt: Date.now() / 1000, position: 0, cursors: {} }, [event])));
  let sends = 0;
  const sender = new PushplusSender("fake-test-token-00000000");
  sender.send = async () => { sends++; if (outcome === "unknown") throw new Error("private timeout"); return "receipt"; };
  const gate = sourceDeliveryGate(fixture, { kind: "local" }, { home });
  const drain = (store: StateStore) => new QqDelivery(store, async () => sender, () => {}, gate)
    .drain(sourceId, "backend", [event], "", async () => ({ host: "backend", event }), new AbortController().signal);
  await Promise.all(stores.slice(0, 2).map(drain));
  await drain(stores[2]!); // A later client/restart cannot resend an existing attempt.
  assert.equal(sends, 1);
  const counts = await Promise.all(stores.map(async store => (await store.qqStatus()).counts));
  assert.equal(counts.reduce((sum, item) => sum + (item[outcome] || 0), 0), 1);
  assert.equal(counts.reduce((sum, item) => sum + (item.suppressed || 0), 0), 2);
});

test("coordination failure never falls back to sending and leaves the local item retryable", async t => {
  const dir = await mkdtemp(join(tmpdir(), "csn-coordination-failed-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const store = new StateStore(resolve(__dirname, "../.."), join(dir, "state.sqlite"));
  await store.qqEnable();
  const event = sample(), sourceId = "a".repeat(64);
  await store.commit(sourceId, 0, { initialized: true, startedAt: Date.now() / 1000, position: 0, cursors: {} }, [event]);
  let sends = 0;
  const logs: string[] = [];
  const delivery = new QqDelivery(store, async () => ({ send: async () => { sends++; return "receipt"; } }), line => logs.push(line),
    async () => { throw new Error("qq_coordination_unavailable"); });
  await delivery.drain(sourceId, "backend", [event], "", async () => ({ host: "backend", event }), new AbortController().signal);
  assert.equal(sends, 0);
  assert.deepEqual((await store.qqStatus()).counts, {});
  assert.ok(logs.includes("QQ: qq_coordination_unavailable"));
});

test("disabling during a shared claim releases only the known-unsent reservation", async t => {
  const dir = await mkdtemp(join(tmpdir(), "csn-coordination-disable-"));
  t.after(() => rm(dir, { recursive: true, force: true }));
  const store = new StateStore(resolve(__dirname, "../.."), join(dir, "state.sqlite"));
  await store.qqEnable();
  const event = sample(), sourceId = "a".repeat(64);
  await store.commit(sourceId, 0, { initialized: true, startedAt: Date.now() / 1000, position: 0, cursors: {} }, [event]);
  let sends = 0, releases = 0;
  const delivery = new QqDelivery(store, async () => ({ send: async () => { sends++; return "receipt"; } }), () => {},
    async () => { await store.qqDisable(); return { acquired: true, release: async () => { releases++; } }; });
  await delivery.drain(sourceId, "backend", [event], "", async () => ({ host: "backend", event }), new AbortController().signal);
  assert.equal(sends, 0);
  assert.equal(releases, 1);
});

test("recipient scopes match across installations without exposing the token", () => {
  const token = "fake-test-token-00000000", source = "a".repeat(64);
  const sender = new PushplusSender(token);
  assert.equal(sender.coordinationScope(source), new PushplusSender(token).coordinationScope(source));
  assert.notEqual(sender.coordinationScope(source), new PushplusSender(token + "1").coordinationScope(source));
  assert.notEqual(sender.coordinationScope(source), new PushplusSender(token, undefined, { channel: "wechat", option: "" }).coordinationScope(source));
  assert.notEqual(sender.coordinationScope(source), sender.coordinationScope("b".repeat(64)));
  assert.match(sender.coordinationScope(source), /^[0-9a-f]{64}$/);
});
