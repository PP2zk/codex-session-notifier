# pushplus channels

Open the **Codex Session Notifier** status bar menu and choose **Select notification channel: <current channel>**, or run **Codex Session Notifier: Select Notification Channel**. One selection applies to all updated windows sharing this local VS Code profile. The default is QQ. Your existing token and enabled state are preserved when upgrading under the same extension ID; reload existing windows before selecting another channel.

Configure recipient bindings in your own pushplus account first. Save a user token or message token with **Configure pushplus Token**. A message token can have provider-side channel or recipient rules; review those rules too. No token needs to appear in settings files or source code.

| Selection | API `channel` | Account prerequisite / `option` |
| --- | --- | --- |
| QQ | `qq` | Bind a robot. Empty option sends to yourself through the default robot. A configuration code can select another robot or QQ group. |
| WeChat | `wechat` | Follow and activate the pushplus service account. A provider-configured account code can be supplied when applicable. |
| WeChat ClawBot | `clawbot` | Bind and activate the bot. The provider requires periodic interaction; see its channel guide. |
| Email | `mail` | Verify your recipient email. Empty option uses the official sending account; a code selects a custom mail channel. |
| App | `app` | Install and bind the pushplus mobile app. |
| Browser / desktop | `extension` | Install and bind a supported pushplus browser extension or desktop client. |
| Webhook | `webhook` | Configure the integration in pushplus, then enter its **configuration code** (required). This includes DingTalk, Feishu, WeCom robots, Bark, Gotify and other integrations supported by pushplus. |
| WeCom application | `cp` | Configure the application in pushplus, then enter its configuration code (required). |
| China Mobile ClawBot | `cmcc` | Bind and activate the channel; restricted to China Mobile users. |
| SMS | `sms` | **Paid.** Bind a phone and maintain provider credits. The provider currently sends a fixed reminder that may omit session details. |
| Voice call | `voice` | **Paid.** Bind a phone and maintain provider credits; content follows the provider's restrictions. |

The input after channel selection is the provider's `option` **code**, not a webhook URL, email address or phone number. Leave it empty unless your selected channel requires a code or you deliberately want a configured destination. Account bindings and configuration codes determine the recipient; review them before sending.

Changing the selection disables automatic delivery. Optionally use **Send Test Notification**, then choose **Enable notifications**. The manual test shows its message before sending. SMS and voice are marked as paid, and enabling their automatic delivery requires an explicit Quick Pick confirmation. No test is mandatory. A submitted request may still arrive through the previous channel, and switching does not resend earlier completions.

This version sends through one selected channel using the existing SDK and fixed HTTPS endpoint, `https://www.pushplus.plus/send`. It supplies `channel`, optional `option`, `template: txt`, session title and minimal completion details. It does not add recipient lists (`to`), topics, callbacks or custom endpoints. Requests are deduplicated and spaced by the existing shared local queue. The configuration code is local metadata; the token stays in SecretStorage.

Only the original QQ workflow has user-confirmed real receipt. All additional channels are **implemented and tested offline against the documented request contract**, with mocked SDK transport, local database tests and command-flow tests. No added-channel account was configured and no real messages were sent during this development. Provider acceptance is asynchronous and does not confirm recipient delivery. Account restrictions, rendering, truncation, quotas and unattended availability can differ between channels.

Delivery-status counts are recent totals across all selected channels, rather than a per-channel history.

Official references, checked November 2, 2026:

- [Message API and channel parameters](https://www.pushplus.plus/doc/guide/api.html)
- [Channel catalog and setup links](https://www.pushplus.plus/doc/channel.html)
- [Email defaults and optional configuration code](https://www.pushplus.plus/doc/extend/mail.html)
- [WeChat ClawBot activation and interaction requirements](https://www.pushplus.plus/doc/channel/clawbot.html)
- [SMS restrictions and charging](https://www.pushplus.plus/doc/extend/sms.html)
