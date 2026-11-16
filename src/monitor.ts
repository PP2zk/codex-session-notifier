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
  notificationBacklog?: boolean;
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
  notificationBacklog?: boolean;
  activity?: Record<string, unknown>;
  diagnostics?: ReaderDiagnostic[];
}
export interface ReaderDiagnostic {
  kind: "activity" | "completion" | "scan";
  sessionId: string;
  sessionName?: string | null;
  turnId?: string | null;
  completedAt?: string | null;
  state: string;
  reason: string;
  bindingReason?: string;
  operation?: string;
  fileBytes?: number;
  observedBytes?: number;
  lookbackBytes?: number;
  offset?: number;
  bytesRead?: number;
  runningCount?: number;
  unknownCount?: number;
  position?: number;
  osError?: number;
  processId?: number;
  cutoff?: number;
}
export interface StoredDiagnostic extends ReaderDiagnostic { observedAt: number; sourceId: string }
export interface VerificationFailure {
  sessionId: string;
  sessionName: string | null;
  turnIds: string[];
  reason: string;
  osError?: number;
  operation?: "OpenProcess" | "QueryFullProcessImageNameW";
  processId?: number;
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
      (result.activityBacklog !== undefined && typeof result.activityBacklog !== "boolean") ||
      (result.notificationBacklog !== undefined && typeof result.notificationBacklog !== "boolean")) {
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
          (failure.osError !== undefined && (!Number.isInteger(failure.osError) || failure.osError < 0 || failure.osError > 0xffffffff)) ||
          (failure.operation !== undefined && !["OpenProcess", "QueryFullProcessImageNameW"].includes(failure.operation)) ||
          (failure.processId !== undefined && (!Number.isInteger(failure.processId) || failure.processId < 0 || failure.processId > 0xffffffff))) {
        throw new Error("invalid_scan_response");
      }
    }
  }
  if (result.diagnostics !== undefined) {
    const diagnostics = result.diagnostics;
    result.diagnostics = [];
    diagnostic: for (const item of Array.isArray(diagnostics) ? diagnostics.slice(0, 256) : []) {
      if (!item || !["activity", "completion", "scan"].includes(item.kind) || typeof item.sessionId !== "string" || item.sessionId.length > 128 ||
          ![item.state, item.reason].every(value => typeof value === "string" && /^[a-z_]+$/.test(value) && value.length <= 128)) continue;
      for (const key of ["sessionName", "turnId", "completedAt", "bindingReason", "operation"] as const) {
        if (item[key] != null && (typeof item[key] !== "string" || item[key]!.length > (key === "sessionName" ? 256 : 128))) continue diagnostic;
      }
      for (const key of ["fileBytes", "observedBytes", "lookbackBytes", "offset", "bytesRead", "runningCount", "unknownCount", "position", "osError", "processId"] as const) {
        if (item[key] !== undefined && (!Number.isFinite(item[key]) || item[key]! < 0 || item[key]! > Number.MAX_SAFE_INTEGER)) continue diagnostic;
      }
      result.diagnostics.push(item);
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
interface Commit { committed: boolean; added: Array<{ sessionId: string; turnId: string }>; diagnostics?: StoredDiagnostic[] }
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
  commit(sourceId: string, revision: number, state: ScanState, events: Completion[], diagnostics: ReaderDiagnostic[] = []): Promise<Commit> {
    const request = { operation: "commit", sourceId, revision, state,
      events: events.map(({ sessionId, turnId, completedAt }) => ({ sessionId, turnId, completedAt })), diagnostics: [] as ReaderDiagnostic[] };
    let remaining = 262144 - Buffer.byteLength(JSON.stringify(request), "utf8");
    // Diagnostic detail must not displace the authoritative cursor/outbox write.
    for (const item of [...diagnostics].sort((a, b) => Number(a.kind === "activity") - Number(b.kind === "activity"))) {
      const bytes = Buffer.byteLength(JSON.stringify(item), "utf8") + 1;
      if (bytes > remaining) continue;
      request.diagnostics.push(item);
      remaining -= bytes;
    }
    return this.call(request);
  }
  qqStatus(): Promise<QqStatus> { return this.call({ operation: "qq_status" }); }
  qqEnable(routeRevision = 0): Promise<{ enabled?: boolean; updated?: boolean }> { return this.call({ operation: "qq_enable", routeRevision }); }
  configureRoute(route: import("./channels").NotificationRoute, routeRevision: number): Promise<{ updated: boolean }> {
    return this.call({ operation: "qq_configure", ...route, routeRevision });
  }
  qqDisable(): Promise<unknown> { return this.call({ operation: "qq_disable" }); }
  qqClaim(sourceId: string, ticket: string, routeRevision = 0): Promise<{ item: QueuedCompletion | null }> { return this.call({ operation: "qq_claim", sourceId, ticket, routeRevision }); }
  qqBegin(ticket: string, routeRevision = 0): Promise<{ updated: boolean }> { return this.call({ operation: "qq_begin", ticket, routeRevision }); }
  qqFinish(ticket: string, outcome: "retry" | "accepted" | "unknown" | "rejected" | "suppressed", receipt?: string, code?: string, routeRevision = 0): Promise<unknown> {
    return this.call({ operation: "qq_finish", ticket, outcome, receipt, code, routeRevision });
  }
  publishActivity(sourceId: string, machine: string, sessions: ActiveSession[], unknown: number): Promise<unknown> {
    return this.call({ operation: "activity_publish", sourceId, machine: machine.slice(0, 256), sessions, unknown });
  }
  activityOverview(): Promise<{ sources: ActivitySource[]; qq: QqStatus }> { return this.call({ operation: "activity_overview" }); }
  diagnostics(): Promise<{ entries: StoredDiagnostic[] }> { return this.call({ operation: "diagnostics" }); }
  processStart(pid: number): Promise<{ startedAt: number | null }> { return this.call({ operation: "process_start", pid }); }
  monitorJoin(sourceId: string, owner: string, startedAt: number): Promise<{ startedAt: number; diagnostics: StoredDiagnostic[] }> {
    return this.call({ operation: "monitor_join", sourceId, owner, startedAt, pid: process.pid });
  }
  monitorLeave(sourceId: string, owner: string): Promise<unknown> { return this.call({ operation: "monitor_leave", sourceId, owner }); }
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
  private presenceStore: StateStore;
  private membership?: { sourceId: string; joined: Promise<{ startedAt: number; diagnostics: StoredDiagnostic[] }> };
  private startedAt = Math.floor(Date.now() / 1000);

  constructor(private root: string, private source: Source, database: string, private options: ReaderOptions,
    private onEvents: (host: string, events: Completion[]) => void,
    private onStatus: (message: string, scan?: ScanResult) => void, private interval = 5000, private reader = scanSource,
    private afterScan?: (sourceId: string, host: string, events: Completion[], signal: AbortSignal, scan: ScanResult) => Promise<void>,
    private onDiagnostics?: (entries: StoredDiagnostic[]) => void,
    private startupBoundary?: Promise<number>) {
    this.store = new StateStore(root, database, options.python, this.controller.signal);
    this.presenceStore = new StateStore(root, database, options.python);
  }

  start(): void { if (!this.stopped && !this.running && !this.timer) void this.tick(); }

  async tick(): Promise<void> {
    if (this.running || this.stopped) return;
    if (this.timer) { clearTimeout(this.timer); this.timer = undefined; }
    this.running = true;
    let delay = this.interval;
    let leased: string | undefined;
    try {
      if (this.startupBoundary) {
        const startedAt = await this.startupBoundary;
        if (!Number.isFinite(startedAt) || startedAt <= 0 || startedAt > Date.now() / 1000 + 1) throw new Error("invalid_startup_boundary");
        this.startedAt = startedAt;
        this.startupBoundary = undefined;
      }
      if (this.stopped) return;
      const fresh: ScanState = { initialized: false, startedAt: this.startedAt, position: 0, cursors: {} };
      const options = { ...this.options, signal: this.controller.signal };
      let bootstrap: ScanResult | undefined;
      if (!this.sourceId) {
        bootstrap = await this.reader(this.root, this.source, fresh, options);
        this.sourceId = bootstrap.sourceId;
      }
      if (this.stopped) return;
      if (!this.membership) {
        const membership = { sourceId: this.sourceId,
          joined: this.presenceStore.monitorJoin(this.sourceId, this.owner, this.startedAt) };
        this.membership = membership;
        try {
          const joined = await membership.joined;
          this.startedAt = joined.startedAt;
          this.onDiagnostics?.(joined.diagnostics);
        } catch (error) {
          if (this.membership === membership) this.membership = undefined;
          await this.presenceStore.monitorLeave(membership.sourceId, this.owner).catch(() => {});
          throw error;
        }
      }
      fresh.startedAt = this.startedAt;
      if (this.stopped) return;
      if (!(await this.store.lease(this.sourceId, this.owner)).acquired) {
        this.onStatus("monitored_in_another_window");
        return;
      }
      leased = this.sourceId;
      const saved = await this.store.load(this.sourceId);
      const previous = saved.state ? { ...saved.state, startedAt: Math.max(saved.state.startedAt, this.startedAt) } : fresh;
      const result = bootstrap && !saved.state ? bootstrap : await this.reader(this.root, this.source,
        { ...previous, knownCompletions: saved.knownCompletions || [] }, options);
      if (result.sourceId !== this.sourceId) {
        await this.leave();
        this.sourceId = undefined;
        throw new Error("source_identity_changed");
      }
      if (this.stopped) return;
      const next = { initialized: true, startedAt: previous.startedAt, position: result.position, cursors: result.cursors,
        activity: result.activity, notificationBacklog: result.notificationBacklog || false };
      const committed = await this.store.commit(this.sourceId, saved.revision, next, result.events, result.diagnostics);
      if (this.stopped) return;
      if (committed.committed) {
        this.onDiagnostics?.(committed.diagnostics || []);
        const added = new Set(committed.added.map(event => `${event.sessionId}\0${event.turnId}`));
        this.onEvents(result.hostname, result.events.filter(event => added.has(`${event.sessionId}\0${event.turnId}`)));
      }
      this.failures = 0;
      this.onStatus(result.warnings.length ? result.warnings.join(", ") : "monitoring",
        committed.committed && this.afterScan ? result : undefined);
      if (committed.committed && this.afterScan && !this.stopped) {
        await this.afterScan(this.sourceId, result.hostname, result.events, this.controller.signal, result);
      }
      // Only advancing, committed backlogs use the short delay. Incomplete
      // records and unverified sources retain normal polling.
      if (committed.committed && (result.activityBacklog || result.notificationBacklog)) delay = Math.min(this.interval, 250);
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

  private async leave(): Promise<void> {
    const membership = this.membership;
    this.membership = undefined;
    if (membership) {
      // Complete a pending join before removing exactly this monitor's token.
      await membership.joined.catch(() => {}).then(() => this.presenceStore.monitorLeave(membership.sourceId, this.owner)).catch(() => {});
    }
  }

  dispose(): Promise<void> {
    this.stopped = true;
    if (this.timer) clearTimeout(this.timer);
    this.controller.abort();
    return this.leave();
  }
}
