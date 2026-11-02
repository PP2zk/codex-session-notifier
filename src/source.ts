export type Source = { kind: "local" } | { kind: "ssh"; host: string };

export function validateSshHost(host: string): string {
  if (!/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(host)) {
    throw new Error("unsupported_ssh_alias");
  }
  return host;
}

export function discoverSource(remoteName: string | undefined, authorities: readonly string[], override = ""): Source {
  if (!remoteName) {
    if (override) throw new Error("ssh_override_requires_remote_window");
    return { kind: "local" };
  }
  if (remoteName !== "ssh-remote") throw new Error("unsupported_remote_kind");
  if (override) return { kind: "ssh", host: validateSshHost(override) };
  const hosts = new Set(authorities.filter(value => value.startsWith("ssh-remote+")).map(value => value.slice(11)));
  if (hosts.size !== 1) throw new Error("remote_source_not_identified");
  return { kind: "ssh", host: validateSshHost([...hosts][0]!) };
}
