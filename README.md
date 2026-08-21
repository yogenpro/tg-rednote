# RedNote → Telegram

[![ci](https://github.com/yogenpro/tg-rednote/actions/workflows/ci.yml/badge.svg)](https://github.com/yogenpro/tg-rednote/actions/workflows/ci.yml)

A self-hosted Telegram bot. Share a Xiaohongshu (RedNote) link with it; it replies with the
note's images or video and its text, as native Telegram media.

No Instant View, no Telegraph, no media hosted by you: the note comes back as a media group.
The bot first offers Telegram the XHS CDN URLs so Telegram does the fetching; when Telegram
refuses those (it does — see below), it transparently streams the bytes through instead,
in memory, and never writes them to disk.

It also reads [1point3acres](https://www.1point3acres.com) threads, in DMs only — see
[1point3acres threads](#1point3acres-threads).

Design rationale for all of this lives in [PLAN.md](PLAN.md); this file is how to run it.

---

## Before you start

**Run it on a residential IP.** XHS login-walls and rate-limits datacenter ranges hard. A
laptop, NAS, or home server is the target; a VPS mostly is not. Nothing here listens on an
inbound port — the bot long-polls Telegram — so there is no tunnel, no certificate, and no
port to forward.

**One instance per person.** Each deployment has its own bot token, its own cookie, its own
IP, and an allowlist. Don't share one bot between people: several XHS sessions authenticating
from a single address is the canonical bot-farm fingerprint, and the risk lands on the
account holders (PLAN §2.4).

You need Docker with Compose v2.

---

## Quick start

**1. Make a bot.** Talk to [@BotFather](https://t.me/BotFather) → `/newbot`. Give it a
username with a random-ish suffix — Telegram's global search surfaces bots by name, and while
the pairing code below is what actually protects the instance, there's no reason to be
findable. Copy the token.

**2. Lock it down in BotFather** (`/mybots` → your bot → Bot Settings):

| Setting | Value | Why |
|---|---|---|
| Allow Groups? | **Off**, unless you want group collection | With it off the bot can only be DMed. Turn it on to let it watch groups for links (see *Collecting links from groups*) |
| Group Privacy | On (default) | Leave it on. The bot only needs to see message text, which privacy mode still delivers for links it is mentioned near — if group collection misses links, make the bot a group admin rather than loosening this |

There is no BotFather setting that restricts who can DM a bot. That's what the allowlist is
for.

**3. Configure and start.**

```bash
cp .env.example .env
$EDITOR .env          # set TG_BOT_TOKEN
docker compose up -d --build
docker compose logs -f bot
```

That builds the bot image and pulls the pinned XHS-Downloader sidecar. The bot waits for the
sidecar to report healthy before it starts. Nothing is published on a host port.

Both containers are `restart: unless-stopped` and cap their logs at 3×10 MB. The bot runs as
an unprivileged user (uid 10001) and its only writable path is the `bot-state` volume.

**4. Claim the instance.** The bot generates a pairing code at first start and prints it:

```bash
docker compose logs bot | grep /start
#   /start A7K-3QP
```

Send that exact line to your bot. You're now the owner and on the allowlist. Anyone who found
the bot before you gets nothing but a "this instance is unclaimed" notice.

(For scripted deploys, set `OWNER_ID` in `.env` instead and the pairing step is skipped.)

**5. Send it a link.** Copy a share link out of the RedNote app — `http://xhslink.com/a/…` —
and paste it in. Pasting the whole Chinese share blurb is fine; the link gets picked out of it.

---

**`rednote.com` links work too.** It is the same site under its international name, and it
serves the same notes — and usefully, not always at the same moment: when one domain walls a
page fetch, the bot retries the other, which recovers notes that would otherwise come back
without comments or without a video's smaller renditions. If your account is on rednote.com rather than xiaohongshu.com, share
the rednote.com form of a link: the bot's own page fetches follow the domain you shared, so
that is what makes a rednote.com session apply. (The downloader sidecar only speaks
xiaohongshu.com, so that hop is rewritten for it either way.)

## Do you need a cookie?

Probably not. Fresh share links carry an `xsec_token`, which is the least-defended path into a
note. Verified on 2026-08-20: an `xhslink.com/o/…` link resolved and returned full metadata
plus 11 image URLs with **no cookie at all**, and from a datacenter IP at that. Start without
one; the bot runs fine that way.

When XHS does start refusing, the bot tells you, marks the stored cookie stale, and takes the
replacement in the same chat window. To get one:

1. Open **xiaohongshu.com** in a browser where you're logged in
2. DevTools → Network → click any request → Request Headers
3. Copy the entire `Cookie` header value
4. Send it to the bot as `/cookie <value>` (or just paste it — it's recognised either way)

The bot deletes your message as soon as it has stored the value, which in a private chat
removes it for both sides. **The cookie still transited Telegram's servers to get there.** If
that matters to you, treat it as burned and rotate it by logging out and back in.

The cookie is stored in `/data/state.json` inside the bot container, mode 0600, and is passed
to the downloader per request — it is never written into the sidecar's config, and never
logged.

---

## Commands

| Command | Who | What |
|---|---|---|
| *(bare link)* | allowlist | Fetch and post the note |
| `/status` | allowlist | Cookie age and health, last successful fetch, sidecar reachability, cache stats |
| `/cookie <value>` | owner | Store the XHS cookie; the message is deleted on receipt |
| `/forgetcookie` | owner | Wipe the stored cookie |
| `/acres <cookie or cURL>` | owner | Store the 1point3acres session; the message is deleted on receipt |
| `/forgetacres` | owner | Wipe the stored 1point3acres session |
| `/allow <user_id>` · `/deny <user_id>` · `/users` | owner | Manage the allowlist |
| `/help` | allowlist | Usage |

### 1point3acres threads

A link to a [1point3acres](https://www.1point3acres.com) thread — any of `/home/thread/<id>`,
`/interview/thread/<id>` or the old `/bbs/thread-<id>-1-1.html` — comes back as a
[telegra.ph](https://telegra.ph) page and a single link, which Telegram opens with Instant
View. The page carries the opening post, its pictures, and the top replies with theirs.

This is the opposite of what the bot does for RedNote, on purpose. A note *is* its images, so
it comes back as native media; a forum thread is thousands of words with the pictures
incidental, and chunking that into four Telegram messages reads far worse than one page. Set
`ACRES_TELEGRAPH=false` for the chunked-message form — which is also the automatic fallback if
telegra.ph is unreachable, so an outage costs the format and not the thread.

Replies that answer another reply are nested under it, the way the forum shows them and the
way a note's comments carry their own replies — the quote block links back to the exact post
it answers, so the conversation reconstructs rather than being guessed at. Replies are ranked
by the post's own 好苗/杂草 score, not by the page's chronological order,
and not by the green/red bar next to each post — that one is labelled 全局 and measures the
*author's* lifetime reputation, not the reply. `ACRES_COMMENTS` sets how many conversations; they come off
the page the post was already read from, so they cost no extra request. A reply's own pictures
stay with that reply.

**Telegraph pages are public to anyone with the link.** That is the point of the feature, and
also why it is nowhere near the group and channel paths.

Thread links work everywhere note links do: in a DM, in a watched group, and on the channel.
**Note that a telegra.ph page is public to anyone with the link**, so publishing a thread to a
channel puts it somewhat further afield than the channel's own membership. Set `ACRES=false`
to turn the feature off entirely, or `ACRES_TELEGRAPH=false` to keep threads inside Telegram
as chunked messages.

The site runs a Cloudflare managed challenge across the whole domain, so unlike RedNote there
is no anonymous mode — it needs your browser's session before it will fetch anything:

1. Open any thread in a logged-in browser.
2. DevTools → Network → the document request → right-click → **Copy as cURL**.
3. Send the bot `/acres <paste>`.

Paste the whole cURL rather than just the cookie: Cloudflare ties `cf_clearance` to the exact
User-Agent that solved the challenge, so the bot stores the UA alongside the cookie. A bare
`Cookie` header works too if your browser is a recent Chrome, but a mismatch shows up as a
challenge that looks like a bad cookie. Like the RedNote cookie, the message is deleted on
receipt and the value is stored 0600 and never logged.

`cf_clearance` is also tied to the IP that earned it, so the browser and the bot want to be on
the same connection. Sessions expire; `/status` shows when the stored one last worked, and a
failed fetch marks it stale and tells the owner.

The site sprays anti-copy junk through every post — hidden `<font class="jammer">` elements
carrying strings like ". From 1point 3acres bbs", plus zero-width characters. None of it
reaches the message. Text hidden behind the points wall is marked `[…]` where it was, rather
than being quietly stitched over.

### Channel mode

Set `CHANNEL_ID` and a link stops being a request and becomes a **submission**: the bot posts
the note to your channel and replies to the submitter with a link to the post.

```
CHANNEL_ID=@my_rednote_channel     # or -1001234567890 for a private channel
```

The bot must be an **administrator of the channel with "Post Messages"**. That is checked once
at startup, so a misconfiguration shows up as one line in the log rather than as a failure on
someone's first submission:

```
publishing submissions to My Channel (@my_rednote_channel)
```

If the channel is unusable the bot still runs and answers submitters directly, so nothing is
lost while you fix the permission.

#### With a discussion group

Link a discussion group to the channel (**Edit → Discussion** in the Telegram app — there is no
Bot API for it) and **add the bot to that group as an administrator**. The bot finds it from the
channel's `linked_chat_id`; there is nothing to configure.

Telegram copies every channel post into the linked group, and a reply to that copy shows up as
a comment on the post. So the bot puts everything that isn't the main event underneath:

| | Channel post | Comments |
|---|---|---|
| First 10 media | ✓ | |
| Caption | note text | |
| Media 11+ | | ✓ marked `[2/2]` |
| Description overflow | | ✓ |
| Top comments | | ✓ always, even when they'd fit the caption |

The channel stays a clean feed; the detail lives in the thread. The copy is asynchronous —
measured at 6.5s in practice — so the bot waits for it (up to `Bot.THREAD_TIMEOUT`, 20s). If it
never arrives the extras are chained onto the channel post instead, so nothing is lost.

The bot only accepts submissions in direct messages. Anything said in the discussion group is
ignored, so it never talks back to people commenting.

#### Collecting links from groups

Add the bot to a group and links posted there become submissions too. It stays quiet there
apart from one thing: when a note reaches the channel, it replies to the message that carried
the link with a permalink to the post (or, for a link that's already up, a pointer to the
existing one). Nothing else — no progress, no errors, no answers to commands or chatter. A
failed fetch is silent in the group and visible only in the log.

A group starts being watched when **someone on the allowlist adds the bot to it**. If a stranger
adds the bot somewhere, the group is ignored and the owner is told how to opt in:

```
/groups                      list the groups being watched
/allowgroup <chat_id>        watch one the bot is already in
/denygroup  <chat_id>        stop watching
```

Being removed from a group stops the watch automatically. Group submissions are published under
the same rules as DM ones, including the duplicate check, so the same link shared in two groups
posts once.

* **Resubmissions don't duplicate.** Published note ids are recorded in `state.json`, so the
  second person to send the same link gets *"Already on the channel — see the post"* and the
  channel stays clean. This survives restarts; the index keeps the newest 1000 entries.
* **The post is self-contained**: album, caption, comments, and any description overflow all
  land in the channel, with follow-ups replying to the album so the post threads properly.
* **Submitters aren't named in the channel.** Who sent a link stays between them and the bot.
* Permalinks are `https://t.me/<name>/<id>` for a public channel and `https://t.me/c/<id>/<id>`
  for a private one — the latter only opens for channel members.
* `/status` reports the channel and how many notes have been published.

### What comes back

A note arrives as native Telegram media with the title, description, hashtags, author and a link
back in the caption.

* **Long descriptions** spill into follow-up text messages — the caption keeps the tail
  (hashtags, author, source link) and the rest follows.
* **Albums over 10 items** are split across several media groups, because that is Telegram's
  hard limit. Each part is captioned `[1/3]`, `[2/3]`, … and replies to the part before it, so
  the whole set reads as one threaded post rather than loose albums.
* **Top comments** (up to `COMMENTS`, default 5) come with each note — replies, like counts and
  poster location included. They are scraped from the note page: XHS's comment API needs a
  signed header, but the page embeds the first few comments. Where they land follows one rule —
  **the note's own text has first claim on the caption**:

  * short note → comments go in the caption, so forwarding the album carries them along;
  * long note → the text already needs a follow-up message, so the comments move there
    wholesale and ride *inside* that message, keeping the note to two messages rather than three.

  Whole comments are dropped from the end rather than cut mid-sentence. If the scrape fails the
  note is delivered anyway.

---

## Configuration

Everything except the token has a working default.

| Variable | Default | Notes |
|---|---|---|
| `TG_BOT_TOKEN` | — | Required. The only deploy-time secret. |
| `OWNER_ID` | unset | Skips the pairing code. |
| `MEDIA_MODE` | `auto` | `auto` hands CDN URLs to Telegram and falls back to streaming through the bot when a refusal comes back — remembered per CDN family, so ordinary notes keep the fast path. `url` and `upload` pin one behaviour. |
| `LIVE_PHOTOS` | `still` | `still`, `video`, or `both`. `both` doubles album length. |
| `MAX_UPLOAD_BYTES` | `52428800` | Cap when streaming through. Items over it are skipped and reported. |
| `CACHE_SIZE` / `CACHE_TTL_SECONDS` | `128` / `21600` | In-memory note cache; empty on restart. |
| `TAGS_IN_CAPTION` | `true` | Include the note's hashtags. |
| `LOG_FORMAT` | `text` | `json` gives one structured object per line for Alloy/Loki — see [OBSERVABILITY.md](OBSERVABILITY.md). |
| `DEBUG_UPDATES` | `false` | Dumps every incoming update, message text included. Diagnostics only — a mistyped cookie would land in the log. |
| `CHANNEL_ID` | unset | `@name` or `-100…`. Set it to run in channel mode — see below. |
| `COMMENTS` | `5` | Top comments (with their replies) posted as a follow-up message. `0` disables the extra page fetch. |
| `ACRES` | `true` | 1point3acres thread links, DM only. `false` turns the feature off entirely. |
| `ACRES_UA` | unset | Fallback User-Agent for a stored 1point3acres cookie that arrived without one. |
| `ACRES_COMMENTS` | `10` | Top replies attached to a thread, ranked by 好苗/杂草. `0` disables. Costs no extra request. |
| `ACRES_TELEGRAPH` | `true` | Publish threads to telegra.ph and reply with the link. `false` sends chunked messages instead, as does a telegra.ph outage. |

`/status` shows which media path is live: *CDN URL passthrough* means zero bytes moved through
your machine; *streaming through the bot* means at least one CDN family was refused and is being
streamed instead. Both happen in normal use — see the findings below.

---

## Giving XHS a different exit

The reason to run this at home is the IP: XHS login-walls datacenter ranges. If you would
rather run the bot on a server, you can keep just the **XHS-bound** traffic on a home
connection and leave Telegram on the server's own link.

Note what "XHS-bound" covers — it is not only the sidecar:

| Request | Made by |
|---|---|
| Note data | sidecar |
| Short-link resolution (`xhslink.com`) | bot |
| Comment scrape (note page) | bot |
| Media download when streaming through | bot |

Three of the four come from the bot, so routing the sidecar's network namespace alone would
leave most of it exiting from the server. `XHS_PROXY` covers all four: the bot proxies its own
XHS requests and passes the same proxy to the sidecar, which honours a per-request `proxy`.
The Telegram client and the hop to the sidecar are never proxied.

```
XHS_PROXY=http://tailscale:1055
```

Any HTTP proxy works. The compose file ships an optional Tailscale one that exits through a
machine on your tailnet — a Raspberry Pi at home, say:

```bash
# .env
TS_AUTHKEY=tskey-auth-...     # Tailscale admin console → Settings → Keys
TS_EXIT_NODE=home-pi          # the home machine, advertising itself as an exit node
XHS_PROXY=http://tailscale:1055

docker compose --profile tailscale up -d
```

The exit node has to be advertised (`tailscale up --advertise-exit-node` on the home machine)
and approved in the admin console. The container runs in **userspace mode**: no `TUN` device,
no `NET_ADMIN`, no changes to the host's routing table. Its only exposed surface is the HTTP
proxy on the compose network, so nothing else on the machine can accidentally start using the
tunnel.

Sanity checks once it's up:

```bash
docker compose exec tailscale tailscale status | head -3
docker compose exec tailscale tailscale ip -4
# what XHS would see:
docker compose exec bot python -c "import httpx;print(httpx.get('https://api.ipify.org',proxy='http://tailscale:1055').text)"
# and what Telegram sees, which should differ:
docker compose exec bot python -c "import httpx;print(httpx.get('https://api.ipify.org').text)"
```

`/status` shows the proxy when one is set. If the proxy dies, XHS fetches fail while the bot
stays up and answers — it does not silently fall back to the server's own IP.

---

## Verifying the two assumptions

`tools/spike.py` answers the open questions in PLAN §10 against a real link. It's stdlib-only,
so any Python 3.8+ runs it. Uncomment the `ports:` block for `xhs-downloader` in
`docker-compose.yml` first.

```bash
# §9.1 — does a cookieless fetch work for a freshly-shared link?
python3 tools/spike.py "http://xhslink.com/a/xxxxx"

# §9.2 — do XHS CDN URLs survive Telegram's server-side fetch?
python3 tools/spike.py "http://xhslink.com/a/xxxxx" --token "$TG_BOT_TOKEN" --chat <your id>
```

It reports what the downloader returned, what the CDN says with and without a referer (Telegram
sends none), and whether `sendMediaGroup` accepted the URLs. If it didn't, no action is needed —
`MEDIA_MODE=auto` already handles it — but now you know which path you're on.

Both were answered on 2026-08-20 against a real 11-image note:

* **§9.1 — cookieless fetch works.** A fresh `xhslink.com/o/…` link returned full metadata and
  all 11 media URLs with no cookie, from a datacenter IP.
* **§9.2 — CDN passthrough works for some media and not others.** The split is by CDN path,
  and it was only visible after testing a dozen notes:

  | Media | Telegram's fetcher |
  |---|---|
  | `ci.xiaohongshu.com/notes_pre_post/…` (ordinary note images) | accepts |
  | `ci.xiaohongshu.com/note_pre_post_uhdr/…` (Ultra-HDR images) | refuses |
  | `sns-*.xhscdn.com/stream/…` (video) | refuses |

  The refusal is spelled at least three ways — `WEBPAGE_CURL_FAILED`, `WEBPAGE_MEDIA_EMPTY`,
  and a plain `failed to get HTTP URL content` — so `auto` retries as an upload on *any* 400
  rather than matching strings, and remembers the refusal **per CDN family** (host + first path
  segment). One Ultra-HDR note therefore costs one wasted attempt and teaches the sender about
  that family only; ordinary notes keep the URL path for the life of the process.

Measured across live notes: ~14–19 s of that is the sidecar fetch, which dominates. An ordinary
image note lands in ~17 s (3 s of it Telegram), an Ultra-HDR album in ~28 s, a video in 45–75 s
(the bytes cross this machine twice), and a re-send of a cached note in ~3 s with no CDN traffic
at all.

---

## Operating it

**State.** One file: `state.json` on the `bot-state` volume — owner, allowlist, watched groups,
published index, and the XHS cookie. Losing it means re-pairing and re-adding the cookie;
nothing else in the stack is precious. Back it up with:

```bash
docker compose cp bot:/data/state.json ./state-backup.json   # contains the cookie — 0600 it
```

**Health.** Long polling has no port to probe, so the bot touches a heartbeat file every time
`getUpdates` returns and the container healthcheck reads its age. A wedged or crash-looping bot
goes `unhealthy` within a few minutes instead of sitting there looking fine:

```bash
docker compose ps          # bot   Up 2 hours (healthy)
```

**Upgrading the downloader.** When XHS changes its signing and fetches start failing across
the board, the fix is usually upstream, not here:

```bash
# bump the pinned tag in docker-compose.yml, then
docker compose pull xhs-downloader && docker compose up -d
```

The tag is pinned deliberately — the API surface moved between releases (`main.py server` on
8000 in older builds, `main.py api` on 5556 now). Don't switch it to `latest`.

**Logs.** `docker compose logs -f bot`. For a deployment you can't watch, set
`LOG_FORMAT=json` and ship stdout to Loki — [OBSERVABILITY.md](OBSERVABILITY.md) has the Alloy
config, the event vocabulary and the queries worth keeping. Cookie values are never logged, including in
tracebacks, and message text is only logged with `DEBUG_UPDATES=true`.

**Prebuilt image.** Every push to `main` whose tests pass publishes a multi-arch image (amd64 and arm64, so a
Raspberry Pi works) to the GitHub Container Registry:

```bash
docker compose pull bot && docker compose up -d    # skips the local build
```

Tags: `latest` tracks `main`, `sha-<commit>` pins an exact build, and `vX.Y.Z` appears when a
release is tagged.

**Running without Compose.** The image carries working defaults, so it needs only a token and
somewhere to reach the sidecar:

```bash
docker run -d --name xhs-bot --restart unless-stopped \
  -e TG_BOT_TOKEN=… -e XHS_DOWNLOADER_URL=http://your-sidecar:5556 \
  -v xhs-bot-state:/data ghcr.io/yogenpro/tg-rednote:latest
```

**Building against a remote Docker host** (`docker --context …`) works, but the `./xhs-volume`
bind mount resolves on the *remote* machine, so the sidecar there won't see the seeded
`settings.json`. Fine for a build check; for a real deployment, put the repo on the machine that
runs it.

---

## Troubleshooting

| Symptom | Cause |
|---|---|
| `another instance is polling this token (409)` | Two deployments share one token. Telegram allows one poller; stop the other. |
| `bot` container is `unhealthy` | The poll loop hasn't come round in 3 minutes. `docker compose logs bot` — usually a network partition or a wedged fetch. |
| `TG_BOT_TOKEN is required` on repeat | `.env` isn't being read. It must sit next to `docker-compose.yml`; the container restarts until it is. |
| Every fetch fails, `/status` says stale | Cookie expired, or XHS is rate-limiting this IP. Re-extract the cookie; if that doesn't help, wait it out. |
| Fetches fail from the very first try | Usually a datacenter IP. See the first section. |
| `downloader: ⚠️ unreachable` | `docker compose logs xhs-downloader` — most often a bad `settings.json` in `xhs-volume/`. |
| Some album items missing | They exceeded `MAX_UPLOAD_BYTES` while streaming through; the bot says which. |

---

## Development

```bash
uv run --python 3.12 --with 'httpx==0.28.1' --with pytest python -m pytest tests -q
```

49 tests, no network and no Telegram required: link parsing, payload normalisation across both
of the downloader's locales, caption budgeting against Telegram's UTF-16 limits, album
chunking, the URL→upload fallback, and the pairing/allowlist/cookie paths.

```
bot/app/
  main.py        long-polling loop
  handlers.py    allowlist, pairing, cookie custody, the note flow
  xhs.py         downloader client + payload normalisation
  media.py       caption assembly, album delivery, the §2.2 fallback
  telegram.py    Bot API client
  state.py       state.json
  cache.py       LRU
```
