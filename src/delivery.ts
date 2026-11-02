import { randomUUID } from "node:crypto";
import { Completion, QueuedCompletion, StateStore } from "./monitor";
import { ReaderOptions, runReader } from "./probe";
import { Source } from "./source";
import { DEFAULT_ROUTE, NotificationRoute, channelInfo } from "./channels";

export const QQ_TOKEN_KEY = "pushplus.qq.token";

const clean = (text: string) => text.replace(/[\p{Cc}\p{Cf}]/gu, " ").replace(/\s+/g, " ").trim();
const bounded = (text: string, size: number) => text.length <= size ? text : `${text.slice(0, size - 1).replace(/[\uD800-\uDBFF]$/u, "")}…`;

export function qqCompletionMessage(host: string, event: Completion): { title: string; content: string } {
  const name = event.nameState === "resolved" && event.sessionName && clean(event.sessionName) ? clean(event.sessionName) :
    `${event.nameState === "unavailable" ? "Name unavailable" : "Unnamed session"} · ${event.sessionId.slice(0, 8)}`;
  return { title: bounded(name, 100),
    content: `Codex reply complete | Machine: ${bounded(clean(host), 256)} | Session: ${bounded(name, 1500)} | Completed: ${event.completedAt}` };
}

export async function resolveQueued(root: string, source: Source, sourceId: string, item: QueuedCompletion, options: ReaderOptions): Promise<{ host: string; event: Completion }> {
  const data = await runReader(root, source, { mode: "labels", home: options.home || undefined, sessionIds: [item.sessionId] }, options) as
    { schemaVersion?: number; sourceId?: string; hostname?: string; labels?: Array<Partial<Completion>> } | null;
  const label = data?.labels?.[0];
  if (!data || data.schemaVersion !== 3 || data.sourceId !== sourceId || typeof data.hostname !== "string" || data.hostname.length > 256 ||
      !label || label.sessionId !== item.sessionId || !["resolved", "unset", "unavailable"].includes(label.nameState || "") ||
      !["name", "title", "unset"].includes(label.nameSource || "") || !(typeof label.sessionName === "string" || label.sessionName === null)) {
    throw new Error("qq_labels_unavailable");
  }
  return { host: data.hostname, event: { ...label, ...item, evidence: "persisted-completion" } as Completion };
}

interface Sender { send(title: string, content: string): Promise<string> }

export class QqDelivery {
  private busy = false;
  constructor(private store: StateStore, private sender: (route: NotificationRoute) => Promise<Sender | undefined>, private append: (line: string) => void) {}

  async drain(sourceId: string, host: string, events: Completion[], label: string,
    resolve: (item: QueuedCompletion) => Promise<{ host: string; event: Completion }>, signal: AbortSignal): Promise<void> {
    if (this.busy || signal.aborted) return;
    this.busy = true;
    const ticket = randomUUID();
    let claimed = false, started = false;
    let revision = 0;
    let channel = "pushplus";
    try {
      const status = await this.store.qqStatus();
      if (!status.enabled || status.paused) return;
      const route = status.route || DEFAULT_ROUTE;
      revision = status.routeRevision || 0;
      channel = channelInfo(route.channel).label;
      const sender = await this.sender(route);
      if (!sender || signal.aborted) return;
      const { item } = await this.store.qqClaim(sourceId, ticket, revision);
      if (!item) return;
      claimed = true;
      const current = events.find(event => event.sessionId === item.sessionId && event.turnId === item.turnId);
      const resolved = current ? { host, event: current } : await resolve(item);
      const message = qqCompletionMessage(label.trim() || resolved.host, resolved.event);
      if (signal.aborted || !(await this.store.qqBegin(ticket, revision)).updated) return;
      started = true;
      const receipt = await sender.send(message.title, message.content);
      await this.store.qqFinish(ticket, "accepted", receipt, undefined, revision);
      this.append(`${channel}: request_accepted; recipient delivery unconfirmed`);
      claimed = false;
    } catch (error) {
      const code = error instanceof Error && /^qq_[a-z_0-9]+$/.test(error.message) ? error.message : "qq_delivery_unknown";
      if (claimed) {
        await this.store.qqFinish(ticket, started ? (code.startsWith("qq_rejected_") ? "rejected" : "unknown") : "retry", undefined, code, revision).catch(() => {});
        claimed = false;
      }
      this.append(`${channel}: ${code}`);
    } finally {
      if (claimed) await this.store.qqFinish(ticket, started ? "unknown" : "retry", undefined, undefined, revision).catch(() => {});
      this.busy = false;
    }
  }
}
