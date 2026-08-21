"""Reading a note straight off its web page.

The sidecar is the primary source, but it fetches through XHS's signed web API
and that API refuses some notes outright (`获取小红书作品数据失败`) even when the
same note renders fine for an anonymous browser. The server-rendered page
carries the whole note in `window.__INITIAL_STATE__` — images, video streams,
tags, author, timestamp — so when the API path fails, this is the second try.

Same caveat as the comment scrape: XHS owns this markup and can change it. A
miss returns None and the original sidecar error stands.
"""

from __future__ import annotations

import json
import logging
import re
from datetime import datetime, timezone
from typing import Any

from .xhs import Note, clean_text

log = logging.getLogger(__name__)

_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", re.S)


def _state(html: str) -> dict[str, Any] | None:
    match = _STATE_RE.search(html)
    if not match:
        return None
    try:
        return json.loads(match.group(1).replace("undefined", "null"))
    except ValueError:
        return None


def _published(raw: Any) -> str:
    """Epoch milliseconds to the same shape the sidecar reports."""
    try:
        moment = datetime.fromtimestamp(int(raw) / 1000, tz=timezone.utc)
    except (TypeError, ValueError, OSError):
        return ""
    return moment.strftime("%Y-%m-%d %H:%M:%S")


# h264 first: it plays on every Telegram client. The rest are there for when
# the h264 rendition is too big to upload.
_CODECS = ("h264", "h265", "av1", "h266")


def video_variants(video: dict[str, Any]) -> list[tuple[str, int]]:
    """Every rendition of a video as (url, declared size in bytes).

    XHS publishes the same video several times over — h264 at full quality,
    h265 at roughly half the bytes, sometimes more — and states each one's
    size. That's what makes it possible to find one under Telegram's upload
    ceiling without downloading anything first.
    """
    streams = ((video.get("media") or {}).get("stream")) or {}
    variants: list[tuple[str, int]] = []
    for codec in _CODECS:
        for entry in streams.get(codec) or []:
            url = entry.get("masterUrl") or next(iter(entry.get("backupUrls") or []), None)
            if not url:
                continue
            try:
                size = int(entry.get("size") or 0)
            except (TypeError, ValueError):
                size = 0
            variants.append((url, size))
    return variants


def _video_url(video: dict[str, Any]) -> str | None:
    """Pick a playable stream, preferring the codec Telegram is happiest with."""
    variants = video_variants(video)
    return variants[0][0] if variants else None


def parse_note_page(html: str, url: str = "") -> Note | None:
    """Build a Note from a note page, or None if the page isn't one."""
    state = _state(html)
    if not state:
        return None
    data = ((state.get("noteData") or {}).get("data") or {}).get("noteData")
    if not isinstance(data, dict) or not data.get("noteId"):
        return None

    user = data.get("user") or {}
    tags = " ".join(
        f"#{tag['name']}" for tag in data.get("tagList") or [] if isinstance(tag, dict) and tag.get("name")
    )
    note = Note(
        note_id=str(data.get("noteId") or ""),
        kind="video" if data.get("type") == "video" else "image",
        title=clean_text(str(data.get("title") or "")),
        desc=clean_text(str(data.get("desc") or "")),
        tags=tags,
        author=str(user.get("nickName") or ""),
        author_url=(
            f"https://www.xiaohongshu.com/user/profile/{user['userId']}"
            if user.get("userId")
            else ""
        ),
        url=url.split("?")[0] or f"https://www.xiaohongshu.com/explore/{data['noteId']}",
        published=_published(data.get("time")),
        from_page=True,
    )

    if note.kind == "video":
        note.video = _video_url(data.get("video") or {})
        note.video_variants = video_variants(data.get("video") or {})
    else:
        for entry in data.get("imageList") or []:
            if not isinstance(entry, dict):
                continue
            picture = entry.get("url") or next(
                (i.get("url") for i in entry.get("infoList") or [] if i.get("url")), None
            )
            if not picture:
                continue
            note.photos.append(picture)
            # A live photo keeps its still frame here and its clip in `stream`.
            note.lives.append(_video_url({"media": {"stream": entry.get("stream") or {}}})
                              if entry.get("livePhoto") else None)
    return note
