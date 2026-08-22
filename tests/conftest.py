import asyncio
import inspect
import os

import pytest


def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: run this coroutine test on a fresh event loop")


@pytest.hookimpl(tryfirst=True)
def pytest_pyfunc_call(pyfuncitem):
    """Tiny stand-in for pytest-asyncio, so the suite has no plugin dependency."""
    test = pyfuncitem.obj
    if not inspect.iscoroutinefunction(test):
        return None
    kwargs = {name: pyfuncitem.funcargs[name] for name in pyfuncitem._fixtureinfo.argnames}
    asyncio.run(test(**kwargs))
    return True


@pytest.fixture(autouse=True)
def isolate_proxy_env():
    """No developer's shell decides where the bot's clients think they dial.

    httpx reads HTTP_PROXY/HTTPS_PROXY/ALL_PROXY from the environment unless a
    client opts out, and the bot now exports those itself at startup — so a
    test that builds a client is asking a question the ambient environment can
    answer wrongly. Snapshot and restore rather than monkeypatch: what the
    proxy tests exercise is precisely a process-wide write to os.environ.
    """
    names = (
        "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
        "http_proxy", "https_proxy", "all_proxy", "no_proxy",
    )
    saved = {name: os.environ.get(name) for name in names}
    for name in names:
        os.environ.pop(name, None)
    yield
    for name, value in saved.items():
        if value is None:
            os.environ.pop(name, None)
        else:
            os.environ[name] = value
