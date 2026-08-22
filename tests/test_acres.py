"""1point3acres: link shapes, the anti-copy jammers, and the DM-only flow."""

from __future__ import annotations

import copy
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from app.telegram import TelegramError  # noqa: E402
from app.acres import (  # noqa: E402
    Acres,
    attachment_images,
    parse_replies,
    reply_gallery,
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
    source_nodes,
    to_nodes,
    thread_id,
    to_text,
)

from app.comments import Comment  # noqa: E402
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


def test_the_standing_attachment_banner_is_not_read_as_a_gate():
    # Discuz prints `attach_nopermission attach_tips` at the top of every post
    # cell whether or not there is an attachment, and whether or not you are
    # logged in — treating it as a gate warns about nothing on every thread.
    _text, _images, _locked, needs_login = to_text(PAGE)
    assert not needs_login
    # A bare attach_nopermission wraps one attachment the reader cannot have.
    assert to_text('<div class="attach_nopermission">x</div>')[3]


def test_a_points_wall_is_marked_rather_than_spliced_over():
    text, _images, locked, _needs_login = to_text(PAGE)
    assert locked
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
    assert thread.locked
    # The standing "log in to view attachments" banner is not a gate.
    assert not thread.needs_login


def test_attachments_are_taken_over_thumbnails_and_chrome_is_dropped():
    thread = parse_thread(decode(page_bytes()), "https://www.1point3acres.com/bbs/thread-1-1-1.html")
    assert thread.images == [
        "https://www.1point3acres.com/bbs/data/attachment/forum/shot.png",
        "https://oss.1p3a.com/asset/2026/pic.png",
    ]


# Discuz puts a post's own uploads in a `pattl` block *after* the cell, and
# every post on the page — replies included — has one in the same shape.
ATTACHED = """<html><head><meta charset="gbk"><base href="https://www.1point3acres.com/bbs/" /></head>
<body>
<div id="post_100"><table class="plhin">
<td class="t_f" id="postmessage_100">the opening post</td>
<div class="pattl"><ignore_js_op><dl><dd>
<a href="javascript:;" onclick="imageRotate('aimg_1', 1)"><img src="static/image/common/rleft.gif" /></a>
<img id="aimg_1" aid="1" src="static/image/common/none.gif"
 zoomfile="https://oss.1p3a.com/forum/202608/14/mine.jpg"
 file="https://oss.1p3a.com/forum/202608/14/mine.jpg" />
</dd></dl></ignore_js_op></div>
</table></div>
<div id="post_200"><table class="plhin">
<td class="t_f" id="postmessage_200">a reply</td>
<div class="pattl"><ignore_js_op><img id="aimg_2" aid="2" src="static/image/common/none.gif"
 zoomfile="https://oss.1p3a.com/forum/202608/14/theirs.jpg" /></ignore_js_op></div>
</table></div></body></html>"""


def test_an_attachment_rendered_after_the_cell_is_still_the_posts_own():
    thread = parse_thread(ATTACHED, "https://www.1point3acres.com/home/thread/5")
    assert thread.body == "the opening post"
    assert thread.images == ["https://oss.1p3a.com/forum/202608/14/mine.jpg"]


def test_a_replys_attachment_is_never_posted_as_the_authors():
    thread = parse_thread(ATTACHED, "https://www.1point3acres.com/home/thread/5")
    assert not any("theirs" in url for url in thread.images)


def test_only_images_carrying_an_attachment_attribute_are_collected():
    # The same region holds rotate buttons and the forum's house ads; neither
    # has zoomfile or file.
    assert attachment_images('<img src="https://oss.1p3a.com/asset/2026/vip-banner.png" />') == []
    assert attachment_images('<img src="static/image/common/rleft.gif" />') == []


def test_a_page_that_is_not_a_thread_parses_to_nothing():
    assert parse_thread("<html><body>提示信息</body></html>", "u") is None


def test_a_quoted_table_does_not_truncate_the_post():
    html = (
        '<td class="t_f" id="postmessage_1">before'
        "<table><tr><td>quoted</td></tr></table>after</td>"
    )
    thread = parse_thread(html, "https://www.1point3acres.com/home/thread/9")
    assert "before" in thread.body and "after" in thread.body


# ---- replies ---------------------------------------------------------

def _quote(parent_pid, parent_author, text="quoted words"):
    """A Discuz quote block: it links back to the exact post it answers."""
    return (
        '<div class="quote"><blockquote><font size="2">'
        f'<a href="forum.php?mod=redirect&amp;goto=findpost&amp;pid={parent_pid}&amp;ptid=1">'
        f'<font color="#999999">{parent_author} 发表于 2026-08-20 09:54:54</font></a>'
        f"</font><br />{text}</blockquote></div>"
    )


def _post(pid, author, body, *, add=0, sub=0, up=999, down=1, starter=False, image="",
          anonymous=False):
    """One post block in the shape Discuz serves.

    `up`/`down` are the green/red bar, which is labelled 全局 — the author's
    lifetime reputation. `add`/`sub` are 好苗/杂草 on this post.

    An anonymous poster gets no itemprop="author" block and no profile link;
    the handle sits as bare text in the byline cell, which is the only place
    it appears.
    """
    byline = (
        f'<p class="authi"><img class="authicn vm" id="authicon{pid}" '
        f'src="static/image/common/online_member.gif" /> {author}&nbsp;'
        f'<em id="authorposton{pid}">4 小时前</em></p>'
        if anonymous
        else f'<div itemprop="author" itemscope>'
        f'<a itemprop="url" href="home.php?mod=space&amp;uid={pid}0"></a>'
        f'<span itemprop="name">{author}</span></div>'
        f'<p class="authi"><img class="authicn vm" id="authicon{pid}" src="x.gif" /> '
        f'<a href="space-uid-{pid}0.html" class="xi2">{author}</a>'
        f'<em id="authorposton{pid}">4 小时前</em></p>'
    )
    return f"""<div id="post_{pid}"><table class="plhin">
{byline}
{'<img class="authicn vm" src="static/image/common/ico_lz.png" />' if starter else ''}
<i id="rec_add_{pid}" style="display:none">{add}</i>
<i id="rec_sub_{pid}" style="display:none">{sub}</i>
<span>全局：</span>
<i id="upvote_{pid}" style="color:#16a34a">{up}</i><i id="downvote_{pid}" style="color:#ef4444">{down}</i>
<td class="t_f" id="postmessage_{pid}">{body}</td>
{f'<div class="pattl"><ignore_js_op><img id="aimg_{pid}" aid="{pid}" src="static/image/common/none.gif" zoomfile="{image}" file="{image}" /></ignore_js_op></div>' if image else ''}
</table></div>"""


THREAD_PAGE = (
    '<html><head><base href="https://www.1point3acres.com/bbs/" /></head><body>'
    + _post(1, "starter", "the opening post", add=120, sub=2, starter=True)
    # A loud author (huge global bar) with a post nobody liked.
    + _post(2, "loudmouth", "nobody agreed", add=0, sub=0, up=99999, down=8000)
    + _post(3, "quiet", "everybody agreed", add=40, sub=1, up=3, down=0,
            image="https://oss.1p3a.com/forum/2026/mine.jpg")
    + _post(4, "starter", '<div class="quote"><blockquote><font size="2">'
            '<a href="x"><font color="#999999">loudmouth 发表于 2026-08-20 09:54:54</font></a>'
            "</font><br />nobody agreed</blockquote></div>I disagree", add=9, sub=0, starter=True)
    + _post(5, "downvoted", "widely disliked", add=1, sub=30,
            image="https://oss.1p3a.com/forum/2026/theirs.jpg")
    + _post(6, "blank", "", add=500, sub=0)
    + "</body></html>"
)


def test_replies_rank_by_the_posts_own_score_not_the_authors_reputation():
    replies = parse_replies(THREAD_PAGE, limit=10, starter="starter")
    assert [c.author for c in replies] == ["quiet", "starter", "loudmouth", "downvoted"]
    # loudmouth has the biggest 全局 bar on the page and still ranks third.
    assert replies[0].likes == "39"


ANONYMOUS_PAGE = (
    '<html><head><base href="https://www.1point3acres.com/bbs/" /></head><body>'
    + '<span id="thread_subject">asking without my name on it</span>'
    + _post(1, "匿名用户-3G7AI", "the opening post", add=5, anonymous=True)
    + _post(2, "DoraEstel", "a named reply", add=53)
    + _post(3, "匿名用户-JZ4OW", "one anonymous reply", add=9, anonymous=True)
    + _post(4, "匿名用户-JDSMH", "a different anonymous reply", add=30, anonymous=True)
    + "</body></html>"
)


def test_anonymous_posters_keep_the_handle_the_forum_gave_them():
    # Two anonymous posters in one thread are two people; collapsing both to
    # "anon" reads as one person answering themselves.
    replies = parse_replies(ANONYMOUS_PAGE, limit=10)
    assert [c.author for c in replies] == [
        "DoraEstel", "匿名用户-JDSMH", "匿名用户-JZ4OW",
    ]


def test_an_anonymous_starter_does_not_borrow_a_repliers_byline():
    # An anonymous opening post carries no itemprop="author" and no profile
    # link, so an unbounded search finds the first *named* reply and credits
    # the thread to them — seen live on thread 1186472.
    thread = parse_thread(ANONYMOUS_PAGE, "https://www.1point3acres.com/home/thread/9")
    assert thread.author == "匿名用户-3G7AI"
    assert thread.author_url == ""


def test_the_opening_post_is_never_one_of_the_replies():
    assert "the opening post" not in " ".join(c.text for c in parse_replies(THREAD_PAGE))


def test_a_quote_is_stripped_but_the_name_it_answers_survives():
    reply = next(c for c in parse_replies(THREAD_PAGE) if c.text == "I disagree")
    assert reply.replying_to == "loudmouth"
    assert "nobody agreed" not in reply.text


def test_the_thread_starter_is_marked_when_they_answer_in_their_own_thread():
    replies = parse_replies(THREAD_PAGE, starter="starter")
    assert next(c for c in replies if c.text == "I disagree").location == "OP"
    assert next(c for c in replies if c.author == "quiet").location == ""


def test_a_net_negative_or_zero_score_shows_no_count():
    replies = parse_replies(THREAD_PAGE)
    assert next(c for c in replies if c.author == "downvoted").likes == ""
    assert next(c for c in replies if c.author == "loudmouth").likes == ""


def test_an_empty_reply_is_dropped_however_popular():
    assert not any(c.author == "blank" for c in parse_replies(THREAD_PAGE))


def test_the_reply_limit_is_honoured_and_zero_turns_them_off():
    assert len(parse_replies(THREAD_PAGE, limit=2)) == 2
    assert parse_replies(THREAD_PAGE, limit=0) == []


def test_replies_come_off_the_same_parse_as_the_post():
    thread = parse_thread(THREAD_PAGE, "https://www.1point3acres.com/home/thread/5", replies=3)
    assert thread.body == "the opening post"
    assert len(thread.comments) == 3


def test_a_replys_picture_belongs_to_that_reply_not_to_the_post():
    thread = parse_thread(THREAD_PAGE, "https://www.1point3acres.com/home/thread/5", replies=10)
    assert thread.images == []  # the opening post has none of its own
    quiet = next(c for c in thread.comments if c.author == "quiet")
    assert quiet.images == ["https://oss.1p3a.com/forum/2026/mine.jpg"]


def test_the_gallery_names_whose_reply_each_picture_came_from():
    one = [Comment(author="phase", text="t", images=["https://oss/a.jpg"])]
    urls, caption = reply_gallery(one)
    assert urls == ["https://oss/a.jpg"] and "phase" in caption and "reply" in caption

    two = one + [Comment(author="other", text="t", images=["https://oss/b.jpg"])]
    urls, caption = reply_gallery(two)
    assert urls == ["https://oss/a.jpg", "https://oss/b.jpg"]
    assert "phase" in caption and "other" in caption


def test_a_thread_with_no_reply_pictures_has_no_gallery():
    assert reply_gallery([Comment(author="a", text="t")]) == ([], "")


def test_the_gallery_stops_at_one_albums_worth():
    many = [Comment(author="a", text="t", images=[f"https://oss/{i}.jpg" for i in range(20)])]
    assert len(reply_gallery(many)[0]) == 10


def test_a_comment_carrying_a_picture_links_to_it():
    from app.comments import render_comments

    rendered = render_comments(
        [Comment(author="phase", text="look", images=["https://oss/a.jpg"])], limit=500
    )
    assert '<a href="https://oss/a.jpg">📷</a>' in rendered


# A conversation: 20 is answered by 21, which is answered in turn by 22.
NESTED_PAGE = (
    '<html><head><base href="https://www.1point3acres.com/bbs/" /></head><body>'
    + _post(1, "starter", "the opening post", add=99, starter=True)
    + _post(10, "popular", "everyone agreed", add=50)
    + _post(20, "asker", "how long does it take?", add=5)
    + _post(21, "answerer", _quote(20, "asker") + "about six weeks", add=3)
    + _post(22, "starter", _quote(21, "answerer") + "thank you", add=1, starter=True)
    + _post(30, "quoting_op", _quote(1, "starter") + "replying to the thread itself", add=2)
    + "</body></html>"
)


def test_a_conversation_nests_under_the_reply_it_answers():
    replies = parse_replies(NESTED_PAGE, limit=10, starter="starter")
    top = [c.author for c in replies]
    assert top == ["popular", "asker", "quoting_op"]  # 21 and 22 are not top-level
    conversation = next(c for c in replies if c.author == "asker")
    assert [r.author for r in conversation.replies] == ["answerer", "starter"]


def test_a_nested_reply_drops_the_arrow_to_what_it_already_sits_under():
    conversation = next(
        c for c in parse_replies(NESTED_PAGE, starter="starter") if c.author == "asker"
    )
    direct, deeper = conversation.replies
    assert direct.replying_to == ""          # it is directly under "asker"
    assert deeper.replying_to == "answerer"  # it answers a sibling, so say so


def test_answering_the_thread_starter_is_not_worth_an_arrow():
    # Most replies answer the opening post; saying so on every one is noise.
    quoting_op = next(
        c for c in parse_replies(NESTED_PAGE, starter="starter") if c.author == "quoting_op"
    )
    assert quoting_op.replying_to == ""


def test_the_limit_counts_conversations_not_posts():
    replies = parse_replies(NESTED_PAGE, limit=2, starter="starter")
    assert [c.author for c in replies] == ["popular", "asker"]
    # The conversation travels with the reply it belongs to.
    assert len(replies[1].replies) == 2


def test_the_link_handed_back_is_the_one_that_was_shared():
    """Every share shape addresses one tid and the fetch needs the Discuz
    permalink, but answering with a URL the reader never sent — in an older UI
    than the one they were on — is a needless surprise."""
    thread = Thread(tid="9", url="https://www.1point3acres.com/bbs/thread-9-1-1.html")
    assert thread.link.endswith("/bbs/thread-9-1-1.html")

    thread.share_url = "https://www.1point3acres.com/home/thread/9"
    assert thread.link == "https://www.1point3acres.com/home/thread/9"
    head, _overflow = render(thread, limit=1024)
    assert "/home/thread/9" in head and "/bbs/thread-9-1-1.html" not in head


def test_the_page_points_home_to_the_shared_link_too():
    thread = Thread(
        tid="9", url="https://www.1point3acres.com/bbs/thread-9-1-1.html",
        share_url="https://www.1point3acres.com/home/thread/9", body="b",
    )
    blob = str(to_nodes(thread) + source_nodes(thread))
    assert "/home/thread/9" in blob and "/bbs/thread-9-1-1.html" not in blob


def test_a_conversation_reaches_the_telegraph_page_nested():
    thread = Thread(tid="9", url="u", body="b")
    thread.comments = parse_replies(NESTED_PAGE, starter="starter")
    nodes = to_nodes(thread)
    quoted = [n for n in nodes if isinstance(n, dict) and n.get("tag") == "blockquote"]
    assert quoted, "nested replies should be indented under what they answer"
    assert "answerer" in str(quoted)


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


class FakeTelegraph:
    def __init__(self, url="https://telegra.ph/a-thread-08-21", error=None):
        self.url = url
        self.error = error
        self.pages: list[tuple[str, str, list, str, str]] = []
        self.accounts = 0

    async def create_account(self, short_name, author_name=""):
        self.accounts += 1
        return "tok"

    async def create_page(self, token, title, content, *, author_name="", author_url=""):
        if self.error:
            raise self.error
        self.pages.append((token, title, content, author_name, author_url))
        return self.url

    async def aclose(self):
        return None


def acres_bot(tmp_path, *, thread=THREAD, error=None, owner=1, telegraph=None):
    """`telegraph=None` exercises the chunked-message delivery; pass a
    FakeTelegraph to exercise the page."""
    bot, telegram, state = make_bot(tmp_path, owner=owner)
    # A copy: publishing records the page on the thread, and the module-level
    # THREAD would carry that into the next test.
    bot.acres = FakeAcres(copy.deepcopy(thread) if thread else thread, error)
    bot.acres_sender = FakeSender()
    bot.telegraph = telegraph
    return bot, telegram, state


@pytest.mark.asyncio
async def test_top_replies_ride_in_the_message_with_the_post(tmp_path):
    from app.comments import Comment

    thread = Thread(
        tid="9", url="u", title="T", body="short post",
        comments=[Comment(author="someone", text="a good reply", likes="12")],
    )
    bot, telegram, _state = acres_bot(tmp_path, thread=thread)
    await bot.handle_update(message(LINK))
    assert len(telegram.sent) == 1  # not split off into a message of its own
    assert "a good reply" in telegram.texts and "👍 12" in telegram.texts


@pytest.mark.asyncio
async def test_when_the_post_overflows_the_replies_follow_it(tmp_path):
    from app.comments import Comment

    thread = Thread(
        tid="9", url="u", title="T", body="x" * 6000,
        comments=[Comment(author="someone", text="a good reply", likes="12")],
    )
    bot, telegram, _state = acres_bot(tmp_path, thread=thread)
    await bot.handle_update(message(LINK))
    assert len(telegram.sent) > 1
    assert "a good reply" in telegram.sent[-1][1]


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
async def test_reply_pictures_travel_in_their_own_album_under_their_own_name(tmp_path):
    thread = Thread(
        tid="9", url="u", title="T", body="post text",
        comments=[Comment(author="phase", text="look", images=["https://oss/a.jpg"])],
    )
    bot, telegram, _state = acres_bot(tmp_path, thread=thread)
    await bot.handle_update(message(LINK))
    (_chat, items, caption, reply_to, _part) = bot.acres_sender.sends[0]
    assert [item.url for item in items] == ["https://oss/a.jpg"]
    assert "phase" in caption
    # Hung off the message it belongs under rather than sent adrift.
    assert reply_to == 901  # the id FakeTelegram gave the post


@pytest.mark.asyncio
async def test_a_failed_reply_gallery_does_not_fail_the_thread(tmp_path):
    thread = Thread(
        tid="9", url="u", title="T", body="post text",
        comments=[Comment(author="phase", text="look", images=["https://oss/a.jpg"])],
    )
    bot, telegram, _state = acres_bot(tmp_path, thread=thread)

    async def boom(*a, **k):
        raise TelegramError("sendMediaGroup", 400, "nope")

    bot.acres_sender.send = boom
    await bot.handle_update(message(LINK))
    assert "post text" in telegram.texts  # the thread still landed


@pytest.mark.asyncio
async def test_the_second_fetch_of_a_thread_is_served_from_cache(tmp_path):
    bot, _telegram, _state = acres_bot(tmp_path)
    await bot.handle_update(message(LINK))
    await bot.handle_update(message("https://www.1point3acres.com/bbs/thread-1186859-1-1.html"))
    assert len(bot.acres.calls) == 1


# ---- the Telegraph page ----------------------------------------------

@pytest.mark.asyncio
async def test_a_thread_becomes_one_page_and_one_link(tmp_path):
    paper = FakeTelegraph()
    bot, telegram, state = acres_bot(tmp_path, telegraph=paper)
    await bot.handle_update(message(LINK))
    assert len(telegram.sent) == 1
    assert "https://telegra.ph/a-thread-08-21" in telegram.texts
    # The page keeps the forum author's name, not the bot's.
    _token, title, _content, author, author_url = paper.pages[0]
    assert title == "A thread" and author == "someone"
    assert state.telegraph_token == "tok"


@pytest.mark.asyncio
async def test_the_account_is_made_once_and_reused(tmp_path):
    paper = FakeTelegraph()
    bot, _telegram, _state = acres_bot(tmp_path, telegraph=paper)
    await bot.handle_update(message(LINK))
    bot.acres.thread_result = Thread(tid="2", url="u", title="Another", body="b")
    await bot.handle_update(message("https://www.1point3acres.com/home/thread/2"))
    assert paper.accounts == 1 and len(paper.pages) == 2


@pytest.mark.asyncio
async def test_resending_a_link_hands_back_the_page_that_exists(tmp_path):
    paper = FakeTelegraph()
    bot, telegram, _state = acres_bot(tmp_path, telegraph=paper)
    await bot.handle_update(message(LINK))
    await bot.handle_update(message(LINK))
    assert len(paper.pages) == 1  # not a second page for the same thread
    assert telegram.texts.count("https://telegra.ph/a-thread-08-21") == 2


@pytest.mark.asyncio
async def test_a_telegraph_outage_costs_the_format_not_the_thread(tmp_path):
    from app.telegraph import TelegraphError

    paper = FakeTelegraph(error=TelegraphError("down"))
    bot, telegram, _state = acres_bot(tmp_path, telegraph=paper)
    await bot.handle_update(message(LINK))
    assert "the post body" in telegram.texts  # fell back to chunked messages


@pytest.mark.asyncio
async def test_the_page_carries_the_post_the_replies_and_the_way_home(tmp_path):
    from app.comments import Comment

    thread = Thread(
        tid="9", url="https://www.1point3acres.com/bbs/thread-9-1-1.html", title="T",
        body="first para\n\nsecond para", images=["https://oss/post.jpg"],
        comments=[Comment(author="phase", text="a reply", likes="3",
                          images=["https://oss/reply.jpg"])],
    )
    paper = FakeTelegraph()
    bot, _telegram, _state = acres_bot(tmp_path, thread=thread, telegraph=paper)
    await bot.handle_update(message(LINK))
    blob = str(paper.pages[0][2])
    for expected in ("first para", "second para", "phase", "a reply",
                     "https://oss/post.jpg", "https://oss/reply.jpg",
                     "Read it on 1point3acres"):
        assert expected in blob, expected


def group_message(text, *, chat=-100, user=1, message_id=5):
    return {
        "update_id": 1,
        "message": {
            "message_id": message_id,
            "from": {"id": user},
            "chat": {"id": chat, "type": "supergroup"},
            "text": text,
        },
    }


def with_channel(bot, state):
    state.allow_group(-100, "g", 1)
    bot.channel = {"id": -200, "username": "chan", "title": "C"}
    return bot


@pytest.mark.asyncio
async def test_a_thread_from_a_group_is_published_and_answered_with_a_permalink(tmp_path):
    paper = FakeTelegraph()
    bot, telegram, state = acres_bot(tmp_path, telegraph=paper)
    with_channel(bot, state)
    await bot.handle_update(group_message(LINK))

    posted = [(chat, text) for chat, text in telegram.sent if chat == -200]
    answered = [(chat, text) for chat, text in telegram.sent if chat == -100]
    assert len(posted) == 1 and "telegra.ph" in posted[0][1]
    assert len(answered) == 1 and "https://t.me/chan/" in answered[0][1]


@pytest.mark.asyncio
async def test_a_group_hears_nothing_when_the_fetch_fails(tmp_path):
    bot, telegram, state = acres_bot(tmp_path, error=AcresError("challenge", "cf"))
    with_channel(bot, state)
    await bot.handle_update(group_message(LINK))
    # Not a word in the group, and no DevTools lecture either.
    assert [chat for chat, _t in telegram.sent if chat == -100] == []


@pytest.mark.asyncio
async def test_a_resubmitted_thread_points_at_the_post_that_exists(tmp_path):
    paper = FakeTelegraph()
    bot, telegram, state = acres_bot(tmp_path, telegraph=paper)
    with_channel(bot, state)
    await bot.handle_update(group_message(LINK))
    await bot.handle_update(group_message(LINK, message_id=6))

    assert len(paper.pages) == 1  # not published twice
    assert "Already on the channel" in telegram.texts


def test_the_dedupe_index_says_which_site_an_id_came_from(tmp_path):
    """One index, two sites. A forum tid is a short number and a note id a long
    hex string, but sharing the index without namespacing invites exactly one
    very confusing bug."""
    from app.handlers import acres_key

    assert acres_key("1186859") == "1p3a:1186859"
    assert acres_key("") == ""


@pytest.mark.asyncio
async def test_a_thread_and_a_note_with_the_same_id_do_not_collide(tmp_path):
    paper = FakeTelegraph()
    bot, telegram, state = acres_bot(tmp_path, telegraph=paper)
    with_channel(bot, state)
    # A note already published under the bare id.
    state.record_published("1186859", -200, 42)
    await bot.handle_update(group_message(LINK))
    assert len(paper.pages) == 1  # the thread is still new
    assert "Already on the channel" not in telegram.texts


@pytest.mark.asyncio
async def test_a_private_user_gets_the_thread_in_the_dm_and_the_channel_gets_nothing(tmp_path):
    paper = FakeTelegraph()
    bot, telegram, state = acres_bot(tmp_path, telegraph=paper)
    with_channel(bot, state)
    await bot.handle_update(message("/mode private"))
    telegram.sent.clear()

    await bot.handle_update(message(LINK))

    assert [chat for chat, _t in telegram.sent] == [1]
    assert "telegra.ph" in telegram.texts
    assert state.published("1p3a:1186859") is None


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
