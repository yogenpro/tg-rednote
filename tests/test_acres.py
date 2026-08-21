"""1point3acres: link shapes, the anti-copy jammers, and the DM-only flow."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from app.acres import (  # noqa: E402
    Acres,
    AcresError,
    Thread,
    canonical,
    decode,
    find_acres_link,
    looks_like_cookie,
    parse_credentials,
    parse_thread,
    render,
    scrub,
    thread_id,
    to_text,
)

from test_handlers import FakeSender, FakeTelegram, make_bot, message  # noqa: E402


# A trimmed copy of what the forum actually serves: GBK, jammers between every
# line, a points-walled block, and an attachment the anonymous view hides.
PAGE = """<html><head><meta http-equiv="Content-Type" content="text/html; charset=gbk" />
<base href="https://www.1point3acres.com/bbs/" /></head><body>
<h1 class="ts"><span id="thread_subject">数据岗-终于上岸</span></h1>
<a href="tag-47-1.html" class="taglink"><span>数科面经</span></a>
<div itemprop="author" itemscope><strong><a itemprop="url" href="home.php?mod=space&amp;uid=172978"
><span itemprop="name">Jason_Yuan</span></a></strong></div>
<meta itemprop="datePublished" content="2023-6-16 15:34" />
<br/><span style="margin-top: 3px"><font color="#666">2023(4-6月)</font> <b>硬士</b> - Onsite</span>
<div class="t_fsz"><table><tr><td class="t_f" id="postmessage_18658818" itemprop="articleBody">
<div class="attach_nopermission attach_tips"><div><h3>注册一亩三分地论坛</h3></div></div>
First line.<font class="jammer">. From 1point 3acres bbs</font><br />
<font class="jammer"></font><br />
Second line.<font class="jammer">-baidu 1point3acres</font><br />
<span style="display:none">invisible</span>Third line.<font class="jammer">. Χ</font><br />
Hidden next:<div class="locked"><div class="locked-blur">您好！</div>
本帖隐藏的内容需要积分高于 188<a href="x">VIP</a></div>and back.
<img id="aimg_1" src="static/image/common/none.gif" zoomfile="data/attachment/forum/shot.png" file="data/attachment/forum/shot.png" />
<img src="https://oss.1p3a.com/asset/2026/pic.png" />
<img src="static/image/smiley/default/smile.gif" />
</td></tr></table></div></body></html>"""


def page_bytes() -> bytes:
    return PAGE.encode("gbk")


# ---- links -----------------------------------------------------------

@pytest.mark.parametrize(
    "text,tid",
    [
        ("https://www.1point3acres.com/home/thread/1186859", "1186859"),
        ("look: https://www.1point3acres.com/bbs/thread-1186859-1-1.html here", "1186859"),
        ("https://www.1point3acres.com/interview/thread/1186859?utm_source=x", "1186859"),
        ("www.1point3acres.com/bbs/forum.php?mod=viewthread&tid=1186859", "1186859"),
        ("1point3acres.com/home/thread/1186859", "1186859"),
    ],
)
def test_every_share_shape_resolves_to_the_same_thread(text, tid):
    link = find_acres_link(text)
    assert link and thread_id(link) == tid
    assert canonical(link) == f"https://www.1point3acres.com/bbs/thread-{tid}-1-1.html"


def test_links_without_a_thread_id_are_not_claimed():
    assert find_acres_link("https://www.1point3acres.com/home/thread/") is None
    assert find_acres_link("https://www.1point3acres.com/bbs/forum.php?mod=forumdisplay") is None
    assert find_acres_link("https://www.xiaohongshu.com/explore/650a") is None


def test_a_trailing_bracket_is_not_part_of_the_link():
    assert find_acres_link("(https://www.1point3acres.com/home/thread/12345)").endswith("12345")


# ---- the interference ------------------------------------------------

def test_jammers_never_reach_the_text():
    text, _images, _locked, _login = to_text(PAGE)
    for poison in ("1point 3acres", "baidu 1point3acres", "Χ", "invisible"):
        assert poison not in text
    assert "First line." in text and "Second line." in text and "Third line." in text


def test_a_points_wall_is_marked_rather_than_spliced_over():
    text, _images, locked, needs_login = to_text(PAGE)
    assert locked and needs_login
    assert "本帖隐藏" not in text  # the wall's own blurb is not the post
    # The wall is a <div>, so it breaks the line the way a browser would; what
    # matters is that the gap is visible rather than silently sewn shut.
    assert "Hidden next: […]" in text and "and back." in text


def test_invisible_codepoints_are_stripped_but_zwj_survives():
    assert scrub("a​b⁠c﻿d­e") == "abcde"
    # ZWJ holds compound emoji together; stripping it would break them.
    assert scrub("\U0001f469‍\U0001f4bb") == "\U0001f469‍\U0001f4bb"


def test_watermarks_that_leak_outside_a_jammer_are_still_scrubbed():
    assert scrub("real text. From 1point 3acres bbs more") == "real text more"
    assert scrub("x.留学论坛-一亩三分地 y") == "x y"
    # A genuine mention of the site is not punctuation-led, so it stays.
    assert "1point3acres" in scrub("I found it on 1point3acres yesterday")


def test_the_watermark_scrub_does_not_eat_ordinary_sentences():
    # ".google" and ".Waral" only match with the non-ASCII tail the jammer
    # appends; an English sentence that happens to hit those words survives.
    assert scrub("I applied. google was slow that day") == "I applied. google was slow that day"
    assert scrub("Ask Waral about it") == "Ask Waral about it"
    assert scrub("that. google \u0438") == "that"


def test_markup_newlines_do_not_become_blank_lines():
    text, _i, _l, _n = to_text("a<br />\nb<br />\nc")
    assert text == "a\nb\nc"


# ---- the page --------------------------------------------------------

def test_gbk_pages_decode_by_their_declared_charset():
    assert "数据岗" in decode(page_bytes())


def test_parse_pulls_the_opening_post_apart():
    thread = parse_thread(decode(page_bytes()), "https://www.1point3acres.com/bbs/thread-1-1-1.html")
    assert thread.title == "数据岗-终于上岸"
    assert thread.author == "Jason_Yuan"
    assert thread.author_url.endswith("space-uid-172978.html")
    assert thread.published == "2023-6-16 15:34"
    assert thread.forum == "数科面经"
    assert "2023(4-6月)" in thread.summary
    assert thread.locked and thread.needs_login


def test_attachments_are_taken_over_thumbnails_and_chrome_is_dropped():
    thread = parse_thread(decode(page_bytes()), "https://www.1point3acres.com/bbs/thread-1-1-1.html")
    assert thread.images == [
        "https://www.1point3acres.com/bbs/data/attachment/forum/shot.png",
        "https://oss.1p3a.com/asset/2026/pic.png",
    ]


def test_a_page_that_is_not_a_thread_parses_to_nothing():
    assert parse_thread("<html><body>提示信息</body></html>", "u") is None


def test_a_quoted_table_does_not_truncate_the_post():
    html = (
        '<td class="t_f" id="postmessage_1">before'
        "<table><tr><td>quoted</td></tr></table>after</td>"
    )
    thread = parse_thread(html, "https://www.1point3acres.com/home/thread/9")
    assert "before" in thread.body and "after" in thread.body


# ---- credentials -----------------------------------------------------

def test_a_curl_paste_yields_both_the_cookie_and_the_user_agent():
    paste = (
        "curl 'https://www.1point3acres.com/bbs/thread-1-1-1.html' "
        "-H 'accept: text/html' "
        "-H 'cookie: cf_clearance=abc; Vhcs_2132_auth=def; _gid=1' "
        "-H 'user-agent: Mozilla/5.0 (Macintosh) Chrome/130'"
    )
    cookie, agent = parse_credentials(paste)
    assert cookie == "cf_clearance=abc; Vhcs_2132_auth=def; _gid=1"
    assert agent == "Mozilla/5.0 (Macintosh) Chrome/130"
    assert looks_like_cookie(cookie)


def test_a_bare_cookie_is_taken_as_is_and_leaves_the_agent_open():
    cookie, agent = parse_credentials("cf_clearance=abc; Vhcs_2132_saltkey=zz; _gid=1")
    assert cookie.startswith("cf_clearance=") and agent == ""


def test_a_url_is_never_taken_for_a_cookie():
    # The cookie handler deletes the message it is handed, so a link that
    # happens to carry one of the markers must not reach it.
    assert not looks_like_cookie("https://oss.1p3a.com/asset/202207/22/79541hxivrbxkfzlvfg.png")
    assert not looks_like_cookie("https://example.com/callback?_auth=abcdefghijklmnopqrstuvw")


def test_an_xhs_cookie_is_not_mistaken_for_a_forum_one():
    assert not looks_like_cookie("a1=abc; webId=def; web_session=ghi; gid=jkl; xsecappid=xhs-pc")


# ---- rendering -------------------------------------------------------

def test_render_keeps_the_footer_and_hands_back_the_overflow():
    thread = Thread(
        tid="7", url="https://www.1point3acres.com/bbs/thread-7-1-1.html",
        title="T", author="A", published="2026-01-02 03:04", forum="F", body="x" * 3000,
    )
    head, overflow = render(thread, limit=1024)
    assert "open on 1point3acres" in head and "A · 2026-01-02 · F" in head
    assert overflow and len(overflow) > 1500


def test_a_gated_thread_says_so_in_the_post_itself():
    thread = Thread(tid="7", url="u", title="T", body="short", locked=True)
    head, overflow = render(thread, limit=1024)
    # In the message, not tacked on as one of its own.
    assert "points wall" in head and overflow == ""


def test_the_gated_warning_is_reserved_for_rather_than_stolen_from_the_body():
    body = "x" * 5000
    plain = render(Thread(tid="7", url="u", body=body), limit=1024)[0]
    gated = render(Thread(tid="7", url="u", body=body, locked=True), limit=1024)[0]
    from app.media import tg_len

    assert tg_len(strip(plain)) <= 1024 and tg_len(strip(gated)) <= 1024
    assert "points wall" in gated


def strip(html: str) -> str:
    import re
    from html import unescape

    return unescape(re.sub(r"<[^>]+>", "", html))


# ---- the flow --------------------------------------------------------

class FakeAcres:
    def __init__(self, thread=None, error=None):
        self.thread_result = thread
        self.error = error
        self.calls: list[tuple[str, str | None, str | None]] = []

    def headers(self, cookie, user_agent=None):
        return {"Cookie": cookie or "", "User-Agent": user_agent or "default"}

    async def thread(self, url, cookie=None, user_agent=None):
        self.calls.append((url, cookie, user_agent))
        if self.error:
            raise self.error
        return self.thread_result

    async def aclose(self):
        return None


THREAD = Thread(
    tid="1186859",
    url="https://www.1point3acres.com/bbs/thread-1186859-1-1.html",
    title="A thread",
    author="someone",
    published="2026-08-01 10:00",
    body="the post body",
)

LINK = "https://www.1point3acres.com/home/thread/1186859"


def acres_bot(tmp_path, *, thread=THREAD, error=None, owner=1):
    bot, telegram, state = make_bot(tmp_path, owner=owner)
    bot.acres = FakeAcres(thread, error)
    bot.acres_sender = FakeSender()
    return bot, telegram, state


@pytest.mark.asyncio
async def test_a_thread_link_in_a_dm_comes_back_as_text(tmp_path):
    bot, telegram, _state = acres_bot(tmp_path)
    await bot.handle_update(message(LINK))
    assert "A thread" in telegram.texts and "the post body" in telegram.texts
    assert bot.acres.calls[0][0] == LINK


@pytest.mark.asyncio
async def test_images_ride_in_an_album_with_the_text_as_its_caption(tmp_path):
    thread = Thread(tid="9", url="u", title="T", body="b", images=["https://oss/1", "https://oss/2"])
    bot, telegram, _state = acres_bot(tmp_path, thread=thread)
    await bot.handle_update(message(LINK))
    (chat, items, caption, _reply, _part) = bot.acres_sender.sends[0]
    assert chat == 1 and len(items) == 2 and "T" in caption


@pytest.mark.asyncio
async def test_the_second_fetch_of_a_thread_is_served_from_cache(tmp_path):
    bot, _telegram, _state = acres_bot(tmp_path)
    await bot.handle_update(message(LINK))
    await bot.handle_update(message("https://www.1point3acres.com/bbs/thread-1186859-1-1.html"))
    assert len(bot.acres.calls) == 1


@pytest.mark.asyncio
async def test_a_group_never_hears_about_a_thread_link(tmp_path):
    bot, telegram, state = acres_bot(tmp_path)
    state.allow_group(-100, "g", 1)
    bot.channel = {"id": -200, "username": "c", "title": "C"}
    await bot.handle_update(
        {
            "update_id": 1,
            "message": {
                "message_id": 5,
                "from": {"id": 1},
                "chat": {"id": -100, "type": "supergroup"},
                "text": LINK,
            },
        }
    )
    assert telegram.sent == []
    assert bot.acres.calls == []


@pytest.mark.asyncio
async def test_the_challenge_tells_the_owner_how_to_fix_it_and_marks_the_cookie_stale(tmp_path):
    bot, telegram, state = acres_bot(tmp_path, error=AcresError("challenge", "cf"))
    state.set_acres_cookie("cf_clearance=x; _auth=y", "UA/1")
    await bot.handle_update(message(LINK))
    assert "Copy as cURL" in telegram.texts
    assert state.acres_cookie_status == "stale"


@pytest.mark.asyncio
async def test_a_guest_is_pointed_at_the_owner_not_at_devtools(tmp_path):
    bot, telegram, state = acres_bot(tmp_path, error=AcresError("challenge", "cf"))
    state.allow(2)
    await bot.handle_update(message(LINK, user=2))
    assert "Ask the bot's owner" in telegram.texts
    assert "DevTools" not in telegram.texts


@pytest.mark.asyncio
async def test_setting_the_cookie_from_a_curl_paste_stores_the_agent_and_deletes_the_message(tmp_path):
    bot, telegram, state = acres_bot(tmp_path)
    paste = (
        "curl 'https://www.1point3acres.com/' "
        "-H 'cookie: cf_clearance=abcdefghijklmnop; Vhcs_2132_auth=qrstuvwxyz' "
        "-H 'user-agent: Chrome/130'"
    )
    await bot.handle_update(message(f"/acres {paste}"))
    assert state.acres_cookie == "cf_clearance=abcdefghijklmnop; Vhcs_2132_auth=qrstuvwxyz"
    assert state.acres_ua == "Chrome/130"
    assert telegram.deleted == [(1, 99)]
    assert "cf_clearance" not in telegram.texts  # never echo the value back


@pytest.mark.asyncio
async def test_a_bare_forum_cookie_is_recognised_without_the_command(tmp_path):
    bot, telegram, state = acres_bot(tmp_path)
    await bot.handle_update(message("cf_clearance=abcdefghijklmnop; Vhcs_2132_auth=qrstuvwxyz012345"))
    assert state.acres_cookie and state.cookie is None


@pytest.mark.asyncio
async def test_a_cookie_without_its_agent_warns_about_cloudflare(tmp_path):
    bot, telegram, state = acres_bot(tmp_path)
    await bot.handle_update(
        message("/acres cf_clearance=abcdefghijklmnop; Vhcs_2132_auth=qrstuvwxyz012345")
    )
    assert state.acres_ua is None
    assert "User-Agent" in telegram.texts


@pytest.mark.asyncio
async def test_only_the_owner_can_set_the_forum_cookie(tmp_path):
    bot, telegram, state = acres_bot(tmp_path)
    state.allow(2)
    await bot.handle_update(message("/acres cf_clearance=abcdefghijklmnopqrstuvwxyz012345", user=2))
    assert state.acres_cookie is None
    assert "Only the owner" in telegram.texts


@pytest.mark.asyncio
async def test_forgetting_the_cookie_clears_it_from_the_sender_too(tmp_path):
    bot, telegram, state = acres_bot(tmp_path)
    state.set_acres_cookie("cf_clearance=x", "UA/1")
    await bot.handle_update(message("/forgetacres"))
    assert state.acres_cookie is None and state.acres_ua is None
