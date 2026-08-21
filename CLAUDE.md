# tg-rednote — working notes for Claude

A self-hosted Telegram bot that turns Xiaohongshu (RedNote) share links into native Telegram
media groups. Read `PLAN.md` for the design rationale and `README.md` for how to run it. This
file is for whoever picks the work up next; `TODO.md` holds the open items.

## Ground rules for this repo

- **`PLAN.md` is a historical record. Do not edit it.** It captures the design as it stood
  before the code existed. Where reality diverged, the divergence is written down here and in
  `README.md`, not by rewriting the plan.
- **Never log message text, and never let a cookie reach a traceback.** A user pastes their
  cookie as a plain message; anything that logs message bodies leaks it. `xhs.py` raises
  `from None` for exactly this reason (PLAN §7). Inbound logging records lengths and the parsed
  link, never the text.
- **Tests run without network, Telegram, or the sidecar.** `tests/conftest.py` carries a tiny
  asyncio shim so there's no pytest-asyncio dependency. Keep it that way.
- Every behaviour learned from live traffic should land as a regression test — most of the
  sharp edges here were invisible until a real note hit them.

## Layout

```
bot/app/
  main.py       polling loop, graceful shutdown, per-update task guard
  handlers.py   allowlist, pairing bootstrap, cookie custody, the note flow
  xhs.py        sidecar client, link parsing/resolution, payload normalisation
  media.py      caption assembly, album chunking, URL-vs-upload delivery
  comments.py   top comments scraped from the note page
  acres.py      1point3acres threads: link shapes, de-jamming, DM-only delivery
  state.py      atomic 0600 state.json (owner, allowlist, cookie, health)
  telegram.py   raw Bot API client (429 handling, token redaction)
  cache.py      LRU with TTL
tools/spike.py  stdlib-only probe for the two PLAN §10 assumptions
```

## Things that are true and non-obvious

**The sidecar.** `joeanamier/xhs-downloader:2.7` in API mode: `POST /xhs/detail` on 5556, keys
in Chinese, note-type *values* localised (视频/图文/图集 or video/image/LivePhoto). It accepts a
per-request `cookie`, so the bot never rewrites the sidecar's config. It returns HTTP 200 on
failure with `data: null` and the reason in `message` — check the body, not the status.

**Resolve short links here, not in the sidecar.** The sidecar's own resolver intermittently
returns `提取小红书作品链接失败` for `xhslink.com` links that redirect fine from this process.
`XhsDownloader.resolve()` follows short links with browser headers and hands the sidecar a
canonical `www.xiaohongshu.com/...` URL carrying the `xsec_token`. Long URLs are only
string-normalised — following them lands on `/website-login/error?redirectPath=…` and throws the
note id away (`_unwrap_login_wall` recovers it if a redirect does end there).

**Telegram accepts some XHS CDN URLs and refuses others**, split by path:
`ci.xiaohongshu.com/notes_pre_post/…` is accepted, `note_pre_post_uhdr/…` (Ultra-HDR) and
`sns-*.xhscdn.com/stream/…` (video) are refused. The refusal is spelled at least three ways
(`WEBPAGE_CURL_FAILED`, `WEBPAGE_MEDIA_EMPTY`, `failed to get HTTP URL content`), so auto mode
retries as an upload on *any* 400 and remembers the refusal **per CDN family** rather than
flipping one process-wide flag. A family is host + leading directory, ignoring segments that are
just a token or a timestamp (`ci.xiaohongshu.com/<token>`, `sns-webpic-qc.xhscdn.com/<minute>/…`)
— otherwise every image mints its own family and nothing is ever learned. And when an album
mixes families, only the item Telegram names in `failed to send message #N` is blamed: a single
Ultra-HDR image otherwise condemns the ordinary family it travelled with.

**Caption limits are UTF-16 code units on the parsed text.** Markup and href targets are free;
emoji cost two. `tg_len` exists for this. Measure captions with tags stripped, or the tests lie.

**The note page is the sidecar's backstop** (`page.py`). XHS's signed web API — which the
sidecar uses — refuses some notes with `获取小红书作品数据失败` while the server-rendered page
serves the same note to an anonymous browser. `detail()` therefore tries the page before giving
up, and only re-raises the sidecar's error if that fails too. Verified live on
`http://xhslink.com/o/5TWUMDydMKo`. Page media comes from `sns-webpic-qc.xhscdn.com/<minute>/…`,
which is why `media_family` also ignores all-digit leading segments.

**A cookie without `web_session` is not a login.** One was set live and changed nothing: the
same notes failed with and without it. `a1`/`webId`/`acw_tc`/`abRequestId` are anonymous
device cookies that a logged-out browser hands out freely, so `looks_like_cookie` accepting them
means a useless cookie can be stored and then marked stale by the next failure.

**Comments come from the note page.** XHS's comment API answers 406 without a signed `x-s`
header, but `window.__INITIAL_STATE__` embeds the first five top-level comments with replies,
cookieless. It's best-effort: a parse miss returns `[]` and the note still delivers.

**Caption priority order**: the note's text first, comments only with what's left. If the text
overflows at all, comments move to the follow-up message entirely (and ride inside the last
text chunk, so an overflowing note costs two messages, not three). An earlier version reserved
caption room for comments up front and pushed descriptions into a second message needlessly.

**Chat actions expire after ~5s.** A video can take a minute, so `Bot._busy()` refreshes the
action every 4s for the life of the job. Without it the chat looks dead and people re-send.

**Channel mode** (`CHANNEL_ID`) turns a link into a submission: post to the channel, reply to
the submitter with a permalink. `Bot.check_channel()` resolves the chat and verifies posting
rights once at startup — an unusable channel logs an error and leaves `self.channel = None`, so
the bot degrades to answering submitters directly instead of failing per-submission. Published
note ids live in `state.json` (`published`, newest 1000) so a resubmitted link returns the
existing post rather than duplicating it.

**Groups hear exactly one thing: the finished post.** `_handle_link(chat_id=None, …)` is the
silent path — `_reply(None, …)` is a no-op, so progress and failures can't leak into a group —
while `announce_to=(chat, message)` carries the permalink back as a reply to the message that
offered the link. Watched groups live in
`state.groups`, added when an allowlisted user adds the bot (`my_chat_member`, which is why
`allowed_updates` includes it) or by `/allowgroup`.

**Continuations live in the message, not the reply chain.** Posting extras into a linked
discussion group was tried and reverted: a forwarded post loses its thread, so the reader can't
reach them. Instead every message but the last gets a `continues ↓` link appended, pointing at
the next message's permalink. The link is added by *editing* after the whole sequence is sent —
predicting message ids breaks as soon as anything interleaves — and caption room is reserved for
it up front.

**XHS traffic can be routed separately from Telegram** (`XHS_PROXY`). The split matters because
the bot itself makes three of the four XHS requests — short-link resolution, the comment scrape
and media downloads — so proxying only the sidecar would miss most of it. `XhsDownloader` keeps
two clients for this reason: `_api` (the sidecar hop, never proxied) and `_client` (XHS, proxied),
and it passes the same proxy to the sidecar in the request payload, which upstream honours
(verified: a dead proxy turns a working fetch into a failure). Telegram is never proxied.

**Telegram caps bot uploads at 50 MB** and XHS serves video well past it. The note page lists
every rendition with a declared `size` (h264 full quality, h265 at roughly half the bytes), so
`MediaSender._fetch_within_budget` retries with the largest one that fits. Sizes come from the
same page fetch that collects comments (`XhsDownloader.enrich`), so it costs no extra request.
If nothing fits the note is skipped — deliberately, rather than posting a degraded stand-in.

**Container health is a heartbeat, not a port.** The bot writes `HEARTBEAT_PATH` after every
successful `getUpdates`; the HEALTHCHECK fails if it's older than 180s. Long polling has nothing
to probe, and "process is up" would stay green through a crash-loop.

**Logs are the only telemetry** (`logs.py`). `LOG_FORMAT=json` emits one object per line with
a stable `event` name; `fields(...)` carries structure on a nested key so it can't collide with
`LogRecord` attributes, and `rid` (a contextvar set per submission) ties one note's lines
together. Alloy turns the lines into Prometheus metrics, so the bot never has to listen on a
port. Event vocabulary and queries: `OBSERVABILITY.md`. When adding a log line that someone
might later want to count, give it an `event`.

**1point3acres is a second site, and deliberately unlike the first.** Everything about
`acres.py` follows from three facts. *Cloudflare runs a managed challenge over the whole
domain*, so there is no anonymous mode at all — TLS impersonation (curl_cffi, every
`impersonate` profile) still gets the interstitial, because the challenge wants JavaScript.
The only way in is the owner's own browser session, which is why `/acres` accepts a **Copy as
cURL** paste: `cf_clearance` is bound to the User-Agent that solved the challenge, so the UA
is stored beside the cookie rather than guessed. *The post body is poisoned*: between every
`<br>` sits a `<font class="jammer">` carrying ". From 1point 3acres bbs", "-baidu
1point3acres", a lone Greek chi, and `<span style="display:none">` hides more. `to_text` is an
`HTMLParser` subclass that drops hidden elements wholesale — recognising the payloads is a
losing game, and the paywall block nests divs, which no regex can unwind. *Pages are GBK.*
Points-walled text leaves a visible `[…]` rather than being sewn shut, because splicing the
two halves together reads as the author's own sentence.

**A post's pictures are not in the post.** Discuz renders attachments in a `pattl` block
*after* the `t_f` cell, not inside it, unless the author placed them inline with
`[attachimg]` — so the body extractor never sees them, and only `<img>` carrying
`zoomfile`/`file` is one (the same region holds rating chrome and house ads). Every post on
the page has such a block, replies included, so the search is bounded by the next
`<div id="post_">`; verified live on a thread whose only picture belonged to a reply, which
would otherwise have been posted as the author's. Related: `attach_nopermission attach_tips`
is Discuz's standing "log in to view attachments" banner and appears on every post cell
regardless — it is *not* a gate, and reading it as one warned about a points wall on threads
that were entirely visible.

**The site is DM-only, and that is enforced in two places.** `_handle_group_message` returns
before it ever looks for a thread link, and there is no channel path at all — no submission,
no dedupe, no announcement. Threads are long, often half-paywalled, and the channel is for
RedNote.

**Two cookies now, and the order they are matched in matters.** A 1point3acres cookie carries
`_gid=`, and the RedNote matcher accepts anything containing `gid=`, so the forum check runs
first. `acres.looks_like_cookie` also refuses anything containing `://`: the cookie handler
*deletes* the message it is given, and an `oss.1p3a.com` image URL would otherwise be eaten.

**CI is one workflow, `.github/workflows/ci.yml`, with two jobs.** `pytest` runs the suite on
3.12 and 3.13 for every push and pull request — it needs no network, sidecar or Telegram, so a
red run is a real failure, never flake. `image` *needs* it, builds `bot/` for amd64 and arm64
(QEMU) and pushes to `ghcr.io/yogenpro/tg-rednote`: `latest` tracks main, `sha-<commit>` pins a
build, `vX.Y.Z` appears on a release tag. It skips pull requests. The two were separate
workflows first; `needs:` inside one workflow is what gates the push on the tests, and a
`workflow_run` chain was rejected because it re-queues the build and arrives without the tag
ref that names the image. The package is public, so `docker pull` needs no login. Actions are
pinned to majors that run on Node 24; older ones only produce deprecation annotations.

## Running it locally (no Docker)

The image builds and smoke-tests clean, but day-to-day work here uses a venv (system Python is
3.8; the bot needs 3.12). Note that if you build against a *remote* Docker context, bind mounts
resolve on the remote host, so `./xhs-volume` there is not the seeded one:

```bash
uv venv --python 3.12 /tmp/botenv && uv pip install --python /tmp/botenv/bin/python \
    -r bot/requirements.txt pytest
/tmp/botenv/bin/python -m pytest tests -q            # from the repo root

# sidecar (once): clone XHS-Downloader 2.7, then
.venv/bin/python main.py api                          # serves 127.0.0.1:5556

cd bot && TG_BOT_TOKEN=… OWNER_ID=… XHS_DOWNLOADER_URL=http://127.0.0.1:5556 \
  STATE_PATH=/tmp/live-state.json DEBUG_UPDATES=1 /tmp/botenv/bin/python -u -m app.main
```

Only one process may poll a token: a second one gets 409 and exits by design. When restarting,
kill the old one and wait a few seconds. `DEBUG_UPDATES=1` dumps each update (2 KB max) — useful
for forwarded-message shapes, but it *does* include message text, so keep it off in production.

## Debugging habits that paid off

- `ss -tnp | grep pid=<pid>` tells you whether the bot is actually holding its long poll and
  whether a fetch is in flight. Faster than guessing.
- py-spy can't attach here (`ptrace_scope=1`), so don't reach for it.
- When something looks like a hang, check whether the code path simply *doesn't log*. That was
  the answer once already: a failed fetch replied to the user and wrote nothing to the log.
