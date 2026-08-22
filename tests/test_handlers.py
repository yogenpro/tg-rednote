"""Handler tests: bootstrap, allowlist, cookie custody, and the note flow."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))

from app.config import Config  # noqa: E402
from app.handlers import Bot, message_candidates  # noqa: E402
from app.media import SendReport  # noqa: E402
from app.state import State  # noqa: E402
from app.telegram import TelegramError  # noqa: E402
from app.xhs import Note, XhsError  # noqa: E402


class FakeTelegram:
    def __init__(self):
        self.sent: list[tuple[int, str]] = []
        self.deleted: list[tuple[int, int]] = []
        self.edits: list[tuple[int, int, str]] = []

    async def send_message(self, chat_id, text, *, reply_to=None, preview=False):
        self.sent.append((chat_id, text))
        return {"message_id": 900 + len(self.sent)}

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return True

    async def edit_message_caption(self, chat_id, message_id, caption):
        self.edits.append((chat_id, message_id, caption))
        return {"message_id": message_id}

    async def edit_message_text(self, chat_id, message_id, text):
        self.edits.append((chat_id, message_id, text))
        return {"message_id": message_id}

    async def send_chat_action(self, chat_id, action):
        return None

    @property
    def texts(self):
        return " || ".join(t for _, t in self.sent)


class FakeDownloader:
    def __init__(self, note=None, error=None, comments=()):
        self.cookie = None
        self.note = note
        self.error = error
        self.comment_list = comments
        self.calls: list[tuple[str, str | None]] = []

    async def detail(self, url, cookie):
        self.calls.append((url, cookie))
        if self.error:
            raise self.error
        return self.note

    async def enrich(self, note, limit=5):
        return list(self.comment_list)

    def set_cookie(self, cookie):
        self.cookie = cookie

    async def healthy(self):
        return True


class FakeSender:
    def __init__(self):
        self.sends = []
        self.streaming = False
        self.next_message_id = 1

    async def send(self, chat_id, items, caption, *, reply_to=None, part_from=1, part_total=None):
        self.sends.append((chat_id, list(items), caption, reply_to, part_from))
        return SendReport(
            sent=len(items),
            first_message_id=self.next_message_id,
            parts=[(self.next_message_id, caption)],
        )

    def set_headers(self, **headers):
        self.headers = dict(headers)

    async def aclose(self):
        return None


def make_bot(tmp_path, *, downloader=None, owner=None, **config_kwargs) -> tuple[Bot, FakeTelegram, State]:
    config = Config(bot_token="t", state_path=tmp_path / "state.json", **config_kwargs)
    state = State(config.state_path)
    if owner:
        state.claim_owner(owner)
    telegram = FakeTelegram()
    bot = Bot(config, state, telegram, downloader or FakeDownloader())
    bot.sender = FakeSender()
    bot.bootstrap()
    return bot, telegram, state


def message(text, *, user=1, chat=None, message_id=99):
    return {
        "update_id": 1,
        "message": {
            "message_id": message_id,
            "from": {"id": user, "username": "u"},
            "chat": {"id": chat if chat is not None else user, "type": "private"},
            "text": text,
        },
    }


NOTE = Note(
    note_id="650a",
    kind="image",
    title="Title",
    desc="Desc",
    author="Someone",
    url="https://www.xiaohongshu.com/explore/650a",
    photos=["https://cdn/1", "https://cdn/2"],
    lives=[None, None],
)


# ---- bootstrap (PLAN §7) --------------------------------------------

@pytest.mark.asyncio
async def test_pairing_code_is_required_to_claim_ownership(tmp_path):
    bot, telegram, state = make_bot(tmp_path)
    assert bot.pairing_code

    await bot.handle_update(message("/start", user=666))
    assert state.owner_id is None
    await bot.handle_update(message("/start WRONG1", user=666))
    assert state.owner_id is None
    assert "unclaimed" in telegram.texts

    await bot.handle_update(message(f"/start {bot.pairing_code}", user=7))
    assert state.owner_id == 7
    assert state.allowlist == [7]
    assert bot.pairing_code is None


@pytest.mark.asyncio
async def test_pairing_code_is_case_and_dash_insensitive(tmp_path):
    bot, _telegram, state = make_bot(tmp_path)
    code = bot.pairing_code.replace("-", "").lower()
    await bot.handle_update(message(f"/start {code}", user=7))
    assert state.owner_id == 7


@pytest.mark.asyncio
async def test_owner_id_env_skips_pairing(tmp_path):
    bot, _telegram, state = make_bot(tmp_path, owner_id=555)
    assert bot.pairing_code is None
    assert state.owner_id == 555


@pytest.mark.asyncio
async def test_non_allowlisted_user_is_refused_once(tmp_path):
    bot, telegram, _state = make_bot(tmp_path, owner=7)
    for _ in range(3):
        await bot.handle_update(message("https://xhslink.com/a/x", user=999))
    assert telegram.texts == "Not authorised."
    assert bot.sender.sends == []


# ---- link extraction from real message shapes ------------------------

def test_candidates_cover_text_caption_and_entity_urls():
    msg = {
        "text": "看看这家店",
        "entities": [
            {"type": "bold", "offset": 0, "length": 2},
            {"type": "text_link", "offset": 0, "length": 5, "url": "http://xhslink.cn/o/9m8PCZf2ef0"},
        ],
    }
    assert message_candidates(msg) == ["看看这家店", "http://xhslink.cn/o/9m8PCZf2ef0"]


@pytest.mark.asyncio
async def test_forwarded_message_with_link_only_in_an_entity(tmp_path):
    """A forward often carries the URL in a text_link, not in the visible text."""
    downloader = FakeDownloader(note=NOTE)
    bot, _telegram, _state = make_bot(tmp_path, downloader=downloader, owner=7)
    update = message("这家AYCE真香", user=7)
    update["message"]["entities"] = [
        {"type": "text_link", "offset": 0, "length": 6, "url": "http://xhslink.cn/o/9m8PCZf2ef0"}
    ]
    await bot.handle_update(update)
    assert downloader.calls[0][0] == "http://xhslink.cn/o/9m8PCZf2ef0"
    assert len(bot.sender.sends) == 1


@pytest.mark.asyncio
async def test_unparseable_rednote_share_says_so_instead_of_going_silent(tmp_path):
    bot, telegram, _state = make_bot(tmp_path, owner=7)
    await bot.handle_update(message("打开【小红书】App查看 xiaohongshu 精彩内容", user=7))
    assert "couldn't parse a link" in telegram.texts


@pytest.mark.asyncio
async def test_ordinary_chatter_is_still_ignored(tmp_path):
    bot, telegram, _state = make_bot(tmp_path, owner=7)
    await bot.handle_update(message("hey how are you", user=7))
    assert telegram.sent == []


# ---- cookie custody (PLAN §7) ---------------------------------------

COOKIE = "a1=1234567890abcdef; webId=abc123; web_session=040069b2aaaabbbbccccdddd"


@pytest.mark.asyncio
async def test_cookie_paste_is_deleted_stored_and_never_echoed(tmp_path):
    bot, telegram, state = make_bot(tmp_path, owner=7)
    await bot.handle_update(message(COOKIE, user=7, message_id=1234))

    assert telegram.deleted == [(7, 1234)]
    assert state.cookie == COOKIE
    assert state.cookie_status == "ok"
    assert COOKIE not in telegram.texts
    assert "web_session" not in telegram.texts


@pytest.mark.asyncio
async def test_cookie_command_form(tmp_path):
    bot, telegram, state = make_bot(tmp_path, owner=7)
    await bot.handle_update(message(f"/cookie {COOKIE}", user=7))
    assert state.cookie == COOKIE
    assert telegram.deleted


@pytest.mark.asyncio
async def test_non_owner_cannot_set_cookie_but_message_is_still_deleted(tmp_path):
    bot, telegram, state = make_bot(tmp_path, owner=7)
    state.allow(8)
    await bot.handle_update(message(COOKIE, user=8, message_id=55))
    assert telegram.deleted == [(8, 55)]
    assert state.cookie is None
    assert "Only the owner" in telegram.texts


def test_the_bots_own_fetches_get_the_session_too(tmp_path):
    """The cookie used to reach only the sidecar, which left the page fallback
    and the comment/rendition scrape anonymous — the requests that actually
    hit XHS's walls."""
    downloader = FakeDownloader(note=NOTE)
    bot, _telegram, state = make_bot(tmp_path, downloader=downloader, owner=1)
    assert downloader.cookie is None
    state.set_cookie("a1=x; web_session=abc")
    bot2, _t2, _s2 = make_bot(tmp_path, downloader=downloader, owner=1)
    assert downloader.cookie == "a1=x; web_session=abc"  # picked up at startup


@pytest.mark.asyncio
async def test_a_cookie_without_web_session_is_stored_but_called_out(tmp_path):
    downloader = FakeDownloader(note=NOTE)
    bot, telegram, state = make_bot(tmp_path, downloader=downloader, owner=1)
    await bot.handle_update(message("/cookie a1=abcdefghij; webId=klmnopqrst; acw_tc=uvwxyz012345"))
    assert state.cookie  # stored, not refused
    assert "not a logged-in session" in telegram.texts
    assert "web_session" in telegram.texts


@pytest.mark.asyncio
async def test_a_real_login_is_not_nagged_about(tmp_path):
    downloader = FakeDownloader(note=NOTE)
    bot, telegram, state = make_bot(tmp_path, downloader=downloader, owner=1)
    await bot.handle_update(message("/cookie a1=abcdefghij; web_session=0400698xyz; webId=kl"))
    assert "not a logged-in session" not in telegram.texts
    assert downloader.cookie == "a1=abcdefghij; web_session=0400698xyz; webId=kl"


@pytest.mark.asyncio
async def test_forget_cookie(tmp_path):
    bot, _telegram, state = make_bot(tmp_path, owner=7)
    state.set_cookie(COOKIE)
    await bot.handle_update(message("/forgetcookie", user=7))
    assert state.cookie is None
    assert state.cookie_status == "unset"


# ---- expiry loop (PLAN §7) ------------------------------------------

@pytest.mark.asyncio
async def test_blocked_fetch_marks_cookie_stale_and_notifies_owner_once(tmp_path):
    downloader = FakeDownloader(error=XhsError("blocked", "获取小红书作品数据失败"))
    bot, telegram, state = make_bot(tmp_path, downloader=downloader, owner=7)
    state.set_cookie(COOKIE)

    await bot.handle_update(message("https://xhslink.com/a/x", user=7))
    assert state.cookie_status == "stale"
    assert telegram.texts.count("marked the stored cookie") == 1

    await bot.handle_update(message("https://xhslink.com/a/y", user=7))
    assert telegram.texts.count("marked the stored cookie") == 1  # notified once


@pytest.mark.asyncio
async def test_cookieless_refusal_tells_the_owner_how_to_add_one(tmp_path):
    """With no cookie stored there is nothing to mark stale — say what would fix it."""
    downloader = FakeDownloader(error=XhsError("blocked", "获取小红书作品数据失败"))
    bot, telegram, state = make_bot(tmp_path, downloader=downloader, owner=7)
    assert state.cookie is None

    await bot.handle_update(message("https://xhslink.com/a/x", user=7))

    assert "without a cookie" in telegram.texts
    assert "DevTools" in telegram.texts  # the actual how-to, not just a hint
    assert "/cookie" in telegram.texts
    assert state.cookie_status == "unset"  # nothing to go stale


@pytest.mark.asyncio
async def test_cookieless_refusal_points_a_guest_at_the_owner(tmp_path):
    downloader = FakeDownloader(error=XhsError("blocked", "获取小红书作品数据失败"))
    bot, telegram, state = make_bot(tmp_path, downloader=downloader, owner=7)
    state.allow(9)

    await bot.handle_update(message("https://xhslink.com/a/x", user=9))
    guest = " || ".join(text for chat, text in telegram.sent if chat == 9)
    owner = " || ".join(text for chat, text in telegram.sent if chat == 7)

    # A guest can't set the cookie, so don't hand them the recipe.
    assert "Ask the bot's owner" in guest
    assert "DevTools" not in guest
    # The owner hears about it once, with instructions.
    assert "no cookie" in owner and "DevTools" in owner

    await bot.handle_update(message("https://xhslink.com/a/y", user=9))
    owner_again = " || ".join(text for chat, text in telegram.sent if chat == 7)
    assert owner_again.count("no cookie") == 1


@pytest.mark.asyncio
async def test_bad_link_does_not_blame_the_cookie(tmp_path):
    downloader = FakeDownloader(error=XhsError("bad_link", "提取小红书作品链接失败"))
    bot, telegram, state = make_bot(tmp_path, downloader=downloader, owner=7)
    state.set_cookie(COOKIE)
    await bot.handle_update(message("https://xhslink.com/a/x", user=7))
    assert state.cookie_status == "ok"
    assert "couldn't read a note link" in telegram.texts


# ---- the note flow (PLAN §4, §6) ------------------------------------

@pytest.mark.asyncio
async def test_link_is_fetched_sent_and_then_served_from_cache(tmp_path):
    downloader = FakeDownloader(note=NOTE)
    bot, _telegram, state = make_bot(tmp_path, downloader=downloader, owner=7)

    url = "https://www.xiaohongshu.com/explore/650a?xsec_token=AB"
    await bot.handle_update(message(url, user=7))
    await bot.handle_update(message(url, user=7))

    assert len(downloader.calls) == 1  # second share hit the LRU
    assert len(bot.sender.sends) == 2
    chat, items, caption = bot.sender.sends[0][:3]
    assert chat == 7
    assert [i.url for i in items] == ["https://cdn/1", "https://cdn/2"]
    assert "<b>Title</b>" in caption
    assert state.data["last_successful_fetch"]


@pytest.mark.asyncio
async def test_stored_cookie_is_passed_to_the_downloader(tmp_path):
    downloader = FakeDownloader(note=NOTE)
    bot, _telegram, state = make_bot(tmp_path, downloader=downloader, owner=7)
    state.set_cookie(COOKIE)
    await bot.handle_update(message("https://xhslink.com/a/x", user=7))
    assert downloader.calls[0][1] == COOKIE


@pytest.mark.asyncio
async def test_long_description_overflows_into_follow_up_messages(tmp_path):
    long_note = Note(**{**NOTE.__dict__, "desc": "word " * 800})
    bot, telegram, _state = make_bot(tmp_path, downloader=FakeDownloader(note=long_note), owner=7)
    await bot.handle_update(message("https://xhslink.com/a/x", user=7))
    assert telegram.sent  # the remainder arrived as its own message


@pytest.mark.asyncio
async def test_status_reports_without_leaking_the_cookie(tmp_path):
    bot, telegram, state = make_bot(tmp_path, owner=7)
    state.set_cookie(COOKIE)
    await bot.handle_update(message("/status", user=7))
    assert "cookie: ok" in telegram.texts
    assert COOKIE not in telegram.texts


@pytest.mark.asyncio
async def test_owner_manages_the_allowlist(tmp_path):
    bot, telegram, state = make_bot(tmp_path, owner=7)
    await bot.handle_update(message("/allow 42", user=7))
    assert state.is_allowed(42)
    await bot.handle_update(message("/deny 42", user=7))
    assert not state.is_allowed(42)

    state.allow(8)
    await bot.handle_update(message("/allow 99", user=8))
    assert not state.is_allowed(99)
    assert "Owner only." in telegram.texts


# ---- progress feedback ----------------------------------------------

@pytest.mark.asyncio
async def test_chat_action_is_refreshed_during_a_long_job(tmp_path, monkeypatch):
    """A chat action expires after ~5s; a video note can take a minute."""
    import asyncio

    class SlowDownloader(FakeDownloader):
        async def detail(self, url, cookie):
            await asyncio.sleep(0.3)
            return await super().detail(url, cookie)

    class CountingTelegram(FakeTelegram):
        def __init__(self):
            super().__init__()
            self.actions = []

        async def send_chat_action(self, chat_id, action):
            self.actions.append(action)

    bot, telegram, _ = make_bot(tmp_path, downloader=SlowDownloader(NOTE), owner=1)
    counting = CountingTelegram()
    bot.tg = counting
    # Beat far faster than the real 4s so the test stays quick.
    monkeypatch.setattr(bot, "_beat_interval", 0.05, raising=False)

    await bot.handle_update(message("http://xhslink.com/o/9m8PCZf2ef0"))

    assert len(counting.actions) >= 3, counting.actions


@pytest.mark.asyncio
async def test_chat_action_stops_when_the_job_does(tmp_path, monkeypatch):
    import asyncio

    class CountingTelegram(FakeTelegram):
        def __init__(self):
            super().__init__()
            self.actions = []

        async def send_chat_action(self, chat_id, action):
            self.actions.append(action)

    bot, telegram, _ = make_bot(tmp_path, downloader=FakeDownloader(NOTE), owner=1)
    counting = CountingTelegram()
    bot.tg = counting
    monkeypatch.setattr(bot, "_beat_interval", 0.02, raising=False)

    await bot.handle_update(message("http://xhslink.com/o/9m8PCZf2ef0"))
    settled = len(counting.actions)
    await asyncio.sleep(0.15)
    assert len(counting.actions) == settled


@pytest.mark.asyncio
async def test_video_note_uses_the_video_action(tmp_path):
    class CountingTelegram(FakeTelegram):
        def __init__(self):
            super().__init__()
            self.actions = []

        async def send_chat_action(self, chat_id, action):
            self.actions.append(action)

    video = Note(
        note_id="650b",
        kind="video",
        title="V",
        author="Someone",
        url="https://www.xiaohongshu.com/explore/650b",
        video="https://cdn/v.mp4",
    )
    bot, telegram, _ = make_bot(tmp_path, downloader=FakeDownloader(video), owner=1)
    counting = CountingTelegram()
    bot.tg = counting

    await bot.handle_update(message("http://xhslink.com/o/9m8PCZf2ef0"))
    assert "upload_video" in counting.actions


# ---- comments in the caption ----------------------------------------

@pytest.mark.asyncio
async def test_comments_ride_in_the_album_caption(tmp_path):
    """One message, so forwarding the album carries the comments with it."""
    from app.comments import Comment

    comments = [
        Comment("虎牙", "好吃", likes="10", location="美国", replies=[Comment("作者", "谢谢")]),
        Comment("路人", "在哪家店？", likes="5"),
    ]
    bot, telegram, _ = make_bot(
        tmp_path, downloader=FakeDownloader(NOTE, comments=comments), owner=1
    )
    await bot.handle_update(message("http://xhslink.com/o/x"))

    caption = bot.sender.sends[0][2]
    assert "top comments" in caption
    assert "好吃" in caption and "谢谢" in caption and "在哪家店？" in caption
    assert "Title" in caption  # the note text is still there
    # Nothing was posted as a separate follow-up message.
    assert telegram.sent == []


@pytest.mark.asyncio
async def test_a_long_note_keeps_the_caption_and_moves_comments_down(tmp_path):
    """The note's own text has first claim on the caption (§4.7).

    If it spills into a follow-up message anyway, the comments go with it
    rather than eating caption room the description needed.
    """
    from app.comments import Comment
    from app.media import tg_len
    from app.telegram import CAPTION_LIMIT

    note = Note(
        note_id="650a",
        kind="image",
        title="T" * 60,
        desc="描述" * 900,  # far past the caption limit
        author="Someone",
        url="https://www.xiaohongshu.com/explore/650a",
        photos=[f"https://cdn/{i}" for i in range(3)],
        lives=[None] * 3,
    )
    comments = [Comment(f"u{i}", "评论" * 20, likes="3") for i in range(5)]
    bot, telegram, _ = make_bot(
        tmp_path, downloader=FakeDownloader(note, comments=comments), owner=1
    )
    await bot.handle_update(message("http://xhslink.com/o/x"))

    caption = bot.sender.sends[0][2]
    assert tg_len(_visible(caption)) <= CAPTION_LIMIT
    # The caption is the note, all of it it could hold — and no comments.
    assert "top comments" not in caption
    assert "描述" in caption
    # The comments rode along in the follow-up, not in a message of their own.
    follow_ups = [text for _chat, text in telegram.sent]
    assert follow_ups and "top comments" in follow_ups[-1]
    assert "描述" in follow_ups[-1]


@pytest.mark.asyncio
async def test_a_short_note_keeps_everything_in_one_message(tmp_path):
    from app.comments import Comment

    comments = [Comment("u1", "好吃", likes="3")]
    bot, telegram, _ = make_bot(
        tmp_path, downloader=FakeDownloader(NOTE, comments=comments), owner=1
    )
    await bot.handle_update(message("http://xhslink.com/o/x"))

    assert "top comments" in bot.sender.sends[0][2]
    assert telegram.sent == []  # nothing followed


@pytest.mark.asyncio
async def test_comments_get_their_own_message_when_the_caption_is_full(tmp_path):
    """A caption with no room left must not silently swallow the comments."""
    from app.comments import Comment

    note = Note(
        note_id="650a",
        kind="image",
        title="T" * 300,
        desc="D" * 700,  # fills the caption exactly enough to leave no room
        author="Someone",
        url="https://www.xiaohongshu.com/explore/650a",
        photos=["https://cdn/1", "https://cdn/2"],
        lives=[None, None],
    )
    comments = [Comment("u1", "好吃", likes="3")]
    bot, telegram, _ = make_bot(
        tmp_path, downloader=FakeDownloader(note, comments=comments), owner=1
    )
    await bot.handle_update(message("http://xhslink.com/o/x"))

    caption = bot.sender.sends[0][2]
    follow_ups = " || ".join(text for _chat, text in telegram.sent)
    assert "top comments" not in caption
    assert "top comments" in follow_ups  # dropped from the caption, not lost


@pytest.mark.asyncio
async def test_a_failed_comment_scrape_still_delivers_the_note(tmp_path):
    class Exploding(FakeDownloader):
        async def enrich(self, note, limit=5):
            raise RuntimeError("XHS changed its markup again")

    bot, telegram, _ = make_bot(tmp_path, downloader=Exploding(NOTE), owner=1)
    await bot.handle_update(message("http://xhslink.com/o/x"))

    assert bot.sender.sends  # the album went out anyway
    assert "top comments" not in bot.sender.sends[0][2]


def _visible(html):
    import re
    from html import unescape

    return unescape(re.sub(r"<[^>]+>", "", html))


# ---- channel mode ----------------------------------------------------

def channel_bot(tmp_path, *, downloader=None, username="mychannel", **kw):
    bot, telegram, state = make_bot(
        tmp_path, downloader=downloader or FakeDownloader(NOTE), owner=1,
        channel_id="@mychannel", **kw
    )
    bot.channel = {"id": -1001234567890, "username": username, "title": "My Channel"}
    return bot, telegram, state


@pytest.mark.asyncio
async def test_submission_posts_to_the_channel_and_links_back(tmp_path):
    bot, telegram, state = channel_bot(tmp_path)
    bot.sender.next_message_id = 42

    await bot.handle_update(message("http://xhslink.com/o/x"))

    # The album went to the channel, not back to the submitter.
    assert bot.sender.sends[0][0] == -1001234567890
    reply = telegram.texts
    assert "Published to <b>My Channel</b>" in reply
    assert "https://t.me/mychannel/42" in reply


@pytest.mark.asyncio
async def test_publishing_is_logged_with_the_permalink(tmp_path, caplog):
    """Where a submission landed must be readable from the log alone."""
    import logging

    bot, _telegram, _state = channel_bot(tmp_path)
    bot.sender.next_message_id = 42
    with caplog.at_level(logging.INFO, logger="app.handlers"):
        await bot.handle_update(message("http://xhslink.com/o/x"))

    assert any("https://t.me/mychannel/42" in r.message for r in caplog.records)


@pytest.mark.asyncio
async def test_private_channel_uses_the_c_style_permalink(tmp_path):
    bot, telegram, _ = channel_bot(tmp_path, username=None)
    bot.sender.next_message_id = 7
    await bot.handle_update(message("http://xhslink.com/o/x"))
    assert "https://t.me/c/1234567890/7" in telegram.texts


@pytest.mark.asyncio
async def test_resubmitting_points_at_the_existing_post(tmp_path):
    bot, telegram, state = channel_bot(tmp_path)
    bot.sender.next_message_id = 42

    await bot.handle_update(message("http://xhslink.com/o/x"))
    assert len(bot.sender.sends) == 1
    assert state.published("650a")["message_id"] == 42

    await bot.handle_update(message("http://xhslink.com/o/x"))

    # No second post; the submitter gets the original link.
    assert len(bot.sender.sends) == 1
    assert "Already on the channel" in telegram.texts
    assert telegram.texts.count("https://t.me/mychannel/42") == 2


@pytest.mark.asyncio
async def test_published_index_survives_a_restart(tmp_path):
    bot, telegram, state = channel_bot(tmp_path)
    bot.sender.next_message_id = 42
    await bot.handle_update(message("http://xhslink.com/o/x"))

    reborn = State(state.path)
    assert reborn.published("650a")["message_id"] == 42
    assert reborn.published("nope") is None


@pytest.mark.asyncio
async def test_a_channel_refusal_tells_the_submitter_and_the_owner(tmp_path):
    bot, telegram, state = channel_bot(tmp_path)

    class Refusing:
        sends = []

        async def send(self, chat_id, items, caption, *, reply_to=None, part_from=1, part_total=None):
            raise TelegramError("sendMediaGroup", 400, "Bad Request: not enough rights")

        async def aclose(self):
            return None

    bot.sender = Refusing()
    await bot.handle_update(message("http://xhslink.com/o/x"))

    assert "channel refused the post" in telegram.texts
    assert "not enough rights" in telegram.texts
    assert state.published("650a") is None  # nothing recorded


@pytest.mark.asyncio
async def test_without_a_channel_nothing_changes(tmp_path):
    bot, telegram, state = make_bot(tmp_path, downloader=FakeDownloader(NOTE), owner=1)
    await bot.handle_update(message("http://xhslink.com/o/x"))

    assert bot.sender.sends[0][0] == 1  # straight back to the user
    assert "Published to" not in telegram.texts
    assert state.published("650a") is None


# ---- whose channel is it: the per-user DM setting ---------------------

@pytest.mark.asyncio
async def test_private_mode_answers_the_dm_and_publishes_nothing(tmp_path):
    bot, telegram, state = channel_bot(tmp_path)
    await bot.handle_update(message("/mode private"))

    await bot.handle_update(message("http://xhslink.com/o/x"))

    assert bot.sender.sends[0][0] == 1  # the submitter's own chat
    assert "Published to" not in telegram.texts
    assert state.published("650a") is None


@pytest.mark.asyncio
async def test_switching_back_to_channel_publishes_again(tmp_path):
    bot, telegram, state = channel_bot(tmp_path)
    await bot.handle_update(message("/mode private"))
    await bot.handle_update(message("/mode channel"))
    bot.sender.next_message_id = 42

    await bot.handle_update(message("http://xhslink.com/o/x"))

    assert bot.sender.sends[0][0] == -1001234567890
    assert "https://t.me/mychannel/42" in telegram.texts


@pytest.mark.asyncio
async def test_the_choice_survives_a_restart(tmp_path):
    _bot, _telegram, state = channel_bot(tmp_path)
    state.set_dm_mode(1, "private")
    assert State(state.path).dm_mode(1) == "private"
    assert State(state.path).dm_mode(2) == "channel"  # everyone else is untouched


@pytest.mark.asyncio
async def test_private_mode_is_per_user(tmp_path):
    bot, telegram, state = channel_bot(tmp_path)
    state.allow(2)
    await bot.handle_update(message("/mode private", user=2))
    bot.sender.next_message_id = 42

    await bot.handle_update(message("http://xhslink.com/o/x", user=1))
    assert bot.sender.sends[0][0] == -1001234567890


@pytest.mark.asyncio
async def test_a_private_user_still_feeds_the_channel_from_a_group(tmp_path):
    """The setting is about DMs. A watched group is a submission by definition,
    and one member's preference must not silence it for everyone."""
    bot, telegram, state = channel_bot(tmp_path)
    state.allow_group(-100, "g", 1)
    await bot.handle_update(message("/mode private"))

    await bot.handle_update(
        {
            "update_id": 2,
            "message": {
                "message_id": 5,
                "from": {"id": 1},
                "chat": {"id": -100, "type": "supergroup"},
                "text": "http://xhslink.com/o/x",
            },
        }
    )
    assert bot.sender.sends[0][0] == -1001234567890


@pytest.mark.asyncio
async def test_private_mode_does_not_consult_the_published_index(tmp_path):
    """A note already on the channel is still fetchable privately — "someone
    else published it" is no reason to refuse the sender their own copy."""
    bot, telegram, state = channel_bot(tmp_path)
    state.record_published("650a", -1001234567890, 42)
    await bot.handle_update(message("/mode private"))

    await bot.handle_update(message("http://xhslink.com/o/x"))
    assert len(bot.sender.sends) == 1
    assert "Already on the channel" not in telegram.texts


@pytest.mark.asyncio
async def test_bare_mode_reports_where_links_go(tmp_path):
    bot, telegram, _state = channel_bot(tmp_path)
    await bot.handle_update(message("/mode"))
    assert "submissions" in telegram.texts and "/mode private" in telegram.texts

    await bot.handle_update(message("/mode dm"))
    telegram.sent.clear()
    await bot.handle_update(message("/mode"))
    assert "private" in telegram.texts and "/mode channel" in telegram.texts


@pytest.mark.asyncio
async def test_an_unknown_mode_word_changes_nothing(tmp_path):
    bot, telegram, state = channel_bot(tmp_path)
    await bot.handle_update(message("/mode sideways"))
    assert "Usage" in telegram.texts
    assert state.dm_mode(1) == "channel"


@pytest.mark.asyncio
async def test_without_a_channel_there_is_nothing_to_switch(tmp_path):
    bot, telegram, state = make_bot(tmp_path, downloader=FakeDownloader(NOTE), owner=1)
    await bot.handle_update(message("/mode private"))
    assert "no channel configured" in telegram.texts
    assert state.dm_mode(1) == "channel"  # nothing stored, nothing to undo


@pytest.mark.asyncio
async def test_dm_mode_env_sets_the_default_and_a_user_can_override_it(tmp_path):
    bot, telegram, state = channel_bot(tmp_path, dm_mode="private")
    await bot.handle_update(message("http://xhslink.com/o/x"))
    assert bot.sender.sends[0][0] == 1  # nobody chose, so the default holds

    await bot.handle_update(message("/mode channel"))
    await bot.handle_update(message("http://xhslink.com/o/x"))
    assert bot.sender.sends[1][0] == -1001234567890


@pytest.mark.asyncio
async def test_status_and_help_say_where_a_users_links_go(tmp_path):
    bot, telegram, _state = channel_bot(tmp_path)
    await bot.handle_update(message("/mode private"))
    telegram.sent.clear()
    await bot.handle_update(message("/status"))
    await bot.handle_update(message("/help"))
    assert "your DMs: answered here only" in telegram.texts
    assert "here only — nothing you send goes to" in telegram.texts
    assert "/mode" in telegram.texts


# ---- the discussion group is not ours to talk in ----------------------

GROUP_ID = -1009999999999


@pytest.mark.asyncio
async def test_chatter_in_the_discussion_group_is_ignored(tmp_path):
    """Without this the bot answers 'Not authorised.' to everyone commenting."""
    bot, telegram, _ = channel_bot(tmp_path)

    await bot.handle_update({
        "update_id": 3,
        "message": {
            "message_id": 501,
            "from": {"id": 99999, "username": "stranger"},
            "chat": {"id": GROUP_ID, "type": "supergroup"},
            "text": "nice post! http://xhslink.com/o/y",
        },
    })

    assert telegram.sent == []
    assert bot.sender.sends == []




# ---- continuation links ----------------------------------------------

@pytest.mark.asyncio
async def test_a_post_with_overflow_links_to_its_continuation(tmp_path):
    """A forwarded post loses its reply chain, so the pointer lives in the text."""
    note = Note(
        note_id="650a", kind="image", title="T", desc="D" * 2000,
        author="Someone", url="https://www.xiaohongshu.com/explore/650a",
        photos=[f"https://cdn/{i}" for i in range(3)], lives=[None] * 3,
    )
    bot, telegram, _ = channel_bot(tmp_path, downloader=FakeDownloader(note))
    bot.sender.next_message_id = 42

    await bot.handle_update(message("http://xhslink.com/o/x"))

    assert telegram.edits, "the album caption was never given a link"
    _chat, edited_id, body = telegram.edits[0]
    assert edited_id == 42
    assert "continues ↓" in body
    assert "https://t.me/mychannel/901" in body  # the follow-up's own permalink


@pytest.mark.asyncio
async def test_a_self_contained_post_gets_no_link(tmp_path):
    bot, telegram, _ = channel_bot(tmp_path, downloader=FakeDownloader(NOTE))
    bot.sender.next_message_id = 42

    await bot.handle_update(message("http://xhslink.com/o/x"))
    assert telegram.edits == []


@pytest.mark.asyncio
async def test_every_message_but_the_last_points_onward(tmp_path):
    """Three messages means two links, each aimed at the next one."""
    note = Note(
        note_id="650a", kind="image", title="T", desc="D" * 9000,
        author="Someone", url="https://www.xiaohongshu.com/explore/650a",
        photos=[f"https://cdn/{i}" for i in range(3)], lives=[None] * 3,
    )
    bot, telegram, _ = channel_bot(tmp_path, downloader=FakeDownloader(note))
    bot.sender.next_message_id = 42

    await bot.handle_update(message("http://xhslink.com/o/x"))

    assert len(telegram.sent) >= 2  # the description needed more than one message
    targets = [body.rsplit('href="', 1)[1].split('"')[0] for _c, _m, body in telegram.edits]
    edited = [mid for _c, mid, _b in telegram.edits]
    assert edited[0] == 42
    assert targets[0].endswith("/901")   # album  → first follow-up
    assert targets[1].endswith("/902")   # first  → second follow-up
    in_channel = [t for c, t in telegram.sent if c == -1001234567890]
    assert len(telegram.edits) == len(in_channel)  # the last message points nowhere


# ---- listening in groups ---------------------------------------------

WATCHED = -1005555555555


def group_message(text, *, user=55, chat=WATCHED, title="Some Group"):
    return {
        "update_id": 5,
        "message": {
            "message_id": 77,
            "from": {"id": user, "username": "member"},
            "chat": {"id": chat, "type": "supergroup", "title": title},
            "text": text,
        },
    }


def added_to_group(by_user, *, chat=WATCHED, status="member", title="Some Group"):
    return {
        "update_id": 6,
        "my_chat_member": {
            "chat": {"id": chat, "type": "supergroup", "title": title},
            "from": {"id": by_user, "username": "inviter"},
            "date": 1,
            "old_chat_member": {"user": {"id": 1, "is_bot": True}, "status": "left"},
            "new_chat_member": {"user": {"id": 1, "is_bot": True}, "status": status},
        },
    }


@pytest.mark.asyncio
async def test_a_published_link_is_reported_back_to_the_group(tmp_path):
    """The finished post is the one thing the bot says in a group."""
    bot, telegram, state = channel_bot(tmp_path)
    state.allow_group(WATCHED, "Some Group", 1)
    bot.sender.next_message_id = 42

    await bot.handle_update(group_message("look at this http://xhslink.com/o/x"))

    assert bot.sender.sends[0][0] == -1001234567890
    assert state.published("650a")["message_id"] == 42

    said = [(chat, text) for chat, text in telegram.sent if chat == WATCHED]
    assert len(said) == 1, said
    assert "https://t.me/mychannel/42" in said[0][1]


@pytest.mark.asyncio
async def test_the_report_replies_to_the_message_that_carried_the_link(tmp_path):
    replies = []

    class RecordingTelegram(FakeTelegram):
        async def send_message(self, chat_id, text, *, reply_to=None, preview=False):
            replies.append((chat_id, reply_to))
            return await super().send_message(chat_id, text, reply_to=reply_to)

    bot, _telegram, state = channel_bot(tmp_path)
    bot.tg = RecordingTelegram()
    state.allow_group(WATCHED, "Some Group", 1)
    bot.sender.next_message_id = 42

    await bot.handle_update(group_message("http://xhslink.com/o/x"))  # message_id 77

    assert (WATCHED, 77) in replies


@pytest.mark.asyncio
async def test_a_resubmission_in_a_group_points_at_the_existing_post(tmp_path):
    bot, telegram, state = channel_bot(tmp_path)
    state.allow_group(WATCHED, "Some Group", 1)
    bot.sender.next_message_id = 42

    await bot.handle_update(group_message("http://xhslink.com/o/x"))
    await bot.handle_update(group_message("http://xhslink.com/o/x", user=56))

    assert len(bot.sender.sends) == 1  # posted once
    said = [text for chat, text in telegram.sent if chat == WATCHED]
    assert "Already on the channel" in said[-1]
    assert "https://t.me/mychannel/42" in said[-1]


@pytest.mark.asyncio
async def test_nothing_is_said_in_a_group_when_the_fetch_fails(tmp_path):
    """Only a finished post is worth saying. Failures stay in the log."""
    downloader = FakeDownloader(error=XhsError("blocked", "获取小红书作品数据失败"))
    bot, telegram, state = channel_bot(tmp_path, downloader=downloader)
    state.allow_group(WATCHED, "Some Group", 1)

    await bot.handle_update(group_message("http://xhslink.com/o/x"))

    assert all(chat != WATCHED for chat, _text in telegram.sent), telegram.sent


@pytest.mark.asyncio
async def test_chatter_and_commands_in_groups_are_ignored(tmp_path):
    bot, telegram, state = channel_bot(tmp_path)
    state.allow_group(WATCHED, "Some Group", 1)

    for text in ("hello everyone", "/status", "/help", "/allow 5"):
        await bot.handle_update(group_message(text))

    assert telegram.sent == []
    assert bot.sender.sends == []


@pytest.mark.asyncio
async def test_an_unwatched_group_is_ignored_entirely(tmp_path):
    bot, telegram, _ = channel_bot(tmp_path)
    await bot.handle_update(group_message("http://xhslink.com/o/x", chat=-1009999))
    assert bot.sender.sends == []
    assert telegram.sent == []


@pytest.mark.asyncio
async def test_being_added_by_an_allowlisted_user_starts_the_watch(tmp_path):
    bot, telegram, state = channel_bot(tmp_path)
    await bot.handle_update(added_to_group(1))  # user 1 is the owner

    assert state.listens_in(WATCHED)
    assert "Now watching" in telegram.texts


@pytest.mark.asyncio
async def test_being_added_by_a_stranger_does_not(tmp_path):
    """Anyone can drag a bot into a group; that must not feed the channel."""
    bot, telegram, state = channel_bot(tmp_path)
    await bot.handle_update(added_to_group(999))

    assert not state.listens_in(WATCHED)
    assert "/allowgroup" in telegram.texts  # the owner is told how to opt in
    await bot.handle_update(group_message("http://xhslink.com/o/x"))
    assert bot.sender.sends == []


@pytest.mark.asyncio
async def test_being_removed_stops_the_watch(tmp_path):
    bot, _telegram, state = channel_bot(tmp_path)
    state.allow_group(WATCHED, "Some Group", 1)
    await bot.handle_update(added_to_group(1, status="kicked"))
    assert not state.listens_in(WATCHED)


@pytest.mark.asyncio
async def test_owner_can_list_and_drop_watched_groups(tmp_path):
    bot, telegram, state = channel_bot(tmp_path)
    state.allow_group(WATCHED, "Some Group", 1)

    await bot.handle_update(message("/groups"))
    assert "Some Group" in telegram.texts

    await bot.handle_update(message(f"/denygroup {WATCHED}"))
    assert not state.listens_in(WATCHED)


@pytest.mark.asyncio
async def test_group_admin_commands_are_owner_only(tmp_path):
    bot, telegram, state = channel_bot(tmp_path)
    state.allow(9)
    await bot.handle_update(message("/groups", user=9))
    assert "Owner only" in telegram.texts


# ---- when nothing actually gets sent ---------------------------------

class SkippingSender(FakeSender):
    """Everything was too large or unreachable: nothing left to post."""

    async def send(self, chat_id, items, caption, *, reply_to=None, part_from=1, part_total=None):
        self.sends.append((chat_id, list(items), caption, reply_to, part_from))
        return SendReport(sent=0, skipped=["item 1 too large (88 MB)"], first_message_id=None)


@pytest.mark.asyncio
async def test_an_undeliverable_note_is_not_announced_as_published(tmp_path):
    """Observed live: an 88 MB video was skipped and the group was told
    "Published" with no link, for a post that did not exist."""
    bot, telegram, state = channel_bot(tmp_path)
    bot.sender = SkippingSender()
    state.allow_group(WATCHED, "Some Group", 1)

    await bot.handle_update(group_message("http://xhslink.com/o/x"))

    assert all("Published" not in text for _chat, text in telegram.sent), telegram.sent
    assert state.published("650a") is None  # nothing to point at, nothing remembered


@pytest.mark.asyncio
async def test_a_dm_submitter_is_told_why_nothing_arrived(tmp_path):
    bot, telegram, _ = channel_bot(tmp_path)
    bot.sender = SkippingSender()

    await bot.handle_update(message("http://xhslink.com/o/x"))

    assert "couldn't send any of that note's media" in telegram.texts
    assert "too large (88 MB)" in telegram.texts


@pytest.mark.asyncio
async def test_video_renditions_reach_the_sender(tmp_path):
    """The page fetch happens after the album is first assembled, so the album
    has to be rebuilt — otherwise the renditions are found and then ignored."""
    video = Note(
        note_id="650b",
        kind="video",
        title="V",
        author="Someone",
        url="https://www.xiaohongshu.com/explore/650b",
        video="https://cdn/original.mp4",
    )

    class FindsRenditions(FakeDownloader):
        async def enrich(self, note, limit=5):
            note.video_variants = [
                ("https://cdn/original.mp4", 92_689_815),
                ("https://cdn/h265.mp4", 37_938_283),
            ]
            return []

    bot, _telegram, _state = make_bot(tmp_path, downloader=FindsRenditions(video), owner=1)
    await bot.handle_update(message("http://xhslink.com/o/x"))

    sent_items = bot.sender.sends[0][1]
    assert sent_items[0].alternatives == (("https://cdn/h265.mp4", 37_938_283),)


@pytest.mark.asyncio
async def test_a_profile_link_gets_a_useful_answer(tmp_path):
    downloader = FakeDownloader(error=XhsError("profile", "that link points at a profile"))
    bot, telegram, _ = make_bot(tmp_path, downloader=downloader, owner=1)

    await bot.handle_update(message("https://xhslink.cn/m/58soZQrmwkW"))

    assert "profile, not a note" in telegram.texts
    assert "couldn't read a note link" not in telegram.texts


@pytest.mark.asyncio
async def test_a_profile_link_in_a_group_is_still_silent(tmp_path):
    downloader = FakeDownloader(error=XhsError("profile", "that link points at a profile"))
    bot, telegram, state = channel_bot(tmp_path, downloader=downloader)
    state.allow_group(WATCHED, "Some Group", 1)

    await bot.handle_update(group_message("https://xhslink.cn/m/58soZQrmwkW"))

    assert all(chat != WATCHED for chat, _text in telegram.sent), telegram.sent
