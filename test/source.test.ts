import { test } from "node:test";
import assert from "node:assert/strict";
import { discoverSource, validateSshHost } from "../src/source";
import { remoteCommand, sshArguments } from "../src/probe";

const encodedAuthority = (data: unknown) => `ssh-remote+${Buffer.from(JSON.stringify(data), "utf8").toString("hex")}`;

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

test("Remote SSH hex JSON preserves uppercase aliases instead of using the encoding as a hostname", () => {
  // Fixed fixture in the format emitted by Remote SSH's HostInfo.toAuthorityString.
  const authority = "ssh-remote+7b22686f73744e616d65223a224750552d41227d";
  assert.deepEqual(discoverSource("ssh-remote", [authority]), { kind: "ssh", host: "GPU-A" });
  assert.deepEqual(discoverSource("ssh-remote", [authority, "ssh-remote+GPU-A"]), { kind: "ssh", host: "GPU-A" });
  assert.deepEqual(discoverSource("ssh-remote", ["ssh-remote+deadbeef"]), { kind: "ssh", host: "deadbeef" });
});

test("encoded usernames and ports reach SSH as separate arguments and unrelated metadata is ignored", () => {
  const source = discoverSource("ssh-remote", [encodedAuthority({
    port: 2222, user: "DOMAIN\\reader", hostName: "gpu-a", workspaceId: "workspace", env: { UNTRUSTED: "value" }
  })]);
  assert.deepEqual(source, { kind: "ssh", host: "gpu-a", user: "DOMAIN\\reader", port: 2222 });
  assert.equal(source.kind, "ssh");
  if (source.kind !== "ssh") assert.fail("expected remote source");
  const args = sshArguments(source);
  assert.deepEqual(args.slice(-6), ["-l", "DOMAIN\\reader", "-p", "2222", "gpu-a", "python3 -I - --request e30="]);
  assert.ok(args.includes("BatchMode=yes"));
  assert.ok(!args.some(value => /StrictHostKeyChecking|UserKnownHostsFile|UNTRUSTED/.test(value)));
  assert.throws(() => sshArguments({ kind: "ssh", host: "-Fbad" }), /unsupported_ssh_alias/);
  assert.throws(() => sshArguments({ kind: "ssh", host: "gpu-a", port: 0 }), /unsupported_ssh_port/);
});

test("distinct remote accounts and ports cannot be collapsed into one source", () => {
  for (const other of [{ hostName: "gpu-a", user: "other" }, { hostName: "gpu-a", port: 2222 }]) {
    assert.throws(() => discoverSource("ssh-remote", [encodedAuthority({ hostName: "gpu-a" }), encodedAuthority(other)]), /not_identified/);
  }
  const first = encodedAuthority({ hostName: "gpu-a", user: "reader", port: 2222 });
  const second = encodedAuthority({ port: 2222, user: "reader", hostName: "gpu-a" });
  assert.deepEqual(discoverSource("ssh-remote", [first, second]), { kind: "ssh", host: "gpu-a", user: "reader", port: 2222 });
});

test("invalid encoded routing data never falls back to a hex alias or the local computer", () => {
  for (const data of [{}, { hostName: null }, { hostName: "-oProxyCommand=bad" }, { hostName: "gpu-a;bad" },
    { hostName: "gpu-a", user: "-Fbad" }, { hostName: "gpu-a", user: "a\nb" }, { hostName: "gpu-a", user: 7 },
    { hostName: "gpu-a", port: "2222" }, { hostName: "gpu-a", port: 65536 }, { hostName: "gpu-a", port: 1.5 }]) {
    assert.throws(() => discoverSource("ssh-remote", [encodedAuthority(data)]));
  }
  assert.throws(() => discoverSource("ssh-remote", ["ssh-remote+7b"]), /not_identified/);
  const invalidUtf8 = Buffer.concat([Buffer.from('{"hostName":"gpu-'), Buffer.from([0xff]), Buffer.from('"}')]).toString("hex");
  assert.throws(() => discoverSource("ssh-remote", [`ssh-remote+${invalidUtf8}`]), /not_identified/);
});

test("an explicit SSH alias override replaces the entire discovered route only in Remote SSH windows", () => {
  const authority = encodedAuthority({ hostName: "gpu-a", user: "reader", port: 2222 });
  assert.deepEqual(discoverSource("ssh-remote", [authority], "gpu-b"), { kind: "ssh", host: "gpu-b" });
  assert.throws(() => discoverSource(undefined, [], "gpu-b"), /ssh_override_requires_remote_window/);
  assert.throws(() => discoverSource("wsl", [], "gpu-b"), /unsupported_remote_kind/);
});

test("remote command contains only fixed Python syntax and base64 request data", () => {
  const request = Buffer.from(JSON.stringify({ home: "/tmp/a'$(untrusted)" })).toString("base64");
  const command = remoteCommand(request);
  assert.ok(!command.includes("untrusted"));
  assert.ok(command.startsWith("python3 -I - --request "));
  assert.throws(() => remoteCommand("a;exit"));
});
