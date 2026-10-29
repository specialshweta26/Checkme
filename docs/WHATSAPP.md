# WhatsApp channel

Message your Checkme agents from WhatsApp. One shared business number serves every Checkme user: each person
links their own phone number to their account with a one-time code, and messages are routed to that account's
agents only.

Two providers sit behind the same `ChannelProvider` interface (`app/channels/provider.py`), so routing, jobs,
approvals and the 24-hour window behave the same with either:

| | Twilio (default when `TWILIO_ACCOUNT_SID` is set) | Meta Cloud API |
|---|---|---|
| Webhook | `POST {base}/api/channels/whatsapp/twilio` (form-encoded) | `GET/POST {base}/api/channels/whatsapp/webhook` (JSON) |
| Signature | `X-Twilio-Signature` (HMAC-SHA1 of URL + sorted fields, auth token) | `X-Hub-Signature-256` (HMAC-SHA256 of body, app secret) |
| Text limit | 1600 characters per message | 4096 |
| Buttons | quick-reply Content template if `TWILIO_CONTENT_SID_APPROVAL` is set, otherwise "Reply YES or NO" / numbered list | native reply buttons |
| Outside 24h | `TWILIO_CONTENT_SID_NOTIFY` Content template | `WHATSAPP_TEMPLATE_NOTIFY` template |

`WHATSAPP_PROVIDER=twilio|meta` forces one. When it is empty, Twilio is used if `TWILIO_ACCOUNT_SID` is set,
otherwise Meta. Only the active provider's webhook answers; the other returns 404. The channel stays **off**
(Connectors shows "not set up") until the active provider's required settings are present. Meta Embedded Signup
(each business bringing its own number) can be added later as another provider without changing routing.

The API (`checkme-api`) is a private service. Webhooks reach it through the web app's `/api` proxy, which passes
the raw body and every header (including the signature headers) through unchanged.

## Twilio setup (one time)

Required on `checkme-api`:

| Variable | Value |
|---|---|
| `WHATSAPP_PROVIDER` | `twilio` (optional; implied by `TWILIO_ACCOUNT_SID`) |
| `TWILIO_ACCOUNT_SID` | `AC...` |
| `TWILIO_AUTH_TOKEN` | the account's auth token; validates `X-Twilio-Signature` and is the REST fallback credential |
| `TWILIO_WHATSAPP_NUMBER` | the WhatsApp sender, `whatsapp:+14155238886` or `+14155238886` |
| `TWILIO_API_KEY_SID` / `TWILIO_API_KEY_SECRET` | optional, preferred for the REST API (`SK...` + secret); falls back to SID + auth token |
| `WHATSAPP_PUBLIC_BASE_URL` | the public web URL Twilio calls, e.g. `https://checkme-web-kw68.onrender.com` (defaults to `PUBLIC_WEB_URL`) |
| `TWILIO_CONTENT_SID_APPROVAL` | optional quick-reply Content template for approvals (below) |
| `TWILIO_CONTENT_SID_NOTIFY` | optional approved Content template used outside the 24-hour window (below) |
| `TWILIO_STATUS_CALLBACK` | optional `true` to have Twilio post delivery status to `/api/channels/whatsapp/twilio/status` (failures are logged) |

Steps in the Twilio console:

1. **Sender.** Use an approved WhatsApp sender (*Messaging → Senders → WhatsApp senders*). The Twilio Sandbox
   (`+14155238886`) works for testing, but each tester must first send the sandbox's `join <word>` code, and
   sandbox sessions expire after 3 days.
2. **Webhook.** On the sender (or, for the sandbox, *Messaging → Try it out → WhatsApp sandbox settings*) set
   **When a message comes in** to

   ```
   https://checkme-web-kw68.onrender.com/api/channels/whatsapp/twilio
   ```

   with method **POST**. Twilio signs that exact URL, so it must match `WHATSAPP_PUBLIC_BASE_URL` +
   `/api/channels/whatsapp/twilio` character for character (scheme, host, no trailing slash). The status
   callback URL can stay empty.
3. **Optional approval buttons.** In *Messaging → Content Template Builder* create a **Quick reply** template:
   body `{{1}}`, two buttons with title `Approve` / id `{{2}}` and title `Deny` / id `{{3}}`. Inside the 24-hour
   window it needs no WhatsApp approval. Put its `HX...` SID in `TWILIO_CONTENT_SID_APPROVAL`. Without it,
   approvals arrive as text ending in "Reply YES to approve or NO to deny", and questions as a numbered list.
4. **Optional out-of-window notice.** Create a **Text** template like `Checkme: {{1}} Reply here to see it.`,
   submit it for WhatsApp approval (category *Utility*), and put its `HX...` SID in `TWILIO_CONTENT_SID_NOTIFY`.

The webhook answers with an empty TwiML `<Response></Response>` at once; the message is deduplicated on
`MessageSid` and handled in the background. Inbound media (`MediaUrl0`…) is downloaded with the account's basic
auth, only from `api.twilio.com`.

## Meta setup (one time)

1. **Create the app.** Go to <https://developers.facebook.com/apps>, then *Create app*, then *Other*, then
   *Business*. Link it to your Meta Business portfolio, then add the **WhatsApp** product.
2. **Add the phone number.** Open *WhatsApp → API Setup*. Add and verify the business phone number, or use the
   test number. Copy the **Phone number ID** into `WHATSAPP_PHONE_NUMBER_ID`. This is the ID, not the phone
   number itself.
3. **Create a permanent token.** Go to *Business Settings → Users → System users* and add a system user
   (Admin). Assign it the app and the WhatsApp account with full control. Then choose *Generate new token* for
   the app with the `whatsapp_business_messaging` and `whatsapp_business_management` permissions, and *Never*
   expiry. Put the token in `WHATSAPP_ACCESS_TOKEN`. The 24-hour token shown on API Setup works only for
   testing.
4. **Copy the app secret.** Go to *App settings → Basic → App secret* and put it in `WHATSAPP_APP_SECRET`.
   Every webhook POST is checked with an HMAC-SHA256 of the raw body using this secret.
5. **Set the verify token.** Pick any long random string and put it in `WHATSAPP_VERIFY_TOKEN`.
6. **Set the business number.** Put the business number as digits, for example `15551234567`, in
   `WHATSAPP_BUSINESS_NUMBER`. It is used for the `wa.me` link in Connectors.
7. **Deploy, then connect the webhook.** Deploy with the variables set (and `WHATSAPP_PROVIDER=meta` if any
   `TWILIO_*` value is also set). Open *WhatsApp → Configuration → Webhook → Edit*:
   - Callback URL: `https://checkme-web-kw68.onrender.com/api/channels/whatsapp/webhook` (in general
     `{PUBLIC_WEB_URL}/api/channels/whatsapp/webhook`)
   - Verify token: the same value as `WHATSAPP_VERIFY_TOKEN`
   - Click *Verify and save*. Meta sends `GET ?hub.mode=subscribe&hub.verify_token=…&hub.challenge=…` and the
     API echoes the challenge.
8. **Subscribe to messages.** Under *Webhook fields*, subscribe to **messages**.
9. **Optional: add a template.** Messages can go outside the 24-hour customer-service window only with an
   approved template. Create a *Utility* template with one body variable, for example
   `Checkme: {{1}} Reply here to see it.`. Once it is approved, set `WHATSAPP_TEMPLATE_NOTIFY` to its name
   (language `en` by default).
10. **Go live.** Switch the app to *Live* mode. While the app is in development mode, only numbers added as
    test recipients can message it.

On Render the variables live on `checkme-api` (`render.yaml` declares them with `sync: false`), so set the values
in the dashboard. See `.env.example` for local development.

## Linking a number

1. In Checkme, go to **Connectors → WhatsApp → Connect WhatsApp**. This shows a one-time code (valid for 15
   minutes) and an *Open WhatsApp* `wa.me` link with `link CODE` already typed.
2. Send that message from your phone. The number is linked to your account in E.164 format, and the page updates.
3. To unlink, use *Disconnect* in Connectors or send `/unlink` from WhatsApp.

A number that isn't linked never reaches any agent. It only gets a short "connect from the app" reply, at most
once an hour. Link attempts are rate-limited per number.

## Using it

| Message | What happens |
|---|---|
| any text | your default agent (or the one picked with `/use`) handles it |
| `tony: …` or `@tony …` | that agent handles it (fuzzy name match; if no agent matches, the whole text goes to the default agent) |
| `/use tony` · `/use default` | sticky agent for this number |
| `/agents` | your agents with titles and skills |
| `/room launch` · `/room off` | talk to a group room (its lead agent answers and can delegate) |
| `/new` | start a fresh conversation thread |
| `/help` · `/unlink` | help · disconnect |
| voice note | transcribed with `OPENAI_TRANSCRIBE_MODEL` (whisper-1), then handled as text |
| photo / document | downloaded from the provider, stored like a chat upload, and passed to the agent with its `attachment_key` |

Each message becomes or continues a **background job** on the existing `agent_jobs` worker, so the agent has its
full capabilities: skills, computer, memory and task state. It runs in a per (user, agent, WhatsApp) chat thread
(`ChatSession.state.channel = "whatsapp"`), so context persists and the thread also shows in the app under that
agent, with the live job timeline.

Replies work like this:

- a quick acknowledgement;
- at most 4 progress updates per job, at least 90 seconds apart (plan and status milestones);
- the final answer with markdown converted to WhatsApp formatting (`*bold*`, `_italic_`, `~strike~`, code
  blocks), split into messages of at most 4096 characters (1600 on Twilio);
- files the agent attached, sent as media.

**Approval cards** arrive as *Approve* / *Deny* reply buttons (on Twilio without an approval template, as text:
reply `yes` or `no`). **Questions** arrive as buttons when there are up to 3 options (numbered text on Twilio);
otherwise they come as a numbered list, and you can reply with the number. A button is accepted only
from a number linked to the job's own user.

## 24-hour window

WhatsApp allows free-form messages only within 24 hours of your last message. Checkme stores the time of the last
inbound message for each number. Outside the window:

- the reply is held in the link's outbox;
- if `WHATSAPP_TEMPLATE_NOTIFY` (Meta) or `TWILIO_CONTENT_SID_NOTIFY` (Twilio) is set, the template is sent so you know something is waiting;
- the held messages go out as soon as you message again.

## Security notes

- Meta: the `X-Hub-Signature-256` HMAC is checked over the raw body; a missing or bad signature gets 401.
- Twilio: `X-Twilio-Signature` is checked against the public URL and the form fields; a missing or bad
  signature gets 403.
- Webhook retries are deduplicated by message ID (WhatsApp message id or Twilio `MessageSid`) (`channel_inbound` table). The webhook returns 200
  right away and processes messages in the background.
- Everything is scoped to the link's `user_id`. Agents, rooms and jobs are looked up only within that user.
- Messages are rate-limited per number (`WHATSAPP_RATE_LIMIT_PER_MINUTE`, default 20), along with link attempts
  and link-code creation.
- Tokens, the app secret and message bodies are never logged. Phone numbers in logs are masked.
