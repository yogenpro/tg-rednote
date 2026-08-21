# XHS → Telegram Bot: Implementation Plan

A self-hosted Telegram bot that accepts Xiaohongshu (RedNote) share links and replies with
the note's content rendered as native Telegram messages.

---

## 1. Goal & Non-Goals

**Goal.** User shares an `xhslink.com` URL to the bot; bot replies with the note's images
(or video) plus its text, as a native Telegram media group.

**Non-goals:**

- Instant View / Telegraph rendering (see §2.1)
- Hosting media on infrastructure we own (see §2.2)
- Multi-tenant SaaS operation (see §2.4)
- Public availability — each user deploys their own instance

**Scale assumption.** Dozens of notes per day, single-digit users per instance.

---

## 2. Architecture Decisions

### 2.1 No Instant View — send native Telegram media instead

IV templates are per-domain and Telegram's crawler fetches the page server-side. XHS serves a
login wall to datacenter IPs and renders client-side, so a template would see an empty page.
Templates also require Telegram approval.

The workaround — republish to Telegraph, which gets IV for free — was considered and dropped:

- `telegra.ph/upload` has been dead for years; only externally-hosted images embed
- That forces us to host media ourselves, which we don't want (§2.2)

**Decision:** send a media group + caption. Better mobile UX than IV for a photo-heavy note
anyway, and removes the entire publishing step.

### 2.2 No self-hosted media

Media must not touch storage we own or pay egress on. Two-tier approach:

1. **Preferred:** pass XHS CDN URLs directly to `sendMediaGroup` — Telegram's servers do the
   fetch. Zero bytes through our process. *Verify this works first; may fail on referer checks.*
2. **Fallback:** stream through — fetch from CDN with a proper `Referer`, pipe into the
   multipart upload, discard. In-memory is fine at this scale.

Direct-upload also raises the size ceiling (~50 MB vs ~5 MB photos / ~20 MB other via URL),
which matters for video notes.

### 2.3 Must run on a residential IP

XHS aggressively login-walls and rate-limits cloud IPs. This is the constraint that determines
hosting.

**Rejected: Cloudflare Workers.** Workers egress from shared CF datacenter ranges — among the
worst-reputation IP space from XHS's perspective. `fetch()` has no proxy configuration.

**Rejected: Workers + Gateway egress via Tunnel.** As of June 2026 this is technically possible
(VPC binding `cf1:network` → Cloudflare Mesh → Gateway → egress policy → `cloudflared` →
your IP), but it requires paid Workers VPC and Zero Trust tiers plus significant SASE plumbing.

**Rejected: Workers + fetch relay on home box.** Works and stays mostly free, but if we're
deploying a service on our own machine anyway, that service may as well handle everything.
Two deployments plus a tunnel to save nothing.

**Decision:** single Docker Compose stack on a residential-IP machine. Long polling, no
inbound exposure, no tunnel, no Access policy, no certificates.

### 2.4 Per-instance deployment, not per-user cookies

A shared bot where each user supplies their own XHS cookies was considered and rejected:

- **IP correlation.** All fetches egress from one residential IP. N accounts authenticating
  from one address is the canonical bot-farm fingerprint — stronger than any single-account
  signal. Risk lands on *users'* real accounts, not ours.
- **Credential custody.** XHS session cookies are full account access. Pasting them into
  Telegram puts plaintext in server-side history; `deleteMessage` only covers 48 hours.
- **UX reality.** Extracting `a1` / `web_session` needs devtools, and they expire weekly-ish.

**Decision:** ship a self-hosted image. Each user runs their own container, own bot token, own
cookie, own IP. Allowlist to specific Telegram user IDs.

### 2.5 Chat-based configuration

Cookies are supplied *through the bot*, not baked into deployment. This keeps the deploy
artifact non-user-specific and scriptable.

**The bot token is the one irreducible deploy-time secret** — the container can't poll without
it, so there's no channel to receive it through. Honest claim: "one env var to deploy."

Payoff: when the XHS session expires, the bot catches the auth failure and messages the owner
in the same window where the failure appeared. No SSH, no `.env` editing, no docs.

---

## 3. Components

```
┌─────────────────────────────────────────────────┐
│ docker-compose.yml    (residential IP machine)  │
│                                                 │
│  ┌────────────────┐      ┌───────────────────┐  │
│  │ bot            │─────▶│ xhs-downloader    │  │
│  │ long polling   │ HTTP │ API mode          │  │
│  │ TG_BOT_TOKEN   │      │ download=false    │  │
│  └───────┬────────┘      └───────────────────┘  │
│          │                                      │
│    ┌─────▼──────┐                               │
│    │ state.json │  owner, allowlist, cookie     │
│    └────────────┘                               │
└─────────────────────────────────────────────────┘
         │                          │
         ▼                          ▼
   Telegram Bot API          xiaohongshu.com
```

### xhs-downloader (sidecar)

- Upstream: `JoeanAmier/XHS-Downloader`, API mode
- **Run with the download switch OFF.** We only want metadata + media URLs. Side effect: the
  dedup database never engages (it only records completed downloads), so no shared volume and
  no stale-record trap where a second request returns empty.
- Version drift: current docs use `python main.py api` on port 5556; older releases used
  `main.py server` on 8000. Pin the tag.
- Cookies live here, in a mounted config the bot can rewrite.
- Upgrade path when XHS changes signing: `docker pull`, bot code untouched.

### bot

- Long polling (no webhook, no public endpoint)
- Allowlist check on every update
- Orchestrates: resolve → extract → send
- Owns `state.json`

---

## 4. Request Flow

1. Update arrives. Reject if sender not in allowlist (silent or generic reply).
2. Extract `xhslink.com` / `xiaohongshu.com` URL from message text.
3. Optional: in-memory LRU lookup by note ID (see §6).
4. `POST` to downloader API. It follows the redirect, preserving `xsec_token` from the share
   link, and returns note metadata + media URLs.
5. On auth failure → mark cookie stale, notify owner, abort (§7).
6. Branch on note type:
   - **Images:** chunk into media groups of ≤10, send. Caption on first item.
   - **Video:** send video directly.
   - **Live photos:** send the still only, unless configured otherwise — sending both doubles
     album length.
7. If note text > 1024 chars, truncate the caption and follow with a separate text message
   (4096 limit).

---

## 5. State Model

One small file on one volume. Everything else is disposable.

```json
{
  "owner_id": 123456789,
  "allowlist": [123456789],
  "pairing_code_used": true,
  "xhs_cookie": "...",
  "cookie_set_at": "2026-08-20T00:00:00Z",
  "cookie_status": "ok",
  "last_successful_fetch": "2026-08-20T00:00:00Z"
}
```

Backup story: one file. (Worth wiring into whatever backup pipeline gets stood up.)

---

## 6. Caching

Full statelessness means a re-shared link triggers a fresh XHS fetch — each one a chance to
trip rate limiting on an account we'd rather keep alive.

**Decision:** in-memory LRU keyed by note ID, empty on restart. Covers repeat shares within a
session, adds no durable state. Cache the resolved metadata, not media bytes.

---

## 7. Bootstrap & Security

### Owner bootstrap: pairing code

Naive "first `/start` wins" has a race — bot usernames are publicly searchable, so anyone who
finds the bot between deploy and the operator's first message owns the instance.

**Decision (Homebridge/Jellyfin pattern):**

1. Generate a random pairing code at startup if `owner_id` is unset
2. Print it to stdout
3. Require it in the first `/start`
4. Operator has terminal access by definition — `docker logs` is a free authenticated channel

Optionally also accept an `OWNER_ID` env var for fully-scripted deploys.

### BotFather settings

There is **no** BotFather option restricting who can DM a bot. Available controls are token
management, name/description/commands, group privacy, allow-in-groups, payments, domain,
ownership transfer, delete. Nothing gates private chats.

| Setting | Value | Why |
|---|---|---|
| `/setjoingroups` | **Disable** | Stops the bot being pulled into groups where an allowlisted user's presence might read as authorization |
| Group privacy mode | Enabled (default) | Irrelevant to DMs; no reason to loosen |
| Username | Random-ish suffix | Telegram global search surfaces bots by name. Obscurity only — hence the pairing code. Can't be scripted (claimed interactively, globally unique) |

The pairing code is enforced by shipped code; the two settings above depend on the operator
remembering. Rely on the former.

### Cookie handling

- **Delete the message on receipt.** Bots can delete incoming messages in private chats within
  48 hours; in a private chat this removes it for both sides. Parse → persist → `deleteMessage`
  → confirm with our own message.
- **Never log the cookie value.** Guard against leaking it via exception traces on a malformed
  paste.
- README should note the plaintext transited Telegram's servers regardless.

### Expiry loop

On auth failure: mark `cookie_status: stale`, message the owner with re-extraction
instructions, accept the new value in the same chat. Add `/status` showing cookie age and last
successful fetch.

---

## 8. Telegram Constraints (code against these)

| Constraint | Value |
|---|---|
| Media group size | 10 items (XHS notes go to 18 → chunk) |
| Caption on media | 1024 chars |
| Plain text message | 4096 chars |
| Upload by URL | ~5 MB photos, ~20 MB other |
| Direct upload | ~50 MB |
| `deleteMessage` window | 48 hours, incoming messages in private chats |

Photos and videos can mix within one album.

---

## 9. Build Order

1. **Spike: cookieless fetch.** Share links carry a fresh `xsec_token` — the least-defended
   path, and exactly our use case. If unauthenticated fetches work for freshly-shared notes,
   §7's entire cookie subsystem becomes optional. *Test this before building anything.*
2. **Spike: CDN URL passthrough.** Does `sendMediaGroup` accept `sns-webpic-qc.xhscdn.com`
   URLs, or do referer checks force stream-through? One-line difference, determines §2.2.
3. Compose stack: downloader sidecar + bot skeleton, hardcoded owner ID, images only.
4. Pairing code + allowlist + `state.json`.
5. Cookie ingestion, deletion, expiry loop, `/status`.
6. Video and live-photo branches.
7. LRU cache.
8. README: BotFather steps, username advice, cookie extraction walkthrough, Telegram-history
   caveat.

---

## 10. Open Questions

- Does cookieless fetch hold for fresh share links? (gates §7)
- Do XHS CDN URLs survive Telegram's server-side fetch? (gates §2.2)
- Current XHS-Downloader API surface — confirm against the pinned tag, not the docs
- Do we want a `/note <url>` command form, or bare-URL detection only?

