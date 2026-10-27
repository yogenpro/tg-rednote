"""Private Unix-socket client; the browser never receives the bot token."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json


class LoginError(Exception):
    pass


class LoginSaveError(Exception):
    pass


class LoginClient:
    def __init__(self, socket_path: str):
        self.socket_path = socket_path

    async def request(self, action: str, session: str = "") -> dict:
        writer = None
        try:
            async with asyncio.timeout(10):
                reader, writer = await asyncio.open_unix_connection(
                    self.socket_path, limit=2 * 1024 * 1024,
                )
                writer.write(json.dumps({"action": action, "session": session}).encode() + b"\n")
                await writer.drain()
                response = json.loads(await reader.readline())
                if not isinstance(response, dict) or response.get("error"):
                    raise LoginError("Login helper refused the request")
                return response
        except (OSError, ValueError, TimeoutError):
            # Socket errors and untrusted responses must not reach crash logs.
            raise LoginError("Login helper is unavailable") from None
        finally:
            if writer:
                writer.close()
                with contextlib.suppress(OSError):
                    await writer.wait_closed()


class LoginFlow:
    """One owner DM session, independent of media uploads and polling."""

    def __init__(self, client, telegram, save_cookie, *, timeout=200, interval=1):
        self.client = client
        self.tg = telegram
        self.save_cookie = save_cookie
        self.timeout = timeout
        self.interval = interval
        self.task: asyncio.Task | None = None
        self.lock = asyncio.Lock()

    async def start(self, chat_id):
        async with self.lock:
            if self.task and not self.task.done():
                await self.tg.send_message(chat_id, "A QR login is already running. Use /login_cancel to cancel it.")
                return
            self.task = asyncio.create_task(self._run(chat_id))

    async def cancel(self):
        async with self.lock:
            if self.task and not self.task.done():
                self.task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await self.task
            self.task = None

    async def _run(self, chat_id):
        session = ""
        qr_message = None
        outcome = "QR login failed. The existing cookie was kept. Try /login again or use /cookie."
        delete_failed = False
        try:
            async with asyncio.timeout(self.timeout):
                await self.tg.send_message(chat_id, "Preparing a Xiaohongshu login QR…")
                started = await self.client.request("begin")
                if started.get("status") == "busy":
                    outcome = "The login helper is finishing another session. Try /login again in three minutes."
                else:
                    session = started.get("session", "")
                    if not isinstance(session, str) or not session or len(session) > 128:
                        raise LoginError("Invalid login session")
                    while True:
                        response = await self.client.request("poll", session)
                        status = response.get("status")
                        if status == "pending" and qr_message is None:
                            encoded = response.get("qr", "")
                            if not isinstance(encoded, str) or len(encoded) > 700000:
                                raise LoginError("Invalid QR image")
                            qr = base64.b64decode(encoded, validate=True)
                            if not qr.startswith(b"\x89PNG\r\n\x1a\n") or len(qr) > 512 * 1024:
                                raise LoginError("Invalid QR image")
                            sent = await self.tg.call(
                                "sendPhoto",
                                {"chat_id": chat_id, "protect_content": True,
                                 "caption": "Scan with your Xiaohongshu / RedNote app and approve sign-in. This QR expires within three minutes. /login_cancel cancels."},
                                files={"photo": ("login.png", qr, "image/png")}, retries=0,
                            )
                            qr_message = sent["message_id"]
                        elif status == "success":
                            cookie = response.get("cookie", "")
                            if not isinstance(cookie, str) or len(cookie) > 32768 or any(
                                ord(c) < 0x20 or ord(c) > 0x7e for c in cookie
                            ) or not any(
                                part.strip().startswith("web_session=") and part.strip() != "web_session="
                                for part in cookie.split(";")
                            ):
                                raise LoginError("Missing authenticated cookie")
                            self.save_cookie(cookie)
                            outcome = "Sign-in confirmed. The new cookie is stored and ready for fetches."
                            break
                        elif status in {"expired", "gone"}:
                            outcome = "QR login expired. The existing cookie was kept. Use /login for a new QR."
                            break
                        elif status not in {"starting", "pending"}:
                            break
                        await asyncio.sleep(self.interval)
        except asyncio.CancelledError:
            raise
        except TimeoutError:
            outcome = "QR login timed out. The existing cookie was kept. Use /login to try again."
        except LoginSaveError:
            outcome = "Sign-in was confirmed, but saving the cookie failed. Check /status before trying /login again."
        except Exception:
            # Neither browser/IPC errors nor Telegram errors may expose cookies.
            pass
        finally:
            # Bound cleanup even if the helper or Telegram is unavailable.
            if session:
                with contextlib.suppress(Exception):
                    async with asyncio.timeout(3):
                        await self.client.request("cancel", session)
            if qr_message is not None:
                try:
                    async with asyncio.timeout(3):
                        delete_failed = not await self.tg.delete_message(chat_id, qr_message)
                except Exception:
                    delete_failed = True
                if delete_failed:
                    with contextlib.suppress(Exception):
                        async with asyncio.timeout(2):
                            await self.tg.send_message(chat_id, "I could not delete the login QR. Please delete it manually.")
        with contextlib.suppress(Exception):
            async with asyncio.timeout(10):
                await self.tg.send_message(chat_id, outcome)
