# TODO

Status as of 2026-08-21. Tests: 178 passing. The bot has been exercised live against ~15 real
notes (images, Ultra-HDR albums, videos, forwarded messages, `.com` and `.cn` links).

## Open

- [ ] **A published telegra.ph page is only remembered for the cache TTL.** The URL lives on
      the cached `Thread`, so a resubmission six hours later publishes a second page for the
      same thread. The channel path solves the equivalent problem with a `published` index in
      `state.json`; a thread index would be the same shape, if the litter ever matters.

- [ ] **No thread whose *opening post* has pictures has been delivered yet.** A reply's
      picture has now gone through (thread 1186207), which proves the CDN passthrough works —
      `oss.1p3a.com` answers anonymously and Telegram fetches it. What is still unobserved is
      the opening post's own album, which is the same code path with a different source of
      URLs.

  Was: **No thread with pictures has been delivered yet.** The extraction is now written
      against real attachment markup (`pattl` block after the cell, `zoomfile`/`file` on the
      img) and unit-tested, but no live thread has actually carried one through to Telegram,
      so the delivery half — whether Telegram fetches `oss.1p3a.com` itself or has to be
      handed the bytes — is still unobserved. `auto` mode falls back on a 400, so it should
      self-heal either way.

- [ ] **No refresh path for an expired forum session.** A stale cookie is detected and the
      owner is told, but a `cf_clearance` lasts hours to days, so this will be a recurring
      chore. Nothing automatic is possible without running a browser.

- [ ] **The Alloy config in OBSERVABILITY.md is unverified.** Written from the documented stage
      syntax, never run against a real Alloy. `stage.metrics` in particular has moved between
      Promtail/Alloy versions — check it before relying on the PromQL side.

- [ ] **Videos with no rendition under 50 MB are still dropped.** Note that the skip message
      now says how many renditions were known, and a walled page now logs `page_walled`, so
      the next occurrence can be told apart from "the page fetch was blocked before it could
      look". Seen live on note `6a881659…`.

  Original: By design — the smaller
      renditions are tried first (see below) and if none fits, the note is skipped rather than
      posted in a degraded form. A local Bot API server (`telegram-bot-api`, 2 GB limit) is the
      only way to lift the ceiling itself.

- [ ] **Split routing is untested against a real proxy.** The plumbing is unit-tested and the
      sidecar's `proxy` field was verified live (a dead proxy fails the fetch), but no traffic
      has yet gone through an actual Tailscale exit node. Check both IPs differ once the home
      machine is advertising itself as one.

- [ ] **No way to unpublish.** The bot has `can_delete_messages` in both chats and
      `state.forget_published()` exists, but no command is wired up. Removing a post today means
      deleting it by hand *and* the note staying in the dedupe index.

- [ ] **The container has never run with a real token.** The image was built and smoke-tested
      on a remote host (imports, config, healthcheck both ways, graceful failure on a bad and a
      missing token, a real note fetched through the containerised sidecar), but the live bot
      still runs from a local venv, so no container has completed a Telegram poll. Swapping the
      venv process for `docker compose up -d` is the last step.
- [ ] **No logged-in cookie has ever been tested.** One was set live on 2026-08-20 but carried
      only anonymous cookies (`a1`, `webId`, `acw_tc`, …) with no `web_session`, and made no
      difference to any fetch. (b) is now done — a cookie without `web_session` is stored but
      called out — and so is a prerequisite nobody had noticed: the cookie used to reach only
      the sidecar, so it could not have helped the page fallback or the rendition scrape even
      if it had been a real login. (a) is still open and now worth much more.
- [ ] **Decide whether comments should be cached with the note.** They're re-scraped on every
      send, including cache hits (~1s). Caching them on `Note` would make repeat sends instant
      at the cost of staleness within the 6h TTL.
- [ ] **`tools/spike.py` still reports the old §9.2 conclusion** ("CDN passthrough does not
      work"). The real answer is per-CDN-family — see `CLAUDE.md`. Worth updating or retiring
      the spike now that the question is settled.
- [ ] **No rate limiting of any kind.** A user can queue as many links as they can type; each
      spawns a task. Fine for single-digit users, a problem if the allowlist ever grows.

## Recently done (context for the above)

- 1point3acres, confirmed live on 2026-08-21: a real thread (1186859) fetched with the
  owner's browser session and delivered in 1.3s — 4462 characters, correct title, author,
  forum and date, no jammer leakage. The first fetch also turned up a false positive:
  Discuz prints its "log in to view attachments" banner at the top of every post cell
  regardless, so a fully readable thread was being labelled as behind the points wall.
  Fixed — only a bare `attach_nopermission` counts. A second thread then showed that
  attachments live in a `pattl` block *after* the post cell rather than inside it, and that
  every reply has one too: its only picture belonged to a reply, and would have been posted
  as the author's.

- 1point3acres → telegra.ph: a thread now comes back as one page and one link with Instant
  View, carrying the post, its pictures and the top replies with theirs. A deliberate
  divergence from PLAN §2.1, which rejected Telegraph and is still right for RedNote — see
  CLAUDE.md. Falls back to chunked messages on ACRES_TELEGRAPH=false or a telegra.ph outage.

- 1point3acres reply pictures: attached to the reply that posted them, linked with 📷 in the
  comment, and delivered as their own album captioned with the author — never merged into the
  opening post's album, which would credit the wrong person.

- 1point3acres replies: the top 10 by the post's own 好苗/杂草 score ride in the message with
  the thread, the way a note's comments do. Ranking is done here because Discuz only serves
  chronological order, and it deliberately ignores the per-post green/red bar — that is
  labelled 全局 and measures the author's lifetime reputation, not the reply.

- 1point3acres: thread links in a DM come back as the opening post, with the site's
  `<font class="jammer">` interference and zero-width characters stripped, GBK decoded,
  points-walled gaps marked `[…]`, and its own cookie custody (`/acres`, which accepts a
  "Copy as cURL" paste so `cf_clearance` keeps the User-Agent it was issued for). DM-only by
  construction: no group path, no channel path.

- Group listening and dedupe, both confirmed live on 2026-08-21: a real group has been
  publishing submissions silently for a day, and a note resubmitted there by a second user
  resolved to the existing post (`6a870863…` → `/gradient_canopy/75`) in 0.3s off the cache
  instead of republishing.

- CI: one workflow, two jobs. `pytest` runs the suite on 3.12 and 3.13 for every push and
  PR; `image` needs it, so nothing reaches `ghcr.io/yogenpro/tg-rednote` on a red suite.
  Tags: `latest` on main, `sha-<commit>` always, `vX.Y.Z` on a release tag. Green, image
  public, multi-arch manifest verified.

- Observability: `LOG_FORMAT=json`, an `event` vocabulary, per-submission `rid`, and
  `OBSERVABILITY.md` with the Alloy pipeline (including log-derived Prometheus metrics) and
  ready-made LogQL/PromQL queries.

- Oversized video: the note page lists every rendition with its size, so an 88 MB video is
  retried as the largest rendition under the cap (36 MB for the note that failed live). The
  page fetch that was already happening for comments carries the sizes.

- Page fallback: when the sidecar's signed API refuses a note, the note is read off its own
  web page instead (`page.py`). Rescued a note that failed three times live.

- Split routing: `XHS_PROXY` sends every XHS-bound request (sidecar fetches, short-link
  resolution, comment scrape, media downloads) through a proxy while Telegram goes direct.
  Optional `tailscale` compose profile provides one via a tailnet exit node, userspace mode.

- Packaging: image builds and runs as uid 10001, heartbeat-based HEALTHCHECK, defaults that let
  `docker run` work without compose, `.dockerignore`, optional `.env`, log caps, ordered start
  behind the sidecar's healthcheck.

- Channel mode: submissions published to `CHANNEL_ID`, permalink back to the submitter,
  persistent dedupe, startup permission check, `/status` and `/help` reflect it.
- Continuation links: each message points at the next one's permalink, so a forwarded post
  still leads to its overflow. (Replaced the discussion-thread experiment, which was reverted —
  forwarded posts lose their thread.)
- Group listening: links posted in watched groups publish silently; the bot never speaks in a
  group. Watch list is earned by an allowlisted user adding the bot, or `/allowgroup`.

- Link handling: `xhslink.cn`, links embedded in forwarded/rich messages, URLs that live only in
  `text_link` entities, and local short-link resolution.
- Delivery: per-CDN-family URL/upload decisions; albums over 10 items split into `[1/n]` parts
  that reply to each other; chat action refreshed for the life of a job.
- Comments: top 5 with replies, scraped from the note page, rendered into the album caption
  (dropping whole comments from the end to fit) so a forward carries them along.
- Observability: inbound messages, fetch failures and comment outcomes all log now. Silent
  failure paths cost real debugging time twice.
- Cookie prompting: a refusal with no cookie stored now tells the owner how to add one, and
  points guests at the owner instead.

## What to test a change against

Share links are ephemeral — notes get deleted and XHS stops serving them — so rather than a
list of URLs that will rot, these are the shapes worth covering. Grab a current link for each
from the RedNote app:

| Shape | What it exercises |
|---|---|
| An album of 11+ images | `[1/n]` splitting and the continuation links |
| An Ultra-HDR album (`note_pre_post_uhdr` URLs) | Telegram refusing the CDN, per-family fallback |
| A video over 50 MB | the rendition swap |
| A note the sidecar refuses (`获取小红书作品数据失败`) | the page fallback |
| An `xhslink.cn` link, forwarded rather than typed | `.cn` support and entity extraction |
| A note with an active comment thread | the comment scrape and caption budgeting |
| A profile link (`/user/profile/<id>`) | the "that's a profile, not a note" path |
