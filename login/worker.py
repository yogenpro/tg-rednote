"""Single-purpose, ephemeral XHS QR browser. No TCP listener or bot credentials.

The container boundary isolates Chromium from the bot's state and token. Chromium
runs without its own namespace sandbox because Docker's default seccomp blocks
user namespaces; never run this worker in the bot container or with host mounts.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import json
import os
import re
import secrets
import signal
from dataclasses import dataclass, field
from pathlib import Path
from urllib.parse import unquote, urlsplit

LOGIN_URL = "https://www.xiaohongshu.com/explore"
QR_SELECTOR = ".login-container .qrcode-img"
USER_SELECTOR = ".main-container .user .link-wrapper .channel"
SESSION_SECONDS = 180
RESULT_SECONDS = 30
COOKIE_LIMIT = 32768
QR_LIMIT = 512 * 1024
ALLOWED_DOMAINS = ("xiaohongshu.com", "rednote.com", "xhscdn.com", "rednotecdn.com", "xhsimg.com", "xiaohongshu.net")
COOKIE_NAME = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")

# Requiring an account id, authenticated DOM and a session cookie avoids
# treating freely-issued anonymous device cookies as a completed login.
CURRENT_USER = """() => {
  const u = window.__INITIAL_STATE__?.user;
  const info = u?.userInfo?.value ?? u?.userInfo;
  const id = info?.userId || info?.user_id;
  return !!info && !info.guest && typeof id === 'string' && id.length > 0;
}"""


def allowed_url(url: str) -> bool:
    parsed = urlsplit(url)
    host = parsed.hostname or ""
    return parsed.scheme in {"https", "http"} and any(
        host == domain or host.endswith("." + domain) for domain in ALLOWED_DOMAINS
    )


def cookie_header(cookies: list[dict], domain: str = "xiaohongshu.com") -> str:
    if domain not in {"xiaohongshu.com", "rednote.com"}:
        return ""
    values: dict[str, str] = {}
    for cookie in sorted(cookies, key=lambda c: -len(c.get("path", "/"))):
        cookie_domain = cookie.get("domain", "").lstrip(".")
        if cookie_domain not in {domain, "www." + domain}:
            continue
        name, value = cookie.get("name", ""), cookie.get("value", "")
        if not COOKIE_NAME.fullmatch(name) or any(ord(c) < 0x21 or ord(c) > 0x7e or c == ";" for c in value):
            continue
        values.setdefault(name, value)
    if not values.get("web_session"):
        return ""
    header = "; ".join(f"{name}={value}" for name, value in values.items())
    return header if len(header) <= COOKIE_LIMIT else ""


def proxy_settings(proxy: str) -> dict | None:
    if not proxy:
        return None
    parsed = urlsplit(proxy)
    if parsed.scheme not in {"http", "https", "socks5"} or not parsed.hostname:
        raise ValueError("Unsupported proxy")
    host = parsed.hostname
    if ":" in host:
        host = f"[{host}]"
    result = {"server": f"{parsed.scheme}://{host}:{parsed.port or (443 if parsed.scheme == 'https' else 80)}", "bypass": "<-loopback>"}
    if parsed.username:
        result["username"] = unquote(parsed.username)
        result["password"] = unquote(parsed.password or "")
    return result


class BrowserLogin:
    """Used only by this isolated worker; browser imports are intentionally lazy."""

    def __init__(self, proxy: str):
        self.proxy = proxy_settings(proxy)

    @contextlib.asynccontextmanager
    async def open(self):
        from playwright.async_api import async_playwright

        # No inherited bot/configuration credentials, proxy env or debug traces.
        env = {name: os.environ[name] for name in ("PATH", "HOME", "LANG") if name in os.environ}
        async with async_playwright() as playwright:
            browser = await playwright.chromium.launch(
                executable_path="/usr/bin/chromium", headless=True,
                chromium_sandbox=False, proxy=self.proxy, env=env, timeout=20000,
                args=["--disable-quic", "--force-webrtc-ip-handling-policy=disable_non_proxied_udp"],
            )
            try:
                context = await browser.new_context(accept_downloads=False, service_workers="block")

                async def route(request_route):
                    if allowed_url(request_route.request.url):
                        await request_route.continue_()
                    else:
                        await request_route.abort()

                await context.route("**/*", route)
                await context.route_web_socket("**/*", lambda socket: socket.close())
                page = await context.new_page()
                page.set_default_timeout(10000)
                await page.goto(LOGIN_URL, wait_until="domcontentloaded", timeout=30000)
                # Regional RedNote's explore page does not open a login dialog.
                # Its dedicated login page uses the same QR element as XHS.
                if urlsplit(page.url).hostname in {"rednote.com", "www.rednote.com"}:
                    await page.goto("https://www.rednote.com/login", wait_until="domcontentloaded", timeout=30000)
                yield page, context
            finally:
                await asyncio.wait_for(browser.close(), timeout=5)

    async def run(self, publish_qr):
        async with self.open() as (page, context):
            qr = await page.locator(QR_SELECTOR).screenshot(timeout=20000)
            if len(qr) > QR_LIMIT:
                raise ValueError("QR image too large")
            publish_qr(qr)
            while True:
                if await page.locator(USER_SELECTOR).is_visible() and await page.evaluate(CURRENT_USER):
                    # Confirm the server still recognizes this session on a fresh load.
                    host = urlsplit(page.url).hostname
                    if host not in {"xiaohongshu.com", "www.xiaohongshu.com", "rednote.com", "www.rednote.com"}:
                        raise ValueError("Unexpected login domain")
                    await page.goto(f"https://{host}/explore", wait_until="domcontentloaded", timeout=30000)
                    await page.locator(USER_SELECTOR).wait_for(state="visible", timeout=10000)
                    if await page.evaluate(CURRENT_USER):
                        host = urlsplit(page.url).hostname
                        domain = "rednote.com" if host in {"rednote.com", "www.rednote.com"} else "xiaohongshu.com"
                        cookie = cookie_header(await context.cookies([page.url]), domain)
                        if cookie:
                            return cookie
                await asyncio.sleep(1)


@dataclass
class Session:
    token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    status: str = "starting"
    qr: str = ""
    cookie: str = field(default="", repr=False)
    task: asyncio.Task | None = field(default=None, repr=False)
    expiry: asyncio.Task | None = field(default=None, repr=False)


class Service:
    def __init__(self, browser, *, lifetime=SESSION_SECONDS, retention=RESULT_SECONDS):
        self.browser = browser
        self.lifetime = lifetime
        self.retention = retention
        self.session: Session | None = None
        self.lock = asyncio.Lock()

    async def run(self, session):
        try:
            async with asyncio.timeout(self.lifetime):
                def publish(qr):
                    session.qr = base64.b64encode(qr).decode("ascii")
                    session.status = "pending"

                cookie = await self.browser.run(publish)
                # Enforce the session marker at the service boundary as well.
                if not cookie or len(cookie) > COOKIE_LIMIT or not any(
                    part.strip().startswith("web_session=") and part.strip() != "web_session="
                    for part in cookie.split(";")
                ):
                    raise ValueError("Missing authenticated cookie")
                session.cookie = cookie
                session.status = "success"
        except TimeoutError:
            session.status = "expired"
        except asyncio.CancelledError:
            raise
        except Exception:
            # Playwright errors can contain page content, URLs and cookies.
            session.status = "failed"
        finally:
            session.qr = ""

    async def expire(self, session):
        await asyncio.sleep(self.lifetime + self.retention)
        async with self.lock:
            if self.session is session:
                await self._clear()

    async def _clear(self):
        session, self.session = self.session, None
        if not session:
            return
        for task in (session.task, session.expiry):
            if task and task is not asyncio.current_task():
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await task
        session.cookie = session.qr = ""

    async def request(self, request):
        async with self.lock:
            action = request.get("action")
            if action == "health":
                return {"status": "ready"}
            if action == "begin":
                if self.session:
                    return {"status": "busy"}
                session = self.session = Session()
                session.task = asyncio.create_task(self.run(session))
                session.expiry = asyncio.create_task(self.expire(session))
                return {"status": "starting", "session": session.token}
            session = self.session
            if not session or not secrets.compare_digest(str(request.get("session", "")), session.token):
                return {"status": "gone"}
            if action == "cancel":
                await self._clear()
                return {"status": "cancelled"}
            if action != "poll":
                return {"error": "Unknown action"}
            response = {"status": session.status}
            if session.status == "pending":
                response["qr"] = session.qr
            if session.status == "success":
                response["cookie"] = session.cookie
            if session.status in {"success", "failed", "expired"}:
                await self._clear()
            return response

    async def close(self):
        async with self.lock:
            await self._clear()


async def serve(service: Service, path: Path, stop: asyncio.Event):
    async def connection(reader, writer):
        try:
            async with asyncio.timeout(12):
                request = json.loads(await reader.readline())
                if not isinstance(request, dict):
                    raise ValueError("Invalid request")
                response = await service.request(request)
                writer.write(json.dumps(response).encode() + b"\n")
                await writer.drain()
        except (ValueError, OSError, TimeoutError):
            pass
        finally:
            writer.close()
            with contextlib.suppress(OSError):
                await writer.wait_closed()

    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    path.unlink(missing_ok=True)
    server = await asyncio.start_unix_server(connection, str(path), limit=4096)
    path.chmod(0o600)
    try:
        async with server:
            await stop.wait()
    finally:
        await service.close()
        path.unlink(missing_ok=True)


async def main():
    os.umask(0o077)
    # A worker misconfigured with the bot's environment fails closed.
    if os.environ.get("TG_BOT_TOKEN") or Path("/data/state.json").exists():
        raise SystemExit("QR helper must not have bot credentials or state")
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, stop.set)
    service = Service(BrowserLogin(os.environ.get("PROXY") or os.environ.get("XHS_PROXY", "")))
    await serve(service, Path("/run/xhs-login/login.sock"), stop)


if __name__ == "__main__":
    asyncio.run(main())
