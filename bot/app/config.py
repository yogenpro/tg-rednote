"""Environment-derived configuration.

The bot token is the one irreducible deploy-time secret (PLAN §2.5); everything
else has a working default so that `TG_BOT_TOKEN=... docker compose up` is enough.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name, "").strip()
    return int(raw) if raw else default


def _env_bool(name: str, default: bool) -> bool:
    raw = os.environ.get(name, "").strip().lower()
    if not raw:
        return default
    return raw in {"1", "true", "yes", "on"}


def _env_choice(name: str, default: str, allowed: set[str]) -> str:
    raw = os.environ.get(name, "").strip().lower() or default
    if raw not in allowed:
        raise ValueError(f"{name} must be one of {sorted(allowed)}, got {raw!r}")
    return raw


@dataclass(frozen=True)
class Config:
    bot_token: str
    state_path: Path = Path("/data/state.json")
    # Touched after every successful poll so a container healthcheck can tell
    # a wedged poller from a quiet one.
    heartbeat_path: Path = Path("/tmp/heartbeat")
    downloader_url: str = "http://xhs-downloader:5556"

    # Optional fully-scripted bootstrap (PLAN §7).
    owner_id: int | None = None

    # "auto" tries CDN passthrough first and falls back to stream-through on the
    # first Telegram-side fetch failure (PLAN §2.2).
    media_mode: str = "auto"
    # "still" | "video" | "both" (PLAN §4.6)
    live_photos: str = "still"

    max_upload_bytes: int = 50 * 1024 * 1024
    cache_size: int = 128
    cache_ttl_seconds: int = 6 * 3600

    poll_timeout: int = 30
    http_timeout: float = 60.0
    fetch_timeout: float = 120.0

    channel_id: str = ""  # set to publish submissions to a channel
    # What a DM means when a channel is configured: "channel" makes every link
    # a submission, "private" answers the sender and publishes nothing. It is
    # only the default — each user can set their own with /mode.
    dm_mode: str = "channel"
    # Proxy for XHS-bound traffic only. Telegram is never proxied: it is not
    # the connection that needs a residential IP, and routing it through a
    # home link would only add latency and a failure mode.
    xhs_proxy: str = ""
    # 1point3acres threads, delivered wherever note links are. Nothing to
    # configure beyond the browser User-Agent used when the stored cookie
    # didn't come with one — see acres.py on why cf_clearance cares.
    acres: bool = True
    acres_ua: str = ""
    # Top replies to attach to a thread. They come off the page the post was
    # already read from, so unlike COMMENTS this costs no extra request.
    acres_comments: int = 10
    # A forum thread is text, so it goes to telegra.ph as one page and comes
    # back as one link with Instant View. False falls back to chunked
    # messages, which is also what happens if telegra.ph is unreachable.
    acres_telegraph: bool = True
    tags_in_caption: bool = True
    comments: int = 5  # top comments to append; 0 disables the extra page fetch
    debug_updates: bool = False
    log_format: str = "text"  # "json" for shipping to Loki/Alloy
    api_base: str = "https://api.telegram.org"

    @classmethod
    def from_env(cls) -> "Config":
        token = os.environ.get("TG_BOT_TOKEN", "").strip()
        if not token:
            raise SystemExit(
                "TG_BOT_TOKEN is required. Get one from @BotFather and put it in .env"
            )
        owner_raw = os.environ.get("OWNER_ID", "").strip()
        return cls(
            bot_token=token,
            state_path=Path(os.environ.get("STATE_PATH", "/data/state.json")),
            heartbeat_path=Path(os.environ.get("HEARTBEAT_PATH", "/tmp/heartbeat")),
            downloader_url=os.environ.get(
                "XHS_DOWNLOADER_URL", "http://xhs-downloader:5556"
            ).rstrip("/"),
            owner_id=int(owner_raw) if owner_raw else None,
            media_mode=_env_choice("MEDIA_MODE", "auto", {"auto", "url", "upload"}),
            live_photos=_env_choice("LIVE_PHOTOS", "still", {"still", "video", "both"}),
            max_upload_bytes=_env_int("MAX_UPLOAD_BYTES", 50 * 1024 * 1024),
            cache_size=_env_int("CACHE_SIZE", 128),
            cache_ttl_seconds=_env_int("CACHE_TTL_SECONDS", 6 * 3600),
            poll_timeout=_env_int("POLL_TIMEOUT", 30),
            channel_id=os.environ.get("CHANNEL_ID", "").strip(),
            dm_mode=_env_choice("DM_MODE", "channel", {"channel", "private"}),
            xhs_proxy=os.environ.get("XHS_PROXY", "").strip(),
            acres=_env_bool("ACRES", True),
            acres_ua=os.environ.get("ACRES_UA", "").strip(),
            acres_comments=max(0, min(30, _env_int("ACRES_COMMENTS", 10))),
            acres_telegraph=_env_bool("ACRES_TELEGRAPH", True),
            tags_in_caption=_env_bool("TAGS_IN_CAPTION", True),
            comments=max(0, min(10, _env_int("COMMENTS", 5))),
            debug_updates=_env_bool("DEBUG_UPDATES", False),
            log_format=_env_choice("LOG_FORMAT", "text", {"text", "json"}),
            api_base=os.environ.get("TELEGRAM_API_BASE", "https://api.telegram.org").rstrip("/"),
        )
