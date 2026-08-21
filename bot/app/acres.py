"""1point3acres threads, read off the forum page.

The site is a Discuz X forum behind Cloudflare. Two things make it unlike the
RedNote path:

* **Cloudflare answers anonymous requests with a managed challenge**, so there
  is no cookieless mode at all. TLS impersonation is not enough — the challenge
  wants JavaScript — so the only way in is the owner's own browser cookie,
  which is why this feature is DM-only and owner-provisioned. `cf_clearance` is
  bound to the User-Agent that solved the challenge, so the UA is stored
  alongside the cookie rather than hardcoded.

* **The post body is deliberately poisoned.** Between every line break sits a
  `<font class="jammer">` carrying junk like ". From 1point 3acres bbs",
  "-baidu 1point3acres" or a lone Greek chi, and `<span style="display:none">`
  hides more of the same. A browser never renders any of it; a naive
  tag-stripper splices all of it into the text. `to_text` drops hidden
  elements wholesale rather than trying to recognise the payloads, and scrubs
  invisible codepoints on top.

Pages are GBK, not UTF-8. Everything here is best-effort against markup the
forum owns and can change; a parse miss reports a failure rather than posting
mangled text.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from html import escape
from html.parser import HTMLParser
from urllib.parse import urljoin

import httpx

log = logging.getLogger(__name__)

HOST = "www.1point3acres.com"
BBS_BASE = f"https://{HOST}/bbs/"

# A current desktop Chrome. Only a default: whatever browser solved the
# Cloudflare challenge decides the UA that its cf_clearance is valid for, and
# `parse_credentials` picks that one up from a "Copy as cURL" paste.
DEFAULT_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/128.0.0.0 Safari/537.36"
)

# The share shapes seen in the wild. The thread id is the same number in all of
# them — the new /home/thread/ frontend, the /interview/ view and the original
# Discuz permalink all address one tid — so they all normalise to the forum
# page, which is the only one that serves the post as HTML.
_TAIL = r"[^\s\"<>\\^`{|}，。；！？、【】《》]*"
LINK_RE = re.compile(
    r"(?:https?://)?(?:www\.)?1point3acres\.com/"
    r"(?:bbs/thread-\d+[-\d]*\.html"
    r"|(?:home|interview|bbs)/thread/\d+"
    rf"|bbs/forum\.php\?{_TAIL}"
    r")",
    re.IGNORECASE,
)
_TID_RE = re.compile(r"(?:/thread[-/](\d+)|[?&]tid=(\d+))", re.IGNORECASE)

# Cookies a 1point3acres session actually carries. cf_clearance is the
# Cloudflare pass; the Discuz auth cookie is prefixed with a per-install salt
# (`Vhcs_2132_auth`, `xxxx_2132_saltkey`, …), so it is matched loosely.
COOKIE_MARKERS = ("cf_clearance=", "_auth=", "_saltkey=", "_sid=", "_discuz")

# Elements that are not the post. `jammer` is the poison and is genuinely
# invisible; `locked`, the attachment tips and the "last edited by" line are
# the forum's own furniture, which reads as noise once it is out of context.
_HIDDEN_CLASSES = ("jammer", "locked", "attach_nopermission", "attach_tips", "pstatus")
_DROP_TAGS = {"script", "style", "noscript"}
_VOID_TAGS = {"br", "img", "hr", "input", "meta", "link", "source", "col", "area", "base", "wbr"}
_BREAK_TAGS = {"br", "p", "div", "tr", "li", "blockquote", "h1", "h2", "h3", "h4", "table"}
_HIDDEN_STYLE_RE = re.compile(r"display\s*:\s*none", re.IGNORECASE)

# Zero-width and directional characters, the codepoint-level version of the
# same trick. ZWJ (U+200D) is deliberately *not* here: it is load-bearing
# inside compound emoji, and the jammers observed here work at the element
# level anyway.
_INVISIBLE_RE = re.compile(
    "[\u00ad\u180e\u200b\u200c\u200e\u200f\u202a-\u202e\u2060-\u2064"
    "\u206a-\u206f\u115f\u1160\u3164\ufeff\uffa0]"
)

# Belt and braces: the exact strings the jammers carry, in case the class name
# changes but the payload doesn't. The element-level removal already handles
# all of these, so this list stays *strict* — a looser pattern would eat real
# sentences. ".google" and ".Waral" only match with the non-ASCII tail the
# jammer always appends, and a plain mention of the site is never punctuation-led.
_WATERMARK_RE = re.compile(
    r"[.．]\s*(?:"
    r"(?:From\s+)?1\s?point\s?3\s?acres(?:\.com|\s+bbs)?"
    r"|check\s+1point3acres\s+for\s+more\.?"
    r"|baidu\s+1point3acres"
    r"|\u672c\u6587\u539f\u521b\u81ea1point3acres\S*"
    r"|\u7559\u5b66(?:\u7533\u8bf7)?\u8bba\u575b-\u4e00\u4ea9\u4e09\u5206\u5730"
    r"|(?:Waral|google)\s*\S*[^\x00-\x7f]\S*"
    r")",
    re.IGNORECASE,
)
_WHITESPACE_RE = re.compile(r"\s+")
# Indentation either side of a line break is markup wrapping, not text.
_LINE_EDGE_RE = re.compile("[ \t\u00a0]*\n[ \t\u00a0]*")
_BLANK_RUN_RE = re.compile(r"\n{3,}")

# Smilies, spacers and plugin chrome. None of them are the post's own images.
_CHROME_IMAGE_RE = re.compile(
    r"(?:^|/)(?:static/image/|source/plugin/|images/)|(?:none|blank|spacer)\.gif$",
    re.IGNORECASE,
)


class AcresError(RuntimeError):
    """kind: 'bad_link' | 'challenge' | 'login' | 'network' | 'empty'"""

    def __init__(self, kind: str, message: str):
        super().__init__(message)
        self.kind = kind


@dataclass
class Thread:
    tid: str
    url: str  # the /bbs/ permalink, which is the only shape that serves HTML
    # The shape the reader actually shared. Every share form addresses the same
    # tid and the fetch has to use the Discuz permalink, but handing back a URL
    # the reader did not send — in an older UI than the one they were looking
    # at — is a small, avoidable surprise.
    share_url: str = ""
    title: str = ""
    author: str = ""
    author_url: str = ""
    published: str = ""
    forum: str = ""       # the tag strip under the title ("数科面经")
    summary: str = ""     # the structured header on interview-experience posts
    body: str = ""
    images: list[str] = field(default_factory=list)
    # True when the post contains content gated behind points or a login, so
    # what we deliver is knowingly partial and should say so.
    locked: bool = False
    needs_login: bool = False
    # The best replies on page one, already ranked. They come off the same
    # fetch as the post, so they cost nothing extra.
    comments: list = field(default_factory=list)
    # Where this thread was published, once it has been. Cached with the
    # thread, so re-sending a link inside the cache TTL hands back the page
    # that exists instead of minting another one.
    page: str = ""

    @property
    def link(self) -> str:
        """Where to point a reader. What they shared, if we know it."""
        return self.share_url or self.url


def find_acres_link(text: str) -> str | None:
    match = LINK_RE.search(text or "")
    if not match:
        return None
    url = match.group(0).rstrip(".,!?;:)]}'\"")
    if not thread_id(url):
        return None
    return url if url.lower().startswith("http") else f"https://{url}"


def thread_id(url: str) -> str | None:
    match = _TID_RE.search(url or "")
    if not match:
        return None
    return match.group(1) or match.group(2)


def canonical(url: str) -> str:
    """The one URL shape that serves the post as HTML.

    /home/thread/<tid> is a JavaScript frontend and /interview/thread/<tid> is
    a different view of the same tid; the Discuz permalink is what carries the
    post text, so every share shape is rewritten to it.
    """
    tid = thread_id(url)
    if not tid:
        raise AcresError("bad_link", "no thread id in that link")
    return f"{BBS_BASE}thread-{tid}-1-1.html"


def scrub(text: str) -> str:
    """Strip the interference and tidy what's left."""
    text = _INVISIBLE_RE.sub("", text)
    text = _WATERMARK_RE.sub("", text)
    text = text.replace("\r\n", "\n").replace("\r", "\n").replace("\u00a0", " ")
    text = _LINE_EDGE_RE.sub("\n", text)
    return _BLANK_RUN_RE.sub("\n\n", text).strip()


class _Extractor(HTMLParser):
    """HTML to text, skipping everything a browser wouldn't paint.

    Hidden elements nest (the paywall block is divs inside divs), so this
    tracks open tags and suppresses output until the element that started the
    skip closes again — which a regex over the markup cannot do.
    """

    def __init__(self, base: str = BBS_BASE):
        super().__init__(convert_charrefs=True)
        self.base = base
        self.chunks: list[str] = []
        self.images: list[str] = []
        self.locked = False
        self.needs_login = False
        self._open: list[str] = []
        self._skip_depth: int | None = None

    # -- helpers
    @staticmethod
    def _hidden(attrs: dict[str, str]) -> str | None:
        classes = (attrs.get("class") or "").lower()
        for name in _HIDDEN_CLASSES:
            if name in classes:
                return name
        if _HIDDEN_STYLE_RE.search(attrs.get("style") or ""):
            return "style"
        return None

    def _break(self) -> None:
        if self.chunks and not self.chunks[-1].endswith("\n"):
            self.chunks.append("\n")

    # -- HTMLParser hooks
    def handle_starttag(self, tag: str, attrlist) -> None:
        attrs = {k.lower(): (v or "") for k, v in attrlist}
        hidden = self._hidden(attrs)
        if hidden == "locked":
            # Points-gated text sits mid-sentence. Splicing the two halves
            # together silently would read as the author's own words, so the
            # hole is marked where it actually is.
            self.locked = True
            if self._skip_depth is None:
                self.chunks.append(" […] ")
        elif hidden == "attach_nopermission" and "attach_tips" not in (attrs.get("class") or ""):
            # `attach_nopermission attach_tips` together is Discuz's standing
            # "log in to view attachments" banner: the template prints it at
            # the top of every post cell, attachments or not, logged in or not
            # (verified live — it is there on a thread the cookie read in
            # full). Only a bare `attach_nopermission`, which wraps one
            # attachment the reader may not have, means something is missing.
            self.needs_login = True

        if tag in _VOID_TAGS:
            if self._skip_depth is None:
                if tag == "br":
                    self.chunks.append("\n")
                elif tag == "img" and not hidden:
                    self._image(attrs)
            return

        if self._skip_depth is None and (tag in _DROP_TAGS or hidden):
            self._skip_depth = len(self._open)
        if self._skip_depth is None and tag in _BREAK_TAGS:
            self._break()
        self._open.append(tag)

    def handle_startendtag(self, tag: str, attrlist) -> None:
        self.handle_starttag(tag, attrlist)
        if tag not in _VOID_TAGS:
            self.handle_endtag(tag)

    def handle_endtag(self, tag: str) -> None:
        if tag in _VOID_TAGS:
            return
        # Discuz emits stray closers; only unwind to a tag we actually opened.
        if tag not in self._open:
            return
        while self._open:
            popped = self._open.pop()
            if self._skip_depth is not None and len(self._open) <= self._skip_depth:
                self._skip_depth = None
            if popped == tag:
                break
        if self._skip_depth is None and tag in _BREAK_TAGS:
            self._break()

    def handle_data(self, data: str) -> None:
        if self._skip_depth is None and data:
            # Newlines in the markup are insignificant whitespace — the forum
            # wraps its source, and taking those literally double-spaces every
            # line, because each <br /> is followed by one. The real breaks all
            # arrive as <br> and block tags.
            self.chunks.append(_WHITESPACE_RE.sub(" ", data))

    def _image(self, attrs: dict[str, str]) -> None:
        # Discuz points a thumbnail at a placeholder gif and keeps the real
        # attachment in `zoomfile`/`file`, so those come first.
        src = (
            attrs.get("zoomfile")
            or attrs.get("file")
            or attrs.get("data-original")
            or attrs.get("data-src")
            or attrs.get("src")
            or ""
        ).strip()
        if not src or src.startswith("data:") or _CHROME_IMAGE_RE.search(src):
            return
        url = urljoin(self.base, src)
        if url not in self.images:
            self.images.append(url)

    @property
    def text(self) -> str:
        return scrub("".join(self.chunks))


def to_text(html: str, base: str = BBS_BASE) -> tuple[str, list[str], bool, bool]:
    """(text, image urls, locked, needs_login) for a fragment of post markup."""
    extractor = _Extractor(base)
    extractor.feed(html)
    extractor.close()
    return extractor.text, extractor.images, extractor.locked, extractor.needs_login


def plain(html: str) -> str:
    """Just the visible text of a small fragment — a title, a tag."""
    return to_text(html)[0]


def decode(raw: bytes) -> str:
    """Discuz serves GBK here, and mislabels it often enough to check the meta."""
    head = raw[:2048].decode("ascii", errors="replace").lower()
    declared = re.search(r'charset=["\']?([\w-]+)', head)
    order = [declared.group(1)] if declared else []
    order += ["gbk", "utf-8"]
    for encoding in order:
        try:
            return raw.decode(encoding)
        except (UnicodeDecodeError, LookupError):
            continue
    return raw.decode("gbk", errors="replace")


_SUBJECT_RE = re.compile(r'<span[^>]*id="thread_subject"[^>]*>(.*?)</span>', re.S | re.I)
_AUTHOR_RE = re.compile(
    r'<div[^>]*itemprop="author".*?<span[^>]*itemprop="name"[^>]*>(.*?)</span>', re.S | re.I
)
_UID_RE = re.compile(r'home\.php\?mod=space&(?:amp;)?uid=(\d+)', re.I)
_DATE_RE = re.compile(r'<meta[^>]*itemprop="datePublished"[^>]*content="([^"]*)"', re.I)
_TAG_RE = re.compile(r'<a[^>]*class="taglink[^"]*"[^>]*>(.*?)</a>', re.S | re.I)
# The structured line interview-experience posts carry above the body: term,
# role, degree, outcome. It sits in its own span immediately before the post.
_SUMMARY_RE = re.compile(r'<span style="margin-top: 3px">(.*?)</span>', re.S | re.I)
_BODY_START_RE = re.compile(r'<td[^>]*class="t_f"[^>]*id="postmessage_(\d+)"[^>]*>', re.I)
_TD_RE = re.compile(r"</?td\b", re.I)
_BASE_RE = re.compile(r'<base[^>]*href="([^"]*)"', re.I)
# What the forum shows instead of a thread when you are not entitled to it.
_NOTICE_MARKERS = ("提示信息", "您需要登录才可以", "本主题需要", "无权访问", "只有本人可见")


def _body(html: str, start: re.Match) -> tuple[str, int]:
    """(the post cell, where it ends), counting <td> rather than stopping at
    the first </td>.

    A quoted table inside a post would end a non-greedy match early and lose
    the rest of the text. The end offset matters too: a post's attachments are
    rendered *after* the cell, and finding them means knowing where to look.
    """
    depth = 1
    cursor = start.end()
    for token in _TD_RE.finditer(html, cursor):
        depth += -1 if token.group(0).startswith("</") else 1
        if depth == 0:
            return html[cursor : token.start()], token.start()
    return html[cursor:], len(html)


_POST_RE = re.compile(r'<div id="post_(\d+)"', re.I)
_IMG_TAG_RE = re.compile(r"<img\b[^>]*>", re.I)
_ATTR_RE = re.compile(r'([\w-]+)\s*=\s*"([^"]*)"')


def attachment_images(fragment: str, base: str = BBS_BASE) -> list[str]:
    """Attachment images, which Discuz renders outside the post cell.

    A post's own uploads land in a `pattl` block *after* `t_f`, not inside it,
    unless the author placed them inline with [attachimg] — so the body
    extractor never sees them. Only `<img>` carrying `zoomfile`/`file` counts:
    the same region holds rating chrome and the forum's house ads, and those
    have neither.
    """
    found: list[str] = []
    for tag in _IMG_TAG_RE.finditer(fragment):
        attrs = {key.lower(): value for key, value in _ATTR_RE.findall(tag.group(0))}
        src = (attrs.get("zoomfile") or attrs.get("file") or "").strip()
        if not src or src.startswith("data:") or _CHROME_IMAGE_RE.search(src):
            continue
        url = urljoin(base, src)
        if url not in found:
            found.append(url)
    return found


def _post_extent(html: str, pid: str, after: int) -> int:
    """Where the post that owns `pid` stops.

    Replies carry attachments too, and they are markup-identical to the
    opening post's — verified live on a thread whose only picture belonged to
    a reply. Without this bound the first reply's photos would be posted as
    the author's.
    """
    following = _POST_RE.search(html, after)
    return following.start() if following else len(html)


def parse_thread(html: str, url: str, *, replies: int = 10) -> Thread | None:
    """Build a Thread from a Discuz viewthread page, or None if it isn't one."""
    match = _BODY_START_RE.search(html)
    if not match:
        return None
    base_match = _BASE_RE.search(html)
    base = base_match.group(1) if base_match else BBS_BASE

    cell, cell_end = _body(html, match)
    text, images, locked, needs_login = to_text(cell, base)
    # Anything the author attached rather than inlined sits between the end of
    # the cell and the start of the next post.
    for url in attachment_images(html[cell_end : _post_extent(html, match.group(1), cell_end)], base):
        if url not in images:
            images.append(url)
    thread = Thread(
        tid=thread_id(url) or "",
        url=url,
        title=plain(_SUBJECT_RE.search(html).group(1)) if _SUBJECT_RE.search(html) else "",
        body=text,
        images=images,
        locked=locked,
        needs_login=needs_login,
    )
    author = _AUTHOR_RE.search(html)
    if author:
        thread.author = plain(author.group(1))
    uid = _UID_RE.search(html)
    if uid:
        thread.author_url = f"https://{HOST}/bbs/space-uid-{uid.group(1)}.html"
    published = _DATE_RE.search(html)
    if published:
        thread.published = published.group(1).strip()
    tag = _TAG_RE.search(html)
    if tag:
        thread.forum = plain(tag.group(1))
    thread.comments = parse_replies(html, limit=replies, starter=thread.author)
    summary = _SUMMARY_RE.search(html)
    if summary:
        # Only the header that belongs to the opening post: anything found
        # after the body has started belongs to a reply.
        if summary.start() < match.start():
            thread.summary = plain(summary.group(1))
    return thread


# Replies are the other half of a forum thread, and Discuz serves them on the
# same page as the opening post — so they cost no extra request.
#
# The page is in *chronological* order and there is no way to ask for another:
# `ordertype=1` only reverses it. The ranking has to be done here, and the
# signal to rank on is not the obvious one. Each post shows a green/red bar,
# but it is labelled 全局 — that is the *author's* lifetime reputation, not the
# post's score, and the same user shows identical numbers on every post they
# make in a thread. The per-post score is 好苗/杂草: `rec_add_<pid>` and
# `rec_sub_<pid>`, which do vary post by post.
_QUOTE_RE = re.compile(r'<div class="quote">.*?</blockquote>\s*</div>', re.S | re.I)
_QUOTE_AUTHOR_RE = re.compile(r'<font color="#999999">\s*(.*?)\s+\u53d1\u8868\u4e8e', re.S | re.I)
# The quote links back to the exact post it answers, which is what makes a
# conversation reconstructable rather than guessable from names.
_QUOTE_PID_RE = re.compile(r"goto=findpost&(?:amp;)?pid=(\d+)", re.I)
_AUTHOR_NAME_RE = re.compile(
    r'<div[^>]*itemprop="author".*?<span[^>]*itemprop="name"[^>]*>(.*?)</span>', re.S | re.I
)
_THREAD_STARTER_RE = re.compile(r'ico_lz\.png|\u697c\u4e3b', re.I)


def _score(html: str, pid: str, name: str) -> int:
    match = re.search(rf'<i id="{name}_{pid}"[^>]*>\s*(\d+)\s*</i>', html)
    return int(match.group(1)) if match else 0


def _posts(html: str) -> list[tuple[str, str]]:
    """(pid, markup) for every post on the page, in the order served."""
    marks = list(_POST_RE.finditer(html))
    return [
        (mark.group(1), html[mark.start() : (marks[i + 1].start() if i + 1 < len(marks) else len(html))])
        for i, mark in enumerate(marks)
    ]


def parse_replies(html: str, *, limit: int = 10, starter: str = "") -> list:
    """The best conversations on page one, ranked by net 好苗 and cut to `limit`.

    Replies nest. A quote carries `goto=findpost&pid=<parent>`, an exact
    pointer to the post it answers, so a chain reconstructs precisely rather
    than by matching names — and the forum's own UI shows these inline under
    what they answer. `limit` counts top-level replies; a conversation hanging
    off one travels with it, the way a note's comment brings its subComments.

    Best-effort like everything else here: a shape this does not recognise
    yields fewer replies, never a failed thread.
    """
    from .comments import Comment

    if limit <= 0:
        return []

    records: dict[str, dict] = {}
    for order, (pid, block) in enumerate(_posts(html)):
        if order == 0:
            continue  # the opening post is the thread, not a reply
        cell = _BODY_START_RE.search(block)
        if not cell:
            continue
        raw, cell_end = _body(block, cell)
        parent = ""
        quoted = ""
        quote = _QUOTE_RE.search(raw)
        if quote:
            # The quote repeats a post already on screen, so it comes out; the
            # pid it points at is what lets the reply be filed under it.
            found = _QUOTE_AUTHOR_RE.search(quote.group(0))
            quoted = plain(found.group(1)) if found else ""
            pointer = _QUOTE_PID_RE.search(quote.group(0))
            parent = pointer.group(1) if pointer else ""
            raw = raw.replace(quote.group(0), "")
        text = to_text(raw)[0]
        if not text:
            continue
        author = _AUTHOR_NAME_RE.search(block)
        name = plain(author.group(1)) if author else ""
        net = _score(block, pid, "rec_add") - _score(block, pid, "rec_sub")
        records[pid] = {
            "order": order,
            "parent": parent,
            "score": net,
            "comment": Comment(
                author=name,
                text=text,
                likes="" if net <= 0 else str(net),
                # Not a place: the chip marks the thread starter answering in
                # their own thread, which is usually worth spotting.
                location="OP" if name and name == starter else "",
                # Answering the thread starter is the default and says
                # nothing; answering someone else is the interesting case.
                replying_to=quoted if quoted and quoted not in (name, starter) else "",
                images=attachment_images(block[cell_end:], BBS_BASE),
            ),
        }

    children: dict[str, list[str]] = {}
    roots: list[str] = []
    for pid, record in records.items():
        parent = record["parent"]
        if parent in records:
            children.setdefault(parent, []).append(pid)
        else:
            roots.append(pid)

    def descendants(pid: str) -> list[str]:
        """Everything hanging off `pid`, depth-first, in the order posted."""
        found: list[str] = []
        for child in sorted(children.get(pid, []), key=lambda p: records[p]["order"]):
            found.append(child)
            found.extend(descendants(child))
        return found

    roots.sort(key=lambda pid: (-records[pid]["score"], records[pid]["order"]))
    conversations = []
    for pid in roots[:limit]:
        top = records[pid]["comment"]
        for child in descendants(pid):
            reply = records[child]["comment"]
            if records[child]["parent"] == pid:
                reply.replying_to = ""  # it sits directly under what it answers
            top.replies.append(reply)
        conversations.append(top)
    return conversations


# Ten is one album. A thread with more picture-carrying replies than that is
# not worth a second one — the markers in the text still link to the rest.
GALLERY_LIMIT = 10


def reply_gallery(comments: list, limit: int = GALLERY_LIMIT) -> tuple[list[str], str]:
    """(image urls, caption) for the pictures attached to replies.

    They travel as their own album rather than joining the post's, because an
    album's caption is the opening post and putting someone else's photo under
    it credits the wrong person — the mistake this very thread nearly caused
    when a reply's picture was almost delivered as the author's.
    """
    urls: list[str] = []
    authors: list[str] = []
    for comment in comments:
        for url in comment.images:
            if len(urls) >= limit:
                break
            urls.append(url)
            name = comment.author or "someone"
            if name not in authors:
                authors.append(name)
    if not urls:
        return [], ""
    if len(authors) == 1:
        return urls, f"📷 from {escape(authors[0])}\u2019s reply"
    return urls, "📷 from replies by " + ", ".join(escape(name) for name in authors)


def parse_credentials(text: str) -> tuple[str, str]:
    """(cookie, user agent) from a pasted cookie or a "Copy as cURL".

    The cURL form is worth supporting because `cf_clearance` is only valid for
    the User-Agent that earned it — pasting the curl command carries both, and
    a bare cookie leaves the UA to the default and to luck.
    """
    text = text.strip()
    if not text.lower().startswith("curl"):
        return text, ""
    header = r"""-(?:H|-header)\s+(['"])\s*%s\s*:\s*(.*?)\1"""
    cookie = re.search(header % "cookie", text, re.I | re.S)
    if not cookie:
        cookie = re.search(r"""-(?:b|-cookie)\s+(['"])(.*?)\1""", text, re.I | re.S)
    agent = re.search(header % "user-agent", text, re.I | re.S)
    if not agent:
        agent = re.search(r"""-(?:A|-user-agent)\s+(['"])(.*?)\1""", text, re.I | re.S)
    return (
        cookie.group(2).strip() if cookie else "",
        agent.group(2).strip() if agent else "",
    )


def looks_like_cookie(text: str) -> bool:
    """Is a bare paste a forum session?

    A false positive here is expensive: the cookie handler *deletes* the
    message it was given. A URL can carry any of these markers in a query
    string, so anything that looks like a link is never a cookie.
    """
    stripped = text.strip()
    if len(stripped) < 40 or "\n" in stripped:
        return False
    if "://" in stripped or stripped.lower().startswith("www."):
        return False
    return "=" in stripped and any(m in stripped for m in COOKIE_MARKERS)


class Acres:
    """Fetches a thread page with the owner's browser credentials."""

    def __init__(self, timeout: float = 60.0, user_agent: str = DEFAULT_UA, replies: int = 10):
        self._ua = user_agent or DEFAULT_UA
        self._replies = replies
        self._client = httpx.AsyncClient(timeout=timeout, follow_redirects=True)

    async def aclose(self) -> None:
        await self._client.aclose()

    def headers(self, cookie: str | None, user_agent: str | None = None) -> dict[str, str]:
        headers = {
            "User-Agent": user_agent or self._ua,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Accept-Language": "en-US,en;q=0.9,zh-CN;q=0.8",
            "Referer": f"https://{HOST}/",
        }
        if cookie:
            headers["Cookie"] = cookie
        return headers

    async def thread(
        self, url: str, cookie: str | None = None, user_agent: str | None = None
    ) -> Thread:
        target = canonical(url)
        try:
            response = await self._client.get(target, headers=self.headers(cookie, user_agent))
        except httpx.HTTPError as exc:
            # Never let the cookie ride out on a traceback (PLAN §7).
            raise AcresError("network", f"1point3acres unreachable: {type(exc).__name__}") from None

        html = decode(response.content)
        if _challenged(response.status_code, html):
            raise AcresError(
                "challenge",
                "Cloudflare answered with a challenge instead of the thread",
            )
        if response.status_code >= 400:
            raise AcresError("network", f"1point3acres returned HTTP {response.status_code}")

        thread = parse_thread(html, target, replies=self._replies)
        if thread is not None:
            # Tracking parameters are not part of the thread.
            thread.share_url = url.split("?")[0]
        if thread is None:
            if any(marker in html for marker in _NOTICE_MARKERS):
                raise AcresError("login", "the forum served a notice page instead of the thread")
            raise AcresError("empty", "no post found on that page")
        if not thread.body and not thread.images:
            raise AcresError("empty", "that thread's opening post is empty")
        return thread


def _challenged(status: int, html: str) -> bool:
    return "_cf_chl_opt" in html or (status == 403 and "Just a moment" in html)


# Rendering lives here rather than in media.py because a forum thread's shape
# is nothing like a note's: no tags, a structured header line, and a body that
# is usually far too long for a caption.
def render(thread: Thread, *, limit: int, reserve: int = 0) -> tuple[str, str]:
    """(HTML for the first message, plain-text overflow).

    Same contract as media.build_caption: the footer is reserved up front so it
    survives truncation, and whatever the body cannot fit comes back for a
    follow-up message. Lengths are UTF-16 code units on the *parsed* text.
    """
    from .media import tg_len, tg_truncate

    limit = max(0, limit - reserve)
    link_text = "open on 1point3acres"
    bits = [b for b in (thread.author, thread.published.split(" ")[0], thread.forum) if b]
    foot_plain = " · ".join(bits + [link_text])
    foot_html = " · ".join(
        [escape(b) for b in bits]
        + [f'<a href="{escape(thread.link, quote=True)}">{link_text}</a>']
    )

    head_html = ""
    head_len = 0
    if thread.title:
        head_html = f"<b>{escape(thread.title)}</b>"
        head_len = tg_len(thread.title) + 2
    summary = thread.summary
    if summary and head_len + tg_len(summary) + 2 > limit // 2:
        summary = ""  # a header line is context, never worth the body's room
    if summary:
        head_html = f"{head_html}\n<i>{escape(summary)}</i>" if head_html else f"<i>{escape(summary)}</i>"
        head_len += tg_len(summary) + 1

    # A gated thread has to say so, and it reads as part of the post rather
    # than as a message of its own — so its room is reserved like the footer's.
    gated = thread.locked or thread.needs_login
    note_len = tg_len(GATED_NOTE) + 2 if gated else 0
    room = max(0, limit - (2 + tg_len(foot_plain)) - head_len - note_len)
    if tg_len(thread.body) > room:
        body, overflow = tg_truncate(thread.body, max(0, room - 1))
        body = f"{body.rstrip()}…" if body else ""
        if not body:
            overflow = thread.body
    else:
        body, overflow = thread.body, ""

    blocks = [
        block
        for block in (
            head_html,
            escape(body) if body else "",
            f"<i>{GATED_NOTE}</i>" if gated else "",
            foot_html,
        )
        if block
    ]
    return "\n\n".join(blocks), overflow


# No apostrophes: this is the one string that reaches Telegram unescaped, as
# markup rather than as post text.
GATED_NOTE = (
    "⚠️ Part of this thread sits behind the forum points wall — "
    "open it on 1point3acres for the rest."
)


# ---- Telegraph ------------------------------------------------------

def _paragraphs(text: str) -> list:
    """Text to <p> nodes, keeping single line breaks as <br>.

    A forum post uses both: blank lines separate sections, single newlines are
    a list or a set of figures. Splitting on every newline would space the
    second kind out into unreadable drifts of paragraphs.
    """
    nodes = []
    for block in text.split("\n\n"):
        lines = [line for line in block.split("\n") if line.strip()]
        if not lines:
            continue
        children: list = []
        for index, line in enumerate(lines):
            if index:
                children.append({"tag": "br"})
            children.append(line)
        nodes.append({"tag": "p", "children": children})
    return nodes


def _figure(url: str, caption: str = "") -> dict:
    children: list = [{"tag": "img", "attrs": {"src": url}}]
    if caption:
        children.append({"tag": "figcaption", "children": [caption]})
    return {"tag": "figure", "children": children}


def _comment_nodes(comment) -> list:
    head: list = [{"tag": "strong", "children": [comment.author or "anon"]}]
    if comment.replying_to:
        head.append(f" → {comment.replying_to}")
    chips = " · ".join(
        bit for bit in (comment.likes and f"👍 {comment.likes}", comment.location) if bit
    )
    if chips:
        head.append({"tag": "em", "children": [f" ({chips})"]})
    nodes: list = [{"tag": "p", "children": head}]
    nodes.extend(_paragraphs(comment.text))
    nodes.extend(_figure(url) for url in comment.images)
    return nodes


def to_nodes(thread: Thread) -> list:
    """A whole thread as Telegraph content: post, pictures, then replies.

    Ordered so that `telegraph.trim` cuts from the least valuable end — the
    post survives, the replies are what a 64 KB page loses first.
    """
    nodes: list = []
    meta = " · ".join(bit for bit in (thread.author, thread.published, thread.forum) if bit)
    if meta:
        nodes.append({"tag": "p", "children": [{"tag": "em", "children": [meta]}]})
    if thread.summary:
        nodes.append({"tag": "blockquote", "children": [thread.summary]})
    nodes.extend(_paragraphs(thread.body))
    if thread.locked or thread.needs_login:
        nodes.append({"tag": "p", "children": [{"tag": "em", "children": [GATED_NOTE]}]})
    nodes.extend(_figure(url) for url in thread.images)

    if thread.comments:
        nodes.append({"tag": "hr"})
        nodes.append({"tag": "h3", "children": ["Top replies"]})
        for comment in thread.comments:
            nodes.extend(_comment_nodes(comment))
            if comment.replies:
                # The answers to a reply, indented under it — the shape the
                # forum shows them in, and the shape a note's comments keep.
                nested: list = []
                for reply in comment.replies:
                    nested.extend(_comment_nodes(reply))
                nodes.append({"tag": "blockquote", "children": nested})

    return nodes


def source_nodes(thread: Thread) -> list:
    """The link home. Handed to `telegraph.trim` as the tail it must keep: a
    page that dropped half a thread for length needs the original more, not
    less."""
    return [
        {"tag": "hr"},
        {
            "tag": "p",
            "children": [
                {"tag": "a", "attrs": {"href": thread.link}, "children": ["Read it on 1point3acres"]}
            ],
        },
    ]


TRIMMED_NOTE = "This thread was too long for one page; the rest is on 1point3acres."
