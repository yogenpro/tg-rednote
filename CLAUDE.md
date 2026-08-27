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
  acres.py      1point3acres threads: link shapes, de-jamming, reply ranking
  telegraph.py  telegra.ph client; a forum thread goes out as one page
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

**`rednote.com` is the same site, and the sidecar has never heard of it.** Put a rednote.com
URL to `POST /xhs/detail` and it answers `提取小红书作品链接失败`; rewrite the host to
www.xiaohongshu.com and the identical note fetches fine. The *page*, though, serves the same
`noteData` and the same comments from either domain — verified side by side on one note. So the
sidecar hop is always normalised, while `page_url_for` keeps the bot's own fetches on whichever
domain the link arrived on. That is not tidiness: an account lives on one domain or the other
(international accounts are on rednote.com), a cookie goes with it, and the requests this
process makes are the ones that meet the walls. Sharing a rednote.com link is therefore what
makes a rednote.com session apply.

**One site, two front doors, and not one gate.** A page refused on one domain is worth exactly
one retry on the other: measured with a valid `xsec_token`, xiaohongshu.com answered the
security wall 5/5 while rednote.com served the same note 5/5, order randomised — and recovering
a walled page live then produced 5 comments and a full note where there had been none.
`XhsDownloader._page` does that retry for both callers (`enrich` and `from_page`), triggered by
a wall *or* by a 200 with no note behind it, which is the same thing wearing a different hat.
It costs one extra request and only on failure; a page that works is fetched once. The sidecar
gets no such fallback — it rejects rednote.com outright — so this is purely for the requests
this process makes.

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

**Streaming a family through is not a fix for every item in it.** A 16-image album live on
2026-08-23 (`6a8a819e0000000014029fce`) had one image fail as a URL (`WEBPAGE_CURL_FAILED`,
family streamed through), then fail *again* once uploaded — this time `PHOTO_INVALID_DIMENSIONS`,
which is the actual bytes, not the fetch, and streaming can never fix it. The old code treated
"nothing fresh to blame" as "raise", which lost all 16 images to one bad photo. `send` now tells
apart a culprit index that isn't in the current URL-mode set because it's *out of range* (don't
trust the pointer, blame every URL-mode family, unchanged) from one that's in range but already
upload-mode (nothing fresh — the retry already happened and failed again): only the second case
drops just that item from the group and keeps going. `report.skipped` picks it up same as an
oversized video, so the caller's existing "Skipped: …" reply needed no changes.

**XHS has three wall shapes, and two of them answer HTTP 200.** `/website-login/error?redirectPath=…`
is the login wall; `/404/sec_<token>?source=xhs_sec_server&originalUrl=…` is its bot check, and
rednote.com spells the same check `/404?source=/404/sec_<token>?redirectPath=…` — note the
second `?`, which leaves the target *not a top-level query parameter at all*, so `parse_qs`
never sees it. `_unwrap_login_wall` therefore scans the whole URL for either parameter name.
Because these return 200 with a parseable page, nothing downstream notices — the comment scrape
just comes back empty and a video's renditions are never found, which reads exactly like a note
that has neither. Both keep the wanted URL in a query parameter, so `_unwrap_login_wall` handles
both and `is_wall` names the condition; `enrich` logs `page_walled` rather than returning
silently. Seen live on 2026-08-21 while investigating a video skipped for size.

**Caption limits are UTF-16 code units on the parsed text.** Markup and href targets are free;
emoji cost two. `tg_len` exists for this. Measure captions with tags stripped, or the tests lie.

**The note page is the sidecar's backstop** (`page.py`). XHS's signed web API — which the
sidecar uses — refuses some notes with `获取小红书作品数据失败` while the server-rendered page
serves the same note to an anonymous browser. `detail()` therefore tries the page before giving
up, and only re-raises the sidecar's error if that fails too. Verified live on
`http://xhslink.com/o/5TWUMDydMKo`. Page media comes from `sns-webpic-qc.xhscdn.com/<minute>/…`,
which is why `media_family` also ignores all-digit leading segments.

**The cookie now reaches the bot's own fetches, not just the sidecar's.** It used to go only
in the sidecar's request payload, which left short-link resolution, the page fallback and the
comment/rendition scrape running anonymous — and those are the requests that meet XHS's walls,
so a logged-in session helped the one hop that was least likely to need it.
`XhsDownloader.set_cookie` puts it on the client the bot uses, called at startup and whenever
`/cookie` or `/forgetcookie` runs. Exactly the argument `PROXY` is built on, applied to
the other credential.

**A cookie without `web_session` is not a login.** One was set live and changed nothing: the
same notes failed with and without it. `a1`/`webId`/`acw_tc`/`abRequestId` are anonymous
device cookies that a logged-out browser hands out freely, so `looks_like_cookie` accepting them
means a useless cookie can be stored and then marked stale by the next failure. It is still
accepted — refusing it would be worse, since the markers are not a reliable test — but storing
one without `web_session` now says so in the reply.

**Comments come from the note page.** XHS's comment API answers 406 without a signed `x-s`
header, but `window.__INITIAL_STATE__` embeds the first five top-level comments with replies,
cookieless. It's best-effort: a parse miss returns `[]` and the note still delivers.

**Caption priority order**: the note's text first, comments only with what's left. If the text
overflows at all, comments move to the follow-up message entirely (and ride inside the last
text chunk, so an overflowing note costs two messages, not three). An earlier version reserved
caption room for comments up front and pushed descriptions into a second message needlessly.

**Comments in the caption are all or none, and that is the fix for a real silent loss.**
`fit_into_caption` returns `(caption, leftover)`; it used to return just the caption, appending
whatever fitted and truncating the first comment mid-sentence to do it. The caller could only
compare the caption it got back against the one it passed in, so a half-rendered comment read
as "they all got in" and `trailing` became `[]` — the remainder, and every later comment, went
nowhere at all. The reader saw a sentence stop mid-word with nothing behind it
(`/gradient_canopy/173`, 2026-08-22: one photo, one comment, no second message). Note the shape
of it: *no* room was handled correctly and a *little* room was not, so the bug needed a caption
with a small remainder to appear. `render_comments` still truncates, but only for the follow-up
message, which has no third message to spill into. Both call sites — notes and 1point3acres —
had the same line and both are fixed.

**Chat actions expire after ~5s.** A video can take a minute, so `Bot._busy()` refreshes the
action every 4s for the life of the job. Without it the chat looks dead and people re-send.

**Channel mode** (`CHANNEL_ID`) turns a link into a submission: post to the channel, reply to
the submitter with a permalink. `Bot.check_channel()` resolves the chat and verifies posting
rights once at startup — an unusable channel logs an error and leaves `self.channel = None`, so
the bot degrades to answering submitters directly instead of failing per-submission. Published
note ids live in `state.json` (`published`, newest 1000) so a resubmitted link returns the
existing post rather than duplicating it.

**Whether a DM is a submission is the sender's call, not the deployment's.** `CHANNEL_ID`
used to decide for everyone: configure a channel and every DM link became a submission, which
is wrong for the ordinary case of wanting to *read* something. `/mode private` opts a user out
and `state.dm_modes` remembers it; `DM_MODE` only sets the default for whoever never chose.
Mechanically, `_publishes(user_id)` answers the question once per submission, and
`_handle_link`/`_handle_acres_link` take a `publish` flag and bind `channel = self.channel if
publish else None` at the top — every channel test inside those two methods reads that local,
which is what makes private mode fall down exactly the path a bot with no channel takes.
Two consequences worth keeping: a group link is a submission regardless (watching a group is
the point of watching it, and one member's preference must not silence it for everyone), and
the dedupe index is not consulted in private mode — "someone already published it" is no reason
to refuse a reader their own copy.

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

**The proxy is a default, not a route to a named site** (`PROXY`; `XHS_PROXY` is the old name,
still read). It was an allowlist first — "XHS-bound traffic" — and that shape leaks by
construction: telegra.ph, 1point3acres and `oss.1p3a.com` were all exiting from the server
because nobody had marked them, and so would anything added next. Inverted on 2026-08-22, so
the question is now which destinations argue their way *out* of the proxy. Three do: Telegram
(largest payloads, wants a reliable link rather than a residential IP), telegra.ph (same trade),
and the hop to the sidecar (one container to the next — proxying it would be pointless and a
dead proxy would break a request that never leaves the machine).

Mechanically it is two halves, and both are needed. `main.export_proxy` puts the proxy in
`os.environ` as HTTP_PROXY/HTTPS_PROXY/ALL_PROXY, which httpx reads whenever `trust_env` is on
— so a client written here in a year is proxied without anyone remembering to pass an argument.
That is only safe because the three exemptions are pinned at construction with
`trust_env=False`, in code rather than configuration. Without that pin, setting the proxy would
quietly route Telegram, uploads included. The sidecar is a separate process, so it is told
separately in the request payload, which upstream honours (verified: a dead proxy turns a
working fetch into a failure). `NO_PROXY` covers localhost and the sidecar's own host as belt
and braces. Note what proxying 1point3acres implies: `cf_clearance` is commonly bound to the
IP that solved the challenge, so the exit should match the browser the `/acres` paste came
from.

**The fetching proxy is a plain HTTP proxy on a home box, not an exit node.** An earlier
design ran a second, userspace tailnet node in compose with `TS_EXIT_NODE` — swapped out on
2026-08-23 before it ever carried live traffic. The reasons that design existed still stand
and still rule out the obvious alternative: an exit node is a *per-device* setting, so the
host's daemon can only send every process on the machine home — Telegram, uploads, unrelated
containers — and takes the host offline entirely when home is down; there is no per-process
form of it (app-based split tunnelling is Android-only), and app connectors were rejected on
their own documentation (don't point one at a CDN; discovered routes never pruned; fails
*open*). A forward proxy on the home machine's tailnet IP answers all of it at once:
`PROXY=http://100.x.y.z:8888` — tinyproxy with `Allow 100.64.0.0/10` — reached by both
containers through the host's existing tailscaled route, egressing from home by construction.
What the swap deleted: the whole compose service and its state volume, the auth key that
would one day expire and silently kill the proxy on restart, the second console row, and the
class of failure the healthcheck existed to catch — a node serving the proxy while exiting
from the server's IP cannot happen when the proxy *is* the machine it exits from. Two things
that are new rather than settled: containers reaching a tailnet IP via host forwarding is
the one untested assumption (smoke-test it from inside both containers on first run), and
DNS now resolves on the home box — an HTTP proxy is sent the hostname — so CDN edges are
picked near home, which is what a home browser would see.

**The egress proof is one log line at startup (`proxy_egress`).** The removed tailscale
container's healthcheck grepped `"ExitNode": true` in its status JSON because a userspace
node without an exit node serves its HTTP proxy perfectly and exits from the server's IP —
the single failure the split exists to prevent, and one nothing downstream would notice.
With a plain proxy that state is gone by construction, but a `PROXY` pointed at the wrong box
would still be silent, so `main.probe_egress` makes one best-effort fetch (`api.ipify.org`)
through the proxy at boot and logs the IP it came back on — or the exception *type*, never
the message, since a proxy URL can carry BasicAuth credentials and connect errors quote the
address they failed on. It never blocks startup: the proxy may simply not be up yet, and
failed fetches will say so again on their own.

**Telegram caps bot uploads at 50 MB** and XHS serves video well past it. The note page lists
every rendition with a declared `size` (h264 full quality, h265 at roughly half the bytes), so
`MediaSender._fetch_within_budget` retries with the largest one that fits. Sizes come from the
same page fetch that collects comments (`XhsDownloader.enrich`), so it costs no extra request.
If nothing fits the note is skipped — deliberately, rather than posting a degraded stand-in.

**Shutdown is bounded, and `stopped` is the proof.** A 409 exit was seen live to log
`shutting down` and then sit indefinitely holding an established connection to Telegram; the
same path against a local stub exits instantly, so the culprit was never pinned down. Rather
than guess which client stuck, the three `aclose()` calls run under a 10s `wait_for` and the
process leaves regardless. That matters because a *hung* process still looks alive to anything
watching the pid — only the heartbeat notices. `stopped` is logged after cleanup, so if a hang
ever recurs, its position relative to that line says whether it is in this code or in the event
loop's own teardown.

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

**An anonymous poster has no byline markup at all.** 1point3acres lets people post as
`匿名用户-XXXXX`, and Discuz then omits both `itemprop="author"` and the `space-uid` profile
link — the handle exists only as bare text in the byline cell (`id="authicon<pid>"`). Two
consequences, both seen live on thread 1186472. Falling back to `"anon"` merges every
anonymous poster in a thread into one apparent person; and because the opening post's author
used to be searched for across the *whole page*, an anonymous thread starter was credited with
the first **named reply's** name *and their profile link* — a real person publicly bylined on
a post they did not write. `_author_of(block, pid)` reads the byline cell when the structured
markup is missing, and `parse_thread` bounds its search to the opening post. The already
published telegra.ph page was corrected in place via `editPage`, which keeps the URL the
channel post points at.

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

**Forum replies nest, and the quote says exactly where.** A quote block links back with
`goto=findpost&pid=<parent>` — an exact pointer, so a conversation reconstructs precisely
instead of being guessed at from names. `parse_replies` builds the tree, ranks the *top-level*
replies, and flattens each conversation underneath the one it hangs off, which is the same
one-level shape a note's `subComments` take and what `render_comments` already draws with `↳`.
Telegraph indents them in a `blockquote`. Two arrows are suppressed as noise rather than
information: a reply sitting directly under what it answers, and a reply to the thread starter
(which is what most replies are). `limit` counts conversations, not posts.

**The link handed back is the one that was shared.** Every share shape addresses the same tid
and the fetch has to use the `/bbs/` permalink — it is the only one that serves HTML — but
answering a `/home/thread/<id>` link with a `/bbs/thread-<id>-1-1.html` one hands the reader a
different, older UI than the one they were looking at. `Thread.share_url` keeps what arrived
and `Thread.link` is what the reader ever sees.

**Ranking forum replies: the obvious signal is the wrong one.** Every post shows a green/red
bar, but it is labelled 全局 — the *author's* lifetime reputation, not the post's score; the
same user shows identical numbers on every post they make in a thread, which is how it was
caught. The per-post score is 好苗/杂草, `rec_add_<pid>` and `rec_sub_<pid>`. Discuz serves
replies chronologically and offers no popularity order (`ordertype=1` only reverses), so
`parse_replies` ranks them here, off the same page the post came from — no extra request.
Quotes come out of a reply's text (they repeat a post already on screen) but the quoted name
is kept as `replying_to`, or the answer reads as a non-sequitur.

**A thread goes to telegra.ph, and that is a deliberate divergence from PLAN §2.1.** The plan
rejected Telegraph, and it is still right for RedNote: a note *is* its images, and answering
with a link to a web page is what this bot exists to avoid. A forum thread inverts the trade —
thousands of words, pictures incidental — so 1point3acres publishes one page and sends one
link, which Telegram opens with Instant View. Do not "fix" the inconsistency in either
direction; the two sites want different things. `ACRES_TELEGRAPH=false` restores chunked
messages, and so does a telegra.ph outage: `_publish_page` returns None rather than raising,
and the message path is right behind it. Two API details earn their keep — `createPage` takes
`author_name`/`author_url` per page, so the forum author keeps the byline rather than the
bot, and an external image `src` is stored and rendered verbatim (verified against
oss.1p3a.com), so nothing needs re-hosting. Content is capped at 64 KB, so `trim` drops whole
nodes from the end — which is why replies are ordered last and the link home is passed as
`tail` and never dropped. The page URL is cached on the `Thread`, so re-sending a link inside
the cache TTL hands back the page that exists instead of littering telegra.ph with copies.

**A reply's picture is delivered under the reply's name, not the post's.** Replies carry
attachments in exactly the same `pattl` shape the opening post does, so `parse_replies`
collects them per comment; the comment then renders a 📷 link (markup and href are free
against Telegram's limit, so the marker costs two units however long the URL is) and the
pictures travel as a *separate* album after the text. Joining the post's album would caption
someone else's photo with the opening author's words — the same misattribution the post-extent
bound exists to prevent. `oss.1p3a.com` is public: 200 with no cookie and no referer, so
Telegram fetches it directly.

**1point3acres runs the same three paths RedNote does** — DM, watched group, channel — since
2026-08-21. It was DM-only at first on the reasoning that threads are long, often half
paywalled, and the channel was for notes; publishing to telegra.ph removed the length
objection and the owner asked for it. `_handle_group_message` routes either kind of link, and
`_handle_acres_link` takes the same `chat_id=None` silent path and `announce_to` permalink
that `_handle_link` does. One thing to keep in mind: a telegra.ph page is public to anyone
holding the link, so a channel post republishes a thread past the channel's own membership.

**The dedupe index is shared between the two sites, so forum ids are namespaced** `1p3a:<tid>`
(`acres_key`). A tid is a short number and a note id a long hex string, so a collision is not
realistic — but an index that cannot say which site a key belongs to is the kind of thing that
produces one baffling bug years later.

**Two cookies now, and the order they are matched in matters.** A 1point3acres cookie carries
`_gid=`, and the RedNote matcher accepts anything containing `gid=`, so the forum check runs
first. `acres.looks_like_cookie` also refuses anything containing `://`: the cookie handler
*deletes* the message it is given, and an `oss.1p3a.com` image URL would otherwise be eaten.

**Two compose files, and the base one must stand alone.** `docker-compose.yml` is the whole
deployment: published images, named volumes, not one path out of the repo — so a server installs
it with two `curl`s and never sees the source. `docker-compose.override.yml` is what a checkout
adds, and Compose merges it automatically when the two sit together: `build: ./bot` with
`pull_policy: build` (without which an old `latest` on the machine silently stands in for your
working tree), the seeded `./xhs-volume` bind mount, and `127.0.0.1:5556` for `tools/spike.py`.
Mounts merge *by target*, verified with `docker compose config`, so the bind mount replaces the
`xhs-settings` named volume rather than colliding with it. The trap to watch for: the override
can keep a base file working that no longer stands on its own, so check a compose change with
`docker compose -f docker-compose.yml config` — the deployment shape — not just the merged one.

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
