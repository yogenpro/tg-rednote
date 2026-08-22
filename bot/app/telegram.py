"""A small, direct Bot API client.

Written against the HTTP API rather than a framework because the interesting part
of this bot is switching a single media group between URL passthrough and
multipart stream-through (PLAN §2.2), which is a one-line difference here.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

import httpx

log = logging.getLogger(__name__)

# Telegram limits we code against (PLAN §8).
MEDIA_GROUP_LIMIT = 10
CAPTION_LIMIT = 1024
MESSAGE_LIMIT = 4096

# Descriptions Telegram returns when *its* servers could not fetch a URL we
# handed it. Observed against XHS: WEBPAGE_CURL_FAILED for a whole media group,
# WEBPAGE_MEDIA_EMPTY for an individual item in one. This list is for
# diagnostics — the fallback itself does not depend on it being complete.
URL_FETCH_FAILURES = (
    "failed to get http url content",
    "wrong file identifier/http url specified",
    "webpage_curl_failed",
    "webpage_media_empty",
    "media_empty",
    "image_process_failed",
    "wrong type of the web page content",
    "file must be non-empty",
    "photo_invalid_dimensions",
    "wrong remote file identifier",
    "wrong padding in the string",
    "http url specified",
)


class TelegramError(RuntimeError):
    def __init__(self, method: str, error_code: int, description: str, parameters: dict | None = None):
        super().__init__(f"{method} failed [{error_code}]: {description}")
        self.method = method
        self.error_code = error_code
        self.description = description
        self.parameters = parameters or {}

    @property
    def failed_index(self) -> int | None:
        """Which item of a media group Telegram choked on, if it said.

        Album errors read `failed to send message #3 with the error message …`,
        numbered from one. Knowing which item failed means only that item's CDN
        can be blamed, instead of everything that happened to travel with it.
        """
        match = re.search(r"failed to send message #(\d+)", self.description)
        return int(match.group(1)) if match else None

    @property
    def is_url_fetch_failure(self) -> bool:
        lowered = self.description.lower()
        return self.error_code == 400 and any(p in lowered for p in URL_FETCH_FAILURES)


class Telegram:
    def __init__(self, token: str, api_base: str = "https://api.telegram.org", timeout: float = 60.0):
        self._token = token
        self._url = f"{api_base}/bot{token}"
        # trust_env=False is the one proxy invariant worth making unbreakable.
        # Everything else the bot fetches is proxied by default (PROXY, exported
        # into the environment at startup), and httpx picks env proxies up
        # silently — so without this, turning the proxy on would quietly route
        # Telegram, uploads included, down a home link that has no business
        # carrying them.
        self._client = httpx.AsyncClient(timeout=timeout, trust_env=False)

    async def aclose(self) -> None:
        await self._client.aclose()

    def _redact(self, text: str) -> str:
        return text.replace(self._token, "<token>")

    async def call(
        self,
        method: str,
        payload: dict[str, Any] | None = None,
        files: dict[str, tuple] | None = None,
        *,
        timeout: float | None = None,
        retries: int = 3,
    ) -> Any:
        payload = {k: v for k, v in (payload or {}).items() if v is not None}
        attempt = 0
        while True:
            attempt += 1
            try:
                if files:
                    data = {
                        k: (v if isinstance(v, str) else json.dumps(v, ensure_ascii=False))
                        for k, v in payload.items()
                    }
                    response = await self._client.post(
                        f"{self._url}/{method}", data=data, files=files, timeout=timeout
                    )
                else:
                    response = await self._client.post(
                        f"{self._url}/{method}", json=payload, timeout=timeout
                    )
            except httpx.HTTPError as exc:
                if attempt > retries:
                    raise TelegramError(method, 0, self._redact(repr(exc))) from None
                await asyncio.sleep(min(2 ** attempt, 15))
                continue

            try:
                body = response.json()
            except ValueError:
                body = {"ok": False, "error_code": response.status_code, "description": response.text[:200]}

            if body.get("ok"):
                return body.get("result")

            error = TelegramError(
                method,
                body.get("error_code", response.status_code),
                body.get("description", "unknown error"),
                body.get("parameters"),
            )
            if error.error_code == 429:
                delay = int(error.parameters.get("retry_after", 5))
                log.warning("rate limited on %s, sleeping %ss", method, delay)
                await asyncio.sleep(delay + 1)
                continue
            if error.error_code >= 500 and attempt <= retries:
                await asyncio.sleep(min(2 ** attempt, 15))
                continue
            raise error

    # ---- convenience wrappers ---------------------------------------

    async def get_me(self) -> dict:
        return await self.call("getMe")

    async def get_chat(self, chat_id: int | str) -> dict:
        return await self.call("getChat", {"chat_id": chat_id})

    async def get_chat_member(self, chat_id: int | str, user_id: int) -> dict:
        return await self.call("getChatMember", {"chat_id": chat_id, "user_id": user_id})

    async def get_updates(self, offset: int | None, timeout: int) -> list[dict]:
        return await self.call(
            "getUpdates",
            {"offset": offset, "timeout": timeout, "allowed_updates": ["message", "my_chat_member"]},
            timeout=timeout + 15,
            retries=0,
        )

    async def send_message(
        self, chat_id: int | str, text: str, *, reply_to: int | None = None, preview: bool = False
    ) -> dict:
        return await self.call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "parse_mode": "HTML",
                "reply_to_message_id": reply_to,
                "link_preview_options": {"is_disabled": not preview},
            },
        )

    async def send_chat_action(self, chat_id: int | str, action: str) -> None:
        try:
            await self.call("sendChatAction", {"chat_id": chat_id, "action": action}, retries=0)
        except TelegramError:
            pass  # cosmetic only

    async def edit_message_caption(self, chat_id: int | str, message_id: int, caption: str) -> dict:
        return await self.call(
            "editMessageCaption",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "caption": caption,
                "parse_mode": "HTML",
            },
        )

    async def edit_message_text(self, chat_id: int | str, message_id: int, text: str) -> dict:
        return await self.call(
            "editMessageText",
            {
                "chat_id": chat_id,
                "message_id": message_id,
                "text": text,
                "parse_mode": "HTML",
                "link_preview_options": {"is_disabled": True},
            },
        )

    async def delete_message(self, chat_id: int, message_id: int) -> bool:
        try:
            await self.call("deleteMessage", {"chat_id": chat_id, "message_id": message_id}, retries=0)
            return True
        except TelegramError as exc:
            log.warning("could not delete message: %s", exc.description)
            return False
