import { request } from "node:https";
import type { HttpRequester, HttpRequestOptions, HttpResponse, PushPlusClient } from "@perk-net/perk-pushplus-sdk" with { "resolution-mode": "import" };
import { DEFAULT_ROUTE, NotificationRoute, validateRoute } from "./channels";

// The session name leads the QQ summary; full identification stays in details.
export const QQ_TEST_MESSAGE = {
  title: "Test task",
  content: "Codex test notification | Machine: Local | Session: Test task"
} as const;

/** SDK transport with direct HTTPS, a total deadline and bounded responses. */
export class DirectPushplusTransport implements HttpRequester {
  execute(options: HttpRequestOptions): Promise<HttpResponse> {
    const url = new URL(options.url);
    if (url.origin !== "https://www.pushplus.plus" || url.pathname !== "/send" || url.search || url.username || url.password || options.method !== "POST") {
      return Promise.reject(new Error("qq_endpoint_rejected"));
    }
    return new Promise((resolve, reject) => {
      const body = options.body || "";
      const req = request(url, { method: "POST", agent: false, rejectUnauthorized: true,
        headers: { "Content-Type": "application/json;charset=UTF-8", "Content-Length": Buffer.byteLength(body),
          "User-Agent": "codex-session-notifier" } }, response => {
        let bytes = 0;
        const chunks: Buffer[] = [];
        response.on("data", (chunk: Buffer) => {
          bytes += chunk.length;
          if (bytes > 65536) { response.destroy(); req.destroy(new Error("delivery_unknown")); return; }
          chunks.push(chunk);
        });
        response.on("end", () => { clearTimeout(timer); resolve({ statusCode: response.statusCode || 0, body: Buffer.concat(chunks).toString("utf8") }); });
        response.on("error", () => { clearTimeout(timer); reject(new Error("delivery_unknown")); });
        response.on("aborted", () => { clearTimeout(timer); reject(new Error("delivery_unknown")); });
      });
      const timer = setTimeout(() => { reject(new Error("delivery_unknown")); req.destroy(); }, 10000);
      req.on("error", () => { clearTimeout(timer); reject(new Error("delivery_unknown")); });
      req.end(body);
    });
  }
}

export function validQQToken(value: string): boolean { return /^[A-Za-z0-9_-]{16,256}$/.test(value); }

export class PushplusSender {
  private client?: PushPlusClient;
  private busy = false;

  private readonly route: NotificationRoute;
  constructor(private token: string, private transport: HttpRequester = new DirectPushplusTransport(), route: NotificationRoute = DEFAULT_ROUTE) {
    if (!validQQToken(token)) throw new Error("invalid_qq_token");
    validateRoute(route);
    this.route = { ...route };
  }

  sendTest(title: string, content = title): Promise<string> { return this.send(title, content); }

  async send(title: string, content: string): Promise<string> {
    if (this.busy) throw new Error("qq_send_in_progress");
    if (!title || title.length > 256 || /[\p{Cc}\p{Cf}]/u.test(title)) throw new Error("invalid_qq_title");
    if (!content || content.length > 2048 || /[\p{Cc}\p{Cf}]/u.test(content)) throw new Error("invalid_qq_content");
    this.busy = true;
    try {
      const sdk = await import("@perk-net/perk-pushplus-sdk");
      this.client ??= new sdk.PushPlusClient({ token: this.token, baseUrl: "https://www.pushplus.plus", logRequest: false,
        connectTimeoutMs: 10000, readTimeoutMs: 10000, httpRequester: this.transport });
      // Account bindings live in pushplus. No topic, to, callbacks or endpoint overrides.
      const receipt: unknown = await this.client.send({ title, content, channel: this.route.channel, template: sdk.Template.TXT,
        ...(this.route.option ? { option: this.route.option } : {}) });
      const code = typeof receipt === "string" ? receipt : Array.isArray(receipt) ?
        receipt.find(row => row?.channel === this.route.channel && row?.code === 200)?.shortCode : undefined;
      if (typeof code !== "string" || !/^[A-Za-z0-9_-]{1,128}$/.test(code)) throw new Error("qq_receipt_unconfirmed");
      return code;
    } catch (error) {
      // SDK error messages can include response bodies. Expose a code only.
      if (error instanceof Error && error.name === "PushPlusError" && "code" in error && typeof error.code === "number" && Number.isInteger(error.code) && error.code > 0) throw new Error(`qq_rejected_${error.code}`);
      if (error instanceof Error && error.message === "qq_receipt_unconfirmed") throw error;
      throw new Error("qq_delivery_unknown");
    } finally { this.busy = false; }
  }
}

// Preserve the existing sender API and token/error names for local compatibility.
export { PushplusSender as QqSender };
