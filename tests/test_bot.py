"""Unit tests — no network, no Telegram, no sidecar."""

from __future__ import annotations

import json
import os
import re
import sys
from html import unescape
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from app.cache import LRU  # noqa: E402
from app.handlers import looks_like_cookie  # noqa: E402
from app.media import (  # noqa: E402
    MediaSender,
    MediaTooLarge,
    build_caption,
    chunk,
    split_message,
    tg_len,
    tg_truncate,
)
from app.state import State  # noqa: E402
from app.telegram import CAPTION_LIMIT, TelegramError  # noqa: E402
from app.xhs import MediaItem, Note, cache_key, clean_text, find_link, parse_note  # noqa: E402


def visible(html: str) -> str:
    """What Telegram counts against the caption limit: the text after entity parsing."""
    return unescape(re.sub(r"<[^>]+>", "", html))


# ---- link handling ---------------------------------------------------

@pytest.mark.parametrize(
    "text,expected",
    [
        ("58 复制本条信息，打开【小红书】App查看精彩内容！ http://xhslink.com/a/AbC123，", "http://xhslink.com/a/AbC123"),
        ("https://www.xiaohongshu.com/explore/6501?xsec_token=AB_c", "https://www.xiaohongshu.com/explore/6501?xsec_token=AB_c"),
        ("xhslink.com/m/9xyz", "https://xhslink.com/m/9xyz"),
        ("look at this https://www.xiaohongshu.com/discovery/item/abc123?x=1 ok", "https://www.xiaohongshu.com/discovery/item/abc123?x=1"),
        ("no link here", None),
        ("https://example.com/explore/123", None),
        # The mainland app shares xhslink.cn, the international one xhslink.com.
        ("http://xhslink.cn/a/AbC123", "http://xhslink.cn/a/AbC123"),
        ("看看这个 https://xhslink.cn/m/2Xy9Q，很好吃", "https://xhslink.cn/m/2Xy9Q"),
        ("https://xiaohongshu.cn/explore/650a?x=1", "https://xiaohongshu.cn/explore/650a?x=1"),
    ],
)
def test_find_link(text, expected):
    assert find_link(text) == expected


@pytest.mark.asyncio
async def test_resolve_normalises_without_network():
    """A long note URL must never be followed: the login wall eats the note id."""
    from app.xhs import XhsDownloader

    downloader = XhsDownloader("http://sidecar", timeout=5)
    downloader._client = None  # any network use would blow up here
    assert (
        await downloader.resolve("https://xiaohongshu.cn/explore/650a?x=1")
        == "https://www.xiaohongshu.com/explore/650a?x=1"
    )
    assert (
        await downloader.resolve("https://www.xiaohongshu.com/explore/650a")
        == "https://www.xiaohongshu.com/explore/650a"
    )


@pytest.mark.asyncio
async def test_short_links_are_resolved_here_not_by_the_sidecar():
    """The sidecar's own resolver fails intermittently; ours is the fallback-safe one."""
    import httpx

    from app.xhs import XhsDownloader

    seen = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        return httpx.Response(
            200, request=request,
            html="<html></html>",
        )

    downloader = XhsDownloader("http://sidecar", timeout=5)
    downloader._client = httpx.AsyncClient(
        transport=httpx.MockTransport(handler), follow_redirects=True
    )
    out = await downloader.resolve("http://xhslink.com/o/9m8PCZf2ef0")
    await downloader._client.aclose()

    assert seen == ["http://xhslink.com/o/9m8PCZf2ef0"], seen
    assert out == "http://xhslink.com/o/9m8PCZf2ef0"


def test_unwrap_login_wall():
    from app.xhs import _unwrap_login_wall

    wall = (
        "https://www.xiaohongshu.com/website-login/error?redirectPath="
        "https%3A%2F%2Fwww.xiaohongshu.com%2Fdiscovery%2Fitem%2F650a%3Fxsec_token%3DAB"
    )
    assert _unwrap_login_wall(wall) == (
        "https://www.xiaohongshu.com/discovery/item/650a?xsec_token=AB"
    )
    plain = "https://www.xiaohongshu.com/explore/650a"
    assert _unwrap_login_wall(plain) == plain


def test_unwrap_the_security_wall():
    """XHS's bot check is a second wall shape, and it answers HTTP 200 — so
    nothing downstream notices it unless it is recognised by name."""
    from app.xhs import _unwrap_login_wall, is_wall

    wall = (
        "https://www.xiaohongshu.com/404/sec_kHwJsXEi?source=xhs_sec_server"
        "&originalUrl=http%3A%2F%2Fwww.xiaohongshu.com%2Fdiscovery%2Fitem%2F6a88"
    )
    assert _unwrap_login_wall(wall) == "http://www.xiaohongshu.com/discovery/item/6a88"
    assert is_wall(wall)
    assert is_wall("https://www.xiaohongshu.com/website-login/error?redirectPath=x")
    assert not is_wall("https://www.xiaohongshu.com/explore/6a88")


def test_cache_key_prefers_note_id():
    assert cache_key("https://www.xiaohongshu.com/explore/650a?xsec_token=X") == "650a"
    assert cache_key("https://www.xiaohongshu.com/discovery/item/650a?x=1") == "650a"
    assert cache_key("https://www.xiaohongshu.com/user/profile/u1/650a?x=1") == "650a"
    assert cache_key("https://xhslink.com/a/AbC?x=1") == "https://xhslink.com/a/AbC"


# ---- payload normalisation -------------------------------------------

IMAGE_PAYLOAD = {
    "作品ID": "650a",
    "作品类型": "图文",
    "作品标题": "Title",
    "作品描述": "Body text",
    "作品标签": "#one #two",
    "作者昵称": "Someone",
    "作品链接": "https://www.xiaohongshu.com/explore/650a",
    "发布时间": "2026-08-01_12:00:00",
    "下载地址": ["https://ci.xiaohongshu.com/a?imageView2/format/jpeg", "https://ci.xiaohongshu.com/b?imageView2/format/jpeg"],
    "动图地址": [None, "https://sns-video-bd.xhscdn.com/live2"],
}


def test_parse_image_note():
    note = parse_note(IMAGE_PAYLOAD)
    assert note.kind == "image"
    assert note.note_id == "650a"
    assert len(note.photos) == 2
    assert note.lives == [None, "https://sns-video-bd.xhscdn.com/live2"]


def test_parse_english_locale_type():
    note = parse_note({**IMAGE_PAYLOAD, "作品类型": "image"})
    assert note.kind == "image"
    video = parse_note(
        {**IMAGE_PAYLOAD, "作品类型": "video", "下载地址": ["https://sns-video-bd.xhscdn.com/v"], "动图地址": [None]}
    )
    assert video.kind == "video"
    assert video.video == "https://sns-video-bd.xhscdn.com/v"


def test_parse_unknown_type_falls_back_to_urls():
    note = parse_note(
        {**IMAGE_PAYLOAD, "作品类型": "未知", "下载地址": ["https://sns-video-bd.xhscdn.com/v"], "动图地址": [None]}
    )
    assert note.kind == "video"


def test_parse_space_joined_urls():
    note = parse_note({**IMAGE_PAYLOAD, "下载地址": "https://a/1 https://a/2", "动图地址": "NaN NaN"})
    assert note.photos == ["https://a/1", "https://a/2"]
    assert note.lives == [None, None]


def test_live_photo_modes():
    note = parse_note(IMAGE_PAYLOAD)
    assert [m.kind for m in note.media("still")] == ["photo", "photo"]
    assert [m.kind for m in note.media("video")] == ["photo", "video"]
    assert [m.kind for m in note.media("both")] == ["photo", "photo", "video"]


# ---- text tidying (from a real note) ---------------------------------

def test_topic_tag_markup_becomes_a_real_hashtag():
    assert clean_text("好吃 #湾区美食[话题]# #湾区探店[话题]#") == "好吃 #湾区美食 #湾区探店"


def test_tabs_and_blank_runs_collapse():
    assert clean_text("one\n\t\ntwo") == "one\n\ntwo"
    assert clean_text("a\n\n\n\n\nb") == "a\n\nb"


def test_tag_line_is_dropped_when_already_inline():
    note = parse_note({**IMAGE_PAYLOAD, "作品描述": "text #one[话题]# #two[话题]#", "作品标签": "one two"})
    caption, _ = build_caption(note)
    assert caption.count("#one") == 1
    assert "<i>" not in caption


def test_tag_line_survives_when_not_inline():
    note = parse_note({**IMAGE_PAYLOAD, "作品描述": "no tags here", "作品标签": "one two"})
    caption, _ = build_caption(note)
    assert "<i>one two</i>" in caption


# ---- captions --------------------------------------------------------

def test_tg_len_counts_utf16_units():
    assert tg_len("abc") == 3
    assert tg_len("😀") == 2


def test_caption_fits_and_keeps_footer():
    note = parse_note(IMAGE_PAYLOAD)
    caption, overflow = build_caption(note)
    assert overflow == ""
    assert "<b>Title</b>" in caption
    assert "Someone" in caption and "open on RedNote" in caption
    assert tg_len(visible(caption)) <= CAPTION_LIMIT


def test_caption_truncates_and_overflows():
    note = parse_note({**IMAGE_PAYLOAD, "作品描述": "word " * 600})
    caption, overflow = build_caption(note)
    assert overflow
    assert tg_len(visible(caption)) <= CAPTION_LIMIT
    assert "open on RedNote" in caption  # footer survives truncation
    assert "…" in caption


def test_caption_survives_absurd_title():
    note = parse_note({**IMAGE_PAYLOAD, "作品标题": "T" * 2000, "作品描述": "d" * 100})
    caption, overflow = build_caption(note)
    assert tg_len(visible(caption)) <= CAPTION_LIMIT
    assert "open on RedNote" in caption


def test_caption_escapes_html():
    note = parse_note({**IMAGE_PAYLOAD, "作品标题": "a <b> & c", "作品描述": "<script>"})
    caption, _ = build_caption(note)
    assert "&lt;script&gt;" in caption
    assert "a &lt;b&gt; &amp; c" in caption


def test_caption_drops_duplicated_title_prefix():
    note = parse_note({**IMAGE_PAYLOAD, "作品描述": "Title extra tail"})
    caption, _ = build_caption(note)
    assert caption.count("Title") == 1


def test_split_message_respects_limit():
    pieces = split_message("x " * 5000, limit=100)
    assert all(tg_len(p) <= 100 for p in pieces)
    assert "".join(p.replace(" ", "") for p in pieces) == "x" * 5000


def test_tg_truncate_prefers_boundary():
    head, tail = tg_truncate("hello world foobar", 12)
    assert head == "hello world"
    assert tail == "foobar"


def test_chunking_to_ten():
    items = [MediaItem("photo", f"u{i}") for i in range(18)]
    groups = list(chunk(items))
    assert [len(g) for g in groups] == [10, 8]


# ---- cache -----------------------------------------------------------

def test_lru_evicts_and_counts():
    cache: LRU[int] = LRU(maxsize=2)
    cache.put("a", 1)
    cache.put("b", 2)
    assert cache.get("a") == 1
    cache.put("c", 3)
    assert cache.get("b") is None
    assert len(cache) == 2
    assert cache.hits == 1 and cache.misses == 1


def test_lru_ttl(monkeypatch):
    import app.cache as cache_module

    now = [1000.0]
    monkeypatch.setattr(cache_module.time, "monotonic", lambda: now[0])
    cache: LRU[int] = LRU(maxsize=4, ttl=10)
    cache.put("a", 1)
    now[0] += 5
    assert cache.get("a") == 1
    now[0] += 20
    assert cache.get("a") is None


# ---- state -----------------------------------------------------------

def test_state_roundtrip_and_permissions(tmp_path):
    path = tmp_path / "state.json"
    state = State(path)
    assert state.owner_id is None
    state.claim_owner(42)
    state.set_cookie("a1=x; web_session=y")
    assert state.allow(7) is True
    assert state.allow(7) is False
    assert state.deny(42) is False  # owner is not removable
    assert state.deny(7) is True

    assert oct(os.stat(path).st_mode)[-3:] == "600"
    reloaded = State(path)
    assert reloaded.owner_id == 42
    assert reloaded.cookie == "a1=x; web_session=y"
    assert reloaded.cookie_status == "ok"
    assert json.loads(path.read_text())["pairing_code_used"] is True


def test_cookie_staleness_notifies_once(tmp_path):
    state = State(tmp_path / "s.json")
    state.set_cookie("web_session=abc")
    assert state.mark_cookie_stale() is True
    assert state.mark_cookie_stale() is False
    state.mark_fetch_success()
    assert state.cookie_status == "ok"


@pytest.mark.parametrize(
    "text,expected",
    [
        ("a1=1234567890abcdef; web_session=040069b2abcdef0123456789; gid=xyz", True),
        ("/status", False),
        ("https://xhslink.com/a/abc", False),
        ("a1=short", False),
    ],
)
def test_looks_like_cookie(text, expected):
    assert looks_like_cookie(text) is expected


# ---- the §2.2 fallback ----------------------------------------------

class FakeTelegram:
    def __init__(self, fail_urls: bool, description: str = "Bad Request: failed to get HTTP URL content"):
        self.fail_urls = fail_urls
        self.description = description
        self.calls = []

    async def call(self, method, payload=None, files=None, timeout=None, retries=3):
        self.calls.append((method, payload, sorted((files or {}).keys())))
        sent = [m.get("media") for m in payload.get("media", [])] or [
            payload.get("photo"), payload.get("video")
        ]
        if self.fail_urls and any(str(v).startswith("http") for v in sent):
            raise TelegramError(method, 400, self.description)
        count = len(payload.get("media", [])) if method == "sendMediaGroup" else 1
        self.next_id = getattr(self, "next_id", 0)
        result = []
        for i in range(count):
            self.next_id += 1
            result.append({"message_id": self.next_id, "photo": [{"file_id": f"fid{i}"}]})
        return result


@pytest.mark.asyncio
async def test_sender_uses_urls_when_telegram_accepts_them():
    telegram = FakeTelegram(fail_urls=False)
    sender = MediaSender(telegram, mode="auto")
    items = [MediaItem("photo", f"https://cdn/{i}") for i in range(3)]
    report = await sender.send(1, items, "cap")
    await sender.aclose()

    assert report.sent == 3
    assert report.uploaded is False
    assert telegram.calls[0][0] == "sendMediaGroup"
    assert telegram.calls[0][1]["media"][0]["media"] == "https://cdn/0"
    assert telegram.calls[0][1]["media"][0]["caption"] == "cap"
    assert sender.streaming is False


@pytest.mark.asyncio
async def test_sender_falls_back_to_streaming(monkeypatch):
    telegram = FakeTelegram(fail_urls=True)
    sender = MediaSender(telegram, mode="auto")

    async def fake_download(url):
        return b"bytes", "image/jpeg", url

    monkeypatch.setattr(sender, "_download", fake_download)
    items = [MediaItem("photo", f"https://cdn/{i}") for i in range(2)]
    report = await sender.send(1, items, "cap")

    assert report.sent == 2
    assert report.uploaded is True
    assert sender.streaming is True  # that CDN family is now known-bad
    assert telegram.calls[0][2] == []          # first attempt: URLs
    assert telegram.calls[1][2] == ["file0", "file1"]  # retry: multipart
    assert telegram.calls[1][1]["media"][0]["media"] == "attach://file0"

    # file_ids are remembered, so a re-send costs no fetch at all
    telegram.calls.clear()
    await sender.send(1, items, "cap")
    assert telegram.calls[0][2] == []
    assert telegram.calls[0][1]["media"][0]["media"] == "fid0"
    await sender.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "description",
    [
        # Observed live against XHS media groups.
        'Bad Request: failed to send message #6 with the error message "WEBPAGE_MEDIA_EMPTY"',
        'Bad Request: failed to send message #3 with the error message "WEBPAGE_CURL_FAILED"',
        # Telegram has many spellings for this; auto mode must not depend on the list.
        "Bad Request: something nobody has seen before",
    ],
)
async def test_auto_mode_falls_back_on_any_400(description, monkeypatch):
    telegram = FakeTelegram(fail_urls=True, description=description)
    sender = MediaSender(telegram, mode="auto")

    async def fake_download(url):
        return b"bytes", "image/jpeg", url

    monkeypatch.setattr(sender, "_download", fake_download)
    report = await sender.send(1, [MediaItem("photo", f"https://cdn/{i}") for i in range(2)], "cap")
    await sender.aclose()
    assert report.sent == 2
    assert sender.streaming is True


@pytest.mark.asyncio
async def test_pinned_url_mode_does_not_fall_back():
    telegram = FakeTelegram(fail_urls=True)
    sender = MediaSender(telegram, mode="url")
    with pytest.raises(TelegramError):
        await sender.send(1, [MediaItem("photo", f"https://cdn/{i}") for i in range(2)], "cap")
    await sender.aclose()
    assert sender.streaming is False


@pytest.mark.asyncio
async def test_single_item_uses_send_photo():
    telegram = FakeTelegram(fail_urls=False)
    sender = MediaSender(telegram, mode="url")
    report = await sender.send(1, [MediaItem("photo", "https://cdn/only")], "cap")
    await sender.aclose()
    assert report.sent == 1
    assert telegram.calls[0][0] == "sendPhoto"
    assert telegram.calls[0][1]["photo"] == "https://cdn/only"


@pytest.mark.asyncio
async def test_caption_lands_once_across_groups():
    telegram = FakeTelegram(fail_urls=False)
    sender = MediaSender(telegram, mode="url")
    items = [MediaItem("photo", f"https://cdn/{i}") for i in range(18)]
    await sender.send(1, items, "cap")
    await sender.aclose()

    first, second = telegram.calls[0][1]["media"], telegram.calls[1][1]["media"]
    assert first[0]["caption"] == "[1/2] cap"
    assert all("caption" not in m for m in first[1:])
    # The second album still says which part it is, but not the whole caption.
    assert second[0]["caption"] == "[2/2]"
    assert all("caption" not in m for m in second[1:])


@pytest.mark.asyncio
async def test_split_album_parts_reply_to_the_previous_part():
    telegram = FakeTelegram(fail_urls=False)
    sender = MediaSender(telegram, mode="url")
    items = [MediaItem("photo", f"https://cdn/{i}") for i in range(25)]
    await sender.send(1, items, "cap", reply_to=500)
    await sender.aclose()

    assert len(telegram.calls) == 3
    replies = [call[1]["reply_to_message_id"] for call in telegram.calls]
    # Part 1 answers the user; part 2 hangs off part 1's first message, and so on.
    assert replies == [500, 1, 11]
    captions = [call[1]["media"][0].get("caption") for call in telegram.calls]
    assert captions == ["[1/3] cap", "[2/3]", "[3/3]"]


@pytest.mark.asyncio
async def test_single_group_album_is_not_marked():
    telegram = FakeTelegram(fail_urls=False)
    sender = MediaSender(telegram, mode="url")
    await sender.send(1, [MediaItem("photo", f"https://cdn/{i}") for i in range(4)], "cap")
    await sender.aclose()
    assert telegram.calls[0][1]["media"][0]["caption"] == "cap"


@pytest.mark.asyncio
async def test_oversized_item_is_skipped_not_fatal(monkeypatch):
    from app.media import MediaTooLarge

    telegram = FakeTelegram(fail_urls=False)
    sender = MediaSender(telegram, mode="upload")

    async def fake_download(url):
        if url.endswith("1"):
            raise MediaTooLarge("80 MB")
        return b"bytes", "image/jpeg", url

    monkeypatch.setattr(sender, "_download", fake_download)
    items = [MediaItem("photo", f"https://cdn/{i}") for i in range(3)]
    report = await sender.send(1, items, "cap")
    await sender.aclose()

    assert report.sent == 2
    assert report.skipped == ["item 2 too large (80 MB)"]


@pytest.mark.asyncio
async def test_refusal_is_scoped_to_the_cdn_family(monkeypatch):
    """One Ultra-HDR note must not push every later note onto the slow path.

    Telegram fetches ci.xiaohongshu.com/notes_pre_post/… fine but refuses
    /note_pre_post_uhdr/…, so the lesson has to be remembered per family.
    """
    from app.media import media_family

    class PickyTelegram(FakeTelegram):
        async def call(self, method, payload=None, files=None, timeout=None, retries=3):
            self.calls.append((method, payload, sorted((files or {}).keys())))
            sent = [m.get("media") for m in payload.get("media", [])] or [
                payload.get("photo"), payload.get("video")
            ]
            if any("uhdr" in str(v) and str(v).startswith("http") for v in sent):
                raise TelegramError(method, 400, "Bad Request: failed to get HTTP URL content")
            count = len(payload.get("media", [])) if method == "sendMediaGroup" else 1
            return [{"photo": [{"file_id": f"fid{i}"}]} for i in range(count)]

    telegram = PickyTelegram(fail_urls=False)
    sender = MediaSender(telegram, mode="auto")

    async def fake_download(url):
        return b"bytes", "image/jpeg", url

    monkeypatch.setattr(sender, "_download", fake_download)

    hdr = [MediaItem("photo", f"https://ci.xiaohongshu.com/note_pre_post_uhdr/{i}") for i in range(2)]
    report = await sender.send(1, hdr, "cap")
    assert report.uploaded is True

    telegram.calls.clear()
    plain = [MediaItem("photo", f"https://ci.xiaohongshu.com/notes_pre_post/{i}") for i in range(2)]
    report = await sender.send(1, plain, "cap")
    await sender.aclose()

    # The ordinary family stays on the fast path: one call, no multipart, no retry.
    assert report.uploaded is False
    assert len(telegram.calls) == 1
    assert telegram.calls[0][2] == []
    assert telegram.calls[0][1]["media"][0]["media"] == plain[0].url

    assert media_family(hdr[0].url) == "ci.xiaohongshu.com/note_pre_post_uhdr"
    assert media_family(plain[0].url) == "ci.xiaohongshu.com/notes_pre_post"


def test_root_level_tokens_bucket_by_host():
    """Seen live: ci.xiaohongshu.com/<token> with no directory at all.

    Bucketing on the token would make every image its own family, so a refusal
    would teach the sender nothing about the next one.
    """
    from app.media import media_family

    a = "https://ci.xiaohongshu.com/1040g2sg323cdgfcl7a004au8350eslf5anl1nbo?imageView2/format/jpeg"
    b = "https://ci.xiaohongshu.com/1040g2sg323dmrqu20a004au8350eslf5u8brkm0?imageView2/format/jpeg"
    assert media_family(a) == media_family(b) == "ci.xiaohongshu.com"
    # …and it still doesn't swallow the directory-based families.
    assert media_family("https://ci.xiaohongshu.com/notes_uhdr/x") == "ci.xiaohongshu.com/notes_uhdr"
    assert media_family("https://sns-video-bd.xhscdn.com/stream/79/110/a.mp4") == (
        "sns-video-bd.xhscdn.com/stream"
    )


def test_timestamped_buckets_bucket_by_host():
    """The page fallback serves .../<minute>/<hash>/notes_pre_post/… URLs.

    That leading segment changes every minute, so treating it as a family would
    mean re-learning the same lesson forever.
    """
    from app.media import media_family

    a = "http://sns-webpic-qc.xhscdn.com/202608210833/abc/notes_pre_post/1040g3k"
    b = "http://sns-webpic-qc.xhscdn.com/202608210901/def/notes_pre_post/1040g3k"
    assert media_family(a) == media_family(b) == "sns-webpic-qc.xhscdn.com"


@pytest.mark.asyncio
async def test_a_400_that_upload_cannot_fix_surfaces(monkeypatch):
    """Auto mode retries once per family, then lets the error through."""

    class AlwaysRefuses:
        def __init__(self):
            self.calls = []

        async def call(self, method, payload=None, files=None, timeout=None, retries=3):
            self.calls.append(sorted((files or {}).keys()))
            raise TelegramError(method, 400, "Bad Request: chat not found")

    telegram = AlwaysRefuses()
    sender = MediaSender(telegram, mode="auto")

    async def fake_download(url):
        return b"bytes", "image/jpeg", url

    monkeypatch.setattr(sender, "_download", fake_download)
    with pytest.raises(TelegramError):
        await sender.send(1, [MediaItem("photo", "https://cdn/x/1")], "cap")
    await sender.aclose()

    # Exactly two attempts: URL, then upload. No third.
    assert telegram.calls == [[], ["file0"]]


# ---- comments --------------------------------------------------------

def _state_page(comments: list[dict]) -> str:
    payload = {"noteData": {"data": {"commentData": {"comments": comments}}}}
    return "<html><script>window.__INITIAL_STATE__=" + json.dumps(payload) + "</script></html>"


def _raw(content, nickname="someone", likes="10", location="美国", subs=()):
    return {
        "content": content,
        "user": {"nickname": nickname},
        "likeViewCount": likes,
        "ipLocation": location,
        "subComments": list(subs),
    }


def test_parse_comments_reads_the_embedded_state():
    from app.comments import parse_comments

    page = _state_page([
        _raw("好吃[飞吻R]", nickname="虎牙", subs=[_raw("谢谢[害羞R]", nickname="作者", likes="3")]),
        _raw("在哪家店？", nickname="路人"),
    ])
    comments = parse_comments(page)

    assert [c.author for c in comments] == ["虎牙", "路人"]
    # XHS sticker codes are app-only markup and come out.
    assert comments[0].text == "好吃"
    assert comments[0].replies[0].text == "谢谢"
    assert comments[0].replies[0].author == "作者"
    assert comments[0].likes == "10"
    assert comments[0].location == "美国"


def test_parse_comments_survives_a_page_without_any():
    from app.comments import parse_comments

    assert parse_comments("<html>nothing here</html>") == []
    assert parse_comments("<script>window.__INITIAL_STATE__={oops</script>") == []
    assert parse_comments(_state_page([])) == []


def test_comment_limit_is_respected():
    from app.comments import parse_comments

    page = _state_page([_raw(f"c{i}") for i in range(9)])
    assert len(parse_comments(page, limit=5)) == 5


def test_render_drops_whole_comments_rather_than_cutting_them():
    from app.comments import parse_comments, render_comments

    page = _state_page([_raw("x" * 300, nickname=f"u{i}") for i in range(5)])
    comments = parse_comments(page)
    block = render_comments(comments, limit=700)

    assert tg_len(visible(block)) <= 700
    # Some comments made it, the rest were dropped intact — no "xxx…" mid-comment.
    assert block.count("💬") < 5
    assert "…" not in block


def test_render_truncates_a_single_giant_comment():
    from app.comments import parse_comments, render_comments

    comments = parse_comments(_state_page([_raw("y" * 5000)]))
    block = render_comments(comments, limit=400)

    assert 0 < tg_len(visible(block)) <= 400
    assert block.endswith("…")


def test_render_gives_up_gracefully_when_there_is_no_room():
    from app.comments import parse_comments, render_comments

    comments = parse_comments(_state_page([_raw("hello")]))
    assert render_comments(comments, limit=5) == ""
    assert render_comments([], limit=4000) == ""


def test_rendered_comments_escape_html():
    from app.comments import parse_comments, render_comments

    comments = parse_comments(_state_page([_raw("<b>not bold</b> & co", nickname="<script>")]))
    block = render_comments(comments, limit=4000)

    assert "&lt;b&gt;not bold&lt;/b&gt;" in block
    assert "&lt;script&gt;" in block
    assert "<script>" not in block


# ---- split routing ---------------------------------------------------

def test_only_xhs_traffic_takes_the_proxy():
    """The residential IP is for XHS. Telegram and the sidecar go direct."""
    from app.media import MediaSender
    from app.telegram import Telegram
    from app.xhs import XhsDownloader

    def proxy_of(client) -> str | None:
        """The proxy httpx will actually dial, or None for a direct client.

        httpx hangs a proxied transport off `_mounts` rather than replacing the
        default one, so both places have to be looked at.
        """
        for transport in [*client._mounts.values(), client._transport]:
            url = getattr(getattr(transport, "_pool", None), "_proxy_url", None)
            if url:
                return f"{url.scheme.decode()}://{url.host.decode()}:{url.port}"
        return None

    proxy = "http://tailscale:1055"
    downloader = XhsDownloader("http://xhs-downloader:5556", proxy=proxy)
    sender = MediaSender(FakeTelegram(fail_urls=False), proxy=proxy)
    telegram = Telegram("123:abc")

    # The note page (short links, comments) and the CDN ride the proxy…
    assert proxy_of(downloader._client) == "http://tailscale:1055"
    assert proxy_of(sender._client) == "http://tailscale:1055"
    # …the hop to the sidecar next door does not…
    assert proxy_of(downloader._api) is None
    # …and neither does Telegram, which has no reason to leave by that door.
    assert proxy_of(telegram._client) is None


def test_no_proxy_configured_changes_nothing():
    from app.xhs import XhsDownloader

    downloader = XhsDownloader("http://xhs-downloader:5556")
    assert downloader._proxy is None
    assert downloader._client._mounts == {}


@pytest.mark.asyncio
async def test_the_sidecar_is_told_to_use_the_proxy_too():
    """The sidecar fetches XHS itself, so the setting has to reach it."""
    import httpx

    from app.xhs import XhsDownloader

    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.update(json.loads(request.content))
        return httpx.Response(200, json={"message": "ok", "data": None}, request=request)

    downloader = XhsDownloader("http://sidecar", proxy="http://tailscale:1055")
    downloader._api = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    # The page fallback must not reach for the network from a unit test.
    downloader._client = httpx.AsyncClient(
        transport=httpx.MockTransport(lambda r: httpx.Response(404, request=r))
    )
    with pytest.raises(Exception):
        await downloader.detail("https://www.xiaohongshu.com/explore/650a")
    await downloader._api.aclose()
    await downloader._client.aclose()

    assert seen["proxy"] == "http://tailscale:1055"
    assert seen["url"] == "https://www.xiaohongshu.com/explore/650a"


# ---- reading a note off its page (the sidecar's backstop) -------------

def _note_page(note: dict) -> str:
    state = {"noteData": {"data": {"noteData": note}}}
    return "<html><script>window.__INITIAL_STATE__=" + json.dumps(state) + "</script></html>"


IMAGE_PAGE = {
    "noteId": "650a",
    "type": "normal",
    "title": "Title",
    "desc": "Body #湾区美食[话题]#",
    "time": 1787210508000,
    "user": {"nickName": "Someone", "userId": "u1"},
    "tagList": [{"name": "one"}, {"name": "two"}],
    "imageList": [
        {"url": "http://cdn/1.jpg", "livePhoto": False, "stream": {}},
        {
            "url": "http://cdn/2.jpg",
            "livePhoto": True,
            "stream": {"h264": [{"masterUrl": "http://cdn/2.mp4"}]},
        },
    ],
}

VIDEO_PAGE = {
    "noteId": "650b",
    "type": "video",
    "title": "V",
    "desc": "",
    "time": 1787210508000,
    "user": {"nickName": "Someone", "userId": "u1"},
    "tagList": [],
    "imageList": [{"url": "http://cdn/cover.jpg", "livePhoto": False}],
    "video": {"media": {"stream": {
        "h265": [{"masterUrl": "http://cdn/hevc.mp4"}],
        "h264": [{"masterUrl": "http://cdn/avc.mp4", "backupUrls": ["http://cdn/backup.mp4"]}],
    }}},
}


def test_page_gives_up_an_image_note():
    from app.page import parse_note_page

    note = parse_note_page(_note_page(IMAGE_PAGE), "https://www.xiaohongshu.com/explore/650a?x=1")

    assert note.note_id == "650a" and note.kind == "image"
    assert note.author == "Someone"
    assert note.author_url.endswith("/user/profile/u1")
    assert note.url == "https://www.xiaohongshu.com/explore/650a"  # query stripped
    assert note.tags == "#one #two"
    assert note.desc == "Body #湾区美食"          # same tidying as the sidecar path
    assert note.published == "2026-08-20 07:21:48"  # epoch ms → UTC, sidecar shape
    assert note.photos == ["http://cdn/1.jpg", "http://cdn/2.jpg"]
    assert note.lives == [None, "http://cdn/2.mp4"]
    assert len(note.media("both")) == 3           # two stills plus the live clip


def test_page_gives_up_a_video_note():
    from app.page import parse_note_page

    note = parse_note_page(_note_page(VIDEO_PAGE))

    assert note.kind == "video"
    # h264 is preferred over the h265 listed first: it plays everywhere.
    assert note.video == "http://cdn/avc.mp4"
    assert len(note.media("still")) == 1


def test_page_falls_back_to_a_backup_url():
    from app.page import parse_note_page

    page = json.loads(json.dumps(VIDEO_PAGE))
    page["video"]["media"]["stream"] = {"h264": [{"backupUrls": ["http://cdn/backup.mp4"]}]}
    assert parse_note_page(_note_page(page)).video == "http://cdn/backup.mp4"


@pytest.mark.parametrize(
    "html",
    [
        "<html>no state here</html>",
        "<script>window.__INITIAL_STATE__={broken</script>",
        '<script>window.__INITIAL_STATE__={"noteData":{"data":{}}}</script>',
        '<script>window.__INITIAL_STATE__={"noteData":{"data":{"noteData":{}}}}</script>',
    ],
)
def test_page_returns_nothing_when_it_is_not_a_note(html):
    from app.page import parse_note_page

    assert parse_note_page(html) is None


@pytest.mark.asyncio
async def test_detail_falls_back_to_the_page_when_the_api_refuses():
    """Observed live: the signed API refuses notes the page serves happily."""
    import httpx

    from app.xhs import XhsDownloader

    downloader = XhsDownloader("http://sidecar")
    downloader._api = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, request=r,
                                 json={"message": "获取小红书作品数据失败", "data": None})
    ))
    downloader._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, request=r, html=_note_page(IMAGE_PAGE))
    ))

    note = await downloader.detail("https://www.xiaohongshu.com/explore/650a")
    await downloader.aclose()

    assert note.note_id == "650a"
    assert len(note.photos) == 2


@pytest.mark.asyncio
async def test_the_original_error_stands_when_the_page_is_no_help():
    import httpx

    from app.xhs import XhsDownloader, XhsError

    downloader = XhsDownloader("http://sidecar")
    downloader._api = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, request=r,
                                 json={"message": "获取小红书作品数据失败", "data": None})
    ))
    downloader._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(404, request=r)
    ))

    with pytest.raises(XhsError) as caught:
        await downloader.detail("https://www.xiaohongshu.com/explore/650a")
    await downloader.aclose()
    assert caught.value.kind == "blocked"
    assert "获取小红书作品数据失败" in str(caught.value)


def test_telegram_names_the_failing_item():
    err = TelegramError(
        "sendMediaGroup", 400,
        'Bad Request: failed to send message #3 with the error message "WEBPAGE_CURL_FAILED"',
    )
    assert err.failed_index == 3
    assert TelegramError("sendPhoto", 400, "Bad Request: chat not found").failed_index is None


@pytest.mark.asyncio
async def test_a_mixed_album_only_blames_the_item_that_failed(monkeypatch):
    """Observed live: one Ultra-HDR image in an album blacklisted the good family.

    Telegram says which item it choked on, so only that one's CDN is marked and
    the rest of the album keeps the fast path.
    """
    from app.media import media_family

    class NamesTheItem:
        def __init__(self):
            self.calls = []

        async def call(self, method, payload=None, files=None, timeout=None, retries=3):
            entries = payload.get("media", [])
            self.calls.append([str(m.get("media", ""))[:40] for m in entries])
            for position, entry in enumerate(entries, start=1):
                if "uhdr" in str(entry.get("media")) and str(entry["media"]).startswith("http"):
                    raise TelegramError(
                        method, 400,
                        f'Bad Request: failed to send message #{position} with the error '
                        f'message "WEBPAGE_CURL_FAILED"',
                    )
            return [{"message_id": i, "photo": [{"file_id": f"fid{i}"}]} for i in range(len(entries))]

    telegram = NamesTheItem()
    sender = MediaSender(telegram, mode="auto")

    async def fake_download(url):
        return b"bytes", "image/jpeg", url

    monkeypatch.setattr(sender, "_download", fake_download)

    items = [
        MediaItem("photo", "https://ci.xiaohongshu.com/notes_pre_post/a"),
        MediaItem("photo", "https://ci.xiaohongshu.com/note_pre_post_uhdr/b"),
        MediaItem("photo", "https://ci.xiaohongshu.com/notes_pre_post/c"),
    ]
    report = await sender.send(1, items, "cap")
    await sender.aclose()

    assert report.sent == 3
    # Only the Ultra-HDR family was condemned.
    assert sender._refused == {"ci.xiaohongshu.com/note_pre_post_uhdr"}
    # The retry uploaded that one item and kept URLs for the other two.
    retry = telegram.calls[1]   # the fake records the first 40 chars of each
    assert retry[0].startswith("https://ci.xiaohongshu.com/notes_pre")
    assert retry[1].startswith("attach://")
    assert retry[2].startswith("https://ci.xiaohongshu.com/notes_pre")
    assert media_family(items[0].url) not in sender._refused


# ---- oversized video, smaller rendition ------------------------------

@pytest.mark.asyncio
async def test_an_oversized_video_falls_back_to_a_rendition_that_fits(monkeypatch):
    """Live case: XHS served 88 MB, Telegram's ceiling is 50 MB, and the same
    video existed at 37.9 MB."""
    sizes = {
        "https://cdn/original.mp4": 92_689_815,   # 88.4 MB — what the sidecar gave us
        "https://cdn/h264.mp4": 62_213_884,       # 59.3 MB — still too big
        "https://cdn/h265.mp4": 37_938_283,       # 36.2 MB — this one fits
    }
    fetched = []

    telegram = FakeTelegram(fail_urls=True)
    sender = MediaSender(telegram, mode="auto", max_bytes=50 * 1024 * 1024)

    async def fake_download(url):
        fetched.append(url)
        if sizes[url] > sender._max_bytes:
            raise MediaTooLarge(f"{sizes[url] // 1024 // 1024} MB")
        return b"bytes", "video/mp4", url

    monkeypatch.setattr(sender, "_download", fake_download)

    item = MediaItem(
        "video",
        "https://cdn/original.mp4",
        alternatives=(("https://cdn/h264.mp4", 62_213_884), ("https://cdn/h265.mp4", 37_938_283)),
    )
    report = await sender.send(1, [item], "cap")
    await sender.aclose()

    assert report.sent == 1 and report.skipped == []
    # Original first, then the largest rendition that fits — not the smallest
    # available, and the one known to be too big is never even attempted.
    assert fetched == ["https://cdn/original.mp4", "https://cdn/h265.mp4"]


@pytest.mark.asyncio
async def test_a_video_with_no_rendition_that_fits_is_skipped(monkeypatch):
    """Per the brief: if nothing fits, skip it — don't post a degraded stand-in."""
    telegram = FakeTelegram(fail_urls=True)
    sender = MediaSender(telegram, mode="auto", max_bytes=50 * 1024 * 1024)

    async def fake_download(url):
        raise MediaTooLarge("88 MB")

    monkeypatch.setattr(sender, "_download", fake_download)

    item = MediaItem("video", "https://cdn/a.mp4", alternatives=(("https://cdn/b.mp4", 80_000_000),))
    report = await sender.send(1, [item], "cap")
    await sender.aclose()

    assert report.sent == 0
    assert "too large" in report.skipped[0]


def test_variants_are_read_off_the_page_with_their_sizes():
    from app.page import video_variants

    video = {"media": {"stream": {
        "h265": [{"masterUrl": "https://cdn/h265.mp4", "size": 37938283}],
        "h264": [{"masterUrl": "https://cdn/h264.mp4", "size": 62213884}],
        "av1": [{"backupUrls": ["https://cdn/av1.mp4"], "size": "12345"}],
        "h266": [{"size": 99}],  # no url at all
    }}}

    assert video_variants(video) == [
        ("https://cdn/h264.mp4", 62213884),   # h264 leads: it plays everywhere
        ("https://cdn/h265.mp4", 37938283),
        ("https://cdn/av1.mp4", 12345),       # string sizes are coerced
    ]


def test_a_video_note_offers_its_other_renditions():
    note = Note(
        note_id="650b",
        kind="video",
        video="https://cdn/h264.mp4",
        video_variants=[("https://cdn/h264.mp4", 62213884), ("https://cdn/h265.mp4", 37938283)],
    )
    item = note.media("still")[0]
    assert item.url == "https://cdn/h264.mp4"
    assert item.alternatives == (("https://cdn/h265.mp4", 37938283),)  # itself excluded


@pytest.mark.asyncio
async def test_a_profile_link_is_named_as_such_without_asking_the_sidecar():
    """Seen live: xhslink.cn/m/… resolves to /user/profile/<id> with no note."""
    import httpx

    from app.xhs import XhsDownloader, XhsError

    downloader = XhsDownloader("http://sidecar")

    def refuse(request):  # the sidecar must not be troubled with this
        raise AssertionError(f"the sidecar was called with {request.url}")

    downloader._api = httpx.AsyncClient(transport=httpx.MockTransport(refuse))
    downloader._client = httpx.AsyncClient(transport=httpx.MockTransport(refuse))

    with pytest.raises(XhsError) as caught:
        await downloader.detail("https://www.xiaohongshu.com/user/profile/5a049e6711be1068daa1c076")
    await downloader.aclose()
    assert caught.value.kind == "profile"


@pytest.mark.asyncio
async def test_a_note_under_a_profile_is_still_a_note():
    """/user/profile/<uid>/<note_id> is a real note URL and must not be caught."""
    import httpx

    from app.xhs import XhsDownloader, XhsError

    downloader = XhsDownloader("http://sidecar")
    downloader._api = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, request=r, json={"message": "ok", "data": None})
    ))
    downloader._client = httpx.AsyncClient(transport=httpx.MockTransport(
        lambda r: httpx.Response(404, request=r)
    ))

    with pytest.raises(XhsError) as caught:
        await downloader.detail("https://www.xiaohongshu.com/user/profile/u1/650a?x=1")
    await downloader.aclose()
    assert caught.value.kind == "blocked"  # it reached the sidecar, as it should


# ---- logs a machine can read -----------------------------------------

def _capture(fmt: str, emit) -> list[str]:
    import io
    import logging as _logging

    from app.logs import ContextFilter, JsonFormatter

    stream = io.StringIO()
    handler = _logging.StreamHandler(stream)
    handler.addFilter(ContextFilter())
    handler.setFormatter(
        JsonFormatter() if fmt == "json"
        else _logging.Formatter("%(levelname)s %(name)s: %(message)s")
    )
    logger = _logging.getLogger("app.test_logs")
    logger.handlers[:] = [handler]
    logger.propagate = False
    logger.setLevel(_logging.INFO)
    try:
        emit(logger)
    finally:
        logger.handlers[:] = []
    return [line for line in stream.getvalue().splitlines() if line]


def test_json_lines_carry_the_event_and_its_fields():
    from app.logs import fields

    line = _capture("json", lambda log: log.info(
        "delivered %d item(s)", 3,
        extra=fields(event="delivery", note="650a", items=3, seconds=9.6, mode="url"),
    ))[0]
    record = json.loads(line)

    assert record["event"] == "delivery"
    assert record["items"] == 3 and record["seconds"] == 9.6
    assert record["mode"] == "url" and record["note"] == "650a"
    assert record["level"] == "info" and record["logger"] == "app.test_logs"
    assert record["msg"] == "delivered 3 item(s)"       # still readable by a human
    assert record["ts"].endswith("Z")


def test_one_submission_shares_a_correlation_id():
    from app.logs import current_rid, fields, new_rid

    def emit(log):
        current_rid.set("abc123")
        log.info("fetched", extra=fields(event="note"))
        log.info("delivered", extra=fields(event="delivery"))

    ids = {json.loads(line)["rid"] for line in _capture("json", emit)}
    assert ids == {"abc123"}
    assert new_rid() != new_rid()


def test_fields_cannot_collide_with_logging_internals():
    """`name`, `message`, `args` are LogRecord attributes; passing them as
    fields must not raise or corrupt the line."""
    from app.logs import fields

    line = _capture("json", lambda log: log.info(
        "hello", extra=fields(event="odd", name="not-the-logger", message="not-the-msg", args=[1]),
    ))[0]
    record = json.loads(line)

    assert record["logger"] == "app.test_logs"   # the real logger name survives
    assert record["msg"] == "hello"
    assert record["name"] == "not-the-logger"    # …and the field is still there
    assert record["args"] == [1]


def test_exceptions_land_in_a_queryable_field():
    def emit(log):
        try:
            raise ValueError("boom")
        except ValueError:
            log.exception("it broke")

    record = json.loads(_capture("json", emit)[0])
    assert "ValueError: boom" in record["error"]
    assert record["level"] == "error"


def test_text_format_stays_human():
    from app.logs import fields

    line = _capture("text", lambda log: log.info("delivered", extra=fields(event="delivery")))[0]
    assert line == "INFO app.test_logs: delivered"
