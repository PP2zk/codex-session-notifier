// Official /send channels: https://www.pushplus.plus/doc/guide/api.html
export const CHANNELS = [
  { id: "qq", label: "QQ", detail: "Default personal robot; option can select a configured robot or group." },
  { id: "wechat", label: "WeChat", detail: "Follow and activate the pushplus WeChat service account." },
  { id: "clawbot", label: "WeChat ClawBot", detail: "Bind and activate in pushplus; recurring user interaction is required." },
  { id: "mail", label: "Email", detail: "Verify your email in pushplus; option optionally selects a custom mail channel." },
  { id: "app", label: "App", detail: "Bind the pushplus mobile app." },
  { id: "extension", label: "Browser / desktop", detail: "Bind a pushplus browser extension or desktop client." },
  { id: "webhook", label: "Webhook", detail: "Configure a webhook in pushplus; enter its configuration code, not its URL.", requiredOption: true },
  { id: "cp", label: "WeCom application", detail: "Configure a WeCom application in pushplus and enter its configuration code.", requiredOption: true },
  { id: "cmcc", label: "China Mobile ClawBot", detail: "Bind and activate in pushplus; China Mobile users only." },
  { id: "sms", label: "SMS", detail: "Paid channel. Bind a phone in pushplus. Fixed reminder text may omit session details.", paid: true },
  { id: "voice", label: "Voice call", detail: "Paid channel. Bind a phone in pushplus; content is subject to provider restrictions.", paid: true }
] as const;

export type ChannelId = typeof CHANNELS[number]["id"];
export interface NotificationRoute { channel: ChannelId; option: string }
export const DEFAULT_ROUTE: NotificationRoute = { channel: "qq", option: "" };

export function channelInfo(id: string = "qq") {
  const found = CHANNELS.find(channel => channel.id === id);
  if (!found) throw new Error("qq_invalid_channel");
  return found;
}

export function validOption(value: string): boolean {
  return value.length <= 256 && value === value.trim() && !/[\p{Cc}\p{Cf}]/u.test(value) && !/^https?:/i.test(value);
}

export function validateRoute(route: NotificationRoute): void {
  const info = channelInfo(route.channel);
  if (typeof route.option !== "string" || !validOption(route.option) || ("requiredOption" in info && !route.option)) {
    throw new Error("qq_invalid_option");
  }
}
