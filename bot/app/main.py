"""Entrypoint: long polling, no webhook, no inbound exposure (PLAN §2.3)."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
import sys
import time
from urllib.parse import urlsplit

from .config import Config
from .logs import fields, setup_logging
from .handlers import Bot
from .state import State
from .telegram import Telegram, TelegramError
from .xhs import XhsDownloader

log = logging.getLogger("xhsbot")


def export_proxy(config: Config) -> None:
    """Make the proxy the default for every fetch, including ones not yet written.

    httpx reads HTTP_PROXY/HTTPS_PROXY/ALL_PROXY whenever `trust_env` is on,
    which it is unless a client says otherwise — so a client added here in a
    year is proxied without anyone having to remember to pass an argument. That
    is the whole point of the inversion: an unproxied destination has to argue
    for itself. The three that do are pinned with `trust_env=False` at their
    construction (Telegram, telegra.ph, the sidecar hop) and cannot be reached
    by this; NO_PROXY is belt and braces for anything else that never leaves
    the machine. `setdefault`, so an operator who sets these by hand wins.
    """
    if not config.proxy:
        return
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY"):
        os.environ.setdefault(name, config.proxy)
    local = {"localhost", "127.0.0.1"}
    host = urlsplit(config.downloader_url).hostname
    if host:
        local.add(host)
    os.environ.setdefault("NO_PROXY", ",".join(sorted(local)))


def beat(config: Config) -> None:
    """Record that the poller is alive; the container healthcheck reads this."""
    try:
        config.heartbeat_path.write_text(f"{time.time():.0f}\n")
    except OSError as exc:  # a missing heartbeat is not worth dying over
        log.debug("could not write heartbeat: %s", exc)


async def poll(bot: Bot, telegram: Telegram, config: Config, stop: asyncio.Event) -> None:
    offset: int | None = None
    tasks: set[asyncio.Task] = set()
    backoff = 1

    while not stop.is_set():
        try:
            updates = await telegram.get_updates(offset, config.poll_timeout)
            backoff = 1
            beat(config)
        except TelegramError as exc:
            if exc.error_code == 409:
                log.error(
                    "another instance is polling this token (409) — exiting",
                    extra=fields(event="poll_conflict"),
                )
                stop.set()
                break
            log.warning(
                "getUpdates failed: %s (retrying in %ss)", exc.description, backoff,
                extra=fields(event="poll_error", code=exc.error_code, backoff=backoff),
            )
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 60)
            continue

        for update in updates or []:
            offset = update["update_id"] + 1
            task = asyncio.create_task(guard(bot, update))
            tasks.add(task)
            task.add_done_callback(tasks.discard)

    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def guard(bot: Bot, update: dict) -> None:
    try:
        await bot.handle_update(update)
    except Exception:  # a bad note must never take the poller down
        log.exception(
            "unhandled error on update %s", update.get("update_id"),
            extra=fields(event="crash", update_id=update.get("update_id")),
        )


async def run() -> None:
    config = Config.from_env()
    setup_logging(config.log_format)
    export_proxy(config)
    state = State(config.state_path)
    telegram = Telegram(config.bot_token, config.api_base, config.http_timeout)
    downloader = XhsDownloader(config.downloader_url, config.fetch_timeout, config.proxy)
    bot = Bot(config, state, telegram, downloader)

    try:
        me = await telegram.get_me()
        log.info("connected as @%s (%s)", me.get("username"), me.get("id"))
    except TelegramError as exc:
        raise SystemExit(f"could not reach Telegram: {exc.description}") from None

    if not await downloader.healthy():
        log.warning("downloader at %s is not answering yet", config.downloader_url)

    bot.bootstrap()
    await bot.check_channel(me)
    log.info(
        "media mode=%s live-photos=%s cache=%d%s%s",
        config.media_mode,
        config.live_photos,
        config.cache_size,
        # Only worth saying with a channel: without one every DM is private
        # anyway, and the default decides nothing.
        f" dm-default={config.dm_mode}" if bot.channel else "",
        f" proxy={config.proxy}" if config.proxy else "",
        extra=fields(
            event="startup",
            media_mode=config.media_mode,
            live_photos=config.live_photos,
            comments=config.comments,
            channel=bool(config.channel_id),
            dm_mode=config.dm_mode,
            groups=len(state.groups),
            proxied=bool(config.proxy),
        ),
    )

    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except NotImplementedError:  # not a POSIX event loop
            signal.signal(sig, lambda *_: stop.set())

    try:
        await poll(bot, telegram, config, stop)
    finally:
        log.info("shutting down")
        # Bounded on purpose. A 409 exit was seen live to log "shutting down"
        # and then sit there indefinitely, holding an established connection
        # to Telegram — which a supervisor watching the process (rather than
        # the heartbeat) reads as healthy forever. Closing these clients is
        # courtesy; the process is leaving either way. The "stopped" line
        # below marks the end of the part this code controls, so a future
        # hang can be told apart from one in the loop's own teardown.
        try:
            await asyncio.wait_for(_close(bot, downloader, telegram), timeout=10)
        except (TimeoutError, asyncio.TimeoutError):
            log.warning(
                "clients did not close in 10s; exiting anyway",
                extra=fields(event="shutdown_timeout"),
            )
        log.info("stopped", extra=fields(event="stopped"))


async def _close(bot: Bot, downloader: XhsDownloader, telegram: Telegram) -> None:
    await bot.aclose()
    await downloader.aclose()
    await telegram.aclose()


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
