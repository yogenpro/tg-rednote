"""Offline QR custody, authenticated completion, and helper isolation checks."""

from __future__ import annotations

import asyncio
import base64
import json
import sys
import types
from contextlib import asynccontextmanager
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bot"))
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "login"))
from app.login import LoginClient, LoginError, LoginFlow
from worker import BrowserLogin, Service, allowed_url, cookie_header, proxy_settings, serve
from test_handlers import FakeTelegram, make_bot, message

PNG = b"\x89PNG\r\n\x1a\nqr-fixture"
COOKIE = "a1=device; web_session=private-session"


class Browser:
    def __init__(self, *, cookie=COOKIE, error=None):
        self.release = asyncio.Event()
        self.closed = False
        self.cookie = cookie
        self.error = error

    async def run(self, publish):
        try:
            publish(PNG)
            await self.release.wait()
            if self.error:
                raise self.error
            return self.cookie
        finally:
            self.closed = True


class Telegram(FakeTelegram):
    def __init__(self):
        super().__init__()
        self.photos = []
        self.deletion_ok = True

    async def call(self, method, payload, **kwargs):
        assert method == "sendPhoto"
        self.photos.append((payload, kwargs))
        return {"message_id": 401}

    async def delete_message(self, chat_id, message_id):
        self.deleted.append((chat_id, message_id))
        return self.deletion_ok


async def until(predicate):
    async with asyncio.timeout(2):
        while not predicate():
            await asyncio.sleep(0)


@pytest.mark.parametrize("url,allowed", [
    ("https://www.xiaohongshu.com/explore", True),
    ("https://sns-webpic.xhscdn.com/a", True),
    ("https://fe-s10.rednotecdn.com/login.js", True),
    ("https://www.rednote.com/explore", True),
    ("https://xiaohongshu.com.attacker.test", False),
    ("http://127.0.0.1/admin", False),
    ("http://169.254.169.254/latest/meta-data", False),
    ("file:///data/state.json", False),
    ("https://api.telegram.org", False),
])
def test_browser_destination_allowlist(url, allowed):
    assert allowed_url(url) is allowed


def test_cookie_requires_session_and_rejects_unrelated_domains_and_header_injection():
    cookie = lambda name, value, domain=".xiaohongshu.com", path="/": dict(
        name=name, value=value, domain=domain, path=path,
    )
    assert cookie_header([cookie("a1", "anonymous")]) == ""
    assert cookie_header([cookie("web_session", "wrong", "attacker.test")]) == ""
    assert cookie_header([cookie("web_session", "bad\r\nvalue")]) == ""
    assert cookie_header([cookie("web_session", "bad;value")]) == ""
    assert cookie_header([cookie("web_session", "x" * 32768)]) == ""
    assert cookie_header([
        cookie("web_session", "parent"),
        cookie("web_session", "specific", path="/explore"),
        cookie("extra", "secret", "example.com"),
        cookie("invalid name", "bad"),
    ]) == "web_session=specific"
    regional = [cookie("web_session", "regional", ".rednote.com")]
    assert cookie_header(regional) == ""
    assert cookie_header(regional, "rednote.com") == "web_session=regional"
    assert cookie_header(regional, "attacker.test") == ""


def test_proxy_credentials_separated_and_never_fall_back():
    assert proxy_settings("") is None
    assert proxy_settings("http://name:p%40ss@proxy.test:8888") == {
        "server": "http://proxy.test:8888", "username": "name", "password": "p@ss",
        "bypass": "<-loopback>",
    }
    with pytest.raises(ValueError):
        proxy_settings("file:///bad")


async def test_worker_one_session_and_cookie_delivered_only_once_to_capability_holder():
    browser = Browser()
    service = Service(browser)
    try:
        assert await service.request({"action": "health"}) == {"status": "ready"}
        assert service.session is None
        started = await service.request({"action": "begin"})
        token = started["session"]
        await until(lambda: service.session.status == "pending")
        assert (await service.request({"action": "begin"}))["status"] == "busy"
        assert (await service.request({"action": "poll", "session": "wrong"}))["status"] == "gone"
        response = await service.request({"action": "poll", "session": token})
        assert "cookie" not in response
        assert base64.b64decode(response["qr"]) == PNG
        browser.release.set()
        await until(lambda: service.session.status == "success")
        response = await service.request({"action": "poll", "session": token})
        assert response == {"status": "success", "cookie": COOKIE}
        assert browser.closed and service.session is None
        assert (await service.request({"action": "poll", "session": token}))["status"] == "gone"
    finally:
        await service.close()


@pytest.mark.parametrize("outcome", ["expiry", "cancel", "error", "anonymous"])
async def test_worker_failure_discards_qr_and_cookie(outcome):
    browser = Browser(cookie="a1=anonymous" if outcome == "anonymous" else COOKIE,
                      error=ValueError("cookie=must-not-escape") if outcome == "error" else None)
    service = Service(browser, lifetime=0.01 if outcome == "expiry" else 180)
    try:
        token = (await service.request({"action": "begin"}))["session"]
        await until(lambda: service.session.status == "pending")
        if outcome == "cancel":
            assert (await service.request({"action": "cancel", "session": token}))["status"] == "cancelled"
        else:
            if outcome != "expiry":
                browser.release.set()
            await until(lambda: service.session.status in {"expired", "failed"})
            response = await service.request({"action": "poll", "session": token})
            assert response == {"status": "expired" if outcome == "expiry" else "failed"}
        assert browser.closed and service.session is None
    finally:
        await service.close()


async def test_abandoned_worker_session_is_erased_without_bot_polling():
    browser = Browser()
    service = Service(browser, lifetime=0.01, retention=0.01)
    await service.request({"action": "begin"})
    await until(lambda: service.session is None)
    assert browser.closed


async def test_socket_flow_success_preserves_cookie_until_confirmed_and_deletes_private_qr(tmp_path):
    browser = Browser()
    service = Service(browser)
    stop = asyncio.Event()
    path = tmp_path / "socket" / "login.sock"
    server = asyncio.create_task(serve(service, path, stop))
    tg, stored = Telegram(), ["old-cookie"]
    flow = LoginFlow(LoginClient(str(path)), tg, stored.append, interval=0.001)
    try:
        await until(lambda: path.exists() and path.stat().st_mode & 0o777 == 0o600)
        assert path.stat().st_mode & 0o777 == 0o600
        await flow.start(1)
        await until(lambda: tg.photos)
        assert stored == ["old-cookie"]
        assert tg.photos[0][0]["chat_id"] == 1
        assert tg.photos[0][0]["protect_content"] is True
        assert tg.photos[0][1]["files"]["photo"][1] == PNG
        await flow.start(1)
        assert "already running" in tg.texts
        browser.release.set()
        await flow.task
        assert stored == ["old-cookie", COOKIE]
        assert tg.deleted == [(1, 401)]
        assert "Sign-in confirmed" in tg.texts and COOKIE not in tg.texts
        assert service.session is None and browser.closed
    finally:
        await flow.cancel()
        stop.set()
        await server
    assert not path.exists()


class Client:
    def __init__(self, result):
        self.result = result
        self.calls = []
        self.release = asyncio.Event()

    async def request(self, action, session=""):
        self.calls.append((action, session))
        if action == "begin":
            return {"session": "capability"}
        if action == "cancel":
            return {"status": "cancelled"}
        await self.release.wait()
        return self.result


@pytest.mark.parametrize("result", [
    {"status": "expired"}, {"status": "failed"}, {"status": "gone"},
    {"status": "success", "cookie": "a1=anonymous"},
    {"status": "success", "cookie": "web_session=value\r\nInjected: yes"},
    {"status": "pending", "qr": "not base64"},
])
async def test_unsuccessful_or_invalid_helper_response_keeps_existing_cookie(result):
    client, tg, saved = Client(result), Telegram(), []
    client.release.set()
    flow = LoginFlow(client, tg, saved.append, interval=0)
    await flow.start(1)
    await flow.task
    assert saved == []
    assert client.calls[-1] == ("cancel", "capability")
    assert "existing cookie was kept" in tg.texts.lower()


async def test_cancel_and_timeout_close_remote_session_without_cookie_update():
    for timeout in (0.01, 200):
        client, tg, saved = Client({}), Telegram(), []
        flow = LoginFlow(client, tg, saved.append, timeout=timeout)
        await flow.start(1)
        await until(lambda: len(client.calls) == 2)
        if timeout == 200:
            await flow.cancel()
        else:
            await flow.task
            assert "timed out" in tg.texts
        assert saved == []
        assert client.calls[-1] == ("cancel", "capability")


async def test_failed_qr_deletion_is_reported():
    client, tg = Client({"status": "pending", "qr": base64.b64encode(PNG).decode()}), Telegram()
    tg.deletion_ok = False
    client.release.set()
    flow = LoginFlow(client, tg, lambda _: None, interval=0)
    await flow.start(1)
    await until(lambda: tg.photos)
    await flow.cancel()
    assert "delete it manually" in tg.texts


async def test_manual_cookie_import_cancels_real_pending_flow_before_it_can_overwrite(tmp_path):
    bot, tg, state = make_bot(tmp_path, owner=1)
    client = Client({"status": "success", "cookie": COOKIE})
    bot.login = LoginFlow(client, tg, bot._save_login_cookie, interval=0)
    await bot.handle_update(message("/login"))
    await until(lambda: len(client.calls) == 2)
    imported = "web_session=" + "manual" * 10
    await bot.handle_update(message("/cookie " + imported))
    client.release.set()
    await asyncio.sleep(0)
    assert state.cookie == imported and bot.xhs.cookie == imported
    assert client.calls[-1] == ("cancel", "capability")
    assert bot.login.task is None
    await bot.aclose()


async def test_concurrent_login_commands_create_only_one_remote_session():
    client, tg = Client({}), Telegram()
    flow = LoginFlow(client, tg, lambda _: None)
    await asyncio.gather(flow.start(1), flow.start(1), flow.start(1))
    await until(lambda: len(client.calls) == 2)
    assert sum(action == "begin" for action, _ in client.calls) == 1
    assert tg.texts.count("already running") == 2
    await asyncio.gather(flow.cancel(), flow.cancel())
    assert client.calls[-1] == ("cancel", "capability") and flow.task is None


async def test_cookie_storage_failure_restores_committed_state_and_reports_separately(tmp_path, monkeypatch):
    bot, tg, state = make_bot(tmp_path, owner=1)
    state.set_cookie("web_session=old")
    bot.xhs.set_cookie(state.cookie)
    client = Client({"status": "success", "cookie": COOKIE})
    client.release.set()
    bot.login = LoginFlow(client, tg, bot._save_login_cookie, interval=0)

    def fail():
        raise OSError("This error must not expose " + COOKIE)

    monkeypatch.setattr(state, "save", fail)
    await bot.handle_update(message("/login"))
    await bot.login.task
    assert state.cookie == bot.xhs.cookie == "web_session=old"
    assert "saving the cookie failed" in tg.texts
    assert COOKIE not in tg.texts
    await bot.aclose()


async def test_absent_helper_reports_safe_error(tmp_path):
    tg = Telegram()
    flow = LoginFlow(LoginClient(str(tmp_path / "absent.sock")), tg, lambda _: pytest.fail("unexpected save"))
    await flow.start(1)
    await flow.task
    assert "existing cookie was kept" in tg.texts
    assert not tg.photos


async def test_owner_only_private_command_and_cookie_replacement_cancel(tmp_path):
    bot, tg, state = make_bot(tmp_path, owner=1)

    class Login:
        starts = 0
        cancellations = 0

        async def start(self, chat_id):
            assert chat_id == 1
            self.starts += 1

        async def cancel(self):
            self.cancellations += 1

    login = bot.login = Login()
    state.allow(2)
    for command in ("/login", "/login_cancel", "/forgetcookie"):
        await bot.handle_update(message(command, user=2))
    assert login.starts == login.cancellations == 0
    group = message("/login", user=1, chat=-100123)
    group["message"]["chat"]["type"] = "supergroup"
    await bot.handle_update(group)
    assert login.starts == 0
    await bot.handle_update(message("/login"))
    assert login.starts == 1
    await bot.handle_update(message("/login_cancel"))
    assert login.cancellations == 1
    cookie = "web_session=" + "x" * 40
    await bot.handle_update(message("/cookie " + cookie))
    assert login.cancellations == 2 and state.cookie == cookie
    await bot.handle_update(message("/forgetcookie"))
    assert login.cancellations == 3 and state.cookie is None
    await bot.aclose()
    assert login.cancellations == 4


async def test_browser_reloads_and_checks_user_before_exporting_cookie():
    class Locator:
        async def screenshot(self, **kwargs):
            return PNG

        async def is_visible(self):
            return True

        async def wait_for(self, **kwargs):
            pass

    class Page:
        url = "https://www.xiaohongshu.com/explore"
        reloads = 0
        checks = 0

        def locator(self, _):
            return Locator()

        async def evaluate(self, _):
            self.checks += 1
            return True

        async def goto(self, url, **kwargs):
            self.reloads += 1

    class Context:
        async def cookies(self, urls):
            return [{"domain": ".xiaohongshu.com", "name": "web_session", "value": "confirmed"}]

    browser, page = BrowserLogin(""), Page()

    @asynccontextmanager
    async def open_page():
        yield page, Context()

    browser.open = open_page
    seen = []
    assert await browser.run(seen.append) == "web_session=confirmed"
    assert seen == [PNG] and page.reloads == 1 and page.checks == 2


async def test_regional_browser_uses_dedicated_login_page_and_scrubs_inherited_secrets(monkeypatch):
    launches, visits, routes, closed = [], [], {}, []

    class Page:
        url = ""

        def set_default_timeout(self, timeout):
            pass

        async def goto(self, url, **kwargs):
            visits.append(url)
            self.url = "https://www.rednote.com/explore" if len(visits) == 1 else url

    class Context:
        async def route(self, pattern, callback):
            routes["http"] = callback

        async def route_web_socket(self, pattern, callback):
            routes["websocket"] = callback

        async def new_page(self):
            return Page()

    class Browser:
        async def new_context(self, **kwargs):
            assert kwargs == {"accept_downloads": False, "service_workers": "block"}
            return Context()

        async def close(self):
            closed.append(True)

    async def launch(**kwargs):
        launches.append(kwargs)
        return Browser()

    @asynccontextmanager
    async def playwright():
        yield types.SimpleNamespace(chromium=types.SimpleNamespace(launch=launch))

    monkeypatch.setitem(sys.modules, "playwright", types.ModuleType("playwright"))
    module = types.ModuleType("playwright.async_api")
    module.async_playwright = playwright
    monkeypatch.setitem(sys.modules, "playwright.async_api", module)
    monkeypatch.setenv("TG_BOT_TOKEN", "must-not-reach-browser")
    monkeypatch.setenv("HTTP_PROXY", "http://must-not-leak.test")
    async with BrowserLogin("http://intended.test:8888").open():
        pass
    assert visits == ["https://www.xiaohongshu.com/explore", "https://www.rednote.com/login"]
    assert closed == [True]
    assert launches[0]["proxy"]["server"] == "http://intended.test:8888"
    assert set(launches[0]["env"]) <= {"PATH", "HOME", "LANG"}
    assert "must-not" not in json.dumps(launches)

    class Route:
        def __init__(self, url):
            self.request = types.SimpleNamespace(url=url)
            self.result = ""

        async def continue_(self):
            self.result = "allowed"

        async def abort(self):
            self.result = "blocked"

    for url, expected in [("https://fe-s10.rednotecdn.com/login.js", "allowed"),
                          ("http://169.254.169.254/latest/meta-data", "blocked")]:
        route = Route(url)
        await routes["http"](route)
        assert route.result == expected
