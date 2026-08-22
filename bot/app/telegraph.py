"""Publishing a long post as a Telegraph page.

PLAN §2.1 rejected Telegraph for RedNote, and that still holds there: a note
*is* its images, and answering with a link to a web page is the thing this bot
exists to avoid. A forum thread inverts the trade — thousands of words with
the pictures incidental — so 1point3acres publishes to telegra.ph and sends one
link, which Telegram opens with Instant View and no round trip to a browser.

Two things about the API are worth knowing. `createPage` takes `author_name`
and `author_url` per page, so each thread keeps its own author's name rather
than the bot's. And an external image `src` is stored and rendered verbatim
(verified against oss.1p3a.com), so the forum's pictures need no re-hosting.

Pages are public to anyone with the URL. That is the point of the feature, and
it is also the reason it is off the group and channel paths entirely.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Union

import httpx

log = logging.getLogger(__name__)

API_BASE = "https://api.telegra.ph"
TITLE_LIMIT = 256
# Telegraph refuses content past 64 KB of encoded JSON. Staying under it is
# the caller's problem, so `trim` is offered here rather than discovered as a
# CONTENT_TOO_BIG at publish time.
CONTENT_LIMIT = 64 * 1024

Node = Union[str, dict[str, Any]]


class TelegraphError(RuntimeError):
    pass


def encode(content: list[Node]) -> str:
    return json.dumps(content, ensure_ascii=False)


def trim(
    content: list[Node], limit: int = CONTENT_LIMIT, *, tail: list[Node] = (), note: str = ""
) -> list[Node]:
    """Drop nodes off the end until the page fits.

    Whole nodes go rather than a slice through the middle of one, so the page
    ends on a complete paragraph. Callers order content worst-to-lose last —
    replies after the post — which makes this a priority list. `tail` is kept
    whatever happens: the link back to the original is the last thing a
    truncated page should lose.
    """
    tail = list(tail)
    if len(encode(content + tail).encode()) <= limit:
        return content + tail
    cut: list[Node] = (
        [{"tag": "p", "children": [{"tag": "em", "children": [note]}]}] if note else []
    )
    room = limit - len(encode(cut + tail).encode()) - 16
    kept: list[Node] = []
    for node in content:
        if len(encode(kept + [node]).encode()) > room:
            break
        kept.append(node)
    return kept + cut + tail


class Telegraph:
    def __init__(self, timeout: float = 30.0, base_url: str = API_BASE):
        self._base = base_url.rstrip("/")
        # Exempt from PROXY for the same reason as Telegram, and pinned the same
        # way: publishing a page wants a reliable link, not a residential IP.
        # See the Telegram client on why trust_env=False rather than a setting.
        self._client = httpx.AsyncClient(timeout=timeout, trust_env=False)

    async def aclose(self) -> None:
        await self._client.aclose()

    async def _call(self, method: str, payload: dict[str, Any]) -> Any:
        try:
            response = await self._client.post(f"{self._base}/{method}", data=payload)
            body = response.json()
        except httpx.HTTPError as exc:
            raise TelegraphError(f"telegra.ph unreachable: {type(exc).__name__}") from None
        except ValueError:
            raise TelegraphError("telegra.ph returned a non-JSON response") from None
        if not body.get("ok"):
            raise TelegraphError(str(body.get("error") or "unknown error"))
        return body.get("result")

    async def create_account(self, short_name: str, author_name: str = "") -> str:
        result = await self._call(
            "createAccount",
            {"short_name": short_name[:32], "author_name": author_name[:128]},
        )
        token = (result or {}).get("access_token")
        if not token:
            raise TelegraphError("createAccount returned no token")
        return token

    async def create_page(
        self,
        token: str,
        title: str,
        content: list[Node],
        *,
        author_name: str = "",
        author_url: str = "",
    ) -> str:
        payload = {
            "access_token": token,
            # An empty title is rejected, and a thread with no subject is not
            # worth failing over.
            "title": (title.strip() or "Untitled")[:TITLE_LIMIT],
            "content": encode(content),
            "return_content": "false",
        }
        if author_name:
            payload["author_name"] = author_name[:128]
        # Telegraph validates the URL and refuses the whole page over a bad
        # one, which is a poor reason to lose a thread.
        if author_url.startswith(("http://", "https://")):
            payload["author_url"] = author_url[:512]
        result = await self._call("createPage", payload)
        url = (result or {}).get("url")
        if not url:
            raise TelegraphError("createPage returned no url")
        return url
