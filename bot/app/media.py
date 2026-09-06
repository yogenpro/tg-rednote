"""Caption assembly and album delivery.

Two things live here:

* pure text helpers that code against Telegram's caption/message limits (§8);
* the sender, which prefers handing XHS CDN URLs to Telegram and falls back to
  streaming the bytes through this process when Telegram's fetch is refused
  (PLAN §2.2). A refusal is remembered per CDN family, so one Ultra-HDR note
  does not push every later note onto the slow path.
"""

from __future__ import annotations

import asyncio
import logging
import mimetypes
from dataclasses import dataclass, field
from html import escape
from urllib.parse import urlsplit
from typing import Iterable, Iterator

import httpx

from .cache import LRU
from .logs import fields
from .telegram import CAPTION_LIMIT, MEDIA_GROUP_LIMIT, MESSAGE_LIMIT, Telegram, TelegramError
from .xhs import MediaItem, Note

log = logging.getLogger(__name__)

# XHS CDNs refuse requests without a plausible referer.
CDN_HEADERS = {
    "Referer": "https://www.xiaohongshu.com/",
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
    ),
}
DEFAULT_TYPES = {"photo": ("image/jpeg", ".jpg"), "video": ("video/mp4", ".mp4")}


def tg_len(text: str) -> int:
    """Telegram counts limits in UTF-16 code units, so emoji cost two."""
    return len(text.encode("utf-16-le")) // 2


def tg_truncate(text: str, limit: int) -> tuple[str, str]:
    """Split `text` so the head is at most `limit` UTF-16 units."""
    if tg_len(text) <= limit:
        return text, ""
    cut = limit
    while cut > 0 and tg_len(text[:cut]) > limit:
        cut -= 1
    head, tail = text[:cut], text[cut:]
    # Prefer a whitespace boundary if one is reasonably close.
    space = head.rfind("\n")
    if space < limit * 0.6:
        space = head.rfind(" ")
    if space > limit * 0.6:
        head, tail = text[:space], text[space:]
    return head.rstrip(), tail.strip()


def split_message(text: str, limit: int = MESSAGE_LIMIT) -> list[str]:
    chunks: list[str] = []
    remaining = text
    while remaining:
        head, remaining = tg_truncate(remaining, limit)
        if not head:  # pathological single token longer than the limit
            head, remaining = remaining[:limit], remaining[limit:]
        chunks.append(head)
    return chunks


def build_caption(
    note: Note, *, tags: bool = True, limit: int = CAPTION_LIMIT, reserve: int = 0
) -> tuple[str, str]:
    """Return (caption HTML, plain-text overflow).

    The tail — tags, author, source link — is reserved up front so it survives
    truncation; the title gets what is left, and the description gets the rest,
    with anything that does not fit handed back for a follow-up message (§4.7).

    Lengths are measured against the *parsed* text, which is what Telegram
    counts: markup and href targets are free. `reserve` holds back room for
    something prepended later — the "[1/2]" marker on a split album.
    """
    limit = max(0, limit - reserve)
    title = note.title.strip()
    desc = note.desc.strip()
    if title and desc.startswith(title):
        desc = desc[len(title):].strip()

    link_text = "open on RedNote"
    bits = [b for b in (note.author, note.published.split(" ")[0] if note.published else "") if b]
    foot_plain = " · ".join(bits + ([link_text] if note.url else []))
    foot_html = " · ".join(
        [escape(b) for b in bits]
        + ([f'<a href="{escape(note.url, quote=True)}">{link_text}</a>'] if note.url else [])
    )
    tag_line = note.tags.strip() if tags else ""
    if tag_line and all(f"#{tag}" in desc for tag in tag_line.split()):
        tag_line = ""  # already inline in the description; don't say it twice

    # Tail block as rendered: "\n\n" + [tags + "\n"] + footer.
    tail_len = 2 + tg_len(foot_plain) + (tg_len(tag_line) + 1 if tag_line else 0)
    room = max(0, limit - tail_len)

    if title and tg_len(title) + 2 > room:  # absurd title: shrink it, keep the tail
        title, _dropped = tg_truncate(title, max(0, room - 3))
        title = f"{title}…" if title else ""
    head_len = tg_len(title) + 2 if title else 0

    budget = max(0, room - head_len)
    if tg_len(desc) > budget:
        body, overflow = tg_truncate(desc, max(0, budget - 1))
        body = f"{body.rstrip()}…" if body else ""
        if not body:
            overflow = desc
    else:
        body, overflow = desc, ""

    blocks = []
    if title:
        blocks.append(f"<b>{escape(title)}</b>")
    if body:
        blocks.append(escape(body))
    tail = "\n".join(([f"<i>{escape(tag_line)}</i>"] if tag_line else []) + ([foot_html] if foot_html else []))
    if tail:
        blocks.append(tail)
    return "\n\n".join(blocks), overflow


def chunk(items: list[MediaItem], size: int = MEDIA_GROUP_LIMIT) -> Iterator[list[MediaItem]]:
    for start in range(0, len(items), size):
        yield items[start : start + size]


def first_message_id(result) -> int | None:
    """The id to hang the next part of a split album off."""
    messages = result if isinstance(result, list) else [result]
    for message in messages:
        if isinstance(message, dict) and message.get("message_id"):
            return message["message_id"]
    return None


class MediaTooLarge(RuntimeError):
    pass


@dataclass
class SendReport:
    sent: int = 0
    skipped: list[str] = field(default_factory=list)
    uploaded: bool = False
    first_message_id: int | None = None
    # (message id, caption HTML) for the caption-bearing message of each group,
    # so a caller can go back and append a "continued" link.
    parts: list[tuple[int, str]] = field(default_factory=list)
    # Follow-up captions a group could not carry because every item in it was
    # dropped. The text must not go down with the photos, so the caller gets
    # it back to send as a message.
    unused_captions: list[str] = field(default_factory=list)


def media_family(url: str) -> str:
    """A coarse bucket for "media that Telegram's fetcher treats alike".

    XHS serves ordinary note images from ci.xiaohongshu.com/notes_pre_post/…
    and Ultra-HDR ones from ci.xiaohongshu.com/note_pre_post_uhdr/…; Telegram
    fetches the first happily and refuses the second. Host alone is too coarse
    to tell them apart and the full URL is too fine to generalise, so bucket on
    host plus the leading *directory*.

    Some URLs carry the token straight off the root — ci.xiaohongshu.com/1040g…
    — and there the token is a filename, not a directory. Bucketing on it would
    give every single image its own family and nothing would ever be learned,
    so those fall back to the host.
    """
    parts = urlsplit(url)
    path = parts.path.lstrip("/")
    directory = path.split("/", 1)[0] if "/" in path else ""
    # The page fallback hands out .../202608210833/<hash>/notes_pre_post/… —
    # a minute-stamped bucket. Keying on it would mint a new family every
    # minute, so anything that is just digits is treated as noise too.
    if directory.isdigit():
        directory = ""
    return f"{parts.netloc}/{directory}" if directory else parts.netloc


class MediaSender:
    def __init__(
        self,
        telegram: Telegram,
        *,
        mode: str = "auto",
        max_bytes: int = 50 * 1024 * 1024,
        file_ids: LRU[str] | None = None,
        timeout: float = 120.0,
        proxy: str = "",
        headers: dict[str, str] | None = None,
    ):
        self._tg = telegram
        self._configured_mode = mode
        self._upload = mode == "upload"
        self._refused: set[str] = set()
        self._max_bytes = max_bytes
        self._file_ids = file_ids if file_ids is not None else LRU(maxsize=512)
        # A download is a fetch like any other: when the bot's traffic is
        # routed somewhere specific, the bytes go the same way as the page that
        # named them — XHS's CDN and the forum's alike.
        self._client = httpx.AsyncClient(
            timeout=timeout,
            follow_redirects=True,
            headers=headers if headers is not None else CDN_HEADERS,
            proxy=proxy or None,
        )

    async def aclose(self) -> None:
        await self._client.aclose()

    def set_headers(self, **headers: str) -> None:
        """Update the download headers in place.

        The 1point3acres attachments are served only to a logged-in session, so
        the sender's cookie has to follow whatever the owner last stored rather
        than being fixed when the process started.
        """
        self._client.headers.update({k: v for k, v in headers.items() if v})

    @property
    def streaming(self) -> bool:
        """True once anything is known to need stream-through."""
        return self._upload or bool(self._refused)

    def _needs_upload(self, url: str) -> bool:
        return self._upload or media_family(url) in self._refused

    async def _download(self, url: str) -> tuple[bytes, str, str]:
        async with self._client.stream("GET", url) as response:
            response.raise_for_status()
            declared = response.headers.get("content-length")
            if declared and int(declared) > self._max_bytes:
                raise MediaTooLarge(f"{int(declared) // 1024 // 1024} MB")
            buffer = bytearray()
            async for piece in response.aiter_bytes(64 * 1024):
                buffer.extend(piece)
                if len(buffer) > self._max_bytes:
                    raise MediaTooLarge(f">{self._max_bytes // 1024 // 1024} MB")
            content_type = (response.headers.get("content-type") or "").split(";")[0].strip()
        return bytes(buffer), content_type, url

    async def _fetch_within_budget(self, item: MediaItem) -> tuple[bytes, str, str]:
        """Download `item`, standing in a smaller rendition if it doesn't fit.

        Telegram caps bot uploads at 50 MB and XHS routinely serves video past
        that, but it publishes the same video at several sizes. The largest one
        that fits beats posting nothing.
        """
        try:
            return await self._download(item.url)
        except MediaTooLarge:
            if not item.alternatives:
                raise

        fits = sorted(
            (pair for pair in item.alternatives if 0 < pair[1] <= self._max_bytes),
            key=lambda pair: -pair[1],
        )
        for url, size in fits:
            try:
                result = await self._download(url)
            except (MediaTooLarge, httpx.HTTPError) as exc:
                log.info("rendition %s did not work out (%s)", url.split("?")[0], type(exc).__name__)
                continue
            log.info(
                "swapped in a %d MB rendition of %s", size // 1024 // 1024, item.url.split("?")[0],
                extra=fields(event="rendition_swapped", megabytes=size // 1024 // 1024),
            )
            return result
        # Say how many were on offer: "no smaller rendition" reads the same
        # whether the note had none to begin with or the page fetch was walled
        # before it could find them, and those need different fixes.
        raise MediaTooLarge(
            f">{self._max_bytes // 1024 // 1024} MB, no smaller rendition "
            f"({len(item.alternatives)} known)"
        )

    def _naming(self, kind: str, content_type: str, index: int) -> tuple[str, str]:
        fallback_type, fallback_ext = DEFAULT_TYPES[kind]
        if not content_type or not content_type.startswith(kind.replace("photo", "image")):
            content_type = fallback_type
        extension = mimetypes.guess_extension(content_type) or fallback_ext
        return f"media{index}{extension}", content_type

    async def _build(
        self, items: list[MediaItem], caption: str | None
    ) -> tuple[list[dict], dict[str, tuple], list[str], list[int]]:
        """Return (media entries, files, skip reasons, source indexes)."""
        media: list[dict] = []
        files: dict[str, tuple] = {}
        skipped: list[str] = []
        sources: list[int] = []

        for index, item in enumerate(items):
            entry: dict = {"type": item.kind}
            if item.kind == "video":
                entry["supports_streaming"] = True

            cached = self._file_ids.get(item.url)
            if cached:
                entry["media"] = cached
            elif self._needs_upload(item.url):
                try:
                    payload, content_type, _u = await self._fetch_within_budget(item)
                except MediaTooLarge as exc:
                    skipped.append(f"item {index + 1} too large ({exc})")
                    continue
                except httpx.HTTPError as exc:
                    skipped.append(f"item {index + 1} unreachable ({type(exc).__name__})")
                    continue
                name, content_type = self._naming(item.kind, content_type, index)
                key = f"file{index}"
                files[key] = (name, payload, content_type)
                entry["media"] = f"attach://{key}"
            else:
                entry["media"] = item.url

            if caption is not None and not media:
                entry["caption"] = caption
                entry["parse_mode"] = "HTML"
            media.append(entry)
            sources.append(index)
        return media, files, skipped, sources

    def _remember(self, result, items: list[MediaItem], sources: list[int]) -> None:
        messages = result if isinstance(result, list) else [result]
        for message, source in zip(messages, sources):
            item = items[source]
            file_id = None
            if message.get("photo"):
                file_id = message["photo"][-1].get("file_id")
            elif message.get("video"):
                file_id = message["video"].get("file_id")
            elif message.get("document"):
                file_id = message["document"].get("file_id")
            if file_id:
                self._file_ids.put(item.url, file_id)

    async def send(
        self,
        chat_id: int | str,
        items: list[MediaItem],
        caption: str,
        *,
        reply_to: int | None = None,
        part_from: int = 1,
        part_total: int | None = None,
        followup_captions: list[str] | None = None,
    ) -> SendReport:
        """Send an album, chunked to Telegram's ten-item limit.

        `part_from`/`part_total` let one album be split across two destinations
        — a channel post and its discussion thread — while the "[2/3]" markers
        keep counting from where the previous destination left off.

        `followup_captions` fills the groups after the first, in order. Without
        it a photo-overflow group carries only its "[2/2]" marker — a caption
        Telegram gives it for free — while the note's text overflow goes out as
        a message of its own right behind it. Carrying the text on the photos
        makes those two messages one. Whatever a group could not carry (every
        item in it was dropped) comes back in `report.unused_captions`.
        """
        report = SendReport()
        followup = [piece for piece in (followup_captions or []) if piece]
        groups = list(chunk(items))
        total = part_total if part_total is not None else len(groups)

        for offset, group in enumerate(groups):
            position = part_from + offset
            # A split album reads as one post: every part is marked, and each
            # part replies to the one before it so Telegram threads them.
            group_caption = caption if offset == 0 else (followup.pop(0) if followup else "")
            pending_caption = group_caption or None
            if total > 1:
                marker = f"[{position}/{total}]"
                pending_caption = f"{marker} {pending_caption}" if pending_caption else marker
            result = None
            while True:
                media, files, skipped, sources = await self._build(group, pending_caption)
                report.skipped.extend(skipped)
                if not media:
                    break
                try:
                    result = await self._send_group(chat_id, media, files, reply_to)
                except TelegramError as exc:
                    # Any 400 on a URL-mode send is worth one retry as an upload:
                    # Telegram spells this failure several ways (WEBPAGE_CURL_FAILED,
                    # WEBPAGE_MEDIA_EMPTY, ...) and enumerating them is a losing game.
                    # If the real problem was something else, the retry surfaces it.
                    # Blame only what Telegram actually named. An album can mix
                    # families — an Ultra-HDR image next to an ordinary one —
                    # and marking every URL in the group would condemn the
                    # family that was working.
                    by_url = [
                        (index, media_family(group[source].url))
                        for index, (entry, source) in enumerate(zip(media, sources), start=1)
                        if str(entry.get("media", "")).startswith("http")
                    ]
                    culprit = exc.failed_index
                    in_range = culprit is not None and culprit <= len(sources)
                    # A culprit outside the range we sent isn't trustworthy as a
                    # pointer (Telegram's numbering has been seen not to line up
                    # 1:1) — blame everything URL-mode, same as no culprit at
                    # all. But a culprit *inside* the range that isn't in by_url
                    # is already upload-mode (its family was refused on a
                    # previous pass through this loop): there is nothing fresh
                    # to blame, and falling back to "blame everything" here
                    # would condemn unrelated families Telegram never named.
                    if culprit is None or not in_range:
                        families = {family for _i, family in by_url}
                    else:
                        families = {family for index, family in by_url if index == culprit}
                    fresh = families - self._refused
                    if self._configured_mode == "auto" and exc.error_code == 400 and fresh:
                        log.warning(
                            "Telegram refused %s%s (%s); streaming that CDN family through instead",
                            ", ".join(sorted(fresh)),
                            "" if exc.is_url_fetch_failure else " for an unrecognised reason",
                            exc.description,
                            extra=fields(
                                event="cdn_refused",
                                families=sorted(fresh),
                                reason=exc.description[:120],
                                recognised=exc.is_url_fetch_failure,
                            ),
                        )
                        self._refused |= families
                        continue
                    # Streaming didn't fix it either (or there was nothing left
                    # to try streaming): the bytes themselves are the problem —
                    # an XHS panorama landing on PHOTO_INVALID_DIMENSIONS is what
                    # was seen live — not the fetch. Drop just the named item and
                    # keep the rest of the album moving rather than losing all of
                    # it to one photo.
                    if self._configured_mode == "auto" and exc.error_code == 400 and in_range:
                        bad_index = sources[culprit - 1]
                        report.skipped.append(f"item {bad_index + 1} rejected by Telegram ({exc.description})")
                        log.warning(
                            "Telegram rejected item %d (%s); dropping it and continuing",
                            bad_index + 1,
                            exc.description,
                            extra=fields(event="item_rejected", reason=exc.description[:120]),
                        )
                        del group[bad_index]
                        continue
                    raise
                self._remember(result, group, sources)
                anchor = first_message_id(result)
                if report.first_message_id is None:
                    report.first_message_id = anchor
                if anchor:
                    report.parts.append((anchor, pending_caption or ""))
                report.sent += len(media)
                report.uploaded = report.uploaded or bool(files)
                break
            if result is None and offset > 0 and group_caption:
                # The group never went out — every item in it was dropped — so
                # the text it was to carry must not go down with the photos.
                followup.insert(0, group_caption)
            reply_to = (first_message_id(result) if result else None) or reply_to
            if total > 1:
                await asyncio.sleep(1)  # be gentle with album rate limits
        report.unused_captions = followup
        return report

    async def _send_group(
        self, chat_id: int | str, media: list[dict], files: dict[str, tuple], reply_to: int | None
    ):
        if len(media) == 1:
            entry = media[0]
            method = "sendVideo" if entry["type"] == "video" else "sendPhoto"
            field_name = "video" if entry["type"] == "video" else "photo"
            payload = {
                "chat_id": chat_id,
                field_name: entry["media"],
                "caption": entry.get("caption"),
                "parse_mode": entry.get("parse_mode"),
                "reply_to_message_id": reply_to,
            }
            if entry["type"] == "video":
                payload["supports_streaming"] = True
            return await self._tg.call(method, payload, files or None, timeout=300.0)
        return await self._tg.call(
            "sendMediaGroup",
            {"chat_id": chat_id, "media": media, "reply_to_message_id": reply_to},
            files or None,
            timeout=300.0,
        )
