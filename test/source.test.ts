import { test } from "node:test";
import assert from "node:assert/strict";
import { discoverSource, validateSshHost } from "../src/source";
import { remoteCommand } from "../src/probe";

test("a remote window never silently falls back to the local machine", () => {
  assert.deepEqual(discoverSource(undefined, []), { kind: "local" });
  assert.deepEqual(discoverSource("ssh-remote", ["ssh-remote+gpu-a"]), { kind: "ssh", host: "gpu-a" });
  assert.throws(() => discoverSource("ssh-remote", []), /not_identified/);
  assert.throws(() => discoverSource("wsl", []), /unsupported_remote_kind/);
  assert.throws(() => discoverSource("ssh-remote", ["ssh-remote+gpu-a", "ssh-remote+gpu-b"]));
});

test("SSH aliases cannot inject options or remote shell syntax", () => {
  for (const value of ["-oProxyCommand=bad", "a;bad", "a b", "a\nb", "$(bad)", "a/../../b"]) {
    assert.throws(() => validateSshHost(value));
  }
  assert.equal(validateSshHost("gpu-server-vpn"), "gpu-server-vpn");
});

test("remote command contains only fixed Python syntax and base64 request data", () => {
  const request = Buffer.from(JSON.stringify({ home: "/tmp/a'$(untrusted)" })).toString("base64");
  const command = remoteCommand(request);
  assert.ok(!command.includes("untrusted"));
  assert.ok(command.startsWith("python3 -I - --request "));
  assert.throws(() => remoteCommand("a;exit"));
});
