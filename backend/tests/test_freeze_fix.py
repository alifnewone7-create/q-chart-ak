"""Regression tests for the ~24h silent-freeze bug.

Covers:
  - qx.QuotexManager.ensure_connected() bounded timeouts + lock release
  - Old client is closed exactly once when replaced
  - pyquotex.api.QuotexAPI.start_websocket() handshake deadline
  - pyquotex.api.QuotexAPI.close() bounded thread join
  - pyquotex.stable_api.Quotex.connect() closes previous api first
  - pyquotex.stable_api.Quotex.close() safe when api is None
  - bot._watchdog() lifecycle, revival, tick-stall exit, resilience
  - start.py logging setup (RotatingFileHandler, no logging.disable)
  - Regression checks for messages/premium_emojis/user_sender/charting
"""
import asyncio
import importlib
import io
import logging
import os
import sys
import time
import types
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))


# ---------------------------------------------------------------------------
# 1. Every changed module imports cleanly
# ---------------------------------------------------------------------------

CHANGED_MODULES = [
    "bot", "start", "qx", "sessions", "messages", "charting",
    "premium_emojis", "user_sender",
    "pyquotex.api", "pyquotex.stable_api",
]


@pytest.mark.parametrize("name", CHANGED_MODULES)
def test_module_imports_and_compiles(name):
    mod = importlib.import_module(name)
    assert mod is not None
    # byte-compile check
    src = mod.__file__
    if src and src.endswith(".py"):
        compile(open(src, "rb").read(), src, "exec")


# ---------------------------------------------------------------------------
# 2. qx.QuotexManager.ensure_connected() — deadlock fix
# ---------------------------------------------------------------------------

class _FakeClient:
    def __init__(self, connect_delay=0.0, connect_result=(True, "ok"),
                 check_delay=0.0, check_result=True,
                 close_delay=0.0, close_raises=False):
        self.connect_delay = connect_delay
        self.connect_result = connect_result
        self.check_delay = check_delay
        self.check_result = check_result
        self.close_delay = close_delay
        self.close_raises = close_raises
        self.close_calls = 0

    async def connect(self):
        if self.connect_delay:
            await asyncio.sleep(self.connect_delay)
        return self.connect_result

    async def check_connect(self):
        if self.check_delay:
            await asyncio.sleep(self.check_delay)
        return self.check_result

    async def change_account(self, kind):
        return None

    async def get_instruments(self):
        return []

    async def close(self):
        self.close_calls += 1
        if self.close_delay:
            await asyncio.sleep(self.close_delay)
        if self.close_raises:
            raise RuntimeError("boom")


@pytest.fixture
def qx_module(monkeypatch, tmp_path):
    import qx as qx_mod
    monkeypatch.setattr(qx_mod, "CONNECT_TIMEOUT", 1)
    monkeypatch.setattr(qx_mod, "CHECK_TIMEOUT", 1)
    monkeypatch.setattr(qx_mod, "CLOSE_TIMEOUT", 1)
    monkeypatch.setattr(qx_mod, "_session_file", lambda: tmp_path / "session.json")
    return qx_mod


async def test_ensure_connected_bounded_when_connect_hangs(qx_module):
    """The exact deadlock: client.connect never returns."""
    made = []

    def _make(fresh):
        c = _FakeClient(connect_delay=3600)
        made.append(c)
        return c

    mgr = qx_module.QuotexManager()
    mgr._make_client = _make

    start = time.time()
    with pytest.raises(ConnectionError):
        await mgr.ensure_connected()
    elapsed = time.time() - start
    # 2 modes x ~1s CONNECT_TIMEOUT + ~1s backoff + slack
    assert elapsed < 20, f"ensure_connected did not bound the hang (took {elapsed:.1f}s)"
    assert len(made) >= 1


async def test_ensure_connected_releases_lock_after_stall(qx_module):
    """After a stalled attempt, subsequent callers must not deadlock."""
    def _make(fresh):
        return _FakeClient(connect_delay=3600)

    mgr = qx_module.QuotexManager()
    mgr._make_client = _make

    with pytest.raises(ConnectionError):
        await mgr.ensure_connected()
    # Lock must be free now
    assert not mgr._lock.locked()

    # A second call also completes (also fails, but in bounded time)
    with pytest.raises(ConnectionError):
        await asyncio.wait_for(mgr.ensure_connected(), timeout=15)


async def test_concurrent_ensure_connected_not_stuck(qx_module):
    def _make(fresh):
        return _FakeClient(connect_delay=3600)

    mgr = qx_module.QuotexManager()
    mgr._make_client = _make

    results = await asyncio.gather(
        mgr.ensure_connected(), mgr.ensure_connected(), return_exceptions=True
    )
    assert all(isinstance(r, ConnectionError) for r in results)


async def test_stalled_check_connect_falls_through_to_reconnect(qx_module):
    """When health-check hangs on a supposedly-connected client, we time out
    and reconnect instead of blocking."""
    made = []

    def _make(fresh):
        c = _FakeClient()
        made.append(c)
        return c

    mgr = qx_module.QuotexManager()
    mgr._make_client = _make
    # Pretend a healthy connection with a hanging health-check
    stalled = _FakeClient(check_delay=3600)
    mgr.client = stalled
    mgr.connected = True

    await asyncio.wait_for(mgr.ensure_connected(), timeout=10)
    assert mgr.connected is True
    assert len(made) >= 1  # reconnect actually happened
    # the stalled client was replaced -> discarded via close()
    assert stalled.close_calls == 1


async def test_old_client_is_closed_exactly_once_on_reconnect(qx_module):
    made = []

    def _make(fresh):
        c = _FakeClient()
        made.append(c)
        return c

    mgr = qx_module.QuotexManager()
    mgr._make_client = _make
    old = _FakeClient()
    mgr.client = old
    mgr.connected = False  # forces reconnect

    await mgr.ensure_connected()
    assert old.close_calls == 1
    assert mgr.client is not old


async def test_close_timeout_bounds_a_hanging_close(qx_module):
    """A close() that never returns must not block ensure_connected."""
    def _make(fresh):
        return _FakeClient()

    mgr = qx_module.QuotexManager()
    mgr._make_client = _make
    hanging = _FakeClient(close_delay=3600)
    mgr.client = hanging
    mgr.connected = False

    start = time.time()
    await mgr.ensure_connected()
    assert time.time() - start < 10


async def test_last_ok_updated_on_success(qx_module):
    mgr = qx_module.QuotexManager()
    mgr._make_client = lambda fresh: _FakeClient()
    mgr.last_ok = 0
    await mgr.ensure_connected()
    assert mgr.last_ok > 0


# ---------------------------------------------------------------------------
# 3. pyquotex.api start_websocket handshake deadline + close bounded join
# ---------------------------------------------------------------------------

class _FakeState:
    def __init__(self):
        self.check_websocket_if_connect = None
        self.check_websocket_if_error = False
        self.check_rejected_connection = 0
        self.websocket_error_reason = None
        self.SSID = "ssid"
        self.ssl_Mutual_exclusion = False
        self.ssl_Mutual_exclusion_write = False


async def test_start_websocket_times_out_when_flags_never_set(monkeypatch):
    from pyquotex import api as pyq_api

    monkeypatch.setattr(pyq_api, "WS_HANDSHAKE_TIMEOUT", 1)

    class _WsClient:
        def __init__(self, api):
            pass

    class _Thread:
        daemon = True

        def __init__(self, *a, **kw):
            pass

        def start(self):
            pass

        def is_alive(self):
            return True

        def join(self, timeout=None):
            return None

    monkeypatch.setattr(pyq_api, "WebsocketClient", _WsClient)
    monkeypatch.setattr(pyq_api.threading, "Thread", _Thread)

    # Build a stand-in object with only what start_websocket touches
    obj = types.SimpleNamespace()
    obj.state = _FakeState()
    obj.https_url = "https://x"
    obj.host = "qxbroker.com"
    obj.session_data = {"token": "t"}
    obj.websocket = types.SimpleNamespace(run_forever=lambda **kw: None)
    obj.websocket_client = None
    obj.websocket_thread = None

    async def _fake_auth():
        return True, "ok"

    obj.authenticate = _fake_auth

    start = time.time()
    ok, reason = await pyq_api.QuotexAPI.start_websocket(obj)
    elapsed = time.time() - start
    assert ok is False
    assert "handshake timeout" in reason.lower()
    assert elapsed < 6


def test_start_websocket_source_still_handles_all_four_flag_paths():
    src = (BACKEND_DIR / "pyquotex" / "api.py").read_text()
    body = src.split("async def start_websocket")[1].split("async def ")[0]
    # All four original return paths are still present
    assert 'return False, self.state.websocket_error_reason' in body
    assert 'return False, "Websocket connection closed."' in body
    assert 'return True, "Websocket connected successfully!!!"' in body
    assert 'return True, "Websocket Token Rejected."' in body
    assert 'return False, "Websocket handshake timeout."' in body


async def test_close_bounded_join_when_thread_stays_alive(monkeypatch):
    from pyquotex import api as pyq_api
    monkeypatch.setattr(pyq_api, "WS_JOIN_TIMEOUT", 1)

    class _T:
        def __init__(self):
            self.joined_with = None

        def join(self, timeout=None):
            self.joined_with = timeout

        def is_alive(self):
            return True

    class _Ws:
        def close(self):
            return None

    obj = types.SimpleNamespace()
    obj.websocket_client = object()
    obj.websocket = _Ws()
    obj.websocket_thread = _T()

    start = time.time()
    result = await pyq_api.QuotexAPI.close(obj)
    assert result is True
    assert obj.websocket_client is None
    assert obj.websocket_thread.joined_with == 1
    # bounded, not blocking forever
    assert time.time() - start < 5


async def test_close_safe_when_websocket_client_is_none():
    from pyquotex import api as pyq_api
    obj = types.SimpleNamespace(websocket_client=None,
                                websocket_thread=None, websocket=None)
    result = await pyq_api.QuotexAPI.close(obj)
    assert result is True


# ---------------------------------------------------------------------------
# 4. pyquotex.stable_api.Quotex.connect closes the OLD api first
# ---------------------------------------------------------------------------

async def test_stable_api_connect_closes_previous_api(monkeypatch):
    from pyquotex import stable_api

    close_order = []

    class _OldApi:
        def __init__(self):
            self.closed = False

        async def close(self):
            close_order.append("old_close")
            self.closed = True

    class _NewApi:
        def __init__(self, *a, **kw):
            close_order.append("new_init")
            self.trace_ws = False
            self.session_data = {}
            self.current_asset = None
            self.current_period = None
            self.state = types.SimpleNamespace(SSID=None)
            self.instruments = None
            self.candles = None
            self.historical_candles = None
            self.candle_v2_data = {}

        async def authenticate(self):
            return True, "ok"

        async def connect(self, is_demo):
            close_order.append("new_connect")
            self.state.check_websocket_if_connect = 1
            return True, "ok"

    monkeypatch.setattr(stable_api, "QuotexAPI", _NewApi)

    q = stable_api.Quotex.__new__(stable_api.Quotex)
    q.api = _OldApi()
    q.host = "h"
    q.email = "e"
    q.password = "p"
    q.lang = "en"
    q.resource_path = "."
    q.user_data_dir = "."
    q.proxies = None
    q.debug_ws_enable = False
    q.session_data = {"token": "t"}
    q.asset_default = None
    q.period_default = 60
    q.account_is_demo = 1

    async def _check_connect():
        return True

    q.check_connect = _check_connect

    old = q.api
    await stable_api.Quotex.connect(q)
    assert old.closed is True
    # old close ran BEFORE the new api was constructed
    assert close_order.index("old_close") < close_order.index("new_init")


async def test_stable_api_close_when_api_is_none():
    from pyquotex import stable_api
    q = stable_api.Quotex.__new__(stable_api.Quotex)
    q.api = None
    result = await stable_api.Quotex.close(q)
    assert result is True


# ---------------------------------------------------------------------------
# 5. bot._watchdog + post_init/post_shutdown
# ---------------------------------------------------------------------------

class _FakeSM:
    def __init__(self, active=False, task=None):
        self.active = active
        self.task = task

    async def _loop(self):
        return None


class _FakeTicks:
    def __init__(self, started_at=None, last_tick=None):
        self.started_at = started_at
        self.last_tick = last_tick if last_tick is not None else time.time()
        self.task = None
        self.started = False
        self.stopped = False

    async def start(self):
        self.started = True

    def stop(self):
        self.stopped = True


class _FakeNotify:
    def __init__(self):
        self.closed = False

    async def close(self):
        self.closed = True


async def test_watchdog_revives_dead_session_loop(monkeypatch):
    import bot as bot_mod
    monkeypatch.setattr(bot_mod, "WATCHDOG_INTERVAL", 0.05)
    monkeypatch.setattr(bot_mod, "TICK_STALL_LIMIT", 999999)

    # a completed task (session loop died)
    async def _done():
        return None

    dead = asyncio.create_task(_done())
    await dead

    sm = _FakeSM(active=True, task=dead)
    monkeypatch.setattr(bot_mod, "SM", sm)
    monkeypatch.setattr(bot_mod, "TICKS", _FakeTicks(started_at=None))

    wd = asyncio.create_task(bot_mod._watchdog())
    await asyncio.sleep(0.2)
    wd.cancel()
    try:
        await wd
    except asyncio.CancelledError:
        pass

    assert sm.task is not None and sm.task is not dead
    assert not sm.task.done() or sm.task.result() is None
    sm.task.cancel()


async def test_watchdog_ignores_running_or_inactive_session(monkeypatch):
    import bot as bot_mod
    monkeypatch.setattr(bot_mod, "WATCHDOG_INTERVAL", 0.05)
    monkeypatch.setattr(bot_mod, "TICK_STALL_LIMIT", 999999)

    async def _busy():
        await asyncio.sleep(10)

    running = asyncio.create_task(_busy())
    try:
        # active but task still running => do not touch it
        sm = _FakeSM(active=True, task=running)
        monkeypatch.setattr(bot_mod, "SM", sm)
        monkeypatch.setattr(bot_mod, "TICKS", _FakeTicks(started_at=None))
        wd = asyncio.create_task(bot_mod._watchdog())
        await asyncio.sleep(0.2)
        wd.cancel()
        try:
            await wd
        except asyncio.CancelledError:
            pass
        assert sm.task is running
    finally:
        running.cancel()

    # inactive session => watchdog must leave SM.task alone
    async def _done():
        return None

    dead = asyncio.create_task(_done())
    await dead
    sm = _FakeSM(active=False, task=dead)
    monkeypatch.setattr(bot_mod, "SM", sm)
    wd = asyncio.create_task(bot_mod._watchdog())
    await asyncio.sleep(0.2)
    wd.cancel()
    try:
        await wd
    except asyncio.CancelledError:
        pass
    assert sm.task is dead


async def test_watchdog_hard_exits_on_tick_stall(monkeypatch):
    import bot as bot_mod
    monkeypatch.setattr(bot_mod, "WATCHDOG_INTERVAL", 0.05)
    monkeypatch.setattr(bot_mod, "TICK_STALL_LIMIT", 1)

    class _Exit(Exception):
        pass

    calls = []

    def _fake_exit(code):
        calls.append(code)
        raise _Exit()

    monkeypatch.setattr(bot_mod.os, "_exit", _fake_exit)
    monkeypatch.setattr(bot_mod, "SM", _FakeSM(active=False))
    monkeypatch.setattr(bot_mod, "TICKS",
                        _FakeTicks(started_at=time.time() - 100, last_tick=time.time() - 100))

    wd = asyncio.create_task(bot_mod._watchdog())
    await asyncio.sleep(0.3)
    wd.cancel()
    try:
        await wd
    except asyncio.CancelledError:
        pass
    assert calls and all(c == 1 for c in calls)


async def test_watchdog_does_not_exit_before_ticks_started(monkeypatch):
    """CRITICAL negative test: bot booting / Quotex down at startup
    must NOT trigger a hard exit."""
    import bot as bot_mod
    monkeypatch.setattr(bot_mod, "WATCHDOG_INTERVAL", 0.05)
    monkeypatch.setattr(bot_mod, "TICK_STALL_LIMIT", 1)

    calls = []
    monkeypatch.setattr(bot_mod.os, "_exit", lambda c: calls.append(c))
    monkeypatch.setattr(bot_mod, "SM", _FakeSM(active=False))
    # started_at is None -> collector hasn't begun yet
    monkeypatch.setattr(bot_mod, "TICKS",
                        _FakeTicks(started_at=None, last_tick=time.time() - 100))

    wd = asyncio.create_task(bot_mod._watchdog())
    await asyncio.sleep(0.3)
    wd.cancel()
    try:
        await wd
    except asyncio.CancelledError:
        pass
    assert calls == []


async def test_watchdog_does_not_exit_on_recent_tick(monkeypatch):
    import bot as bot_mod
    monkeypatch.setattr(bot_mod, "WATCHDOG_INTERVAL", 0.05)
    monkeypatch.setattr(bot_mod, "TICK_STALL_LIMIT", 999)

    calls = []
    monkeypatch.setattr(bot_mod.os, "_exit", lambda c: calls.append(c))
    monkeypatch.setattr(bot_mod, "SM", _FakeSM(active=False))
    monkeypatch.setattr(bot_mod, "TICKS",
                        _FakeTicks(started_at=time.time() - 100, last_tick=time.time()))

    wd = asyncio.create_task(bot_mod._watchdog())
    await asyncio.sleep(0.2)
    wd.cancel()
    try:
        await wd
    except asyncio.CancelledError:
        pass
    assert calls == []


async def test_watchdog_swallows_unexpected_exception_and_keeps_running(monkeypatch):
    import bot as bot_mod
    monkeypatch.setattr(bot_mod, "WATCHDOG_INTERVAL", 0.05)

    # A property that raises the first time and behaves next time
    class _Boom:
        def __init__(self):
            self.n = 0

        @property
        def active(self):
            self.n += 1
            if self.n == 1:
                raise RuntimeError("boom")
            return False

        @property
        def task(self):
            return None

    monkeypatch.setattr(bot_mod, "SM", _Boom())
    monkeypatch.setattr(bot_mod, "TICKS", _FakeTicks(started_at=None))
    monkeypatch.setattr(bot_mod, "TICK_STALL_LIMIT", 999999)

    wd = asyncio.create_task(bot_mod._watchdog())
    await asyncio.sleep(0.3)
    assert not wd.done(), "watchdog should have swallowed the exception"
    wd.cancel()
    try:
        await wd
    except asyncio.CancelledError:
        pass


async def test_post_init_starts_watchdog_and_shutdown_cancels_it(monkeypatch):
    import bot as bot_mod

    fake_ticks = _FakeTicks(started_at=None)
    fake_notify = _FakeNotify()
    monkeypatch.setattr(bot_mod, "TICKS", fake_ticks)
    monkeypatch.setattr(bot_mod, "NOTIFY", fake_notify)
    # keep watchdog cheap
    monkeypatch.setattr(bot_mod, "WATCHDOG_INTERVAL", 60)
    monkeypatch.setattr(bot_mod, "SM", _FakeSM(active=False))

    app = types.SimpleNamespace(bot_data={})
    await bot_mod.post_init(app)
    wd = app.bot_data.get("watchdog")
    assert isinstance(wd, asyncio.Task) and not wd.done()
    assert fake_ticks.started

    await bot_mod.post_shutdown(app)
    # Give the task a moment to finalize its cancelled state
    for _ in range(20):
        if wd.done():
            break
        await asyncio.sleep(0.05)
    assert wd.done()
    assert fake_ticks.stopped
    assert fake_notify.closed


# ---------------------------------------------------------------------------
# 6. start.py logging: no logging.disable, RotatingFileHandler on disk
# ---------------------------------------------------------------------------

def test_start_py_installs_rotating_file_handler(tmp_path, monkeypatch):
    from logging.handlers import RotatingFileHandler
    # execute start.py's module-level setup in isolation
    src = (BACKEND_DIR / "start.py").read_text()
    # Source should NOT actually call logging.disable (ignore mentions in comments)
    import re as _re
    code_only = _re.sub(r"#.*", "", src)
    code_only = _re.sub(r'"""[\s\S]*?"""', "", code_only)
    assert "logging.disable(" not in code_only

    # Reset root logger, execute the top-of-file block, verify handler is attached
    root = logging.getLogger()
    saved_handlers, saved_level = root.handlers[:], root.level
    root.handlers = []
    root.setLevel(logging.NOTSET)
    try:
        # Redirect the log dir to tmp so we don't touch backend/data
        monkeypatch.chdir(tmp_path)
        # rewrite LOG_DIR line to tmp_path so we don't spam backend/data
        setup = src.split("def _run(")[0]
        setup = setup.replace(
            "os.path.join(os.path.dirname(os.path.abspath(__file__)), \"data\")",
            repr(str(tmp_path))
        )
        exec(compile(setup, "start_setup.py", "exec"), {"__name__": "start_setup"})

        rot = [h for h in root.handlers if isinstance(h, RotatingFileHandler)]
        assert len(rot) == 1, f"expected 1 RotatingFileHandler, got {root.handlers}"
        assert root.level == logging.WARNING
        # no StreamHandler on the root
        assert not any(type(h).__name__ == "StreamHandler" for h in root.handlers)

        # Actually emit a log line and verify it landed on disk
        log = logging.getLogger("test_start")
        log.warning("hello-from-start")
        for h in root.handlers:
            h.flush()
        log_file = Path(rot[0].baseFilename)
        assert log_file.exists()
        assert "hello-from-start" in log_file.read_text()
    finally:
        for h in root.handlers:
            try:
                h.close()
            except Exception:
                pass
        root.handlers = saved_handlers
        root.setLevel(saved_level)


# ---------------------------------------------------------------------------
# 7. Regression: owner tag / premium emojis / user_sender entities / charting
# ---------------------------------------------------------------------------

def test_owner_tag_default():
    import config
    assert config.OWNER_TAG == "@BfsTraderQX"


def test_signal_caption_bolds_owner_tag_plainly():
    import messages
    cap = messages.signal_caption("USD/JPY", "CALL", "01:02", 85, "reason",
                                  "@BfsTraderQX")
    assert "<b>@BfsTraderQX</b>" in cap
    # owner tag itself must NOT be styled monospace
    assert "@BfsTraderQX" in cap


def test_premium_emojis_preserve_bold_and_escape_html():
    import premium_emojis
    html_out = premium_emojis.premiumize("<b>@X</b> a<b & <script>")
    assert "<b>@X</b>" in html_out
    # < outside <b>/</b> must have been escaped
    assert "&lt;script&gt;" in html_out or "&lt;script" in html_out
    assert "&amp;" in html_out


def test_plain_html_preserves_bold_and_strips_custom_emoji():
    import premium_emojis
    text = '<tg-emoji emoji-id="123">✨</tg-emoji> <b>@X</b> &lt;'
    out = premium_emojis.plain_html(text)
    assert "<tg-emoji" not in out
    assert "<b>@X</b>" in out


def test_to_entities_strips_bold_tags_and_bold_entities_utf16_correct():
    import premium_emojis
    text = "hi <b>@BfsTraderQX</b> end"
    plain, ents = premium_emojis.to_entities(text)
    assert "<b>" not in plain and "</b>" not in plain
    assert "@BfsTraderQX" in plain

    spans = premium_emojis.bold_entities(text)
    assert len(spans) == 1
    off, length = spans[0]
    utf16 = plain.encode("utf-16-le")
    sliced = utf16[off * 2: (off + length) * 2].decode("utf-16-le")
    assert sliced == "@BfsTraderQX"


def test_user_sender_entities_include_bold_and_custom_emoji():
    import user_sender
    from telethon.tl.types import MessageEntityBold, MessageEntityCustomEmoji
    text = '<tg-emoji emoji-id="6217660507575291616">✅</tg-emoji> <b>@BfsTraderQX</b>'
    plain, ents = user_sender._entities(text)
    assert any(isinstance(e, MessageEntityBold) for e in ents)
    assert any(isinstance(e, MessageEntityCustomEmoji) for e in ents)


def test_render_chart_signal_and_result_pngs():
    import charting
    candles = [{"time": i * 60, "open": 1.0, "high": 1.1,
                "low": 0.9, "close": 1.05} for i in range(30)]
    png_sig = charting.render_chart(candles, "USD/JPY", badge="CALL",
                                    payout=85, entry_ts=0, entry_str="01:00",
                                    market_name="USD/JPY", result=None)
    assert png_sig[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png_sig) > 1000

    png_res = charting.render_chart(candles, "USD/JPY", badge="CALL",
                                    payout=85, entry_ts=0, entry_str="01:00",
                                    market_name="USD/JPY", result="WIN",
                                    stats={"wins": 7, "losses": 2, "total": 9})
    assert png_res[:8] == b"\x89PNG\r\n\x1a\n"
    assert len(png_res) > 1000


def test_charting_source_no_losses_box_engine_on_both():
    src = (BACKEND_DIR / "charting.py").read_text()
    # Strip comments + docstrings, then confirm no LOSSES box is drawn in code
    import re as _re
    code = _re.sub(r'"""[\s\S]*?"""', "", src)
    code = _re.sub(r"'''[\s\S]*?'''", "", code)
    code = _re.sub(r"#.*", "", code)
    # No LOSSES text drawn anywhere (the code comment above the WINS card also
    # says "losses are intentionally not shown" -> stripped above)
    assert "LOSSES" not in code
    # ENGINE card helper drawn on BOTH images (signal + result branches)
    assert code.count("engine_section(") >= 2
