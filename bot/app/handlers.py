"""Update handling: allowlist, bootstrap, cookie custody, and the note flow."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from datetime import datetime, timezone
from html import escape
from typing import Sequence

from .acres import (
    Acres,
    AcresError,
    Thread,
    find_acres_link,
    parse_credentials,
    render as render_thread,
    reply_gallery,
    source_nodes,
    thread_id,
    to_nodes,
    TRIMMED_NOTE,
)
from .acres import looks_like_cookie as looks_like_acres_cookie
from .cache import LRU
from .config import Config
from .logs import current_rid, fields, new_rid
from .comments import (
    comment_albums,
    fit_into_caption,
    relink_images,
    render_comments,
    strip_tags,
)
from .media import MEDIA_GROUP_LIMIT, MediaSender, build_caption, tg_len, tg_truncate
from .state import State, generate_pairing_code
from .telegraph import Telegraph, TelegraphError, trim
from .telegram import CAPTION_LIMIT, MESSAGE_LIMIT, Telegram, TelegramError
from .xhs import MediaItem, Note, XhsDownloader, XhsError, cache_key, find_link

log = logging.getLogger(__name__)


@contextlib.asynccontextmanager
async def _nothing():
    """Stand-in for a progress indicator when there is no one watching."""
    yield None

COOKIE_MARKERS = ("web_session=", "a1=", "webid=", "gid=")

HELP = """<b>RedNote → Telegram</b>

Send me a Xiaohongshu share link and I'll post the note back as native media.

A <b>1point3acres</b> thread link works too: it comes back as a single page with the \
post, its pictures and the top replies.

/status — cookie age, last successful fetch, sidecar health
/cookie &lt;value&gt; — set the XHS cookie (the message is deleted on receipt)
/forgetcookie — wipe the stored cookie
/help — this message"""

OWNER_HELP = HELP + """

<b>Owner only</b>
/allow &lt;user_id&gt; · /deny &lt;user_id&gt; · /users
/groups · /allowgroup &lt;chat_id&gt; · /denygroup &lt;chat_id&gt;
/acres &lt;cookie or curl&gt; · /forgetacres"""

# Shown at the foot of a message that has a continuation. Kept short: it costs
# caption room, which the note's own text would rather have.
# 1point3acres scores a post with 好苗/杂草 — a thumbs up, not a heart.
ACRES_LIKE = "👍"


# What people actually type. "channel" and "private" are the stored values;
# everything else here is a synonym someone will reach for first.
MODE_WORDS = {
    "channel": "channel",
    "publish": "channel",
    "submit": "channel",
    "public": "channel",
    "on": "channel",
    "private": "private",
    "dm": "private",
    "me": "private",
    "quiet": "private",
    "off": "private",
}


def acres_key(tid: str) -> str:
    """The dedupe index is shared with RedNote, so a forum id says so."""
    return f"1p3a:{tid}" if tid else ""


def _page_message(thread: Thread, page: str) -> str:
    """What comes back when a thread went to telegra.ph.

    The Telegraph link goes first: Telegram previews the first link it finds,
    and the preview is the point — Instant View opens the whole thread inside
    the client.
    """
    meta = " · ".join(
        escape(bit) for bit in (thread.author, thread.published.split(" ")[0], thread.forum) if bit
    )
    lines = [f'📄 <a href="{escape(page, quote=True)}">{escape(thread.title or "Untitled")}</a>']
    if meta:
        lines.append(meta)
    lines.append(f'<a href="{escape(thread.link, quote=True)}">open on 1point3acres</a>')
    return "\n".join(lines)

CONTINUED = "continues ↓"
CONTINUED_COST = 16  # the text, plus the blank line before it

ACRES_INSTRUCTIONS = (
    "1point3acres sits behind a Cloudflare challenge, so I need your browser's "
    "own session.\n\nOpen a thread in a logged-in browser → DevTools → Network → "
    "the document request → right-click → <b>Copy as cURL</b>, then send it here "
    "as <code>/acres &lt;paste&gt;</code>.\n\nPasting the whole cURL matters: "
    "Cloudflare ties <code>cf_clearance</code> to the exact User-Agent that "
    "solved the challenge, and the paste carries both. A bare "
    "<code>Cookie</code> header works too if the browser is a recent Chrome.\n\n"
    "Same caveat as the RedNote cookie: I delete the message on receipt, but it "
    "has already transited Telegram's servers."
)

COOKIE_INSTRUCTIONS = (
    "Open <b>xiaohongshu.com</b> in a logged-in browser → DevTools → "
    "Network → any request → copy the whole <code>Cookie</code> request header, "
    "then send it here as <code>/cookie &lt;value&gt;</code>.\n\n"
    "I delete the message as soon as I've stored it, but it still transited "
    "Telegram's servers — treat the cookie as burned if that matters to you."
)


def _age(iso: str | None) -> str:
    if not iso:
        return "never"
    try:
        then = datetime.fromisoformat(iso.replace("Z", "+00:00"))
    except ValueError:
        return iso
    delta = datetime.now(timezone.utc) - then
    seconds = int(delta.total_seconds())
    if seconds < 90:
        return f"{seconds}s ago"
    if seconds < 5400:
        return f"{seconds // 60}m ago"
    if seconds < 172800:
        return f"{seconds // 3600}h ago"
    return f"{seconds // 86400}d ago"


def message_candidates(message: dict) -> list[str]:
    """Every place a share link can hide in a Telegram message.

    Forwarded messages and rich pastes routinely carry the URL only in a
    `text_link` entity, where the visible text is a title and the URL is
    attached to it — searching `text` alone finds nothing.
    """
    candidates = []
    for field in ("text", "caption"):
        value = message.get(field)
        if value:
            candidates.append(value)
    for field in ("entities", "caption_entities"):
        for entity in message.get(field) or []:
            url = entity.get("url")
            if url:
                candidates.append(url)
    return candidates


def looks_like_cookie(text: str) -> bool:
    stripped = text.strip()
    if len(stripped) < 40 or "\n" in stripped.strip():
        return False
    return sum(marker in stripped for marker in COOKIE_MARKERS) >= 1 and "=" in stripped


class Bot:
    # Telegram drops a chat action after ~5s; refresh just inside that.
    _beat_interval = 4.0

    def __init__(self, config: Config, state: State, telegram: Telegram, downloader: XhsDownloader):
        self.config = config
        self.state = state
        self.tg = telegram
        self.xhs = downloader
        self.notes: LRU[Note] = LRU(maxsize=config.cache_size, ttl=config.cache_ttl_seconds)
        self._told_owner_cookieless = False
        self.file_ids: LRU[str] = LRU(maxsize=512)
        self.sender = MediaSender(
            telegram,
            mode=config.media_mode,
            max_bytes=config.max_upload_bytes,
            file_ids=self.file_ids,
            proxy=config.proxy,
        )
        # 1point3acres: its own client, cache and sender. The sender needs
        # separate headers — the forum serves attachments only to a logged-in
        # session — but the same proxy: PROXY is every fetch the bot makes, not
        # a site-specific route.
        self.acres: Acres | None = None
        self.acres_sender: MediaSender | None = None
        self.telegraph: Telegraph | None = None
        self.threads: LRU[Thread] = LRU(maxsize=config.cache_size, ttl=config.cache_ttl_seconds)
        if config.acres:
            self.acres = Acres(
                timeout=config.fetch_timeout,
                user_agent=config.acres_ua,
                replies=config.acres_comments,
                proxy=config.proxy,
            )
            self.acres_sender = MediaSender(
                telegram,
                mode=config.media_mode,
                max_bytes=config.max_upload_bytes,
                file_ids=self.file_ids,
                headers=self.acres.headers(state.acres_cookie, state.acres_ua),
                proxy=config.proxy,
            )
            if config.acres_telegraph:
                self.telegraph = Telegraph(timeout=config.http_timeout)
        # The bot's own XHS fetches share the stored session, not just the
        # sidecar's (see XhsDownloader.set_cookie).
        if state.cookie:
            downloader.set_cookie(state.cookie)
        self.pairing_code: str | None = None
        self.started_at = time.monotonic()
        self._refused: set[int] = set()
        self._warned_cookieless = False
        # Filled in by check_channel() at startup when CHANNEL_ID is set.
        self.channel: dict | None = None

    # ---- bootstrap ---------------------------------------------------

    def bootstrap(self) -> None:
        if self.config.owner_id and not self.state.owner_id:
            self.state.claim_owner(self.config.owner_id)
            log.info("owner set from OWNER_ID: %s", self.config.owner_id)
        if self.state.owner_id:
            log.info("owner is %s, %d user(s) allowlisted", self.state.owner_id, len(self.state.allowlist))
            return
        self.pairing_code = generate_pairing_code()
        log.info("=" * 58)
        log.info("No owner yet. Send this to the bot to claim it:")
        log.info("    /start %s", self.pairing_code)
        log.info("=" * 58)

    async def check_channel(self, me: dict) -> None:
        """Resolve the target channel and confirm we may post to it.

        Doing this at startup turns "the bot silently can't publish" into one
        clear line in the log, instead of a failure on someone's first
        submission.
        """
        target = self.config.channel_id
        if not target:
            return
        try:
            chat = await self.tg.get_chat(target)
            member = await self.tg.get_chat_member(target, me["id"])
        except TelegramError as exc:
            log.error("CHANNEL_ID=%s is unusable: %s", target, exc.description)
            log.error("Add the bot to the channel as an admin with 'Post Messages'.")
            return

        status = member.get("status")
        can_post = member.get("can_post_messages", status == "creator")
        if status not in ("administrator", "creator") or not can_post:
            log.error(
                "the bot is %s in %s and cannot post — grant 'Post Messages'",
                status or "not a member",
                chat.get("title") or target,
            )
            return

        self.channel = {
            "id": chat.get("id", target),
            "username": chat.get("username"),
            "title": chat.get("title") or str(target),
        }
        log.info(
            "publishing submissions to %s (%s)",
            self.channel["title"],
            f"@{self.channel['username']}" if self.channel["username"] else "private channel",
        )

    def _publishes(self, user_id: int | None) -> bool:
        """Does a DM from this user become a channel submission?

        A group link always is — that is what watching a group means. A DM is
        the one place where the answer belongs to the sender, so it is theirs
        to set; DM_MODE only decides for those who never have.
        """
        if not self.channel:
            return False
        return self.state.dm_mode(user_id, self.config.dm_mode) == "channel"

    def _mode_line(self, user_id: int | None) -> str:
        """One sentence on where this user's links go, and how to change it."""
        where = (
            f"@{self.channel['username']}"
            if self.channel["username"]
            else f"<b>{escape(self.channel['title'])}</b>"
        )
        if self._publishes(user_id):
            return (
                f"Your links are <b>submissions</b>: I publish them to {where} and send "
                "you a link to the post.\n\nSend <code>/mode private</code> to keep them "
                "here between us instead."
            )
        return (
            f"Your links are <b>private</b>: I fetch them and answer here, and nothing "
            f"goes to {where}.\n\nSend <code>/mode channel</code> to submit them instead."
        )

    async def _handle_mode(self, chat_id: int, user_id: int, text: str) -> None:
        if not self.channel:
            await self._reply(
                chat_id,
                "There is no channel configured, so everything I fetch already comes "
                "straight back to you here.",
            )
            return
        parts = text.split()
        if len(parts) < 2:
            await self._reply(chat_id, self._mode_line(user_id))
            return
        wanted = MODE_WORDS.get(parts[1].strip().lower())
        if not wanted:
            await self._reply(
                chat_id,
                "Usage: <code>/mode channel</code> (submit what you send) or "
                "<code>/mode private</code> (keep it in this chat).\n\n"
                + self._mode_line(user_id),
            )
            return
        self.state.set_dm_mode(user_id, wanted)
        log.info(
            "user %s set dm mode to %s", user_id, wanted,
            extra=fields(event="dm_mode", user=user_id, mode=wanted),
        )
        await self._reply(chat_id, "Noted. " + self._mode_line(user_id))

    async def _handle_group_message(self, chat: dict, message: dict) -> None:
        """Publish any RedNote link. The only thing said back is the permalink."""
        chat_id = chat.get("id")
        if not self.state.listens_in(chat_id):
            return
        candidates = message_candidates(message)
        link = next((found for found in map(find_link, candidates) if found), None)
        thread = next((found for found in map(find_acres_link, candidates) if found), None)
        if not link and not (thread and self.acres):
            return
        if not self.channel:
            log.info("ignoring a link from %s: no channel configured", chat.get("title"))
            return
        current_rid.set(new_rid())
        site = "" if link else "1p3a"
        log.info(
            "submission from group %s (%s): %s",
            chat.get("title") or chat_id,
            (message.get("from") or {}).get("id"),
            link or thread,
            extra=fields(
                event="submission", source="group", group=chat_id,
                **({"site": site} if site else {}),
            ),
        )
        # No reply chat: progress and failures stay out of the group entirely.
        # The finished post is the exception — it goes back as a reply to the
        # message that offered the link, so whoever shared it can see where it
        # landed.
        handle = self._handle_link if link else self._handle_acres_link
        await handle(
            None,
            None,
            link or thread,
            (message.get("from") or {}).get("id"),
            announce_to=(chat_id, message.get("message_id")),
        )

    async def _handle_membership(self, event: dict) -> None:
        """React to the bot being added to or removed from a group."""
        chat = event.get("chat") or {}
        if chat.get("type") not in ("group", "supergroup"):
            return
        status = (event.get("new_chat_member") or {}).get("status")
        actor = (event.get("from") or {}).get("id")
        title = chat.get("title") or str(chat.get("id"))

        if status in ("left", "kicked"):
            if self.state.deny_group(chat["id"]):
                log.info("removed from %s; no longer listening there", title)
            return
        if status not in ("member", "administrator", "creator"):
            return

        # Anyone can drag a bot into a group; only a trusted invite makes it
        # a source for the channel.
        if self.state.is_allowed(actor):
            if self.state.allow_group(chat["id"], title, actor):
                log.info("listening for links in %s (added by %s)", title, actor)
                await self._notify_owner(
                    f"👂 Now watching <b>{escape(title)}</b> for RedNote links. "
                    "I'll publish what I find and stay silent there."
                )
            return
        log.warning("added to %s by %s, who is not allowlisted — ignoring it", title, actor)
        await self._notify_owner(
            f"I was added to <b>{escape(title)}</b> (<code>{chat['id']}</code>) by an "
            f"unknown user (<code>{actor}</code>). I'm ignoring it.\n\n"
            f"To watch it anyway: <code>/allowgroup {chat['id']}</code>"
        )

    async def _send_and_track(
        self,
        chat_id: int | str,
        text: str,
        *,
        reply_to: int | None = None,
        preview: bool = False,
    ) -> int | None:
        """Like _reply, but hands back the message id so it can be linked to."""
        try:
            sent = await self.tg.send_message(
                chat_id, text, reply_to=reply_to, preview=preview
            )
        except TelegramError as exc:
            log.error("could not post a follow-up to %s: %s", chat_id, exc.description)
            return None
        return (sent or {}).get("message_id")

    async def _link_the_chain(
        self,
        chat_id: int | str,
        chain: list[tuple[int, str, bool]],
        *,
        image_links: dict[str, str] | None = None,
    ) -> None:
        """Finalize normal continuations and comment-picture marker links.

        Telegram's reply chain vanishes when a post is forwarded elsewhere, so
        normal text/media continuations need an in-body pointer. Comment-image
        albums are deliberately not in that chain: they reply to the main post
        (or its comment-overflow message) instead. If a channel permalink is
        available, their 📷 markers are folded into the same edit as any normal
        continuation, so a message is touched at most once.
        """
        for index, (message_id, original, is_caption) in enumerate(chain):
            body = relink_images(original, image_links) if image_links else original
            next_id = None
            if index + 1 < len(chain):
                next_id = chain[index + 1][0]
                link = self.message_link(next_id)
                if link:
                    body = f'{body}\n\n<a href="{link}">{CONTINUED}</a>'
            if body == original:
                continue
            try:
                if is_caption:
                    await self.tg.edit_message_caption(chat_id, message_id, body)
                else:
                    await self.tg.edit_message_text(chat_id, message_id, body)
            except TelegramError as exc:
                log.warning(
                    "could not finalize message %s%s: %s",
                    message_id,
                    f" to {next_id}" if next_id else "",
                    exc.description,
                )

    def message_link(self, message_id: int) -> str | None:
        """A t.me permalink for a post in the configured channel."""
        if not self.channel or not message_id:
            return None
        username = self.channel["username"]
        if username:
            return f"https://t.me/{username}/{message_id}"
        internal = str(self.channel["id"]).removeprefix("-100")
        return f"https://t.me/c/{internal}/{message_id}"

    # ---- entry point -------------------------------------------------

    async def handle_update(self, update: dict) -> None:
        if update.get("my_chat_member"):
            await self._handle_membership(update["my_chat_member"])
            return

        message = update.get("message")
        if not message:
            return
        chat = message.get("chat") or {}
        chat_id = chat.get("id")

        # Groups are listen-only: a RedNote link there becomes a submission and
        # nothing is ever said back. Everything else in a group — including a
        # channel's discussion mirror — is none of our business.
        if chat.get("type") != "private":
            await self._handle_group_message(chat, message)
            return

        sender = message.get("from") or {}
        user_id = sender.get("id")
        if not user_id or not chat_id:
            return
        text = (message.get("text") or message.get("caption") or "").strip()
        candidates = message_candidates(message)

        if not self.state.owner_id:
            await self._try_pairing(chat_id, user_id, message.get("message_id"), text)
            return

        if not self.state.is_allowed(user_id):
            log.warning("rejected update from %s (@%s)", user_id, sender.get("username"))
            if user_id not in self._refused:
                self._refused.add(user_id)
                await self._reply(chat_id, "Not authorised.")
            return

        if not text and not candidates:
            return

        # Cookie first: a bare paste must be recognised before anything logs it.
        # Two sites now, so the markers have to be tried in order: a
        # 1point3acres cookie carries `_gid=`, and the RedNote matcher would
        # otherwise claim it on the strength of "gid=".
        if (
            text.startswith("/acres")
            or text.lower().startswith("curl ")
            or looks_like_acres_cookie(text)
        ):
            await self._handle_acres_cookie(chat_id, user_id, message.get("message_id"), text)
            return
        if text.startswith("/cookie") or looks_like_cookie(text):
            await self._handle_cookie(chat_id, user_id, message.get("message_id"), text)
            return

        command = text.split()[0].lower().split("@")[0]
        if command in ("/start", "/help"):
            await self._reply(chat_id, self._help_text(user_id))
            return
        if command == "/status":
            await self._handle_status(chat_id, user_id)
            return
        if command == "/forgetcookie":
            self.state.clear_cookie()
            self.xhs.set_cookie(None)
            await self._reply(chat_id, "Cookie wiped. Fetches will run unauthenticated.")
            return
        if command == "/forgetacres":
            if user_id != self.state.owner_id:
                await self._reply(chat_id, "Owner only.")
                return
            self.state.clear_acres_cookie()
            if self.acres_sender:
                self.acres_sender.set_headers(Cookie="")
            await self._reply(
                chat_id,
                "1point3acres cookie wiped. Threads will fail at the Cloudflare "
                "challenge until a new one is set.",
            )
            return
        if command == "/mode":
            await self._handle_mode(chat_id, user_id, text)
            return
        if command in ("/allow", "/deny", "/users"):
            await self._handle_admin(chat_id, user_id, command, text)
            return
        if command in ("/allowgroup", "/denygroup", "/groups"):
            await self._handle_groups(chat_id, user_id, command, text)
            return

        link = next((found for found in map(find_link, candidates) if found), None)
        thread = next((found for found in map(find_acres_link, candidates) if found), None)
        # Never log message text: a mistyped cookie would end up in the log.
        log.info(
            "message %s from %s: %d chars, %d candidate(s), link=%s",
            message.get("message_id"),
            user_id,
            len(text),
            len(candidates),
            link or thread or "none",
        )
        if self.config.debug_updates:
            log.info("update dump: %s", json.dumps(message, ensure_ascii=False)[:2000])

        if thread and not link:
            if not self.acres:
                await self._reply(chat_id, "1point3acres support is switched off here.")
                return
            current_rid.set(new_rid())
            publish = self._publishes(user_id)
            log.info(
                "submission from %s", user_id,
                extra=fields(
                    event="submission", source="dm", site="1p3a", private=not publish
                ),
            )
            await self._handle_acres_link(
                chat_id, message.get("message_id"), thread, user_id, publish=publish
            )
            return
        if link:
            current_rid.set(new_rid())
            publish = self._publishes(user_id)
            log.info(
                "submission from %s", user_id,
                extra=fields(event="submission", source="dm", private=not publish),
            )
            await self._handle_link(
                chat_id, message.get("message_id"), link, user_id, publish=publish
            )
            return
        if command.startswith("/"):
            await self._reply(chat_id, "Unknown command. /help")
            return
        if any(marker in " ".join(candidates).lower() for marker in ("xhslink", "xiaohongshu", "xhs.cn")):
            await self._reply(
                chat_id,
                "That looks like a RedNote share, but I couldn't parse a link out of it. "
                "Try pasting the URL on its own.",
                reply_to=message.get("message_id"),
            )

    # ---- pairing -----------------------------------------------------

    async def _try_pairing(self, chat_id: int, user_id: int, message_id: int | None, text: str) -> None:
        parts = text.split()
        supplied = parts[1].strip().upper() if len(parts) > 1 else ""
        if parts and parts[0].lower().startswith("/start") and supplied and self.pairing_code:
            if supplied.replace("-", "") == self.pairing_code.replace("-", ""):
                self.state.claim_owner(user_id)
                self.pairing_code = None
                log.info("owner claimed by %s", user_id)
                await self._reply(
                    chat_id,
                    "Paired. You own this instance and are on the allowlist.\n\n" + HELP,
                )
                return
        log.warning("failed pairing attempt from %s", user_id)
        await self._reply(
            chat_id,
            "This instance is unclaimed. Its operator can find the pairing code "
            "in <code>docker compose logs bot</code> and send "
            "<code>/start &lt;code&gt;</code>.",
        )

    # ---- cookie ------------------------------------------------------

    async def _handle_cookie(self, chat_id: int, user_id: int, message_id: int | None, text: str) -> None:
        # Delete first: the value is on Telegram's servers until we do (PLAN §7).
        deleted = await self.tg.delete_message(chat_id, message_id) if message_id else False

        if user_id != self.state.owner_id:
            await self._reply(chat_id, "Only the owner can set the cookie.")
            return

        value = text.split(None, 1)[1].strip() if text.startswith("/cookie") and " " in text else text.strip()
        if not value or not looks_like_cookie(value):
            await self._reply(
                chat_id,
                "That doesn't look like an XHS cookie (no <code>web_session</code> / "
                "<code>a1</code>).\n\n" + COOKIE_INSTRUCTIONS,
            )
            return

        self.state.set_cookie(value)
        self.xhs.set_cookie(value)
        warnings = []
        if not deleted:
            warnings.append("⚠️ I could not delete your message — delete it manually.")
        if "web_session=" not in value:
            # a1/webId/acw_tc are device cookies a logged-out browser hands out
            # freely. Storing one and calling it healthy is how an afternoon
            # gets spent wondering why nothing changed.
            warnings.append(
                "⚠️ There is no <code>web_session</code> in that, which means it is <b>not "
                "a logged-in session</b> — just the anonymous device cookies any browser "
                "gets. It will not open anything a cookieless fetch cannot. Copy the "
                "<code>Cookie</code> header again from a tab where you are actually signed in."
            )
        await self._reply(
            chat_id,
            f"Cookie stored ({len(value)} chars) and marked healthy."
            + ("\n\n" + "\n\n".join(warnings) if warnings else ""),
        )

    async def _handle_acres_cookie(
        self, chat_id: int, user_id: int, message_id: int | None, text: str
    ) -> None:
        # Delete first: the value is on Telegram's servers until we do (PLAN §7).
        deleted = await self.tg.delete_message(chat_id, message_id) if message_id else False

        if user_id != self.state.owner_id:
            await self._reply(chat_id, "Only the owner can set the 1point3acres cookie.")
            return
        if not self.acres:
            await self._reply(chat_id, "1point3acres support is switched off here.")
            return

        value = text.split(None, 1)[1].strip() if text.startswith("/acres") and " " in text else text
        cookie, user_agent = parse_credentials(value)
        if not cookie or not looks_like_acres_cookie(cookie):
            await self._reply(
                chat_id,
                "I couldn't find a 1point3acres session in that (no "
                "<code>cf_clearance</code> or forum auth cookie).\n\n" + ACRES_INSTRUCTIONS,
            )
            return

        self.state.set_acres_cookie(cookie, user_agent)
        if self.acres_sender:
            self.acres_sender.set_headers(**self.acres.headers(cookie, user_agent))
        warnings = []
        if not deleted:
            warnings.append("⚠️ I could not delete your message — delete it manually.")
        if not user_agent:
            # Without the browser's own UA the Cloudflare pass is a coin flip,
            # and the failure looks like "the cookie is wrong" instead.
            warnings.append(
                "⚠️ No User-Agent came with that, so I'll use a default one. If threads "
                "come back as a Cloudflare challenge, resend it as a <b>Copy as cURL</b> "
                "paste — <code>cf_clearance</code> only works for the browser that earned it."
            )
        await self._reply(
            chat_id,
            f"1point3acres cookie stored ({len(cookie)} chars"
            + (", with its User-Agent" if user_agent else "")
            + ")." + ("\n\n" + "\n\n".join(warnings) if warnings else ""),
        )

    async def _handle_acres_link(
        self,
        chat_id: int | None,
        message_id: int | None,
        link: str,
        user_id: int | None = None,
        *,
        announce_to: tuple[int, int | None] | None = None,
        publish: bool = True,
    ) -> None:
        """Fetch a forum thread and deliver it.

        Same contract as the note flow: `chat_id` is where the submitter is
        waiting — None for a group submission, which produces no progress and
        no errors — and `announce_to` is who to tell about the finished post.
        `publish` False keeps the thread in the DM it came from even though a
        channel is configured (/mode private).
        """
        started = time.monotonic()
        # Everything downstream asks "are we curating a channel?" — which for
        # one submission also means "is this one meant for it?".
        channel = self.channel if publish else None
        key = thread_id(link) or link
        thread = self.threads.get(key)
        cached = thread is not None
        if thread is None:
            async with self._busy(chat_id, "typing") if chat_id else _nothing():
                try:
                    thread = await self.acres.thread(
                        link, self.state.acres_cookie, self.state.acres_ua
                    )
                except AcresError as exc:
                    await self._handle_acres_error(chat_id, message_id, exc, user_id)
                    return
            self.state.mark_acres_success()
            self.threads.put(key, thread)

        items = [MediaItem("photo", url) for url in thread.images]
        in_replies = sum(len(comment.images) for comment in thread.comments)
        log.info(
            "thread %s: %d char(s), %d image(s) (+%d in replies), %d repl(ies)%s, requested by %s",
            thread.tid or "?",
            len(thread.body),
            len(items),
            in_replies,
            len(thread.comments),
            " [cached]" if cached else "",
            user_id or chat_id or "?",
            extra=fields(
                event="note",
                site="1p3a",
                note=thread.tid,
                kind="thread",
                items=len(items),
                reply_items=in_replies,
                comments=len(thread.comments),
                cached=cached,
                locked=thread.locked or thread.needs_login,
            ),
        )

        # In channel mode a link is a submission, so a resubmission points at
        # the existing post. The key is namespaced: a forum tid is a short
        # number and a note id a long hex string, but sharing one index between
        # two sites without saying which is which invites exactly one very
        # confusing bug.
        published_key = acres_key(thread.tid)
        if channel:
            already = self.state.published(published_key)
            if already:
                link_back = self.message_link(already["message_id"])
                log.info(
                    "thread %s was already published: %s", thread.tid, link_back,
                    extra=fields(
                        event="duplicate", site="1p3a", note=thread.tid, url=link_back
                    ),
                )
                where, in_reply_to = announce_to or (chat_id, message_id)
                await self._reply(
                    where,
                    f'Already on the channel — <a href="{link_back}">see the post</a>.'
                    if link_back
                    else "That one is already on the channel.",
                    reply_to=in_reply_to,
                )
                return

        # Where it goes: the channel if we are curating one, else back to
        # whoever asked. A channel post cannot reply to a user's message.
        target = channel["id"] if channel else chat_id
        if target is None:  # a group submission with nowhere to publish
            return
        reply_to = None if channel else message_id

        if self.telegraph:
            page = await self._publish_page(thread)
            if page:
                sent = await self._send_and_track(
                    target, _page_message(thread, page), reply_to=reply_to, preview=True
                )
                log.info(
                    "delivered thread %s as %s in %.1fs",
                    thread.tid or "?", page, time.monotonic() - started,
                    extra=fields(
                        event="delivery", site="1p3a", note=thread.tid, mode="telegraph",
                        url=page, items=len(items),
                        seconds=round(time.monotonic() - started, 1),
                    ),
                )
                if channel:
                    where, in_reply_to = announce_to or (chat_id, message_id)
                    await self._announce_published(
                        where, in_reply_to, published_key, sent, site="1p3a"
                    )
                return
            # Falling through on purpose: a telegra.ph outage should cost the
            # nicer format, not the thread.

        # No images means no album, and then the first message is a message
        # rather than a caption — four times the room for the post's text.
        parts = -(-len(items) // MEDIA_GROUP_LIMIT) if items else 0
        marker = len(f"[{parts}/{parts}] ") if parts > 1 else 0
        limit = CAPTION_LIMIT if items else MESSAGE_LIMIT
        head, overflow = render_thread(thread, limit=limit, reserve=marker)
        # Same order of precedence as a note: the post's own text has first
        # claim, replies take what is left, and if the post spills over at all
        # they move to the follow-up wholesale rather than splitting.
        if overflow:
            trailing = thread.comments
        else:
            head, trailing = fit_into_caption(
                head, thread.comments, limit=limit - marker, like=ACRES_LIKE
            )

        first: int | None = None
        previous: int | None = None
        if items:
            try:
                async with self._busy(chat_id, "upload_photo") if chat_id else _nothing():
                    report = await self.acres_sender.send(
                        target, items, head, reply_to=reply_to
                    )
            except TelegramError as exc:
                log.exception("send failed for thread %s", thread.tid)
                await self._reply(
                    chat_id,
                    "Telegram refused the images: " + f"<code>{escape(exc.description)}</code>",
                    reply_to=message_id,
                )
                report = None
            if report and report.sent:
                previous = report.first_message_id
                if report.skipped:
                    log.warning(
                        "thread %s skipped %s", thread.tid or "?", "; ".join(report.skipped),
                        extra=fields(
                            event="media_skipped", site="1p3a", note=thread.tid,
                            count=len(report.skipped),
                        ),
                    )
            else:
                # The text is the point of a forum post; images failing must not
                # take it down with them.
                previous = await self._send_and_track(target, head, reply_to=reply_to)
        else:
            previous = await self._send_and_track(target, head, reply_to=reply_to)
        first = previous

        for piece in self._follow_up(overflow, trailing, limit=MESSAGE_LIMIT, like=ACRES_LIKE):
            sent = await self._send_and_track(target, piece, reply_to=previous)
            previous = sent or previous

        # Pictures that belong to replies rather than to the post, in an album
        # of their own so the credit lands on whoever posted them.
        gallery, gallery_caption = reply_gallery(thread.comments)
        if gallery:
            try:
                async with self._busy(chat_id, "upload_photo") if chat_id else _nothing():
                    await self.acres_sender.send(
                        target,
                        [MediaItem("photo", url) for url in gallery],
                        gallery_caption,
                        reply_to=previous,
                    )
            except TelegramError as exc:
                # The thread itself is already delivered; a failed gallery is
                # a missing extra, not a failed submission.
                log.warning(
                    "reply gallery failed for thread %s: %s", thread.tid, exc.description,
                    extra=fields(event="media_skipped", site="1p3a", note=thread.tid,
                                 count=len(gallery)),
                )

        log.info(
            "delivered thread %s in %.1fs",
            thread.tid or "?",
            time.monotonic() - started,
            extra=fields(
                event="delivery",
                site="1p3a",
                note=thread.tid,
                items=len(items),
                seconds=round(time.monotonic() - started, 1),
            ),
        )
        if channel:
            where, in_reply_to = announce_to or (chat_id, message_id)
            await self._announce_published(where, in_reply_to, published_key, first, site="1p3a")

    async def _publish_page(self, thread: Thread) -> str | None:
        """The thread as one Telegraph page, or None if that did not work out.

        Never raises: the message-per-chunk delivery is right behind it, and a
        thread the reader can have in a worse format beats no thread.
        """
        if thread.page:
            return thread.page
        try:
            token = self.state.telegraph_token
            if not token:
                token = await self.telegraph.create_account("1p3a", "1point3acres")
                self.state.set_telegraph_token(token)
                log.info("created a telegra.ph account for this instance")
            content = trim(
                to_nodes(thread), tail=source_nodes(thread), note=TRIMMED_NOTE
            )
            thread.page = await self.telegraph.create_page(
                token,
                thread.title,
                content,
                author_name=thread.author,
                author_url=thread.author_url,
            )
            return thread.page
        except TelegraphError as exc:
            log.warning(
                "telegra.ph refused thread %s: %s", thread.tid or "?", exc,
                extra=fields(event="telegraph_failed", site="1p3a", note=thread.tid,
                             detail=str(exc)[:200]),
            )
            return None

    async def _handle_acres_error(
        self, chat_id: int, message_id: int | None, exc: AcresError, user_id: int | None = None
    ) -> None:
        log.warning(
            "1point3acres fetch failed (%s): %s", exc.kind, exc,
            extra=fields(event="fetch_failed", site="1p3a", kind=exc.kind, detail=str(exc)[:200]),
        )
        if exc.kind == "bad_link":
            await self._reply(
                chat_id, "I couldn't read a thread id out of that.", reply_to=message_id
            )
            return
        if exc.kind == "network":
            await self._reply(
                chat_id, "1point3acres isn't answering. Try again shortly.", reply_to=message_id
            )
            return
        if exc.kind == "empty":
            await self._reply(
                chat_id,
                "That thread's opening post has nothing I can send.",
                reply_to=message_id,
            )
            return

        # "challenge" or "login": both mean the stored session no longer works,
        # and both are fixed the same way.
        wall = (
            "Cloudflare answered with a challenge instead of the thread."
            if exc.kind == "challenge"
            else "The forum served a login notice instead of the thread."
        )
        if user_id == self.state.owner_id:
            await self._reply(
                chat_id,
                f"{wall} "
                + (
                    "The stored cookie has expired or was earned by a different browser.\n\n"
                    if self.state.acres_cookie
                    else "I have no 1point3acres cookie stored.\n\n"
                )
                + ACRES_INSTRUCTIONS,
                reply_to=message_id,
            )
        else:
            await self._reply(
                chat_id,
                f"{wall} Ask the bot's owner to refresh the 1point3acres session.",
                reply_to=message_id,
            )
        if self.state.acres_cookie and self.state.mark_acres_cookie_stale():
            await self._notify_owner(
                "⚠️ A 1point3acres fetch hit the wall, so I've marked that cookie "
                "<b>stale</b>.\n\n" + ACRES_INSTRUCTIONS
            )

    # ---- status ------------------------------------------------------

    def _help_text(self, user_id: int) -> str:
        base = OWNER_HELP if user_id == self.state.owner_id else HELP
        if not self.channel:
            return base
        where = (
            f"@{self.channel['username']}"
            if self.channel["username"]
            else escape(self.channel["title"])
        )
        if self._publishes(user_id):
            opening = (
                f"Send me a Xiaohongshu share link to submit it to <b>{where}</b>. If the "
                "fetch works I'll publish it there and send you a link to the post."
            )
        else:
            opening = (
                "Send me a Xiaohongshu share link and I'll post the note back as native "
                f"media, here only — nothing you send goes to <b>{where}</b>."
            )
        return base.replace(
            "Send me a Xiaohongshu share link and I'll post the note back as native media.",
            opening,
            1,
        ).replace(
            "/status — ",
            "/mode — whether your links are submissions or stay in this chat\n/status — ",
            1,
        )

    async def _handle_groups(self, chat_id: int, user_id: int, command: str, text: str) -> None:
        if user_id != self.state.owner_id:
            await self._reply(chat_id, "Owner only.")
            return

        groups = self.state.groups
        if command == "/groups":
            if not groups:
                await self._reply(
                    chat_id,
                    "Not watching any groups. Add me to one — links posted there become "
                    "submissions, and I stay silent.",
                )
                return
            listing = "\n".join(
                f"<code>{cid}</code> — {escape(str(info.get('title') or '?'))}"
                for cid, info in groups.items()
            )
            await self._reply(chat_id, f"<b>Watching</b>\n{listing}")
            return

        parts = text.split()
        if len(parts) < 2:
            await self._reply(chat_id, f"Usage: <code>{command} &lt;chat_id&gt;</code>")
            return
        try:
            target = int(parts[1])
        except ValueError:
            await self._reply(chat_id, "That is not a chat id.")
            return

        if command == "/denygroup":
            removed = self.state.deny_group(target)
            await self._reply(
                chat_id, f"{target} {'is no longer watched' if removed else 'was not watched'}."
            )
            return

        title = str(target)
        try:
            chat = await self.tg.get_chat(target)
            title = chat.get("title") or title
        except TelegramError as exc:
            await self._reply(chat_id, f"I can't see that chat: <code>{escape(exc.description)}</code>")
            return
        added = self.state.allow_group(target, title, user_id)
        await self._reply(
            chat_id,
            f"Watching <b>{escape(title)}</b>." if added else f"Already watching {escape(title)}.",
        )

    async def _handle_status(self, chat_id: int, user_id: int | None = None) -> None:
        healthy = await self.xhs.healthy()
        uptime = int(time.monotonic() - self.started_at)
        cookie_line = {
            "unset": "none (unauthenticated fetches)",
            "ok": f"ok, set {_age(self.state.data.get('cookie_set_at'))}",
            "stale": f"⚠️ stale since a failed fetch, set {_age(self.state.data.get('cookie_set_at'))}",
        }[self.state.cookie_status]
        lines = [
            "<b>Status</b>",
            f"cookie: {cookie_line}",
            f"last successful fetch: {_age(self.state.data.get('last_successful_fetch'))}",
            f"downloader: {'reachable' if healthy else '⚠️ unreachable'}",
            f"media: {'streaming through the bot' if self.sender.streaming else 'CDN URL passthrough'}",
            f"cache: {len(self.notes)} notes ({self.notes.hits} hit / {self.notes.misses} miss), "
            f"{len(self.file_ids)} file ids",
            *(
                [
                    "1point3acres: "
                    + {
                        "unset": "no cookie (threads will hit the Cloudflare wall)",
                        "ok": f"ok, set {_age(self.state.data.get('acres_cookie_set_at'))}",
                        "stale": "⚠️ stale since a failed fetch, set "
                        + _age(self.state.data.get("acres_cookie_set_at")),
                    }[self.state.acres_cookie_status]
                    + (", with UA" if self.state.acres_ua else "")
                ]
                if self.acres
                else []
            ),
            *(
                [
                    "your DMs: "
                    + (
                        "submitted to the channel"
                        if self._publishes(user_id)
                        else "answered here only"
                    )
                ]
                if self.channel
                else []
            ),
            f"allowlist: {len(self.state.allowlist)} user(s)",
            f"watching: {len(self.state.groups)} group(s)",
            *([f"fetching via: {escape(self.config.proxy)}"] if self.config.proxy else []),
            *(
                [
                    f"channel: {escape(self.channel['title'])}"
                    + (f" (@{self.channel['username']})" if self.channel["username"] else " (private)")
                    + f", {len(self.state.data.get('published') or {})} published"
                ]
                if self.channel
                else (["channel: ⚠️ configured but not usable"] if self.config.channel_id else [])
            ),
            f"uptime: {uptime // 3600}h {uptime % 3600 // 60}m",
        ]
        await self._reply(chat_id, "\n".join(lines))

    # ---- admin -------------------------------------------------------

    async def _handle_admin(self, chat_id: int, user_id: int, command: str, text: str) -> None:
        if user_id != self.state.owner_id:
            await self._reply(chat_id, "Owner only.")
            return
        if command == "/users":
            listing = "\n".join(
                f"• <code>{uid}</code>{' (owner)' if uid == self.state.owner_id else ''}"
                for uid in self.state.allowlist
            )
            await self._reply(chat_id, f"<b>Allowlist</b>\n{listing}")
            return
        parts = text.split()
        if len(parts) < 2 or not parts[1].lstrip("-").isdigit():
            await self._reply(chat_id, f"Usage: <code>{command} &lt;telegram_user_id&gt;</code>")
            return
        target = int(parts[1])
        if command == "/allow":
            added = self.state.allow(target)
            await self._reply(chat_id, f"{target} {'added' if added else 'was already on the list'}.")
        else:
            removed = self.state.deny(target)
            await self._reply(
                chat_id,
                f"{target} removed." if removed else f"{target} is not removable (unknown, or the owner).",
            )

    # ---- the note flow (PLAN §4) -------------------------------------

    def _warn_owner_cookieless(self) -> bool:
        """True once per process, so a busy chat doesn't spam the owner."""
        if self._told_owner_cookieless:
            return False
        self._told_owner_cookieless = True
        return True

    @contextlib.asynccontextmanager
    async def _busy(self, chat_id: int, action: str = "upload_photo"):
        """Hold a chat action up for as long as the work takes.

        Telegram expires an action after ~5s, but a video note can take the
        better part of a minute to fetch and stream through. Without a refresh
        the user watches a silent chat and assumes the bot died.
        """

        async def beat() -> None:
            while True:
                await asyncio.sleep(self._beat_interval)
                await self.tg.send_chat_action(chat_id, action)

        # Fire the first one inline so the indicator is up before any awaiting.
        await self.tg.send_chat_action(chat_id, action)
        task = asyncio.create_task(beat())
        try:
            yield task
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task

    async def _handle_link(
        self,
        chat_id: int | None,
        message_id: int | None,
        link: str,
        user_id: int | None = None,
        *,
        announce_to: tuple[int, int | None] | None = None,
        publish: bool = True,
    ) -> None:
        """Fetch a note and deliver it.

        `chat_id` is where the submitter is waiting — None for a group
        submission, which produces no progress or error output at all.
        `announce_to` is (chat, message) to tell about the finished post, which
        for a group submission is the message that carried the link.
        `publish` False keeps the note in the DM it came from even though a
        channel is configured (/mode private).
        """
        started = time.monotonic()
        channel = self.channel if publish else None
        key = cache_key(link)
        note = self.notes.get(key)
        cached = note is not None
        if note is None:
            async with self._busy(chat_id) if chat_id else _nothing():
                try:
                    note = await self.xhs.detail(link, self.state.cookie)
                except XhsError as exc:
                    await self._handle_fetch_error(chat_id, message_id, exc, user_id)
                    return
            self.state.mark_fetch_success()
            self.notes.put(key, note)
            if note.note_id:
                self.notes.put(note.note_id, note)

        items = note.media(self.config.live_photos)
        log.info(
            "note %s: %s, %d media item(s)%s, requested by %s",
            note.note_id or "?",
            note.kind,
            len(items),
            " [cached]" if cached else "",
            user_id or chat_id or "?",
            extra=fields(
                event="note",
                note=note.note_id,
                kind=note.kind,
                items=len(items),
                cached=cached,
                origin="page" if note.from_page else "sidecar",
            ),
        )
        if not items:
            await self._reply(chat_id, "That note has no media I can send.", reply_to=message_id)
            return

        # In channel mode a link is a submission, so a resubmission should point
        # at the existing post rather than duplicate it.
        if channel:
            already = self.state.published(note.note_id)
            if already:
                link_back = self.message_link(already["message_id"])
                log.info(
                    "note %s was already published: %s", note.note_id, link_back,
                    extra=fields(event="duplicate", note=note.note_id, url=link_back),
                )
                where, in_reply_to = announce_to or (chat_id, message_id)
                await self._reply(
                    where,
                    f'Already on the channel — <a href="{link_back}">see the post</a>.'
                    if link_back
                    else "That one is already on the channel.",
                    reply_to=in_reply_to,
                )
                return

        comments = await self._comments(note)
        # Reading the page can turn up smaller renditions of an oversized
        # video, so the album is rebuilt now that they are known. Without this
        # the sender is handed items that were assembled before the fetch.
        items = note.media(self.config.live_photos)

        # A split album prefixes "[1/2] "; keep room so the caption still fits.
        parts = -(-len(items) // MEDIA_GROUP_LIMIT)
        marker = len(f"[{parts}/{parts}] ") if parts > 1 else 0
        budget = CAPTION_LIMIT - marker
        # The note's own text has first claim on the caption. Comments only get
        # what it leaves — and if the text spills into a follow-up message at
        # all, they move there wholesale rather than splitting the thread.
        caption, overflow = build_caption(
            note, tags=self.config.tags_in_caption, reserve=marker
        )
        # Reserve room for `continues ↓` only when normal note/comment text
        # actually continues. Comment-picture albums are replies to that text,
        # not members of its continuation chain, so pictures alone do not spend
        # caption room on a link they do not need.
        continuation_reserved = bool(channel and parts > 1)
        if continuation_reserved:
            caption, overflow = build_caption(
                note, tags=self.config.tags_in_caption, reserve=marker + CONTINUED_COST
            )
        if overflow:
            if channel and not continuation_reserved:
                caption, overflow = build_caption(
                    note, tags=self.config.tags_in_caption, reserve=marker + CONTINUED_COST
                )
                continuation_reserved = True
            trailing = comments
        else:
            caption, trailing = fit_into_caption(
                caption,
                comments,
                limit=budget - (CONTINUED_COST if continuation_reserved else 0),
            )
            if channel and trailing and not continuation_reserved:
                caption, overflow = build_caption(
                    note, tags=self.config.tags_in_caption, reserve=marker + CONTINUED_COST
                )
                continuation_reserved = True
                if overflow:
                    trailing = comments
                else:
                    caption, trailing = fit_into_caption(
                        caption, comments, limit=budget - CONTINUED_COST
                    )

        # Where the album goes: the channel if we're curating one, else back to
        # whoever asked. A channel post can't reply to a user's message.
        target = channel["id"] if channel else chat_id
        if target is None:  # a group submission with nowhere to publish
            return
        reply_to = None if channel else message_id

        # The follow-up text: the note's overflow, then comments. When the
        # album itself splits, the groups after the first are captions in
        # search of text — a photo-overflow group carrying only its "[2/2]"
        # marker is a message spent on nothing — so the overflow flows into
        # those captions first and only what is left becomes a message.
        # (Seen live on a 15-photo note: /gradient_canopy/1137 sent [2/2] with
        # an 18-unit caption and then a third message for the overflow.)
        carry_budgets = (
            [CAPTION_LIMIT - marker - (CONTINUED_COST if channel else 0)] * (parts - 1)
            if parts > 1
            else []
        )
        room = MESSAGE_LIMIT - (CONTINUED_COST if channel else 0)
        pieces = self._follow_up(overflow, trailing, budgets=carry_budgets, limit=room)
        carry = pieces[: len(carry_budgets)]

        action = "upload_video" if any(item.kind == "video" for item in items) else "upload_photo"
        try:
            async with self._busy(chat_id, action) if chat_id else _nothing():
                report = await self.sender.send(
                    target, items, caption, reply_to=reply_to, followup_captions=carry
                )
        except TelegramError as exc:
            log.exception("send failed for note %s", note.note_id)
            await self._reply(
                chat_id,
                ("The channel refused the post: " if channel else "Telegram refused the media: ")
                + f"<code>{escape(exc.description)}</code>",
                reply_to=message_id,
            )
            if channel:
                await self._notify_owner(
                    "⚠️ A submission could not be published to "
                    f"<b>{escape(channel['title'])}</b>: "
                    f"<code>{escape(exc.description)}</code>"
                )
            return

        log.info(
            "delivered %d item(s) in %.1fs via %s%s",
            report.sent,
            time.monotonic() - started,
            "upload" if report.uploaded else "cached file_id/url",
            f", skipped {len(report.skipped)}" if report.skipped else "",
            extra=fields(
                event="delivery",
                note=note.note_id,
                items=report.sent,
                seconds=round(time.monotonic() - started, 1),
                mode="upload" if report.uploaded else "url",
                skipped=len(report.skipped),
            ),
        )
        if report.skipped:
            # The reasons used to go only to the submitter's chat, which a group
            # submission does not have — so they went nowhere at all.
            log.warning(
                "note %s skipped %s", note.note_id or "?", "; ".join(report.skipped),
                extra=fields(event="media_skipped", note=note.note_id, count=len(report.skipped)),
            )
        if not report.sent:
            log.error(
                "nothing was delivered for note %s", note.note_id or "?",
                extra=fields(event="delivery_empty", note=note.note_id),
            )
            await self._reply(
                chat_id,
                "I couldn't send any of that note's media: "
                + "; ".join(escape(s) for s in report.skipped),
                reply_to=message_id,
            )
            return
        # Everything else follows in the same chat, chained onto the post —
        # text the album's captions could not carry (a dropped group hands
        # its share back unused) plus whatever never fit in a caption.
        chain: list[tuple[int, str, bool]] = [(mid, cap, True) for mid, cap in report.parts]
        previous = report.parts[-1][0] if report.parts else report.first_message_id
        for piece in report.unused_captions + pieces[len(carry) :]:
            sent = await self._send_and_track(target, piece, reply_to=previous)
            if sent:
                chain.append((sent, piece, False))
                previous = sent

        # A comment's pictures, each set under its author's name. They all
        # reply to the note itself — or to the message that carries comments
        # after caption overflow — instead of forming an unrelated reply chain.
        # A refused or empty album costs its pictures, not the delivery: the
        # note is already out.
        comment_parent = previous
        links: dict[str, str] = {}
        delivered = 0
        albums_sent = 0
        for caption, urls in comment_albums(comments):
            try:
                async with self._busy(chat_id, "upload_photo") if chat_id else _nothing():
                    part = await self.sender.send(
                        target,
                        [MediaItem("photo", url) for url in urls],
                        caption,
                        reply_to=comment_parent,
                    )
            except TelegramError as exc:
                log.warning(
                    "comment pictures for %s were refused: %s",
                    note.note_id or "?", exc.description,
                    extra=fields(
                        event="media_skipped", note=note.note_id, count=len(urls)
                    ),
                )
                continue
            if not part.sent:
                continue
            delivered += part.sent
            albums_sent += 1
            # Each entry from comment_albums is one Telegram-sized group, so
            # the marker for its lead image names precisely this album.
            if channel and part.first_message_id:
                where = self.message_link(part.first_message_id)
                if where:
                    links[urls[0]] = where
        if delivered:
            log.info(
                "comment pictures for %s: %d photo(s) in %d album(s)",
                note.note_id or "?", delivered, albums_sent,
                extra=fields(
                    event="comment_gallery", note=note.note_id,
                    images=delivered, albums=albums_sent,
                ),
            )

        # A reply chain is invisible once a message is forwarded out of the
        # channel, so each normal message also carries a link to the next one.
        # Comment-image albums rely on their reply parent instead; marker edits
        # are folded into this pass without adding them to the chain.
        if channel and (len(chain) > 1 or links):
            await self._link_the_chain(target, chain, image_links=links)

        if report.skipped:
            await self._reply(chat_id, "Skipped: " + "; ".join(escape(s) for s in report.skipped))

        if channel:
            where, in_reply_to = announce_to or (chat_id, message_id)
            await self._announce_published(
                where, in_reply_to, note.note_id, report.first_message_id
            )

    @staticmethod
    def _follow_up(
        overflow: str,
        comments: list,
        *,
        budgets: Sequence[int] = (),
        limit: int = MESSAGE_LIMIT,
        like: str = "♥",
    ) -> list[str]:
        """Messages to send after the album: the rest of the text, then comments.

        The comments ride in the *last* piece when they fit, so a note
        that needed a second message doesn't also need a third.

        `budgets` are piece sizes that come ahead of the usual `limit` run:
        the caption budgets of an album's overflow groups, which carry text
        the group was going to waste on a "[2/2]" marker of its own. The
        overflow fills them in order, and whatever spills past — or arrives
        with none — is split at `limit` as before.
        """
        pieces: list[tuple[str, int]] = []  # (html, the budget it was sized to)
        remaining = overflow
        for budget in budgets:
            if not remaining:
                break
            head, remaining = tg_truncate(remaining, budget)
            if head:
                pieces.append((escape(head), budget))
        while remaining:
            head, remaining = tg_truncate(remaining, limit)
            if not head:  # pathological single token longer than the limit
                head, remaining = remaining[:limit], remaining[limit:]
            pieces.append((escape(head), limit))
        if comments:
            if pieces:
                body, budget = pieces[-1]
                room = budget - tg_len(strip_tags(body)) - 2
                block = render_comments(comments, limit=room, like=like)
                if block:
                    pieces[-1] = (f"{body}\n\n{block}", budget)
                    comments = []
            if comments:  # nothing to ride, or no room left in what there was
                budget = budgets[len(pieces)] if len(pieces) < len(budgets) else limit
                block = render_comments(comments, limit=budget - 16, like=like)
                if block:
                    pieces.append((block, budget))
        return [html for html, _budget in pieces]

    async def _announce_published(
        self,
        chat_id: int | None,
        message_id: int | None,
        key: str,
        first_message_id: int | None,
        *,
        site: str = "",
    ) -> None:
        """Tell the submitter where their entry landed."""
        if not first_message_id:
            # Nothing landed, so there is nothing to point at and nothing to
            # remember. Saying "published" here would be a lie.
            log.error("no message id for %s; not recording it", key or "?")
            return
        link = self.message_link(first_message_id)
        self.state.record_published(key, self.channel["id"], first_message_id)
        log.info(
            "published %s to %s: %s",
            key or "?",
            self.channel["title"],
            link or f"message {first_message_id}",
            extra=fields(
                event="published", note=key, message_id=first_message_id, url=link,
                **({"site": site} if site else {}),
            ),
        )
        title = escape(self.channel["title"])
        if link:
            body = f'Published to <b>{title}</b> — <a href="{link}">see the post</a>.'
        else:
            body = f"Published to <b>{title}</b>."
        await self._reply(chat_id, body, reply_to=message_id)

    async def _comments(self, note: Note) -> list:
        """Top comments for the caption. A failed scrape never fails a note."""
        if not self.config.comments:
            return []
        try:
            comments = await self.xhs.enrich(note, self.config.comments)
        except Exception:  # a garnish is not worth failing a delivery over
            log.exception("comment scrape failed for note %s", note.note_id)
            return []
        log.info(
            "comments for %s: %d fetched", note.note_id or "?", len(comments),
            extra=fields(
                event="comments", note=note.note_id, count=len(comments),
                images=sum(len(c.images) for c in comments),
            ),
        )
        return comments

    async def _handle_fetch_error(
        self, chat_id: int, message_id: int | None, exc: XhsError, user_id: int | None = None
    ) -> None:
        # Without this the user sees an error and the log shows nothing at all.
        log.warning(
            "fetch failed (%s): %s", exc.kind, exc,
            extra=fields(event="fetch_failed", kind=exc.kind, detail=str(exc)[:200]),
        )
        if exc.kind == "profile":
            await self._reply(
                chat_id,
                "That's someone's profile, not a note. Open the note you want and share "
                "that link instead.",
                reply_to=message_id,
            )
            return
        if exc.kind == "bad_link":
            await self._reply(chat_id, "I couldn't read a note link out of that.", reply_to=message_id)
            return
        if exc.kind == "network":
            await self._reply(chat_id, "The downloader sidecar isn't answering. Try again shortly.")
            return
        if exc.kind == "empty":
            await self._reply(chat_id, "That note came back without any media.", reply_to=message_id)
            return

        # "blocked": login wall, expired cookie, or rate limiting (PLAN §7).
        if not self.state.cookie:
            # Nothing to go stale — say what would actually fix it instead of
            # leaving the user to guess.
            await self._reply(
                chat_id,
                "XHS refused that fetch. I'm running <b>without a cookie</b>, and some notes "
                "are only readable when signed in — a cookie would likely fix this one.\n\n"
                + (
                    COOKIE_INSTRUCTIONS
                    if user_id == self.state.owner_id
                    else "Ask the bot's owner to add one."
                ),
                reply_to=message_id,
            )
            if user_id != self.state.owner_id and self._warn_owner_cookieless():
                await self._notify_owner(
                    "⚠️ A fetch was refused and I have <b>no cookie</b> stored. Some notes need "
                    "one.\n\n" + COOKIE_INSTRUCTIONS
                )
            return

        await self._reply(
            chat_id,
            "XHS refused that fetch — login wall, expired cookie, or rate limiting.",
            reply_to=message_id,
        )
        if self.state.mark_cookie_stale():
            await self._notify_owner(
                "⚠️ A fetch was refused, so I've marked the stored cookie <b>stale</b>.\n\n"
                + COOKIE_INSTRUCTIONS
            )
        elif not self.state.cookie and not self._warned_cookieless:
            self._warned_cookieless = True
            await self._notify_owner(
                "⚠️ An unauthenticated fetch was refused. Setting a cookie may help.\n\n"
                + COOKIE_INSTRUCTIONS
            )

    # ---- plumbing ----------------------------------------------------

    async def _notify_owner(self, text: str) -> None:
        if self.state.owner_id:
            await self._reply(self.state.owner_id, text)

    async def _reply(
        self,
        chat_id: int | str | None,
        text: str,
        *,
        reply_to: int | None = None,
        preview: bool = False,
    ) -> None:
        # A group submission has no one to answer: everything the bot would
        # have said is simply not said (and never leaks into the group).
        if chat_id is None:
            return
        try:
            await self.tg.send_message(chat_id, text, reply_to=reply_to, preview=preview)
        except TelegramError as exc:
            if reply_to and "reply message not found" in exc.description.lower():
                await self.tg.send_message(chat_id, text, preview=preview)
                return
            log.error("could not reply to %s: %s", chat_id, exc.description)

    async def aclose(self) -> None:
        await self.sender.aclose()
        if self.acres:
            await self.acres.aclose()
        if self.acres_sender:
            await self.acres_sender.aclose()
        if self.telegraph:
            await self.telegraph.aclose()
