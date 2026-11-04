import { execFile } from "node:child_process";
import { readFile } from "node:fs/promises";
import { join } from "node:path";
import { Source, SshSource, validateSshSource } from "./source";

export interface ProbeResult {
  schemaVersion: 1;
  platform: string;
  hostname: string;
  pythonVersion: string;
  codexHome: string;
  databaseExists: boolean;
  samples: Array<{ sessionId: string; source: "vscode"; archived?: boolean; hasName: boolean; nameSource: "name" | "title" | "unset"; cliVersion?: string;
    fileExists: boolean; fileBytes?: number; bytesRead?: number;
    explicitEvents?: Record<string, number>; completions?: Array<{ turnId: string; completedAt: string | null }> }>;
  warnings: string[];
}

export function remoteCommand(request: string): string {
  if (!/^[A-Za-z0-9+/=]+$/.test(request)) throw new Error("invalid_request_encoding");
  // Use standard SSH stdin execution, avoiding shell quoting and command-length limits.
  return `python3 -I - --request ${request}`;
}

export interface ReaderOptions { home?: string; python?: string; ssh?: string; signal?: AbortSignal }

export function sshArguments(source: SshSource): string[] {
  const target = validateSshSource(source);
  return ["-T", "-o", "BatchMode=yes", "-o", "ConnectTimeout=8", "-o", "ConnectionAttempts=1",
    "-o", "ServerAliveInterval=5", "-o", "ServerAliveCountMax=1",
    ...(target.user === undefined ? [] : ["-l", target.user]),
    ...(target.port === undefined ? [] : ["-p", String(target.port)]), target.host, remoteCommand("e30=")];
}

export function readerInput(script: string, request: unknown): string {
  const encoded = Buffer.from(JSON.stringify(request), "utf8").toString("base64");
  if (encoded.length > 262144) throw new Error("request_budget_exceeded");
  // Carry state through stdin, not a potentially huge remote shell command.
  return `import sys\nsys.argv = ["codex-notifier-reader", "--request", "${encoded}"]\n${script}`;
}

export async function runReader(root: string, source: Source, request: unknown, options: ReaderOptions = {}): Promise<unknown> {
  const scriptPath = join(root, "resources", "probe.py");
  let executable: string;
  let args: string[];
  if (source.kind === "local") {
    executable = options.python || (process.platform === "win32" ? "python" : "python3");
    args = ["-I", "-"];
  } else {
    executable = options.ssh || (process.platform === "win32" ? join(process.env.SystemRoot || "C:\\Windows", "System32", "OpenSSH", "ssh.exe") : "ssh");
    args = sshArguments(source);
  }
  const input = readerInput(await readFile(scriptPath, "utf8"), request);
  return new Promise((resolve, reject) => {
    const child = execFile(executable, args, { encoding: "utf8", timeout: source.kind === "local" ? 15000 : 20000,
      maxBuffer: 524288, windowsHide: true, signal: options.signal }, (error, stdout) => {
      // Only explicitly enumerated reader codes may become UI diagnostics.
      // Read them even when Python exits nonzero; never expose stderr or paths.
      try {
        const data = JSON.parse(stdout);
        if (["database_missing", "unsupported_database_schema", "machine_identity_unavailable"].includes(data?.error)) {
          reject(new Error(data.error));
          return;
        }
      } catch { /* Ordinary process/JSON errors are handled below. */ }
      if (error) {
        reject(new Error(error.killed ? "probe_timeout" : "probe_command_failed"));
        return;
      }
      try {
        resolve(JSON.parse(stdout));
      } catch { reject(new Error("invalid_probe_response")); }
    });
    child.stdin?.on("error", () => { /* execFile reports command failure through its callback. */ });
    child.stdin?.end(input);
  });
}

export async function inspectSource(root: string, source: Source, options: ReaderOptions & { sessionName?: string } = {}): Promise<ProbeResult> {
  const data = await runReader(root, source, { home: options.home || undefined, sessionName: options.sessionName, limit: 3 }, options);
  if (!data || typeof data !== "object" || !("schemaVersion" in data) || data.schemaVersion !== 1 ||
      !("samples" in data) || !Array.isArray(data.samples) || !("warnings" in data) || !Array.isArray(data.warnings)) {
    throw new Error("invalid_probe_response");
  }
  return data as ProbeResult;
}
