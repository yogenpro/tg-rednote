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
from urllib.parse import unquote
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
#
# rednote.com is the international face of the same site. It serves the very
# same note pages (verified: identical noteData and comments for one note), and
# a signed-in session belongs to one domain or the other — so links to it have
# to be recognised, not ignored.
_TAIL = r"[^\s\"<>\\^`{|}，。；！？、【】《》]+"
_NOTE_PATH = r"(?:explore|discovery/item|user/profile)"
LINK_RE = re.compile(
    r"(?:https?://)?(?:"
    rf"xhslink\.(?:com|cn)/{_TAIL}"
    rf"|(?:www\.)?xiaohongshu\.(?:com|cn)/{_NOTE_PATH}/{_TAIL}"
    rf"|(?:www\.)?rednote\.com/{_NOTE_PATH}/{_TAIL}"
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
    r"^https?://(?:www\.)?(?:xiaohongshu\.(?:com|cn)|rednote\.com)"
    r"/(?:explore|discovery/item|user/profile)/",
    re.IGNORECASE,
)
# Which domain a link arrived on. The sidecar only knows xiaohongshu.com, so
# that hop is always rewritten; the bot's own page fetches follow the link the
# user actually shared, because that is the domain their session is on.
_REDNOTE_RE = re.compile(r"^https?://(?:www\.)?rednote\.com", re.IGNORECASE)
_NOTE_ID_RE = re.compile(r"/(?:explore|item)/([0-9a-zA-Z]+)")
_PROFILE_NOTE_RE = re.compile(r"/user/profile/[0-9a-zA-Z]+/([0-9a-zA-Z]+)")
# A profile with nothing after it. XHS share sheets produce these from a user's
# page, and there is no note behind one — worth saying so rather than reporting
# a generic parse failure.
_PROFILE_ONLY_RE = re.compile(
    r"^https?://(?:www\.)?(?:xiaohongshu\.com|rednote\.com)/user/profile/[0-9a-zA-Z]+/?(?:\?|$)",
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
    """Upstream's patterns require exactly www.xiaohongshu.com.

    rednote.com is included because the sidecar rejects it outright —
    `提取小红书作品链接失败` — even though the note behind it fetches perfectly
    once the domain is rewritten (verified live against XHS-Downloader 2.7).
    """
    return re.sub(
        r"^(https?://)(?:www\.)?(?:xiaohongshu\.(?:com|cn)|rednote\.com)",
        r"\1www.xiaohongshu.com",
        url,
        flags=re.IGNORECASE,
    )


def sibling_host(url: str) -> str:
    """The other domain serving the same note.

    xiaohongshu.com and rednote.com are one site with two front doors, and
    measurably not one gate: with a valid xsec_token, xiaohongshu.com answered
    the security wall five times out of five while rednote.com served the same
    note five out of five, order randomised. A refusal on one is therefore
    worth exactly one retry on the other.
    """
    if _REDNOTE_RE.match(url):
        return re.sub(
            r"^(https?://)(?:www\.)?rednote\.com", r"\1www.xiaohongshu.com", url, flags=re.IGNORECASE
        )
    return re.sub(
        r"^(https?://)(?:www\.)?xiaohongshu\.com", r"\1www.rednote.com", url, flags=re.IGNORECASE
    )


def page_url_for(sidecar_url: str, shared: str) -> str:
    """Where to read the note *page* from.

    Both domains serve the same page, but a cookie belongs to one of them. The
    sidecar hop is always xiaohongshu.com because that is all it recognises;
    the requests this process makes follow the domain the link came in on, so
    a rednote.com account's session applies to a rednote.com link.
    """
    if _REDNOTE_RE.match(shared or ""):
        return re.sub(
            r"^(https?://)(?:www\.)?xiaohongshu\.com",
            r"\1www.rednote.com",
            sidecar_url,
            flags=re.IGNORECASE,
        )
    return sidecar_url


# XHS has two ways of saying no, and both keep the URL we asked for in a query
# parameter: /website-login/error?redirectPath=… for the login wall, and
# /404/sec_<token>?source=xhs_sec_server&originalUrl=… for its bot check. The
# second answers HTTP 200, so nothing downstream notices unless it is named.
# Three wall shapes so far: the login wall, the security wall, and the
# rednote.com spelling of the second. The parameter name differs by domain —
# `originalUrl` on xiaohongshu.com, `redirectPath` on rednote.com — and
# rednote nests it inside `?source=/404/sec_X?redirectPath=…`, where it is not
# a top-level query parameter at all. Scanning the whole URL handles every
# arrangement; parsing the query only handled two of the three.
_WALLS = ("website-login/error", "/404/sec_", "/404?source=")
_WALL_TARGET_RE = re.compile(r"(?:redirectPath|originalUrl)=([^&]+)", re.IGNORECASE)


def _unwrap_login_wall(url: str) -> str:
    """Recover the note URL from whichever wall we were bounced to."""
    if not is_wall(url):
        return url
    match = _WALL_TARGET_RE.search(url)
    return unquote(match.group(1)) if match else url


def is_wall(url: str) -> bool:
    """True when a fetch landed on a wall rather than on the note."""
    return any(marker in url for marker in _WALLS)


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


def _usable(html: str) -> bool:
    """Did this page actually come with a note on it?

    A wall answers 200 with parseable markup, so "the request worked" is not
    the same question.
    """
    return bool(((_page_state(html).get("noteData") or {}).get("data") or {}).get("noteData"))


class XhsDownloader:
    def __init__(self, base_url: str, timeout: float = 120.0, proxy: str = ""):
        """`proxy` covers XHS itself, never the sidecar.

        The sidecar sits on the compose network one hop away; sending that hop
        through a proxy would be pointless and, if the proxy is down, fatal to
        requests that would otherwise work. trust_env=False is what holds that
        line now that the proxy is exported into the environment as the default
        for everything else — see `Telegram` for the same pinning.
        """
        self._base = base_url.rstrip("/")
        self._proxy = proxy or None
        # To the sidecar: local, direct.
        self._api = httpx.AsyncClient(timeout=timeout, trust_env=False)
        # To XHS: short-link redirects and the note page a comment scrape reads.
        self._client = httpx.AsyncClient(
            timeout=timeout, headers=BROWSER_HEADERS, proxy=self._proxy
        )
        self._comment_timeout = min(20.0, timeout)

    def set_cookie(self, cookie: str | None) -> None:
        """Give the bot's *own* XHS requests the same session as the sidecar.

        The cookie used to reach only the sidecar, which meant a logged-in
        session helped the signed API and did nothing for the three requests
        this process makes itself — short-link resolution, the page fallback,
        and the comment/rendition scrape. That is the same argument the split
        proxy is built on, and it was never followed through here: the wall
        that hides a video's renditions is hit by *this* client, not the
        sidecar's.
        """
        if cookie:
            self._client.headers["cookie"] = cookie
        else:
            self._client.headers.pop("cookie", None)

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

    async def _page(self, url: str, timeout: float | None = None) -> tuple[str, str] | None:
        """Fetch a note page, falling back to the sibling domain.

        Returns (html, final url), or None when neither domain served it. The
        retry costs one request and only on failure — and it is the difference
        between a note being readable and not, whenever one domain is walling
        this IP and the other is not.
        """
        candidates = [url, sibling_host(url)]
        for attempt, candidate in enumerate(candidates):
            if attempt and candidate == candidates[0]:
                break  # not one of the two known domains; nothing to fall back to
            try:
                response = await self._client.get(
                    candidate, follow_redirects=True, timeout=timeout
                )
                response.raise_for_status()
            except httpx.HTTPError as exc:
                log.info("no page at %s: %s", candidate.split("?")[0], type(exc).__name__)
                continue
            if _usable(response.text) and not is_wall(str(response.url)):
                if attempt:
                    log.info(
                        "%s served the page %s would not", candidate.split("/")[2],
                        candidates[0].split("/")[2],
                        extra=fields(event="sibling_domain", host=candidate.split("/")[2]),
                    )
                return response.text, str(response.url)
            log.info(
                "the page at %s was walled or empty%s", candidate.split("?")[0],
                "" if attempt else "; trying the other domain",
                extra=fields(event="page_walled", host=candidate.split("/")[2]),
            )
        return None

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
        fetched = await self._page(note.page_url, self._comment_timeout)
        if not fetched:
            log.warning(
                "no page for %s on either domain", note.note_id or "?",
                extra=fields(event="page_unavailable", note=note.note_id),
            )
            return []
        html, _final = fetched

        if note.kind == "video" and not note.video_variants:
            state = _page_state(html)
            data = ((state.get("noteData") or {}).get("data") or {}).get("noteData") or {}
            note.video_variants = video_variants(data.get("video") or {})
            log.info(
                "note %s: %d rendition(s)%s", note.note_id, len(note.video_variants),
                (" — " + ", ".join(f"{size // 1024 // 1024}MB" for _u, size in note.video_variants))
                if note.video_variants else "",
                extra=fields(
                    event="renditions", note=note.note_id, count=len(note.video_variants)
                ),
            )
        return parse_comments(html, limit=limit)

    async def healthy(self) -> bool:
        try:
            response = await self._api.get(
                f"{self._base}/", timeout=5.0, follow_redirects=False
            )
            return response.status_code < 500
        except httpx.HTTPError:
            return False

    async def detail(self, url: str, cookie: str | None = None) -> Note:
        shared = url
        url = await self.resolve(url)
        # Where the bot reads the page from, which is not always where the
        # sidecar is pointed — see page_url_for.
        page = page_url_for(url, shared)
        if _PROFILE_ONLY_RE.match(url):
            # No point asking the sidecar: there is no note here to fetch.
            raise XhsError("profile", "that link points at a profile, not a note")
        payload = {"url": url, "download": False, "skip": False}
        if cookie:
            payload["cookie"] = cookie
        if self._proxy:
            # The sidecar does its own fetching in its own container, so the
            # environment the bot exports the proxy into cannot reach it.
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
            fallback = await self.from_page(page)
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
        note.page_url = page
        if not note.media("both"):
            raise XhsError("empty", "note contained no downloadable media")
        return note

    async def from_page(self, url: str) -> Note | None:
        """Second try: read the note off its own page.

        Best-effort — returns None rather than raising, so the caller can
        report the original failure if this doesn't work either.
        """
        from .page import parse_note_page

        fetched = await self._page(url)
        if not fetched:
            return None
        html, final = fetched
        note = parse_note_page(html, final)
        if not note or not note.media("both"):
            return None
        note.page_url = final
        return note
