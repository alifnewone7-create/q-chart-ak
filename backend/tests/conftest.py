"""Pytest bootstrap: env vars + sys.path so backend modules import cleanly."""
import os
import sys
from pathlib import Path

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

os.environ.setdefault("BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
os.environ.setdefault("ADMIN_ID", "1")
os.environ.setdefault("QUOTEX_EMAIL", "a@b.c")
os.environ.setdefault("QUOTEX_PASSWORD", "p")


# --- minimal async test support (pytest-asyncio is not installed in this pod) ---
import asyncio  # noqa: E402
import inspect  # noqa: E402


def pytest_configure(config):
    config.addinivalue_line("markers", "asyncio: run this coroutine test with asyncio.run")


class _OfflineBot:
    """Guard: no test may reach api.telegram.org. Legacy tests (test_bot_units.py)
    stub SessionManager.bot but not notifier.NOTIFY, which used to trigger a real
    HTTP call with the dummy token."""

    def __init__(self):
        self.calls = []

    async def send_message(self, chat_id, text, **kw):
        self.calls.append(("message", chat_id, text))
        return type("Msg", (), {"entities": None, "caption_entities": None})()

    async def send_photo(self, chat_id, photo, caption=None, **kw):
        self.calls.append(("photo", chat_id, caption))
        return type("Msg", (), {"entities": None, "caption_entities": None})()


import pytest  # noqa: E402


@pytest.fixture(autouse=True)
def _no_real_telegram_calls(request):
    import notifier
    previous = notifier.NOTIFY._bot
    if "construct_bot" in request.node.name:
        yield
        return
    if previous is None:
        notifier.NOTIFY._bot = _OfflineBot()
    yield
    notifier.NOTIFY._bot = previous


def pytest_pyfunc_call(pyfuncitem):
    func = pyfuncitem.obj
    if inspect.iscoroutinefunction(func):
        kwargs = {name: pyfuncitem.funcargs[name]
                  for name in pyfuncitem._fixtureinfo.argnames}
        asyncio.run(func(**kwargs))
        return True
    return None
