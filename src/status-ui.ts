import * as vscode from "vscode";
import { ActivitySource, QqStatus, StateStore } from "./monitor";
import { hoverContent, insetHover, notificationState, runningCount } from "./presentation";
import { channelInfo } from "./channels";

export class StatusUI implements vscode.Disposable {
  readonly item = vscode.window.createStatusBarItem("codexSessionNotifier", vscode.StatusBarAlignment.Right, 20);
  private qq: QqStatus = { enabled: false, paused: null, counts: {} };
  private sources: ActivitySource[] = [];
  private monitor = "starting";
  private loaded = false;
  private readonly startupUntil = Date.now() + 30000;
  private readonly freshSources = new Set<string>();
  private readError = false;
  private menuOpen = false;
  private interactions = 0;
  private interactionFocus?: PromiseLike<unknown>;
  private refreshPromise?: Promise<void>;
  private refreshQueued = false;
  private drawTimer?: ReturnType<typeof setTimeout>;
  private rendered = false;
  private firstContent?: string;
  private firstSeenAt?: number;
  private firstChangedAt = 0;
  private stopped = false;
  private timer: ReturnType<typeof setInterval>;

  constructor(private store: () => StateStore, private refreshSource: () => Promise<void>) {
    this.item.name = "Codex Session Notifier";
    this.item.command = "codexSessionNotifier.openMenu";
    // Keep the entry available while loading, without presenting guessed QQ
    // settings or an empty session list as the first hover snapshot.
    this.item.text = "$(comment-discussion) Codex";
    this.item.accessibilityInformation = { label: "Codex Session Notifier: Loading status. Open menu." };
    this.item.show();
    this.timer = setInterval(() => void this.refresh(), 5000);
    void this.refresh();
  }

  setMonitor(message: string): void { if (this.monitor !== message) { this.monitor = message; this.scheduleDraw(); } }

  refresh(): Promise<void> {
    if (this.stopped) return Promise.resolve();
    if (this.refreshPromise) {
      this.refreshQueued = true;
      return this.refreshPromise;
    }
    this.refreshPromise = this.readOverview().finally(() => {
      this.refreshPromise = undefined;
      this.scheduleDraw();
    });
    return this.refreshPromise;
  }

  private async readOverview(): Promise<void> {
    do {
      this.refreshQueued = false;
      try {
        const overview = await this.store().activityOverview();
        if (this.stopped) return;
        this.qq = overview.qq;
        this.sources = overview.sources;
        for (const source of this.sources) {
          if (Date.now() / 1000 - source.updatedAt <= 30) this.freshSources.add(source.sourceId);
        }
        this.loaded = true;
        this.readError = false;
      } catch {
        if (!this.stopped) this.readError = true;
      }
    } while (this.refreshQueued && !this.stopped);
  }

  private scheduleDraw(): void {
    if (this.stopped || this.drawTimer) return;
    this.drawTimer = setTimeout(() => {
      this.drawTimer = undefined;
      if (!this.stopped && !this.refreshPromise) this.draw();
    }, 100);
  }

  private draw(): void {
    if (!this.readError && (!this.loaded || this.monitor === "starting" ||
        (this.monitor === "monitored_in_another_window" && !this.sources.length))) return;
    const count = this.readError ? 0 : runningCount(this.sources);
    const text = `$(comment-discussion) Codex${!this.readError && count ? ` · ${count} running` : ""}`;
    if (this.item.text !== text) this.item.text = text;
    const now = Date.now();
    const checkingSources = new Set(now < this.startupUntil ?
      this.sources.filter(source => !this.freshSources.has(source.sourceId)).map(source => source.sourceId) : []);
    const content = this.readError ? insetHover("**Codex Session Notifier**\n\nSession status is unavailable.\n\n[Open menu](command:codexSessionNotifier.openMenu) · [Refresh](command:codexSessionNotifier.refreshStatus)") :
      hoverContent(this.qq, this.sources, this.monitor, now, checkingSources);
    if (!this.rendered && !this.readError) {
      const now = Date.now();
      this.firstSeenAt ??= now;
      if (this.firstContent !== content) { this.firstContent = content; this.firstChangedAt = now; }
      // Fold the first discoveries and short-lived scan diagnostics into one
      // card. Never hold it indefinitely if a busy source keeps changing.
      const settle = Math.min(2000 - (now - this.firstChangedAt), 8000 - (now - this.firstSeenAt));
      if (settle > 0) {
        this.drawTimer = setTimeout(() => {
          this.drawTimer = undefined;
          if (!this.stopped && !this.refreshPromise) this.draw();
        }, settle);
        return;
      }
    }
    this.rendered = true;
    if (!this.interactions && (this.item.tooltip as vscode.MarkdownString | undefined)?.value !== content) {
      const tooltip = new vscode.MarkdownString(content, true);
      tooltip.supportHtml = true;
      tooltip.isTrusted = { enabledCommands: ["codexSessionNotifier.openMenu", "codexSessionNotifier.refreshStatus"] };
      this.item.tooltip = tooltip;
    }
    const label = this.readError ? "Codex Session Notifier: Status unavailable. Open menu." :
      `Codex Session Notifier: ${channelInfo(this.qq.route?.channel).label} ${notificationState(this.qq)}, ${count} running sessions. Open menu.`;
    if (this.item.accessibilityInformation?.label !== label) this.item.accessibilityInformation = { label };
  }

  async showMenu(): Promise<void> {
    if (this.stopped || this.menuOpen) return;
    this.menuOpen = true;
    try {
      await this.runInteraction(() => this.showMenuActions());
    } finally {
      this.menuOpen = false;
    }
  }

  async runInteraction<T>(action: () => T | PromiseLike<T>): Promise<T> {
    const first = this.interactions++ === 0;
    if (first && !this.stopped) this.item.tooltip = undefined;
    try {
      // Set the return target BEFORE opening any Quick Pick/InputBox, including
      // nested configuration steps. Closing or cancelling then returns to the
      // editor instead of focusing the status-bar item and opening its hover.
      // Do not move focus afterwards: actions may intentionally open settings.
      if (first) this.interactionFocus = vscode.commands.executeCommand("workbench.action.focusActiveEditorGroup");
      await this.interactionFocus;
      return await action();
    } finally {
      this.interactions--;
      if (!this.interactions) {
        this.interactionFocus = undefined;
        if (!this.stopped) this.draw();
      }
    }
  }

  private async showMenuActions(): Promise<void> {
    await this.refresh();
    const channel = channelInfo(this.qq.route?.channel).label;
    const items: Array<vscode.QuickPickItem & { command?: string }> = [
      { label: this.qq.enabled && !this.qq.paused ? "$(bell-slash) Disable notifications" : this.qq.paused ? "$(bell) Resume notifications" : "$(bell) Enable notifications",
        description: "All monitored windows on this computer",
        detail: this.qq.enabled && !this.qq.paused ? "Stop future automatic requests. Requests already submitted may still arrive." : "Notify new reply completions; earlier completions will not be sent.",
        command: this.qq.enabled && !this.qq.paused ? "codexSessionNotifier.disableNotifications" : "codexSessionNotifier.enableNotifications" },
      ...(this.qq.paused ? [{ label: "$(bell-slash) Disable notifications", command: "codexSessionNotifier.disableNotifications" }] : []),
      { label: "pushplus", kind: vscode.QuickPickItemKind.Separator },
      { label: `$(list-selection) Select notification channel: ${channel}`, command: "codexSessionNotifier.selectChannel" },
      { label: "$(send) Send test notification", command: "codexSessionNotifier.testNotification" },
      { label: "$(key) Configure pushplus token", command: "codexSessionNotifier.configureToken" },
      { label: "$(info) Show delivery status", command: "codexSessionNotifier.deliveryStatus" },
      { label: "$(trash) Remove pushplus token", command: "codexSessionNotifier.removeToken" },
      { label: "Monitor", kind: vscode.QuickPickItemKind.Separator },
      { label: "$(refresh) Refresh session list", command: "codexSessionNotifier.refreshStatus" },
      { label: "$(output) Show logs", command: "codexSessionNotifier.showStatus" },
      { label: "$(settings-gear) Open settings", command: "codexSessionNotifier.openSettings" }
    ];
    const selected = await vscode.window.showQuickPick(items, {
      title: `Codex Session Notifier · ${channel} ${notificationState(this.qq)}`,
      placeHolder: `${runningCount(this.sources)} running sessions · Choose an action`, matchOnDescription: true, matchOnDetail: true
    });
    if (selected?.command) {
      if (selected.command === "codexSessionNotifier.refreshStatus") await this.refreshSource();
      else await vscode.commands.executeCommand(selected.command);
      await this.refresh();
    }
  }

  snapshot() { return { text: this.item.text, tooltip: (this.item.tooltip as vscode.MarkdownString | undefined)?.value || "", command: this.item.command }; }

  dispose(): void { this.stopped = true; clearInterval(this.timer); if (this.drawTimer) clearTimeout(this.drawTimer); this.item.dispose(); }
}
