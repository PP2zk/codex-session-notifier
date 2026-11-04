export type SshSource = { kind: "ssh"; host: string; user?: string; port?: number };
export type Source = { kind: "local" } | SshSource;

export function validateSshHost(host: string): string {
  if (typeof host !== "string" || !/^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$/.test(host)) {
    throw new Error("unsupported_ssh_alias");
  }
  return host;
}

export function validateSshSource(source: { host: unknown; user?: unknown; port?: unknown }): SshSource {
  if (typeof source.host !== "string") throw new Error("unsupported_ssh_alias");
  const result: SshSource = { kind: "ssh", host: validateSshHost(source.host) };
  if (source.user !== undefined) {
    // Pass the username as one -l argument, never as shell syntax or user@host.
    if (typeof source.user !== "string" || !source.user || source.user.length > 256 ||
        source.user.startsWith("-") || /[\s\x00-\x1f\x7f]/u.test(source.user)) throw new Error("unsupported_ssh_user");
    result.user = source.user;
  }
  if (source.port !== undefined) {
    if (typeof source.port !== "number" || !Number.isInteger(source.port) || source.port < 1 || source.port > 65535) {
      throw new Error("unsupported_ssh_port");
    }
    result.port = source.port;
  }
  return result;
}

function sourceFromAuthority(authority: string): SshSource {
  // Remote SSH's HostInfo encodes uppercase aliases and explicit user/port as
  // hex UTF-8 JSON. An ordinary hex-looking SSH alias still remains an alias.
  if (authority.length <= 4096 && /^(?:[0-9a-f]{2})+$/i.test(authority)) {
    const decoded = Buffer.from(authority, "hex").toString("utf8");
    if (decoded.trimStart().startsWith("{")) {
      let data: Record<string, unknown>;
      try { data = JSON.parse(decoded); }
      catch { throw new Error("remote_source_not_identified"); }
      if (Buffer.from(decoded, "utf8").toString("hex") !== authority.toLowerCase()) {
        throw new Error("remote_source_not_identified");
      }
      return validateSshSource({ host: data.hostName, user: data.user, port: data.port });
    }
  }
  return validateSshSource({ host: authority });
}

export function discoverSource(remoteName: string | undefined, authorities: readonly string[], override = ""): Source {
  if (!remoteName) {
    if (override) throw new Error("ssh_override_requires_remote_window");
    return { kind: "local" };
  }
  if (remoteName !== "ssh-remote") throw new Error("unsupported_remote_kind");
  if (override) return { kind: "ssh", host: validateSshHost(override) };
  const sources = new Map<string, SshSource>();
  for (const authority of authorities.filter(value => value.startsWith("ssh-remote+"))) {
    const source = sourceFromAuthority(authority.slice(11));
    sources.set(JSON.stringify(source), source);
  }
  if (sources.size !== 1) throw new Error("remote_source_not_identified");
  return [...sources.values()][0]!;
}
