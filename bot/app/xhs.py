"""Client for the XHS-Downloader sidecar, plus normalisation of its payload.

Pinned against XHS-Downloader 2.7: `python main.py api` serves POST /xhs/detail
on 5556 and accepts a per-request `cookie`, so the bot keeps the cookie in its
own state file and never has to rewrite the sidecar's config.

The sidecar's response keys are Chinese, and its *values* for the note type are
localised (zh_CN by default, en_US when the container has no locale), so both
spellings are accepted here.
"""

from __future__ import annotations

import logging
import re
from urllib.parse import parse_qs, unquote, urlsplit
from dataclasses import dataclass, field
from typing import Any

import httpx

from .logs import fields

log = logging.getLogger(__name__)

BROWSER_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_5 like Mac OS X) AppleWebKit/605.1.15 "
        "(KHTML, like Gecko) Version/17.5 Mobile/15E148 Safari/604.1"
    ),
}

# CJK punctuation is excluded so pasted share blurbs don't glue trailing
# characters onto the URL. Both share domains appear in the wild: xhslink.com
# from the international app, xhslink.cn from the mainland one.
_TAIL = r"[^\s\"<>\\^`{|}，。；！？、【】《》]+"
LINK_RE = re.compile(
    r"(?:https?://)?(?:"
    rf"xhslink\.(?:com|cn)/{_TAIL}"
    rf"|(?:www\.)?xiaohongshu\.(?:com|cn)/(?:explore|discovery/item|user/profile)/{_TAIL}"
    r")",
    re.IGNORECASE,
)

# What the sidecar's own extractor recognises (XHS-Downloader 2.7): xhslink.com
# short links, and www.xiaohongshu.com long links. Anything else — an
# xhslink.cn share, a www-less host — has to be resolved to that shape first.
# Only a canonical long URL goes to the sidecar untouched. Short links are
# resolved here: the sidecar's own resolver fails intermittently
# ("提取小红书作品链接失败") on links that redirect perfectly well from here.
_SIDECAR_READY_RE = re.compile(
    r"^https?://www\.xiaohongshu\.com/(?:explore|discovery/item|user/profile)/",
    re.IGNORECASE,
)
_LONG_NOTE_RE = re.compile(
    r"^https?://(?:www\.)?xiaohongshu\.(?:com|cn)/(?:explore|discovery/item|user/profile)/",
    re.IGNORECASE,
)
_NOTE_ID_RE = re.compile(r"/(?:explore|item)/([0-9a-zA-Z]+)")
_PROFILE_NOTE_RE = re.compile(r"/user/profile/[0-9a-zA-Z]+/([0-9a-zA-Z]+)")
# A profile with nothing after it. XHS share sheets produce these from a user's
# page, and there is no note behind one — worth saying so rather than reporting
# a generic parse failure.
_PROFILE_ONLY_RE = re.compile(
    r"^https?://(?:www\.)?xiaohongshu\.com/user/profile/[0-9a-zA-Z]+/?(?:\?|$)",
    re.IGNORECASE,
)

VIDEO_TYPES = {"视频", "video"}
IMAGE_TYPES = {"图文", "图集", "image", "livephoto"}

# Sidecar responses we treat as "the fetch itself failed", i.e. login wall or
# expired cookie, as opposed to "that wasn't a note link".
_BAD_LINK_MESSAGES = ("提取小红书作品链接失败", "failed to extract the links")

# XHS embeds its own tag markup in the description: "#湾区美食[话题]#". Reduced to
# a plain "#湾区美食", it renders as a real Telegram hashtag instead of noise.
_TOPIC_TAG_RE = re.compile(r"#([^#\s\[\]]+)\[话题\]#")
_TRAILING_SPACE_RE = re.compile(r"[ \u00a0]+\n")
_BLANK_RUN_RE = re.compile(r"\n{3,}")


def find_link(text: str) -> str | None:
    match = LINK_RE.search(text or "")
    if not match:
        return None
    url = match.group(0).rstrip(".,!?;:)]}'\"")
    return url if url.lower().startswith("http") else f"https://{url}"


def cache_key(url: str) -> str:
    """Note ID when the URL carries one, otherwise the share URL itself.

    Short xhslink URLs hide the ID until resolved, but a given share link is
    stable, so keying on it is equivalent for repeat shares.
    """
    for pattern in (_NOTE_ID_RE, _PROFILE_NOTE_RE):
        match = pattern.search(url)
        if match:
            return match.group(1)
    return url.split("?")[0].rstrip("/")


def _normalise_host(url: str) -> str:
    """Upstream's patterns require exactly www.xiaohongshu.com."""
    return re.sub(
        r"^(https?://)(?:www\.)?xiaohongshu\.(?:com|cn)",
        r"\1www.xiaohongshu.com",
        url,
        flags=re.IGNORECASE,
    )


def _unwrap_login_wall(url: str) -> str:
    """XHS bounces unauthenticated hits to /website-login/error?redirectPath=…

    The note URL we wanted is inside that parameter, so pull it back out rather
    than handing the sidecar an error page.
    """
    if "website-login/error" not in url:
        return url
    target = parse_qs(urlsplit(url).query).get("redirectPath", [""])[0]
    return unquote(target) if target else url


class XhsError(RuntimeError):
    """kind: 'bad_link' | 'profile' | 'blocked' | 'network' | 'empty'"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass
class MediaItem:
    kind: str  # "photo" | "video"
    url: str
    # Smaller renditions to fall back on if this one is too big to upload.
    alternatives: tuple[tuple[str, int], ...] = ()


@dataclass
class Note:
    note_id: str
    kind: str  # "video" | "image"
    title: str = ""
    desc: str = ""
    tags: str = ""
    author: str = ""
    author_url: str = ""
    url: str = ""
    published: str = ""
    page_url: str = ""  # the resolved URL the note came from (carries xsec_token)
    from_page: bool = False  # True when the sidecar refused and page.py stepped in
    photos: list[str] = field(default_factory=list)
    lives: list[str | None] = field(default_factory=list)
    video: str | None = None
    # (url, declared size) for every rendition, when the page has been read.
    # Telegram caps bot uploads at 50 MB and XHS ships videos well past that,
    # so a smaller rendition is often the difference between posting and not.
    video_variants: list[tuple[str, int]] = field(default_factory=list)

    def media(self, live_photos: str = "still") -> list[MediaItem]:
        """Flatten to an ordered album (PLAN §4.6)."""
        if self.kind == "video":
            if not self.video:
                return []
            spares = tuple((url, size) for url, size in self.video_variants if url != self.video)
            return [MediaItem("video", self.video, alternatives=spares)]
        items: list[MediaItem] = []
        for index, photo in enumerate(self.photos):
            live = self.lives[index] if index < len(self.lives) else None
            if live and live_photos == "video":
                items.append(MediaItem("video", live))
                continue
            items.append(MediaItem("photo", photo))
            if live and live_photos == "both":
                items.append(MediaItem("video", live))
        return items


def _clean(value: Any) -> str:
    if value in (None, "", "NaN"):
        return ""
    return str(value).strip()


def clean_text(value: str) -> str:
    """Tidy note text for display: real hashtags, no stray tabs, no blank runs."""
    value = _TOPIC_TAG_RE.sub(r"#\1", value)
    value = value.replace("\t", " ").replace("\r\n", "\n")
    value = _TRAILING_SPACE_RE.sub("\n", value)
    return _BLANK_RUN_RE.sub("\n\n", value).strip()


def _as_list(value: Any) -> list[str | None]:
    if value is None:
        return []
    if isinstance(value, str):
        # Older builds join these with spaces when data recording is enabled.
        value = value.split()
    return [None if v in (None, "", "NaN") else str(v) for v in value]


def parse_note(data: dict) -> Note:
    kind_raw = _clean(data.get("作品类型")).lower()
    urls = [u for u in _as_list(data.get("下载地址")) if u]
    lives = _as_list(data.get("动图地址"))

    if kind_raw in {t.lower() for t in VIDEO_TYPES}:
        kind = "video"
    elif kind_raw in IMAGE_TYPES:
        kind = "image"
    else:
        # Unknown/localised type: fall back to what the URLs look like.
        kind = "video" if len(urls) == 1 and "video" in urls[0] else "image"

    note = Note(
        note_id=_clean(data.get("作品ID")),
        kind=kind,
        title=clean_text(_clean(data.get("作品标题"))),
        desc=clean_text(_clean(data.get("作品描述"))),
        tags=_clean(data.get("作品标签")),
        author=_clean(data.get("作者昵称")),
        author_url=_clean(data.get("作者链接")),
        url=_clean(data.get("作品链接")),
        published=_clean(data.get("发布时间")).replace("_", " "),
    )
    if kind == "video":
        note.video = urls[0] if urls else None
    else:
        note.photos = urls
        note.lives = lives
    return note


def _page_state(html: str) -> dict:
    """The page's embedded state, or {} — see page.py for the details."""
    from .page import _state

    return _state(html) or {}


class XhsDownloader:
    def __init__(self, base_url: str, timeout: float = 120.0, proxy: str = ""):
        """`proxy` covers XHS itself, never the sidecar.

        The sidecar sits on the compose network one hop away; sending that hop
        through a proxy would be pointless and, if the proxy is down, fatal to
        requests that would otherwise work.
        """
        self._base = base_url.rstrip("/")
        self._proxy = proxy or None
        # To the sidecar: local, direct.
        self._api = httpx.AsyncClient(timeout=timeout)
        # To XHS: short-link redirects and the note page a comment scrape reads.
        self._client = httpx.AsyncClient(
            timeout=timeout, headers=BROWSER_HEADERS, proxy=self._proxy
        )
        self._comment_timeout = min(20.0, timeout)

    async def aclose(self) -> None:
        await self._client.aclose()
        await self._api.aclose()

    async def resolve(self, url: str) -> str:
        """Reshape a link into something the sidecar's extractor recognises.

        The sidecar only knows xhslink.com and www.xiaohongshu.com, so an
        xhslink.cn share has to be redirected here first; the `xsec_token` that
        makes a cookieless fetch work rides along in the final URL.

        Only *short* links are followed over the network. A long note URL just
        gets its host normalised — following it risks landing on the login-wall
        redirect and throwing away the note id we already had.
        """
        if _SIDECAR_READY_RE.match(url):
            return url
        if _LONG_NOTE_RE.match(url):
            return _normalise_host(url)
        try:
            async with self._client.stream(
                "GET", url, follow_redirects=True, headers=BROWSER_HEADERS, timeout=30.0
            ) as response:
                resolved = str(response.url)
        except httpx.HTTPError as exc:
            log.warning("could not resolve %s: %s", url.split("?")[0], type(exc).__name__)
            return url
        resolved = _unwrap_login_wall(resolved)
        resolved = _normalise_host(resolved)
        if resolved != url:
            log.info("resolved %s -> %s", url.split("?")[0], resolved.split("?")[0])
        return resolved

    async def enrich(self, note: Note, limit: int = 5) -> list:
        """Read the note's page once: top comments, and the video's renditions.

        The sidecar reports a single video URL and no size. The page lists every
        rendition with its size, which is what lets an oversized video be
        swapped for one that fits — so the fetch we were already making for
        comments earns its keep twice.
        """
        from .comments import parse_comments
        from .page import video_variants

        if not note.page_url:
            return []
        try:
            response = await self._client.get(note.page_url, follow_redirects=True,
                                              timeout=self._comment_timeout)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.info("no page for %s: %s", note.page_url.split("?")[0], type(exc).__name__)
            return []

        if note.kind == "video" and not note.video_variants:
            state = _page_state(response.text)
            data = ((state.get("noteData") or {}).get("data") or {}).get("noteData") or {}
            note.video_variants = video_variants(data.get("video") or {})
            if note.video_variants:
                log.debug(
                    "note %s has %d rendition(s): %s", note.note_id, len(note.video_variants),
                    ", ".join(f"{size // 1024 // 1024}MB" for _u, size in note.video_variants),
                )
        return parse_comments(response.text, limit=limit)

    async def healthy(self) -> bool:
        try:
            response = await self._api.get(
                f"{self._base}/", timeout=5.0, follow_redirects=False
            )
            return response.status_code < 500
        except httpx.HTTPError:
            return False

    async def detail(self, url: str, cookie: str | None = None) -> Note:
        url = await self.resolve(url)
        if _PROFILE_ONLY_RE.match(url):
            # No point asking the sidecar: there is no note here to fetch.
            raise XhsError("profile", "that link points at a profile, not a note")
        payload = {"url": url, "download": False, "skip": False}
        if cookie:
            payload["cookie"] = cookie
        if self._proxy:
            # The sidecar does its own fetching, so it needs telling separately.
            payload["proxy"] = self._proxy
        try:
            response = await self._api.post(f"{self._base}/xhs/detail", json=payload)
            response.raise_for_status()
            body = response.json()
        except httpx.HTTPError as exc:
            # Never let a traceback carry the cookie back up the stack (PLAN §7).
            raise XhsError("network", f"downloader unreachable: {type(exc).__name__}") from None
        except ValueError:
            raise XhsError("network", "downloader returned a non-JSON response") from None

        message = str(body.get("message", ""))
        data = body.get("data")
        if not data:
            lowered = message.lower()
            if any(m in lowered for m in _BAD_LINK_MESSAGES):
                raise XhsError("bad_link", message or "no note link found")
            # The signed API refuses some notes that render perfectly well for
            # an anonymous browser, so try the page before giving up.
            fallback = await self.from_page(url)
            if fallback:
                log.info(
                    "sidecar refused %s (%s); read it off the page instead",
                    fallback.note_id or url.split("?")[0], message,
                    extra=fields(
                        event="page_fallback", note=fallback.note_id, reason=message[:120]
                    ),
                )
                return fallback
            raise XhsError("blocked", message or "no data returned")

        note = parse_note(data)
        note.page_url = url
        if not note.media("both"):
            raise XhsError("empty", "note contained no downloadable media")
        return note

    async def from_page(self, url: str) -> Note | None:
        """Second try: read the note off its own page.

        Best-effort — returns None rather than raising, so the caller can
        report the original failure if this doesn't work either.
        """
        from .page import parse_note_page

        try:
            response = await self._client.get(url, follow_redirects=True)
            response.raise_for_status()
        except httpx.HTTPError as exc:
            log.info("page fallback could not load %s: %s", url.split("?")[0], type(exc).__name__)
            return None
        note = parse_note_page(response.text, url)
        if not note or not note.media("both"):
            return None
        note.page_url = url
        return note
