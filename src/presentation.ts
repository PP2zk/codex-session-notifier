import type { ActiveSession, ActivitySource, QqStatus } from "./monitor";
import { channelInfo } from "./channels";

export function plainLabel(value: string, max = 256): string {
  return value.replace(/[\p{Cc}\p{Cf}]/gu, " ").replace(/\s+/g, " ").trim().slice(0, max);
}

export function sessionLabel(session: ActiveSession): string {
  return session.nameState === "resolved" && session.sessionName ? plainLabel(session.sessionName) :
    `${session.nameState === "unavailable" ? "Name unavailable" : "Unnamed session"} · ${session.sessionId.slice(0, 8)}`;
}

function markdownText(text: string): string {
  return plainLabel(text).replace(/&/g, "&amp;").replace(/</g, "&lt;").replace(/>/g, "&gt;")
    .replace(/[\\`*_{}\[\]()#+.!|~$-]/g, "\\$&");
}

export function notificationState(qq: QqStatus): string { return !qq.enabled ? "Disabled" : qq.paused ? "Paused" : "Enabled"; }

export function insetHover(content: string): string {
  // Native hover CSS owns padding and removes the last paragraph margin.
  // Static edge blocks preserve vertical space, as in Agent Usage Bar.
  // User labels remain escaped Markdown; only these fixed tags use HTML.
  const body = content.split("\n").map(line => {
    if (!line) return line;
    if (line === "---") return "<h6></h6>\n\n---\n\n<table><tr><td></td></tr></table>";
    if (line.startsWith("- ")) return `${line}&emsp;`;
    const heading = /^(#{1,6} )(.*)$/.exec(line);
    return heading ? `${heading[1]}&emsp;${heading[2]}&emsp;` : `&emsp;${line}&emsp;`;
  }).join("\n");
  return `<h1></h1>\n\n${body}\n\n<h1></h1>`;
}

export function runningCount(sources: ActivitySource[], now = Date.now()): number {
  return sources.filter(source => now / 1000 - source.updatedAt <= 30).reduce((total, source) => total + source.runningCount, 0);
}

export function hoverContent(qq: QqStatus, sources: ActivitySource[], monitor: string, now = Date.now(), checkingSources: ReadonlySet<string> = new Set()): string {
  const count = runningCount(sources, now);
  const lines = ["**Codex Session Notifier**", "", `${qq.enabled && !qq.paused ? "$(bell)" : "$(bell-slash)"} ${channelInfo(qq.route?.channel).label} notifications **${notificationState(qq)}**`, "", "---", "", `**Running sessions · ${count}**`, ""];
  let shown = 0;
  for (const source of [...sources].sort((a, b) => a.machine.localeCompare(b.machine) || a.sourceId.localeCompare(b.sourceId))) {
    if (!source.runningCount || shown >= 12 || now / 1000 - source.updatedAt > 30) continue;
    lines.push(`**${markdownText(source.machine)}**`, "");
    for (const session of [...source.sessions].sort((a, b) => a.sessionId.localeCompare(b.sessionId))) {
      if (shown >= 12) break;
      const since = session.startedAt ? Date.parse(session.startedAt) : NaN;
      const seconds = Number.isFinite(since) ? Math.max(0, Math.floor((now - since) / 1000)) : null;
      const duration = seconds === null ? "" : ` · ${seconds < 60 ? "<1m" : seconds < 3600 ? `${Math.floor(seconds / 60)}m` : `${Math.floor(seconds / 3600)}h ${Math.floor(seconds % 3600 / 60)}m`}`;
      lines.push(`- ${markdownText(sessionLabel(session))}${duration}`);
      shown++;
    }
    lines.push("");
  }
  if (!count) lines.push("No confirmed running sessions.", "");
  if (count > shown) lines.push(`+ ${count - shown} more running sessions`, "");
  const unknown = sources.reduce((sum, source) => sum + source.unknown, 0);
  if (unknown) lines.push(`$(info) Activity is still being checked for ${unknown} session${unknown === 1 ? "" : "s"}.`, "");
  const stale = sources.filter(source => now / 1000 - source.updatedAt > 30).sort((a, b) => a.sourceId.localeCompare(b.sourceId));
  const checking = stale.filter(source => checkingSources.has(source.sourceId));
  const delayed = stale.filter(source => !checkingSources.has(source.sourceId));
  if (checking.length) lines.push(`Checking source status: ${checking.map(source => markdownText(source.machine)).join(", ")}.`, "");
  if (delayed.length) lines.push(`$(history) Source updates delayed: ${delayed.map(source => markdownText(source.machine)).join(", ")}.`, "");
  if (!sources.length) lines.push("Waiting for a fresh source snapshot.", "");
  const state = monitor.split(", ").map(code => ["monitoring", "read_backlog", "monitored_in_another_window"].includes(code) ? "Monitoring" :
    code === "execution_source_unverified" ? "Some completion notifications await source verification (Open menu > Show logs)" : code.replace(/_/g, " ")).join(", ");
  lines.push("---", "", `Current window: ${markdownText(state)}`, "",
    "[Open menu](command:codexSessionNotifier.openMenu) · [Refresh](command:codexSessionNotifier.refreshStatus)");
  return insetHover(lines.join("\n"));
}
