# TODO

Status as of 2026-08-21. Tests: 142 passing. The bot has been exercised live against ~15 real
notes (images, Ultra-HDR albums, videos, forwarded messages, `.com` and `.cn` links).

## Open

- [ ] **The Alloy config in OBSERVABILITY.md is unverified.** Written from the documented stage
      syntax, never run against a real Alloy. `stage.metrics` in particular has moved between
      Promtail/Alloy versions — check it before relying on the PromQL side.

- [ ] **Videos with no rendition under 50 MB are still dropped.** By design — the smaller
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
      difference to any fetch. Worth (a) testing a real logged-in cookie, and (b) deciding
      whether `looks_like_cookie` should warn when `web_session` is absent, since silently
      storing a useless cookie invites exactly this confusion.
- [ ] **Decide whether comments should be cached with the note.** They're re-scraped on every
      send, including cache hits (~1s). Caching them on `Note` would make repeat sends instant
      at the cost of staleness within the 6h TTL.
- [ ] **`tools/spike.py` still reports the old §9.2 conclusion** ("CDN passthrough does not
      work"). The real answer is per-CDN-family — see `CLAUDE.md`. Worth updating or retiring
      the spike now that the question is settled.
- [ ] **No rate limiting of any kind.** A user can queue as many links as they can type; each
      spawns a task. Fine for single-digit users, a problem if the allowlist ever grows.

## Recently done (context for the above)

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
