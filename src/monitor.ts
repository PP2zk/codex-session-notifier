import { execFile } from "node:child_process";
import { join } from "node:path";
import { ReaderOptions, runReader } from "./probe";
import { Source } from "./source";
import { randomUUID } from "node:crypto";

export interface ScanState {
  initialized: boolean;
  startedAt: number;
  position: number;
  cursors: Record<string, unknown>;
  activity?: Record<string, unknown>;
  knownCompletions?: Array<{ sessionId: string; turnId: string }>;
}
export interface Completion {
  sessionId: string;
  sessionName: string | null;
  nameState: "resolved" | "unset" | "unavailable";
  nameSource: "name" | "title" | "unset";
  turnId: string;
  completedAt: string;
  evidence: "persisted-completion";
}
export interface ScanResult {
  schemaVersion: 2;
  sourceId: string;
  hostname: string;
  initialized: true;
  position: number;
  cursors: Record<string, unknown>;
  events: Completion[];
  warnings: string[];
  verificationFailures?: VerificationFailure[];
  bytesRead: number;
  activeSessions?: ActiveSession[];
  activityUnknown?: number;
  activityBacklog?: boolean;
  activity?: Record<string, unknown>;
}
export interface VerificationFailure {
  sessionId: string;
  sessionName: string | null;
  turnIds: string[];
  reason: string;
  osError?: number;
}
export interface ActiveSession {
  sessionId: string;
  turnId: string;
  sessionName: string | null;
  nameState: "resolved" | "unset" | "unavailable";
  startedAt: string | null;
}
export interface ActivitySource {
  sourceId: string;
  machine: string;
  sessions: ActiveSession[];
  runningCount: number;
  unknown: number;
  updatedAt: number;
}

export async function scanSource(root: string, source: Source, state: ScanState, options: ReaderOptions = {}): Promise<ScanResult> {
  const data = await runReader(root, source, { mode: "scan", home: options.home || undefined, ...state }, options);
  if (!data || typeof data !== "object") throw new Error("invalid_scan_response");
  const result = data as Partial<ScanResult>;
  if (result.schemaVersion !== 2 || typeof result.sourceId !== "string" || !/^[0-9a-f]{64}$/.test(result.sourceId) ||
      typeof result.hostname !== "string" || result.hostname.length > 256 || result.initialized !== true ||
      !result.cursors || typeof result.cursors !== "object" || Array.isArray(result.cursors) || Object.keys(result.cursors).length > 128 ||
      !Number.isInteger(result.position) || result.position! < 0 || result.position! > 128 ||
      !Array.isArray(result.events) || result.events.length > 100 || !Array.isArray(result.warnings) ||
      !result.warnings.every(value => typeof value === "string" && /^[a-z_]+$/.test(value)) ||
      typeof result.bytesRead !== "number" || result.bytesRead < 0 || result.bytesRead > 8388608 ||
      (result.activityBacklog !== undefined && typeof result.activityBacklog !== "boolean")) {
    throw new Error("invalid_scan_response");
  }
  for (const event of result.events) {
    if (!event || typeof event !== "object" || ![event.sessionId, event.turnId, event.completedAt].every(value => typeof value === "string" && value.length > 0 && value.length <= 128) ||
        !(event.sessionName === null || typeof event.sessionName === "string") ||
        !["resolved", "unset", "unavailable"].includes(event.nameState) || !["name", "title", "unset"].includes(event.nameSource) ||
        event.evidence !== "persisted-completion" || !Number.isFinite(Date.parse(event.completedAt))) throw new Error("invalid_scan_response");
  }
  if (result.verificationFailures !== undefined) {
    if (!Array.isArray(result.verificationFailures) || result.verificationFailures.length > 128) throw new Error("invalid_scan_response");
    for (const failure of result.verificationFailures) {
      if (!failure || typeof failure.sessionId !== "string" || !failure.sessionId.length || failure.sessionId.length > 128 ||
          !(failure.sessionName === null || typeof failure.sessionName === "string" && failure.sessionName.length <= 512) ||
          !Array.isArray(failure.turnIds) || !failure.turnIds.length || failure.turnIds.length > 100 ||
          !failure.turnIds.every(id => typeof id === "string" && id.length > 0 && id.length <= 128) ||
          !["backend_not_observed", "unsupported_platform", "process_query_failed", "windows_session_failed",
            "windows_register_failed", "windows_list_failed", "process_image_failed"].includes(failure.reason) ||
          (failure.osError !== undefined && (!Number.isInteger(failure.osError) || failure.osError < 0 || failure.osError > 0xffffffff))) {
        throw new Error("invalid_scan_response");
      }
    }
  }
  if (!Array.isArray(result.activeSessions) || result.activeSessions.length > 128 ||
      !Number.isInteger(result.activityUnknown) || result.activityUnknown! < 0 || result.activityUnknown! > 128) throw new Error("invalid_scan_response");
  if (!result.activity || typeof result.activity !== "object" || Array.isArray(result.activity) || Object.keys(result.activity).length > 128) throw new Error("invalid_scan_response");
  for (const session of result.activeSessions) {
    if (!session || ![session.sessionId, session.turnId].every(v => typeof v === "string" && v.length > 0 && v.length <= 128) ||
        !(session.sessionName === null || typeof session.sessionName === "string") || !["resolved", "unset", "unavailable"].includes(session.nameState) ||
        !(session.startedAt === null || typeof session.startedAt === "string" && Number.isFinite(Date.parse(session.startedAt)))) throw new Error("invalid_scan_response");
  }
  return result as ScanResult;
}

interface Stored { revision: number; state: ScanState | null; knownCompletions?: Array<{ sessionId: string; turnId: string }> }
interface Commit { committed: boolean; added: Array<{ sessionId: string; turnId: string }> }
export interface QueuedCompletion { sessionId: string; turnId: string; completedAt: string }
export interface QqStatus { enabled: boolean; paused: string | null; counts: Record<string, number>;
  route?: import("./channels").NotificationRoute; routeRevision?: number }

export class StateStore {
  constructor(private root: string, private database: string, private python = "", private signal?: AbortSignal) {}

  private call<T>(request: unknown): Promise<T> {
    return new Promise((resolve, reject) => {
      const child = execFile(this.python || (process.platform === "win32" ? "python" : "python3"),
        ["-I", join(this.root, "resources", "state.py"), "--db", this.database],
        { encoding: "utf8", timeout: 10000, maxBuffer: 262144, windowsHide: true, signal: this.signal }, (error, stdout) => {
          if (error) { reject(new Error("state_operation_failed")); return; }
          try { resolve(JSON.parse(stdout) as T); } catch { reject(new Error("invalid_state_response")); }
        });
      child.stdin?.on("error", () => {});
      child.stdin?.end(JSON.stringify(request));
    });
  }

  load(sourceId: string): Promise<Stored> { return this.call({ operation: "load", sourceId }); }
  lease(sourceId: string, owner: string): Promise<{ acquired: boolean }> { return this.call({ operation: "lease", sourceId, owner }); }
  release(sourceId: string, owner: string): Promise<unknown> { return this.call({ operation: "release", sourceId, owner }); }
  commit(sourceId: string, revision: number, state: ScanState, events: Completion[]): Promise<Commit> {
    return this.call({ operation: "commit", sourceId, revision, state,
      events: events.map(({ sessionId, turnId, completedAt }) => ({ sessionId, turnId, completedAt })) });
  }
  qqStatus(): Promise<QqStatus> { return this.call({ operation: "qq_status" }); }
  qqEnable(routeRevision = 0): Promise<{ enabled?: boolean; updated?: boolean }> { return this.call({ operation: "qq_enable", routeRevision }); }
  configureRoute(route: import("./channels").NotificationRoute, routeRevision: number): Promise<{ updated: boolean }> {
    return this.call({ operation: "qq_configure", ...route, routeRevision });
  }
  qqDisable(): Promise<unknown> { return this.call({ operation: "qq_disable" }); }
  qqClaim(sourceId: string, ticket: string, routeRevision = 0): Promise<{ item: QueuedCompletion | null }> { return this.call({ operation: "qq_claim", sourceId, ticket, routeRevision }); }
  qqBegin(ticket: string, routeRevision = 0): Promise<{ updated: boolean }> { return this.call({ operation: "qq_begin", ticket, routeRevision }); }
  qqFinish(ticket: string, outcome: "retry" | "accepted" | "unknown" | "rejected", receipt?: string, code?: string, routeRevision = 0): Promise<unknown> {
    return this.call({ operation: "qq_finish", ticket, outcome, receipt, code, routeRevision });
  }
  publishActivity(sourceId: string, machine: string, sessions: ActiveSession[], unknown: number): Promise<unknown> {
    return this.call({ operation: "activity_publish", sourceId, machine: machine.slice(0, 256), sessions, unknown });
  }
  activityOverview(): Promise<{ sources: ActivitySource[]; qq: QqStatus }> { return this.call({ operation: "activity_overview" }); }
}

export function completionText(machine: string, event: Completion): string {
  const clean = (value: string, max: number) => value.replace(/[\p{Cc}\p{Cf}]/gu, " ").replace(/\s+/g, " ").trim().slice(0, max);
  const name = event.nameState === "resolved" && event.sessionName ? clean(event.sessionName, 120) :
    `${event.nameState === "unavailable" ? "Name unavailable · " : "Unnamed session · "}${event.sessionId.slice(0, 8)}`;
  return `Codex reply complete | ${clean(machine, 64)} | ${name}`;
}

export class Monitor {
  private timer?: ReturnType<typeof setTimeout>;
  private running = false;
  private stopped = false;
  private sourceId?: string;
  private failures = 0;
  private owner = randomUUID();
  private controller = new AbortController();
  private store: StateStore;
  private startedAt = Date.now() / 1000;

  constructor(private root: string, private source: Source, database: string, private options: ReaderOptions,
    private onEvents: (host: string, events: Completion[]) => void,
    private onStatus: (message: string, scan?: ScanResult) => void, private interval = 5000, private reader = scanSource,
    private afterScan?: (sourceId: string, host: string, events: Completion[], signal: AbortSignal, scan: ScanResult) => Promise<void>) {
    this.store = new StateStore(root, database, options.python, this.controller.signal);
  }

  start(): void { if (!this.stopped && !this.running && !this.timer) void this.tick(); }

  async tick(): Promise<void> {
    if (this.running || this.stopped) return;
    if (this.timer) { clearTimeout(this.timer); this.timer = undefined; }
    this.running = true;
    let delay = this.interval;
    let leased: string | undefined;
    try {
      const fresh: ScanState = { initialized: false, startedAt: this.startedAt, position: 0, cursors: {} };
      const options = { ...this.options, signal: this.controller.signal };
      let bootstrap: ScanResult | undefined;
      if (!this.sourceId) {
        bootstrap = await this.reader(this.root, this.source, fresh, options);
        this.sourceId = bootstrap.sourceId;
      }
      if (!(await this.store.lease(this.sourceId, this.owner)).acquired) {
        this.onStatus("monitored_in_another_window");
        return;
      }
      leased = this.sourceId;
      const saved = await this.store.load(this.sourceId);
      const previous = saved.state || fresh;
      const result = bootstrap && !saved.state ? bootstrap : await this.reader(this.root, this.source,
        { ...previous, knownCompletions: saved.knownCompletions || [] }, options);
      if (result.sourceId !== this.sourceId) {
        this.sourceId = undefined;
        this.startedAt = Date.now() / 1000;
        throw new Error("source_identity_changed");
      }
      if (this.stopped) return;
      const next = { initialized: true, startedAt: previous.startedAt, position: result.position, cursors: result.cursors, activity: result.activity };
      const committed = await this.store.commit(this.sourceId, saved.revision, next, result.events);
      if (this.stopped) return;
      if (committed.committed) {
        const added = new Set(committed.added.map(event => `${event.sessionId}\0${event.turnId}`));
        this.onEvents(result.hostname, result.events.filter(event => added.has(`${event.sessionId}\0${event.turnId}`)));
      }
      this.failures = 0;
      this.onStatus(result.warnings.length ? result.warnings.join(", ") : "monitoring",
        committed.committed && this.afterScan ? result : undefined);
      if (committed.committed && this.afterScan && !this.stopped) {
        await this.afterScan(this.sourceId, result.hostname, result.events, this.controller.signal, result);
      }
      // Continue unfinished discovery without putting five seconds between
      // every bounded read. Unknown bindings and unsupported logs still poll
      // normally; only an actual lifecycle backfill requests this short delay.
      if (committed.committed && result.activityBacklog) delay = Math.min(this.interval, 250);
    } catch (error) {
      if (!this.stopped) {
        this.failures++;
        const message = error instanceof Error ? error.message : "monitor_failed";
        this.onStatus(/^[a-z_]+$/.test(message) ? message : "monitor_failed");
        delay = Math.min(60000, this.interval * 2 ** Math.min(this.failures, 4));
      }
    } finally {
      if (leased) await this.store.release(leased, this.owner).catch(() => {});
      this.running = false;
      if (!this.stopped) this.timer = setTimeout(() => { this.timer = undefined; void this.tick(); }, delay);
    }
  }

  dispose(): void {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    this.controller.abort();
  }
}
