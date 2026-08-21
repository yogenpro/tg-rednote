# Watching this thing from a distance

Set `LOG_FORMAT=json` and every line becomes one JSON object with a stable `event` name and
typed fields. Alloy tails the container, Loki stores it, and the queries below answer the
questions that actually come up. Nothing listens on a port — the bot long-polls outbound and
writes to stdout, which is all Alloy needs.

```
LOG_FORMAT=json      # text (default) for a terminal, json for shipping
DEBUG_UPDATES=false  # never true in production: it logs message text
```

## What a line looks like

```json
{"ts":"2026-08-21T03:34:12.134Z","level":"info","logger":"app.handlers",
 "msg":"delivered 5 item(s) in 10.0s via cached file_id/url","rid":"0d1b1578",
 "event":"delivery","note":"6a87b8c3…","items":5,"seconds":10.0,"mode":"url","skipped":0}
```

`msg` stays human-readable, so a raw tail is still worth reading. Everything else is for
querying.

**`rid` ties one submission together.** Fetch, comments, delivery and publish all share it, so
a single note's whole journey comes out with one filter — which is the difference between
"something failed at 03:34" and knowing why.

## The events

| `event` | When | Fields worth querying |
|---|---|---|
| `startup` | Process start | `media_mode`, `comments`, `channel`, `groups`, `proxied` |
| `submission` | A link arrives | `source` = `dm` \| `group`, `site` |
| `note` | Metadata in hand | `kind` = `image` \| `video` \| `thread`, `items`, `cached`, `origin` = `sidecar` \| `page` |
| `comments` | Comment scrape done | `count` |
| `delivery` | Media sent | `items`, `seconds`, `mode` = `url` \| `upload` \| `telegraph`, `skipped` |
| `published` | Live on the channel | `message_id`, `url` |
| `duplicate` | Already published | `url` of the original |
| `fetch_failed` | No note | `kind` = `blocked` \| `bad_link` \| `profile` \| `network` \| `empty` |
| `page_fallback` | Sidecar refused, page worked | `reason` |
| `telegraph_failed` | telegra.ph refused a thread; it fell back to messages | `detail` |
| `cdn_refused` | Telegram wouldn't fetch a URL | `families`, `recognised` |
| `renditions` | Video renditions the page listed | `count` |
| `rendition_swapped` | Oversized video downgraded | `megabytes` |
| `page_walled` | The note page answered 200 with a wall behind it | `host` |
| `sibling_domain` | The other domain served a page this one refused | `host` |
| `page_unavailable` | Neither domain served the page | `note` |
| `media_skipped` | Item(s) dropped | `count` |
| `delivery_empty` | Nothing sent at all | `note` |
| `poll_error` / `poll_conflict` | Telegram polling trouble | `code` |
| `crash` | Unhandled error on an update | `update_id` |
| `shutdown_timeout` | Clients did not close within 10s of exiting | — |
| `stopped` | Cleanup finished; the process is leaving | — |

**`site` names the source when it isn't RedNote.** Lines from the 1point3acres path carry
`site="1p3a"` on `submission`, `note` (which also carries `comments`, the replies attached),
`delivery`, `media_skipped` and `fetch_failed`; RedNote
lines have no `site` key at all, so `site=""` selects them. Its `fetch_failed` kinds are its
own: `challenge` (Cloudflare), `login` (the forum's notice page), `bad_link`, `network`,
`empty`.

Health of the process itself is separate: the container's `HEALTHCHECK` reads a heartbeat file
the poll loop touches, so a wedged bot goes `unhealthy` without needing any of this.

## Collecting it (Alloy)

```alloy
discovery.docker "local" {
  host = "unix:///var/run/docker.sock"
}

discovery.relabel "rednote" {
  targets = discovery.docker.local.targets
  rule {
    source_labels = ["__meta_docker_container_name"]
    regex         = "/xhs-bot"
    action        = "keep"
  }
  rule {
    target_label = "service"
    replacement  = "tg-rednote"
  }
}

loki.source.docker "rednote" {
  host       = "unix:///var/run/docker.sock"
  targets    = discovery.relabel.rednote.output
  forward_to = [loki.process.rednote.receiver]
}

loki.process "rednote" {
  stage.json {
    expressions = { event = "event", level = "level", seconds = "seconds", kind = "kind" }
  }

  // Labels stay low-cardinality on purpose: `event` and `level` have a dozen
  // values between them. `rid`, `note` and `url` must NOT become labels — they
  // are unbounded and would shred the index. They stay in the line, where
  // LogQL can still filter on them.
  stage.labels {
    values = { event = "event", level = "level" }
  }

  stage.metrics {
    metric.counter {
      name        = "rednote_events_total"
      description = "Bot events by type"
      match_all   = true
      action      = "inc"
    }
    metric.histogram {
      name        = "rednote_delivery_seconds"
      description = "End-to-end time from link to delivered media"
      source      = "seconds"
      buckets     = [5, 10, 20, 40, 80]
    }
  }

  forward_to = [loki.write.default.receiver]
}

loki.write "default" {
  endpoint {
    url = "http://loki:3100/loki/api/v1/push"
  }
}
```

`stage.metrics` is what makes PromQL possible without the bot exposing a port — the counters
and histogram are produced by Alloy as it reads the lines, and scraped like any other target.
Check the block against your Alloy version; the stage names follow Promtail's and have moved
before.

## Questions and the queries that answer them

**Is it working at all?**
```logql
sum by (event) (count_over_time({service="tg-rednote"} [24h]))
```

**How many notes went out today, and how many failed?**
```logql
sum(count_over_time({service="tg-rednote", event="published"} [24h]))
sum by (kind) (count_over_time({service="tg-rednote", event="fetch_failed"} | json [24h]))
```

**Is delivery getting slower?**
```logql
quantile_over_time(0.95, {service="tg-rednote", event="delivery"} | json | unwrap seconds [1h])
```
or, from the Alloy-derived metric:
```promql
histogram_quantile(0.95, sum(rate(rednote_delivery_seconds_bucket[1h])) by (le))
```

**How often is the sidecar failing us?** (rising means the signed API is degrading, and the
page fallback is carrying more of the load)
```logql
sum(count_over_time({service="tg-rednote", event="page_fallback"} [24h]))
  / sum(count_over_time({service="tg-rednote", event="note"} [24h]))
```

**Are we streaming media through instead of letting Telegram fetch it?** (upload is slower and
uses your bandwidth)
```logql
sum by (mode) (count_over_time({service="tg-rednote", event="delivery"} | json [24h]))
```

**Which CDN families has Telegram refused?**
```logql
{service="tg-rednote", event="cdn_refused"} | json | line_format "{{.families}} {{.reason}}"
```

**How is the forum session holding up?** (a run of these means the cookie needs refreshing)
```logql
sum by (kind) (count_over_time({service="tg-rednote", event="fetch_failed"} | json | site="1p3a" [24h]))
```

**What happened to one specific note?**
```logql
{service="tg-rednote"} | json | note = "6a87b8c3000000003300a208"
```

**Everything about one submission, in order:**
```logql
{service="tg-rednote"} | json | rid = "0d1b1578"
```

**Anything actually broken in the last hour?**
```logql
{service="tg-rednote", level=~"error|warning"} | json
  | event != "cdn_refused"   # expected and self-healing; not a problem
```

**Did it restart unexpectedly?**
```logql
{service="tg-rednote", event="startup"} [7d]
```

## Volume

At the rate this bot runs — dozens of notes a day — expect roughly 8–10 lines per submission
and a few hundred lines a day, which is nothing for Loki. The two things that would change that
are `DEBUG_UPDATES=true` (a 2 KB dump per message, and it contains message text) and httpx's
own INFO logging, which `setup_logging` turns down to WARNING for exactly this reason.
