import { test } from "node:test";
import assert from "node:assert/strict";
import { ActivitySource, QqStatus } from "../src/monitor";
import { hoverContent, runningCount } from "../src/presentation";

const now = Date.parse("2026-11-01T13:00:00Z");
const qq: QqStatus = { enabled: true, paused: null, counts: {} };
const source = (machine: string, name: string, age = 0): ActivitySource => ({
  sourceId: "a".repeat(64), machine, unknown: 0, runningCount: 1, updatedAt: now / 1000 - age,
  sessions: [{ sessionId: "session1", turnId: "turn1", sessionName: name, nameState: "resolved", startedAt: new Date(now - 120000).toISOString() }]
});

test("hover groups fresh sessions by machine and excludes delayed sources from the running count", () => {
  const sources = [source("Local PC", "Test task"), source("GPU server", "Train model"), source("Offline host", "Old task", 40)];
  const content = hoverContent(qq, sources, "monitoring", now);
  assert.equal(runningCount(sources, now), 2);
  for (const text of ["QQ notifications **Enabled**", "Running sessions · 2", "Local PC", "Test task", "GPU server", "Train model", "2m", "Source updates delayed: Offline host"]) assert.ok(content.includes(text), text);
  assert.ok(!content.includes("Old task"));
  assert.ok(!content.includes("$(play-circle)"));
});

test("session names cannot inject commands, HTML, codicons or Markdown into the hover", () => {
  const content = hoverContent(qq, [source("<img src=x>", "[Click](command:evil) $(zap)\nPRIVATE\u202e")], "monitoring", now);
  assert.ok(!content.includes("[Click](command:evil)"));
  assert.ok(!content.includes("$(zap)"));
  assert.ok(!content.includes("<img"));
  assert.ok(!content.includes("\u202e"));
  assert.ok(content.includes("[Open menu](command:codexSessionNotifier.openMenu)"));
});

test("empty or uncertain activity is shown explicitly without inventing a running session", () => {
  const unknown = { ...source("PC", "ignored"), runningCount: 0, sessions: [], unknown: 1 };
  const content = hoverContent({ ...qq, enabled: false }, [unknown], "read_backlog", now);
  assert.ok(content.includes("**Disabled**"));
  assert.ok(content.includes("No confirmed running sessions"));
  assert.ok(content.includes("still being checked for 1 session"));
});

test("source verification explains withheld completions without calling the whole monitor stopped", () => {
  const content = hoverContent(qq, [source("PC", "Still running")], "read_backlog, execution_source_unverified", now);
  assert.ok(content.includes("Still running"));
  assert.ok(content.includes("Some completion notifications await source verification"));
  assert.ok(content.includes("Show logs"));
  assert.ok(!content.includes("execution source unverified"));
});

test("snapshot ordering, heartbeats and shared-monitor diagnostics do not churn hover content", () => {
  const a = source("Host A", "First"), b = { ...source("Host B", "Second"), sourceId: "b".repeat(64) };
  const before = hoverContent(qq, [a, b], "monitoring", now);
  const after = hoverContent(qq, [{ ...b, updatedAt: now / 1000 + 5 }, { ...a, updatedAt: now / 1000 + 5 }], "monitored_in_another_window", now + 5000);
  assert.equal(before, after);
  assert.equal(before, hoverContent(qq, [a, b], "read_backlog", now));
  const recent = { ...a, sessions: [{ ...a.sessions[0]!, startedAt: new Date(now - 1000).toISOString() }] };
  assert.equal(hoverContent(qq, [recent], "monitoring", now), hoverContent(qq, [recent], "monitoring", now + 5000));
  assert.ok(!before.includes("~spin"));
});


test("startup checking is neutral while stale sources remain excluded and other delays remain visible", () => {
  const starting = source("Restarting host", "Old session", 40);
  const delayed = { ...source("Offline host", "Old task", 40), sourceId: "b".repeat(64) };
  const content = hoverContent(qq, [starting, delayed], "monitoring", now, new Set([starting.sourceId]));
  assert.ok(content.includes("Checking source status: Restarting host"));
  assert.ok(content.includes("Source updates delayed: Offline host"));
  assert.ok(!content.includes("Source updates delayed: Restarting host"));
  assert.ok(content.includes("Running sessions · 0"));
  assert.ok(!content.includes("Old session"));
  assert.ok(hoverContent(qq, [starting], "monitoring", now).includes("Source updates delayed: Restarting host"));
});

test("the hover identifies the configured channel while preserving QQ as the upgrade default", () => {
  assert.ok(hoverContent({ ...qq, route: { channel: "wechat", option: "PRIVATE" } }, [], "monitoring", now).includes("WeChat notifications **Enabled**"));
  assert.ok(!hoverContent({ ...qq, route: { channel: "mail", option: "PRIVATE" } }, [], "monitoring", now).includes("PRIVATE"));
  assert.ok(hoverContent(qq, [], "monitoring", now).includes("QQ notifications **Enabled**"));
});
