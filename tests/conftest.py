import asyncio
import inspect

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
