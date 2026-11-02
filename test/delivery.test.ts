import { test } from "node:test";
import assert from "node:assert/strict";
import { mkdtemp, rm } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join, resolve } from "node:path";
import { Completion, StateStore } from "../src/monitor";
import { QqDelivery, qqCompletionMessage } from "../src/delivery";

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
    const create = () => new QqDelivery(store, sender, line => logs.push(line));
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
  }, () => {});
  await delivery.drain(sourceId, "machine", [], "", async () => {
    await store.configureRoute({ channel: "mail", option: "" }, 1);
    await store.qqEnable(2);
    return { host: "machine", event };
  }, new AbortController().signal);
  assert.equal(sends, 0);
  assert.equal((await store.qqStatus()).route?.channel, "mail");
  assert.equal((await store.qqStatus()).counts.accepted, undefined);
});
