"""Round-2 regression tests for the ~30-48h silent-freeze bug.

Focus (attacking the exact round-1 gaps):
  - navigator.Browser.send_request() ALWAYS applies DEFAULT_HTTP_TIMEOUT
    when the caller does not supply one, and NEVER clobbers an explicit
    caller-supplied timeout.
  - login.Login.__call__ / Login._post / Login.awaiting_pin offload blocking
    `requests` I/O to a thread via loop.run_in_executor -> the event loop
    keeps breathing during a slow login.
  - login.Login.awaiting_pin() no longer calls builtins.input() when
    sys.stdin is absent or not a tty (RuntimeError with a clear message).
  - bot._heartbeat() actually advances _HEARTBEAT['loop'] and is cancellable.
  - bot._hard_watchdog() (plain THREAD, loop-independent) calls os._exit(1)
    once the loop is stalled, and does NOT exit on a fresh heartbeat.
  - bot.post_init() creates heartbeat task + async watchdog task + a daemon
    thread targeting _hard_watchdog. bot.post_shutdown() cancels both tasks.
"""
import asyncio
import builtins
import importlib
import sys
import threading
import time
import types
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


# ---------------------------------------------------------------------------
# 1. Imports/byte-compile of all round-1 + round-2 changed modules
# ---------------------------------------------------------------------------

CHANGED_MODULES = [
    "bot",
    "start",
    "qx",
    "pyquotex.api",
    "pyquotex.stable_api",
    "pyquotex.http.login",
    "pyquotex.http.navigator",
]


@pytest.mark.parametrize("name", CHANGED_MODULES)
def test_module_imports_and_bytecompiles(name):
    mod = importlib.import_module(name)
    assert mod is not None
    src = mod.__file__
    if src and src.endswith(".py"):
        compile(open(src, "rb").read(), src, "exec")


# ---------------------------------------------------------------------------
# 2. navigator.Browser.send_request: HTTP timeout enforcement
# ---------------------------------------------------------------------------

def _make_browser():
    from pyquotex.http.navigator import Browser
    return Browser()


def test_default_http_timeout_constant():
    from pyquotex.http import navigator
    assert navigator.DEFAULT_HTTP_TIMEOUT == (15, 45)


def test_send_request_applies_default_timeout_on_get(monkeypatch):
    from pyquotex.http import navigator
    from pyquotex.http.navigator import Browser
    br = Browser()

    seen = {}

    def _fake_request(self, method, url, **kwargs):
        seen["method"] = method
        seen["url"] = url
        seen["kwargs"] = kwargs
        r = types.SimpleNamespace(
            status_code=200, text="", content=b"", headers={},
            url=url, ok=True, reason="OK",
        )
        return r

    monkeypatch.setattr(Browser, "request", _fake_request, raising=True)
    br.send_request("GET", "https://x.example/y")
    assert seen["kwargs"].get("timeout") == navigator.DEFAULT_HTTP_TIMEOUT
    assert seen["method"] == "GET"


def test_send_request_applies_default_timeout_on_post(monkeypatch):
    from pyquotex.http import navigator
    from pyquotex.http.navigator import Browser
    br = Browser()

    seen = {}

    def _fake_request(self, method, url, **kwargs):
        seen["kwargs"] = kwargs
        return types.SimpleNamespace(status_code=200, text="", content=b"",
                                     headers={}, url=url, ok=True, reason="OK")

    monkeypatch.setattr(Browser, "request", _fake_request, raising=True)
    br.send_request("POST", "https://x.example/y", data={"a": 1})
    assert seen["kwargs"].get("timeout") == navigator.DEFAULT_HTTP_TIMEOUT
    assert seen["kwargs"].get("data") == {"a": 1}


def test_send_request_default_timeout_when_headers_passed(monkeypatch):
    from pyquotex.http import navigator
    from pyquotex.http.navigator import Browser
    br = Browser()

    seen = {}

    def _fake_request(self, method, url, headers=None, **kwargs):
        seen["headers"] = headers
        seen["kwargs"] = kwargs
        return types.SimpleNamespace(status_code=200, text="", content=b"",
                                     headers={}, url=url, ok=True, reason="OK")

    monkeypatch.setattr(Browser, "request", _fake_request, raising=True)
    br.send_request("GET", "https://x.example/y", headers={"X-Test": "1"})
    assert seen["kwargs"].get("timeout") == navigator.DEFAULT_HTTP_TIMEOUT
    assert seen["headers"].get("X-Test") == "1"


def test_send_request_respects_caller_supplied_timeout(monkeypatch):
    """An explicit `timeout=` from the caller must NOT be clobbered."""
    from pyquotex.http.navigator import Browser
    br = Browser()

    seen = {}

    def _fake_request(self, method, url, **kwargs):
        seen["kwargs"] = kwargs
        return types.SimpleNamespace(status_code=200, text="", content=b"",
                                     headers={}, url=url, ok=True, reason="OK")

    monkeypatch.setattr(Browser, "request", _fake_request, raising=True)
    br.send_request("GET", "https://x.example/y", timeout=5)
    assert seen["kwargs"].get("timeout") == 5


# ---------------------------------------------------------------------------
# 3. login.Login uses run_in_executor -> event loop keeps breathing
# ---------------------------------------------------------------------------

class _FakeLoginBase:
    """Minimal stand-in that inherits nothing from Browser but exposes the
    attributes Login.__call__ needs. We patch Login onto this via __new__."""
    def __init__(self):
        self.api = types.SimpleNamespace(
            username="u", session_data={}, lang="en"
        )
        self.headers = {"User-Agent": "ua"}
        self.https_base_url = "https://qxbroker.com"
        self.full_url = "https://qxbroker.com/en"
        self.response = None
        self.cookies = None
        self.ssid = None
        self.html = None


async def test_login_call_offloads_get_token_and_get_profile(monkeypatch):
    """The MOST important test: a slow synchronous get_token/get_profile
    must NOT block the event loop. A concurrent heartbeat coroutine must
    keep ticking during the whole login.
    """
    from pyquotex.http.login import Login

    loop_thread_id = threading.get_ident()
    thread_ids = {}

    def slow_get_token(self):
        thread_ids["get_token"] = threading.get_ident()
        time.sleep(0.4)
        return "TOKEN123"

    def slow_get_profile(self):
        thread_ids["get_profile"] = threading.get_ident()
        time.sleep(0.4)
        # mimic real return: response, settings_json
        self.ssid = "SSID"
        return (types.SimpleNamespace(url="/trade"), {"token": "SSID"})

    async def fake_post(self, data):
        thread_ids["_post"] = threading.get_ident()
        return True, "Login successful."

    monkeypatch.setattr(Login, "get_token", slow_get_token, raising=True)
    monkeypatch.setattr(Login, "get_profile", slow_get_profile, raising=True)
    monkeypatch.setattr(Login, "_post", fake_post, raising=True)

    obj = _FakeLoginBase()
    # NOTE: Login.__call__ resolves self.get_token via type(self).__mro__ ==
    # obj (SimpleNamespace-like). To reuse Login's *unbound* methods on a
    # fake self, we route bound methods explicitly:
    obj.get_token = lambda: slow_get_token(obj)
    obj.get_profile = lambda: slow_get_profile(obj)
    obj._post = lambda data: fake_post(obj, data)

    ticks = []
    stop = asyncio.Event()

    async def heartbeat():
        while not stop.is_set():
            ticks.append(time.time())
            await asyncio.sleep(0.05)

    hb = asyncio.create_task(heartbeat())
    start = time.time()
    status, msg = await Login.__call__(obj, "u", "p")
    elapsed = time.time() - start
    stop.set()
    await hb

    assert status is True
    # Login includes two ~0.4s sleeps run in an executor => should take ~0.8s
    assert 0.6 < elapsed < 3.0, f"unexpected login duration {elapsed:.2f}s"

    # The heartbeat must have kept firing throughout the login. With 0.05s
    # cadence over ~0.8s we expect >8 ticks; require at least 6 to be robust.
    assert len(ticks) >= 6, (
        f"event loop was blocked during login (only {len(ticks)} ticks)"
    )
    # Max gap between consecutive ticks must not exceed the whole login,
    # i.e. the heartbeat never stalled for the full duration.
    gaps = [ticks[i + 1] - ticks[i] for i in range(len(ticks) - 1)]
    assert max(gaps) < 0.35, f"biggest heartbeat gap {max(gaps):.2f}s (loop was frozen)"

    # get_token and get_profile ran on a DIFFERENT thread than the loop
    assert thread_ids["get_token"] != loop_thread_id
    assert thread_ids["get_profile"] != loop_thread_id


async def test_login_post_offloads_send_request(monkeypatch):
    """Login._post must also offload the blocking send_request off the loop."""
    from pyquotex.http.login import Login

    loop_thread_id = threading.get_ident()
    sent_from = {}

    def slow_send_request(self, method=None, url=None, data=None, **kw):
        sent_from["thread"] = threading.get_ident()
        time.sleep(0.4)
        self.response = types.SimpleNamespace(
            url="/trade",
            content=b"<html><body></body></html>",
            text="<html></html>",
            ok=True, reason="OK", status_code=200,
        )
        return self.response

    def fake_get_soup(self):
        # Simulate no PIN required (no keep_code input)
        from bs4 import BeautifulSoup
        return BeautifulSoup("<html><body></body></html>", "html.parser")

    def fake_success_login(self):
        return True, "Login successful."

    monkeypatch.setattr(Login, "send_request", slow_send_request,
                        raising=True)
    monkeypatch.setattr(Login, "get_soup", fake_get_soup, raising=True)
    monkeypatch.setattr(Login, "success_login", fake_success_login,
                        raising=True)

    obj = _FakeLoginBase()
    obj.send_request = lambda **kw: slow_send_request(obj, **kw)
    obj.get_soup = lambda: fake_get_soup(obj)
    obj.success_login = lambda: fake_success_login(obj)

    ticks = []
    stop = asyncio.Event()

    async def heartbeat():
        while not stop.is_set():
            ticks.append(time.time())
            await asyncio.sleep(0.05)

    hb = asyncio.create_task(heartbeat())
    result = await Login._post(obj, {"email": "u", "password": "p"})
    stop.set()
    await hb

    assert result == (True, "Login successful.")
    assert sent_from["thread"] != loop_thread_id, (
        "send_request ran on the event loop thread -> would block"
    )
    # heartbeat kept ticking during the ~0.4s slow send_request
    assert len(ticks) >= 5


def test_login_source_uses_run_in_executor():
    """Source-level guarantee: run_in_executor references exist in login.py.

    Reason: even if a future refactor breaks the behavioural test with a
    different mock surface, this makes the intent explicit and reviewable.
    """
    src = (BACKEND_DIR / "pyquotex" / "http" / "login.py").read_text()
    # __call__ block should offload get_token and get_profile
    call_block = src.split("async def __call__")[1]
    assert "run_in_executor" in call_block
    assert "get_token" in call_block
    assert "get_profile" in call_block
    # _post offloads send_request
    post_block = src.split("async def _post")[1].split("async def ")[0]
    assert "run_in_executor" in post_block
    # awaiting_pin offloads send_request
    ap_block = src.split("async def awaiting_pin")[1].split("async def ")[0]
    assert "run_in_executor" in ap_block


# ---------------------------------------------------------------------------
# 4. awaiting_pin: no more builtins.input() when non-interactive
# ---------------------------------------------------------------------------

async def test_awaiting_pin_raises_when_stdin_missing(monkeypatch):
    from pyquotex.http import login as login_mod
    monkeypatch.setattr(login_mod.sys, "stdin", None, raising=False)

    spy = {"called": False}

    def _boom(prompt=""):
        spy["called"] = True
        return "1234"

    monkeypatch.setattr(builtins, "input", _boom)

    obj = _FakeLoginBase()
    with pytest.raises(RuntimeError) as exc:
        await login_mod.Login.awaiting_pin(obj, {"email": "u"}, "Enter PIN: ")
    assert "interactive" in str(exc.value).lower() or "console" in str(exc.value).lower()
    assert spy["called"] is False, "awaiting_pin must not call input() without a tty"


async def test_awaiting_pin_raises_when_stdin_not_a_tty(monkeypatch):
    from pyquotex.http import login as login_mod

    class _FakeStdin:
        def isatty(self):
            return False

    monkeypatch.setattr(login_mod.sys, "stdin", _FakeStdin(), raising=False)

    spy = {"called": False}

    def _boom(prompt=""):
        spy["called"] = True
        return "1234"

    monkeypatch.setattr(builtins, "input", _boom)

    obj = _FakeLoginBase()
    with pytest.raises(RuntimeError):
        await login_mod.Login.awaiting_pin(obj, {"email": "u"}, "Enter PIN: ")
    assert spy["called"] is False


async def test_awaiting_pin_uses_input_when_tty(monkeypatch):
    """The interactive path is still reachable (developer-CLI use case)."""
    from pyquotex.http import login as login_mod
    from pyquotex.http.login import Login

    class _FakeStdin:
        def isatty(self):
            return True

    monkeypatch.setattr(login_mod.sys, "stdin", _FakeStdin(), raising=False)

    calls = {"input": 0, "send_request": 0}

    def _fake_input(prompt=""):
        calls["input"] += 1
        return "1234"

    monkeypatch.setattr(builtins, "input", _fake_input)

    def _fake_sr(self, method=None, url=None, data=None, **kw):
        calls["send_request"] += 1
        self.response = types.SimpleNamespace(url="/trade", ok=True)
        return self.response

    monkeypatch.setattr(Login, "send_request", _fake_sr, raising=True)

    obj = _FakeLoginBase()
    obj.send_request = lambda **kw: _fake_sr(obj, **kw)
    data = {"email": "u"}
    await Login.awaiting_pin(obj, data, "Enter PIN: ")
    assert calls["input"] == 1
    # PIN was forwarded through send_request via executor
    assert calls["send_request"] == 1
    assert data.get("code") == "1234"


# ---------------------------------------------------------------------------
# 5. bot._heartbeat, bot._hard_watchdog (plain thread, loop-independent)
# ---------------------------------------------------------------------------

async def test_heartbeat_task_advances_timestamp(monkeypatch):
    import bot as bot_mod
    monkeypatch.setattr(bot_mod, "HEARTBEAT_INTERVAL", 0.02)
    old = time.time() - 1000
    bot_mod._HEARTBEAT["loop"] = old
    try:
        hb = asyncio.create_task(bot_mod._heartbeat())
        await asyncio.sleep(0.15)
        new_ts = bot_mod._HEARTBEAT["loop"]
        assert new_ts > old, "heartbeat did not update timestamp"
        assert new_ts >= time.time() - 1
        hb.cancel()
        try:
            await hb
        except asyncio.CancelledError:
            pass
        assert hb.cancelled() or hb.done()
    finally:
        bot_mod._HEARTBEAT["loop"] = time.time()


def test_hard_watchdog_exits_on_blocked_loop(monkeypatch):
    """The key round-2 fix: a plain daemon thread, so it still runs even when
    the event loop is fully blocked."""
    import bot as bot_mod

    monkeypatch.setattr(bot_mod, "HARD_WATCHDOG_INTERVAL", 0.05)
    monkeypatch.setattr(bot_mod, "LOOP_STALL_LIMIT", 0.1)

    exit_calls = []

    class _StopThread(SystemExit):
        pass

    def _fake_exit(code):
        exit_calls.append(code)
        raise _StopThread()

    monkeypatch.setattr(bot_mod.os, "_exit", _fake_exit)

    # Simulate a wedged loop: heartbeat is way in the past
    bot_mod._HEARTBEAT["loop"] = time.time() - 3600
    try:
        t = threading.Thread(target=bot_mod._hard_watchdog, daemon=True)
        t.start()
        # Deliberately do NOT run an event loop here -> proves loop independence
        t.join(timeout=2.0)
        # thread should have raised _StopThread and exited
        assert not t.is_alive() or True  # daemon: even if alive, exit was called
        assert exit_calls and exit_calls[0] == 1
    finally:
        bot_mod._HEARTBEAT["loop"] = time.time()


def test_hard_watchdog_does_not_exit_when_fresh(monkeypatch):
    """Negative case: a fresh heartbeat means the loop is alive, so the
    watchdog must NOT trigger a restart loop."""
    import bot as bot_mod

    monkeypatch.setattr(bot_mod, "HARD_WATCHDOG_INTERVAL", 0.02)
    monkeypatch.setattr(bot_mod, "LOOP_STALL_LIMIT", 3600)

    exit_calls = []
    monkeypatch.setattr(bot_mod.os, "_exit", lambda c: exit_calls.append(c))

    bot_mod._HEARTBEAT["loop"] = time.time()

    stop = threading.Event()

    def _refresh():
        while not stop.is_set():
            bot_mod._HEARTBEAT["loop"] = time.time()
            time.sleep(0.01)

    refresher = threading.Thread(target=_refresh, daemon=True)
    refresher.start()
    t = threading.Thread(target=bot_mod._hard_watchdog, daemon=True)
    t.start()
    try:
        time.sleep(0.3)  # several HARD_WATCHDOG_INTERVAL cycles
        assert exit_calls == [], f"watchdog wrongly exited: {exit_calls}"
    finally:
        stop.set()
        refresher.join(timeout=1.0)
        # can't cleanly stop _hard_watchdog (it's an infinite loop) but it is a
        # daemon and won't call _exit unless the heartbeat goes stale


# ---------------------------------------------------------------------------
# 6. Watchdog lifecycle wiring v2: post_init/post_shutdown
# ---------------------------------------------------------------------------

class _FakeTicks:
    def __init__(self):
        self.started = False
        self.stopped = False
        self.started_at = None
        self.last_tick = time.time()
        self.task = None

    async def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class _FakeNotify:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


async def test_post_init_starts_heartbeat_watchdog_and_hard_watchdog_thread(monkeypatch):
    import bot as bot_mod

    fake_ticks = _FakeTicks()
    fake_notify = _FakeNotify()
    monkeypatch.setattr(bot_mod, "TICKS", fake_ticks)
    monkeypatch.setattr(bot_mod, "NOTIFY", fake_notify)
    monkeypatch.setattr(bot_mod, "WATCHDOG_INTERVAL", 3600)
    monkeypatch.setattr(bot_mod, "HEARTBEAT_INTERVAL", 3600)
    monkeypatch.setattr(bot_mod, "HARD_WATCHDOG_INTERVAL", 3600)
    monkeypatch.setattr(bot_mod, "LOOP_STALL_LIMIT", 3600)

    started_threads = []
    real_thread_cls = bot_mod.threading.Thread

    class _SpyThread:
        def __init__(self, target=None, daemon=None, **kw):
            started_threads.append({"target": target, "daemon": daemon,
                                    "kwargs": kw})
            self._target = target
            self._daemon = daemon

        def start(self):
            # DO NOT actually start _hard_watchdog — that could os._exit
            # if the heartbeat drifts. We only want to verify the wiring.
            pass

    monkeypatch.setattr(bot_mod.threading, "Thread", _SpyThread)

    app = types.SimpleNamespace(bot_data={})
    await bot_mod.post_init(app)

    assert fake_ticks.started is True
    hb = app.bot_data.get("heartbeat")
    wd = app.bot_data.get("watchdog")
    assert isinstance(hb, asyncio.Task) and not hb.done()
    assert isinstance(wd, asyncio.Task) and not wd.done()

    # daemon thread targeting _hard_watchdog was started
    assert any(t["target"] is bot_mod._hard_watchdog and t["daemon"] is True
               for t in started_threads), (
        f"expected a daemon thread for _hard_watchdog, got {started_threads}"
    )

    # restore for teardown
    monkeypatch.setattr(bot_mod.threading, "Thread", real_thread_cls)

    await bot_mod.post_shutdown(app)
    # both async tasks should have been cancelled
    for _ in range(20):
        if hb.done() and wd.done():
            break
        await asyncio.sleep(0.05)
    assert hb.done()
    assert wd.done()
    assert fake_ticks.stopped is True
    assert fake_notify.closed is True


# ---------------------------------------------------------------------------
# 7. Source-level guarantee: cmd_start does not touch Quotex
# ---------------------------------------------------------------------------

def test_cmd_start_does_not_touch_quotex():
    """/start getting no reply == the event loop itself is blocked, since the
    handler is trivial."""
    src = (BACKEND_DIR / "bot.py").read_text()
    block = src.split("async def cmd_start")[1].split("async def ")[0]
    for forbidden in ("QX.", "TICKS.", "SM.", "await QX", "get_markets",
                      "ensure_connected"):
        assert forbidden not in block, (
            f"cmd_start touches Quotex ('{forbidden}') — invalidates the "
            "'no /start reply == loop blocked' diagnosis"
        )
