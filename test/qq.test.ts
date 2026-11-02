import { test } from "node:test";
import assert from "node:assert/strict";
import type { HttpRequestOptions } from "@perk-net/perk-pushplus-sdk" with { "resolution-mode": "import" };
import { DirectPushplusTransport, QQ_TEST_MESSAGE, QqSender } from "../src/qq";
import { EventEmitter } from "node:events";
import type { ClientRequest } from "node:http";
import { CHANNELS, validateRoute } from "../src/channels";

test("QQ SDK sends only a minimal title to self over the fixed HTTPS endpoint", async () => {
  const requests: HttpRequestOptions[] = [];
  const sender = new QqSender("a".repeat(32), { execute: async options => {
    requests.push(options);
    return { statusCode: 200, body: JSON.stringify({ code: 200, data: "receipt" }) };
  } });
  assert.equal(await sender.sendTest(QQ_TEST_MESSAGE.title, QQ_TEST_MESSAGE.content), "receipt");
  assert.equal(requests[0]!.url, "https://www.pushplus.plus/send");
  const payload = JSON.parse(requests[0]!.body!);
  assert.deepEqual(Object.keys(payload).sort(), ["channel", "content", "template", "title", "token"]);
  assert.equal(payload.channel, "qq");
  assert.equal(payload.title, "Test task");
  assert.equal(payload.content, "Codex test notification | Machine: Local | Session: Test task");
  assert.equal(payload.token, "a".repeat(32));
});

test("SDK rate-limit guard stops repeated requests and raw provider text is redacted", async () => {
  let calls = 0;
  const sender = new QqSender("b".repeat(32), { execute: async () => {
    calls++;
    return { statusCode: 200, body: JSON.stringify({ code: 900, msg: "PRIVATE TOKEN AND BODY" }) };
  } });
  await assert.rejects(sender.sendTest("test"), { message: "qq_rejected_900" });
  await assert.rejects(sender.sendTest("test"), { message: "qq_rejected_900" });
  assert.equal(calls, 1);
});

test("transport refuses HTTP, other origins, and token query strings before any network request", async () => {
  const transport = new DirectPushplusTransport();
  for (const url of ["http://www.pushplus.plus/send", "https://evil.example/send", "https://www.pushplus.plus/send?token=private"]) {
    await assert.rejects(transport.execute({ url, method: "POST" }), /endpoint_rejected/);
  }
});

test("ambiguous receipts never become confirmed delivery and transport errors stay redacted", async () => {
  const sender = new QqSender("c".repeat(32), { execute: async () => ({ statusCode: 200, body: '{"code":200,"data":null}' }) });
  await assert.rejects(sender.sendTest("test"), { message: "qq_receipt_unconfirmed" });
  const failed = new QqSender("d".repeat(32), { execute: async () => { throw new Error("PRIVATE NETWORK ERROR"); } });
  await assert.rejects(failed.sendTest("test"), { message: "qq_delivery_unknown" });
});

test("total transport deadline rejects even if a destroyed request emits no error", async t => {
  t.mock.timers.enable({ apis: ["setTimeout"] });
  const https: typeof import("node:https") = require("node:https");
  let destroyed = false;
  t.mock.method(https, "request", ((_url: unknown, options: { agent: unknown; rejectUnauthorized: boolean }) => {
    assert.equal(options.agent, false);
    assert.equal(options.rejectUnauthorized, true);
    const fake = new EventEmitter() as EventEmitter & { end(): void; destroy(): void };
    fake.end = () => {};
    fake.destroy = () => { destroyed = true; };
    return fake as unknown as ClientRequest;
  }) as typeof https.request);
  const result = assert.rejects(new DirectPushplusTransport().execute({ method: "POST", url: "https://www.pushplus.plus/send", body: "{}" }), /delivery_unknown/);
  t.mock.timers.tick(10000);
  await result;
  assert.equal(destroyed, true);
});

test("each documented pushplus channel maps to an isolated single-channel SDK request", async () => {
  for (const info of CHANNELS) {
    const option = ["webhook", "cp", "mail", "qq"].includes(info.id) ? "configured-code" : "";
    const sender = new QqSender("a".repeat(32), { execute: async options => {
      assert.equal(options.url, "https://www.pushplus.plus/send");
      assert.deepEqual(JSON.parse(options.body!), { token: "a".repeat(32), title: "Session name", content: "Machine and session details",
        channel: info.id, template: "txt", ...(option ? { option } : {}) });
      return { statusCode: 200, body: JSON.stringify({ code: 200, data: [{ channel: "wrong-channel", code: 200, shortCode: "wrong" },
        { channel: info.id, code: 200, shortCode: "selected" }] }) };
    } }, { channel: info.id, option });
    assert.equal(await sender.send("Session name", "Machine and session details"), "selected", info.id);
  }
});

test("channel validation rejects multi-channel strings, missing required codes and webhook URLs", () => {
  for (const route of [{ channel: "wechat,qq", option: "" }, { channel: "webhook", option: "" },
    { channel: "cp", option: "" }, { channel: "webhook", option: "https://private.example/hook" },
    { channel: "qq", option: "bad\ncode" }]) assert.throws(() => validateRoute(route as Parameters<typeof validateRoute>[0]));
  validateRoute({ channel: "mail", option: "" });
  validateRoute({ channel: "wechat", option: "" });
});

test("a receipt for a different channel or an unsuccessful channel row is unconfirmed", async () => {
  for (const data of [[{ channel: "qq", code: 200, shortCode: "wrong" }], [{ channel: "wechat", code: 900, shortCode: "wrong" }]]) {
    const sender = new QqSender("a".repeat(32), { execute: async () => ({ statusCode: 200, body: JSON.stringify({ code: 200, data }) }) }, { channel: "wechat", option: "" });
    await assert.rejects(sender.send("test", "details"), /qq_receipt_unconfirmed/);
  }
});
