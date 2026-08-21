"""Top comments, scraped from the note page.

The sidecar reports only 評論數量 (a count), and XHS's own comment API answers
406 without a signed `x-s` header, so the comments come from the same place the
web app gets its first paint: the `window.__INITIAL_STATE__` blob embedded in
the note page. That carries the first five top-level comments with their
replies, and needs no cookie.

It is best-effort by design: XHS changes this markup whenever it likes, so a
miss returns an empty list rather than failing the note.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field
from html import escape, unescape
from typing import Any

import httpx

from .media import tg_len, tg_truncate

log = logging.getLogger(__name__)

_STATE_RE = re.compile(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*</script>", re.S)
# XHS renders its own sticker codes inline: "谢谢宝宝[害羞R]". They mean nothing
# outside the app, so they come out.
_STICKER_RE = re.compile(r"\[[^\[\]]{1,12}[RH]\]")


@dataclass
class Comment:
    author: str
    text: str
    likes: str = ""
    location: str = ""
    replies: list["Comment"] = field(default_factory=list)


def _clean(value: str) -> str:
    return _STICKER_RE.sub("", value or "").strip()


def _one(raw: dict[str, Any], *, with_replies: bool) -> Comment | None:
    text = _clean(raw.get("content") or "")
    author = (raw.get("user") or {}).get("nickname") or ""
    if not text:
        return None
    likes = str(raw.get("likeViewCount") or raw.get("likeCount") or "").strip()
    comment = Comment(
        author=author.strip(),
        text=text,
        likes="" if likes in ("", "0") else likes,
        location=(raw.get("ipLocation") or "").strip(),
    )
    if with_replies:
        for sub in raw.get("subComments") or []:
            reply = _one(sub, with_replies=False)
            if reply:
                comment.replies.append(reply)
    return comment


def parse_comments(html: str, limit: int = 5) -> list[Comment]:
    """Pull the embedded comment list out of a note page."""
    match = _STATE_RE.search(html)
    if not match:
        return []
    try:
        state = json.loads(match.group(1).replace("undefined", "null"))
    except ValueError:
        log.debug("note page carried an unparseable __INITIAL_STATE__")
        return []

    raw = (
        state.get("noteData", {})
        .get("data", {})
        .get("commentData", {})
        .get("comments")
    )
    if not isinstance(raw, list):
        return []

    comments: list[Comment] = []
    for entry in raw[:limit]:
        if isinstance(entry, dict):
            parsed = _one(entry, with_replies=True)
            if parsed:
                comments.append(parsed)
    return comments


async def fetch_comments(
    client: httpx.AsyncClient, page_url: str, *, limit: int = 5, timeout: float = 20.0
) -> list[Comment]:
    """Best-effort: never raise, since comments are a garnish on the note."""
    try:
        response = await client.get(page_url, follow_redirects=True, timeout=timeout)
        response.raise_for_status()
    except httpx.HTTPError as exc:
        log.info("no comments for %s: %s", page_url.split("?")[0], type(exc).__name__)
        return []
    return parse_comments(response.text, limit=limit)


def _render_one(comment: Comment, *, reply: bool) -> str:
    bullet = "  ↳" if reply else "💬"
    who = escape(comment.author) if comment.author else "anon"
    meta = " · ".join(bit for bit in (comment.likes and f"♥ {comment.likes}", comment.location) if bit)
    head = f"{bullet} <b>{who}</b>"
    if meta:
        head += f" <i>({escape(meta)})</i>"
    return f"{head}\n{escape(comment.text)}"


def render_comments(comments: list[Comment], *, limit: int) -> str:
    """Render as much of the comment thread as fits in `limit` UTF-16 units.

    Whole comments are dropped from the end rather than cut mid-sentence; a
    comment's replies go with it. An over-long single comment is truncated so
    that at least the top one always makes it in.
    """
    if not comments or limit <= 0:
        return ""

    header = "— top comments —"
    blocks: list[str] = []
    used = tg_len(header)

    for index, comment in enumerate(comments):
        rendered = [_render_one(comment, reply=False)]
        for child in comment.replies:
            rendered.append(_render_one(child, reply=True))
        block = "\n".join(rendered)
        cost = tg_len(_visible(block)) + 2  # separated by a blank line

        if used + cost > limit:
            if index == 0:  # keep something rather than nothing
                skeleton = Comment(comment.author, "", comment.likes, comment.location)
                fixed = tg_len(_visible(_render_one(skeleton, reply=False))) + 2
                room = limit - used - fixed - 1  # the ellipsis costs one
                if room < 20:
                    return ""
                head, _rest = tg_truncate(comment.text, room)
                if not head:
                    return ""
                shortened = Comment(comment.author, f"{head}…", comment.likes, comment.location)
                blocks.append(_render_one(shortened, reply=False))
            break
        blocks.append(block)
        used += cost

    if not blocks:
        return ""
    return header + "\n\n" + "\n\n".join(blocks)


def fit_into_caption(caption_html: str, comments: list[Comment], *, limit: int) -> str:
    """Append what fits of the comment thread to a caption, or return it unchanged.

    Comments ride in the caption so a forwarded album carries them along; they
    take whatever room the note text left behind.
    """
    if not comments:
        return caption_html
    room = limit - tg_len(_visible(caption_html)) - 2
    block = render_comments(comments, limit=room)
    return f"{caption_html}\n\n{block}" if block else caption_html


def strip_tags(html: str) -> str:
    """What Telegram counts: tags are free, and "&amp;" is one character."""
    return unescape(re.sub(r"<[^>]+>", "", html))


_visible = strip_tags
