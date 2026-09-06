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

from .media import MEDIA_GROUP_LIMIT, tg_len, tg_truncate

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
    # A metadata chip shown in parentheses after the name. IP location on a
    # RedNote note; on a forum thread it marks the thread starter's own
    # replies.
    location: str = ""
    # Who this is answering, when the source says so. Forum replies quote the
    # post they answer; without the name a reply reads as a non-sequitur once
    # the quote itself is stripped out.
    replying_to: str = ""
    # Pictures attached to this comment. They are delivered separately — an
    # album belongs to whoever wrote the post it captions — so the comment
    # itself carries one marker per Telegram-sized album.
    images: list[str] = field(default_factory=list)
    replies: list["Comment"] = field(default_factory=list)


def _clean(value: str) -> str:
    return _STICKER_RE.sub("", value or "").strip()


def _picture_urls(raw: dict[str, Any]) -> list[str]:
    """Full-size URLs for a comment's pictures.

    Each picture carries two: `originUrl` is the original (1440 px-ish JPEG,
    ~150 KB) and `url` a ~360 px webp preview XHS generates for list views —
    deliver the original, not the thumbnail. The CDN serves both schemes and
    asks for no referer (verified live on the comment-picture note), so the
    scheme is normalised to https on the way in: a plain `http://` href is a
    mixed-content link once anything ever re-renders it on a page.
    """
    urls: list[str] = []
    for picture in raw.get("pictures") or []:
        if not isinstance(picture, dict):
            continue
        url = str(picture.get("originUrl") or picture.get("url") or "")
        if url.startswith("http://"):
            url = "https://" + url[len("http://") :]
        if url:
            urls.append(url)
    return urls


def _one(raw: dict[str, Any], *, with_replies: bool) -> Comment | None:
    text = _clean(raw.get("content") or "")
    author = (raw.get("user") or {}).get("nickname") or ""
    images = _picture_urls(raw)
    # An image-only comment (content: "", pictures: […]) is still a comment —
    # on purchase-showcase notes it is often the whole point. Only a comment
    # with neither text nor pictures is nothing. (An empty one that carries
    # replies is still dropped with them; nothing was ever rendered for it to
    # hang them off — see TODO.)
    if not text and not images:
        return None
    likes = str(raw.get("likeViewCount") or raw.get("likeCount") or "").strip()
    comment = Comment(
        author=author.strip(),
        text=text,
        likes="" if likes in ("", "0") else likes,
        location=(raw.get("ipLocation") or "").strip(),
        images=images,
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


def _render_one(comment: Comment, *, reply: bool, like: str = "♥") -> str:
    bullet = "  ↳" if reply else "💬"
    who = escape(comment.author) if comment.author else "anon"
    if comment.replying_to:
        who += f" → {escape(comment.replying_to)}"
    meta = " · ".join(
        bit for bit in (comment.likes and f"{like} {comment.likes}", comment.location) if bit
    )
    head = f"{bullet} <b>{who}</b>"
    if meta:
        head += f" <i>({escape(meta)})</i>"
    # One marker represents one Telegram-sized album, not one picture. Its
    # href starts as the first full-size image in that album; once sent to a
    # channel, `relink_images` upgrades it to the album's permalink. A set
    # larger than one media group gets one marker per group.
    for url in comment.images[::MEDIA_GROUP_LIMIT]:
        head += f' <a href="{escape(url, quote=True)}">📷</a>'
    if comment.text:
        return f"{head}\n{escape(comment.text)}"
    return head


HEADER = "— top comments —"

# One album per comment is the point — the credit lands on whoever posted the
# pictures — but the total is capped so a thread of camera-happy commenters
# cannot bury the note above. Markers for whatever falls past the cap still
# link the full-size image, so nothing goes silently missing.
ALBUM_BUDGET = 10


def comment_albums(
    comments: list[Comment], limit: int = ALBUM_BUDGET
) -> list[tuple[str, list[str]]]:
    """Telegram-sized albums for comment pictures: (caption, urls), in order.

    They travel separately from the note's own album for the same reason the
    forum's reply pictures do: an album's caption is the note author's, and
    someone else's photo under it credits the wrong person. A normal comment
    yields one album; an unusually large set is split so each 📷 marker maps
    to exactly one message. The shared budget still caps the total.
    """
    albums: list[tuple[str, list[str]]] = []
    remaining = limit
    for comment in comments:
        if not comment.images or remaining <= 0:
            continue
        urls = comment.images[:remaining]
        remaining -= len(urls)
        whose = f"{escape(comment.author)}'s" if comment.author else "a"
        caption = f"📷 from {whose} comment"
        for start in range(0, len(urls), MEDIA_GROUP_LIMIT):
            albums.append((caption, urls[start : start + MEDIA_GROUP_LIMIT]))
    return albums


def relink_images(html: str, links: dict[str, str]) -> str:
    """Point 📷 markers at the albums that were sent for them.

    `links` maps an album's lead picture URL to the message that delivered it.
    Only the marker anchors carry these URLs as hrefs — comment text is
    escaped plain text and the note's own footer links elsewhere — so replacing
    the href attribute is exact. A URL with no entry keeps its CDN href: that
    is a marker past the album budget, or one sent where no permalink exists
    (a DM), and the raw image is still one tap away.
    """
    for url, target in links.items():
        html = html.replace(
            f'href="{escape(url, quote=True)}"', f'href="{escape(target, quote=True)}"'
        )
    return html


def _pack(
    comments: list[Comment], *, limit: int, like: str
) -> tuple[list[str], int, int]:
    """Whole comments that fit in `limit`, what they cost, and where it stopped.

    Split out of `render_comments` so a caller can tell "they all fitted" from
    "some of them did" — which is exactly what `fit_into_caption` could not do,
    and the reason a truncated comment used to be the end of the thread.
    """
    blocks: list[str] = []
    used = tg_len(HEADER)
    for index, comment in enumerate(comments):
        rendered = [_render_one(comment, reply=False, like=like)]
        for child in comment.replies:
            rendered.append(_render_one(child, reply=True, like=like))
        block = "\n".join(rendered)
        cost = tg_len(_visible(block)) + 2  # separated by a blank line
        if used + cost > limit:
            return blocks, used, index
        blocks.append(block)
        used += cost
    return blocks, used, len(comments)


def render_comments(comments: list[Comment], *, limit: int, like: str = "♥") -> str:
    """Render as much of the comment thread as fits in `limit` UTF-16 units.

    Whole comments are dropped from the end rather than cut mid-sentence; a
    comment's replies go with it. An over-long single comment is truncated so
    that at least the top one always makes it in.

    That truncation is a last resort and belongs to the follow-up message,
    which has no third message behind it to spill into. The caption has one —
    see `fit_into_caption`, which never cuts a comment.
    """
    if not comments or limit <= 0:
        return ""

    blocks, used, stopped = _pack(comments, limit=limit, like=like)
    if stopped == 0:  # not even the first one fits: keep something rather than nothing
        first = comments[0]
        # The skeleton is the first comment with its text emptied: its 📷
        # markers stay, because the shortened comment renders them too and the
        # budget has to pay for what will actually be sent.
        skeleton = Comment(
            first.author, "", first.likes, first.location, first.replying_to,
            images=first.images,
        )
        # Four units on top of the skeleton as rendered: the blank line
        # (two) that separates the block, the line break the text sits behind,
        # and the ellipsis that will end it. (The break used to be paid for by
        # a phantom newline the skeleton rendered with; an image-only comment
        # no longer renders one, so it is counted here instead.)
        fixed = tg_len(_visible(_render_one(skeleton, reply=False, like=like))) + 4
        room = limit - used - fixed
        if room < 20:
            return ""
        head, _rest = tg_truncate(first.text, room)
        if not head:
            return ""
        shortened = Comment(
            first.author, f"{head}…", first.likes, first.location, first.replying_to,
            images=first.images,
        )
        blocks = [_render_one(shortened, reply=False, like=like)]

    if not blocks:
        return ""
    return HEADER + "\n\n" + "\n\n".join(blocks)


def fit_into_caption(
    caption_html: str, comments: list[Comment], *, limit: int, like: str = "♥"
) -> tuple[str, list[Comment]]:
    """Append the comment thread to a caption, or hand it back for the follow-up.

    Returns the caption and whatever did not go into it. All of them or none of
    them, which is the rule the note's own text already follows: a thread split
    across two messages would repeat its header and read as two threads.

    It used to append whatever fitted, truncating the first comment
    mid-sentence to do it, and return only the caption — so the caller, which
    could only compare the caption it got back against the one it passed in,
    read a half-rendered comment as "they all got in" and sent no follow-up at
    all. What the reader saw was a comment ending in an ellipsis with nothing
    behind it, and any later comment gone without trace. Seen live on
    /gradient_canopy/173.
    """
    if not comments:
        return caption_html, []
    room = limit - tg_len(_visible(caption_html)) - 2
    blocks, _used, stopped = _pack(comments, limit=room, like=like)
    if stopped < len(comments):
        return caption_html, list(comments)
    return f"{caption_html}\n\n" + HEADER + "\n\n" + "\n\n".join(blocks), []


def strip_tags(html: str) -> str:
    """What Telegram counts: tags are free, and "&amp;" is one character."""
    return unescape(re.sub(r"<[^>]+>", "", html))


_visible = strip_tags
