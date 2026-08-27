"""Unit tests for the standalone Telegram signal bot.

Covers:
- pyquotex.http.login.Login.get_profile: robust window.settings extraction
- pyquotex.http.login.Login.__call__: graceful failure when settings missing
- qx.QuotexManager.ensure_connected: session.json cleanup + ConnectionError
- ticks.TickCollector: _drain, get_candles, get_closed_candle boundary logic
- sessions.SessionManager._candles: merge of history + tick candles
"""
import asyncio
import json
import os
import sys
import time
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, "/app/backend")

# ---- module imports (order matters because of sys.path) ----
from pyquotex.http.login import Login  # noqa: E402
import qx as qx_mod  # noqa: E402
from ticks import TickCollector  # noqa: E402
from sessions import SessionManager  # noqa: E402


# =============================================================
# Helpers
# =============================================================
class FakeSoup:
    """Mimics BeautifulSoup minimal API used in get_profile()."""

    def __init__(self, html):
        self.html = html

    def find_all(self, tag):
        # crude <script>...</script> extractor
        import re as _re
        return [
            _FakeScript(t) for t in _re.findall(
                r"<script[^>]*>(.*?)</script>", self.html, flags=_re.DOTALL
            )
        ]


class _FakeScript:
    def __init__(self, text):
        self._text = text

    def get_text(self):
        return self._text


def _make_login(html_pages):
    """Build a Login instance with send_request/get_soup mocked to return
    successive HTML pages."""
    login = Login.__new__(Login)
    login.api = MagicMock()
    login.api.session_data = {}
    login.api.username = "u@example.com"
    login.headers = {"User-Agent": "ua"}
    login.full_url = "https://qxbroker.com/en"

    calls = {"i": 0}

    def send_request(method=None, url=None, data=None):
        i = calls["i"]
        calls["i"] = min(i + 1, len(html_pages) - 1)
        resp = MagicMock()
        resp.text = html_pages[i]
        resp.url = "https://qxbroker.com/en/trade"
        login._html = html_pages[i]
        return resp

    login.send_request = send_request
    login.get_soup = lambda: FakeSoup(login._html)
    login.get_cookies = lambda: {"c": "1"}
    return login


# =============================================================
# Login.get_profile tests
# =============================================================
class TestGetProfile:
    """Robust window.settings extraction (main bug fix)."""

    def test_a_empty_first_script_settings_in_later(self, monkeypatch):
        """(a) First <script> tag EMPTY; window.settings is in a later tag."""
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        with patch("pyquotex.http.login.update_session"):
            html = (
                "<html><body>"
                "<script></script>"
                "<script>var foo = 1;</script>"
                '<script>window.settings = {"token":"SSID123","x":1};</script>'
                "</body></html>"
            )
            login = _make_login([html])
            resp, settings = login.get_profile()
            assert settings is not None
            assert settings["token"] == "SSID123"
            assert login.ssid == "SSID123"

    def test_a2_no_spaces_around_equals(self, monkeypatch):
        """Regex robustness: 'window.settings={...};' with no spaces."""
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        with patch("pyquotex.http.login.update_session"):
            html = (
                "<html><body>"
                '<script>window.settings={"token":"NOSPACE","a":2};</script>'
                "</body></html>"
            )
            login = _make_login([html])
            resp, settings = login.get_profile()
            assert settings is not None, "regex must handle no spaces around ="
            assert settings["token"] == "NOSPACE"
            assert login.ssid == "NOSPACE"

    def test_b_no_window_settings_returns_none_no_raise(self, monkeypatch):
        """(b) No window.settings anywhere -> (None, None), no exception."""
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        with patch("pyquotex.http.login.update_session"):
            html = "<html><body><script></script><script>var x=1;</script></body></html>"
            login = _make_login([html, html, html])
            resp, settings = login.get_profile()
            assert resp is None
            assert settings is None

    def test_c_malformed_json_returns_none(self, monkeypatch):
        """(c) window.settings present but JSON malformed -> (None, None)."""
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        with patch("pyquotex.http.login.update_session"):
            html = (
                "<html><body>"
                "<script>window.settings = {this is: not json,,};</script>"
                "</body></html>"
            )
            login = _make_login([html, html, html])
            resp, settings = login.get_profile()
            assert settings is None
            assert resp is None

    def test_no_json_decode_error_propagates(self, monkeypatch):
        """Legacy bug scenario: first script empty -> must NOT raise
        'Expecting value: line 1 column 1 (char 0)'."""
        monkeypatch.setattr(time, "sleep", lambda *_: None)
        with patch("pyquotex.http.login.update_session"):
            html = "<html><body><script></script></body></html>"
            login = _make_login([html, html, html])
            try:
                resp, settings = login.get_profile()
            except json.JSONDecodeError as e:
                pytest.fail(f"get_profile must not raise JSONDecodeError, got: {e}")
            assert (resp, settings) == (None, None)


# =============================================================
# Login.__call__ path
# =============================================================
class TestLoginCall:
    """__call__: when _post returns True but get_profile finds no settings,
    must return (False, <clear msg>), not crash."""

    def test_d_call_returns_false_when_no_settings(self, monkeypatch):
        monkeypatch.setattr(time, "sleep", lambda *_: None)

        login = _make_login(
            ["<html><body><script></script></body></html>"] * 3
        )
        login.get_token = lambda: "tok"

        async def fake_post(_data):
            return True, "Login successful."

        login._post = fake_post
        with patch("pyquotex.http.login.update_session"):
            status, msg = asyncio.get_event_loop().run_until_complete(
                login("u", "p")
            ) if False else asyncio.run(login("u", "p"))
        assert status is False
        assert isinstance(msg, str) and len(msg) > 0
        assert "session token" in msg.lower() or "structure" in msg.lower()


# =============================================================
# qx.QuotexManager.ensure_connected
# =============================================================
class TestEnsureConnected:
    def test_session_json_cleanup_on_connect_exception(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        sess = tmp_path / "session.json"
        sess.write_text('{"stale":true}')
        assert sess.exists()

        mgr = qx_mod.QuotexManager()

        class FakeClient:
            async def connect(self):
                raise ValueError("boom")

            async def check_connect(self):
                return False

        monkeypatch.setattr(qx_mod, "Quotex", lambda **kw: FakeClient())

        with pytest.raises(ConnectionError) as ei:
            asyncio.run(mgr.ensure_connected())
        assert "Quotex connect error" in str(ei.value)
        assert not sess.exists(), "stale session.json must be deleted"

    def test_session_json_cleanup_on_failed_login(self, tmp_path, monkeypatch):
        monkeypatch.chdir(tmp_path)
        sess = tmp_path / "session.json"
        sess.write_text('{"stale":true}')

        mgr = qx_mod.QuotexManager()

        class FakeClient:
            async def connect(self):
                return False, "Login failed. Unknown error"

            async def check_connect(self):
                return False

        monkeypatch.setattr(qx_mod, "Quotex", lambda **kw: FakeClient())
        with pytest.raises(ConnectionError) as ei:
            asyncio.run(mgr.ensure_connected())
        assert "Quotex login failed" in str(ei.value)
        assert not sess.exists()

    def test_no_jsondecodeerror_leak(self, tmp_path, monkeypatch):
        """Even if underlying error is JSONDecodeError, ensure_connected
        wraps it as ConnectionError."""
        monkeypatch.chdir(tmp_path)
        mgr = qx_mod.QuotexManager()

        class FakeClient:
            async def connect(self):
                raise json.JSONDecodeError("Expecting value", "", 0)

            async def check_connect(self):
                return False

        monkeypatch.setattr(qx_mod, "Quotex", lambda **kw: FakeClient())
        with pytest.raises(ConnectionError):
            asyncio.run(mgr.ensure_connected())


# =============================================================
# TickCollector unit tests
# =============================================================
class FakeApi:
    def __init__(self):
        self.realtime_price = {}


class FakeQx:
    def __init__(self):
        self.client = MagicMock()
        self.client.api = FakeApi()
        self.connected = True

    async def ensure_connected(self):
        return

    async def get_markets(self, cat):
        return []


class TestTickCollector:
    def _make(self, now_min):
        qx = FakeQx()
        tc = TickCollector(qx)
        tc.started_at = now_min
        return tc, qx

    def test_drain_builds_ohlc_and_drains_buffer(self):
        now_min = (int(time.time()) // 60) * 60
        tc, qx = self._make(now_min)
        qx.client.api.realtime_price["EURUSD"] = [
            {"time": now_min + 1, "price": 1.10},
            {"time": now_min + 2, "price": 1.12},
            {"time": now_min + 3, "price": 1.09},
            {"time": now_min + 4, "price": 1.11},
        ]
        tc._drain()
        candles = tc.get_candles("EURUSD")
        assert len(candles) == 1
        c = candles[0]
        assert c["time"] == now_min
        assert c["open"] == 1.10
        assert c["high"] == 1.12
        assert c["low"] == 1.09
        assert c["close"] == 1.11
        assert qx.client.api.realtime_price["EURUSD"] == [], "buffer must be drained"

    def test_ticks_before_started_at_discarded(self):
        now_min = (int(time.time()) // 60) * 60
        tc, qx = self._make(now_min)
        qx.client.api.realtime_price["EURUSD"] = [
            {"time": now_min - 30, "price": 9.0},   # before boundary
            {"time": now_min + 5, "price": 1.20},
        ]
        tc._drain()
        candles = tc.get_candles("EURUSD")
        assert len(candles) == 1
        assert candles[0]["open"] == 1.20

    def test_discard_flag_drains_but_stores_nothing(self):
        now_min = (int(time.time()) // 60) * 60
        tc, qx = self._make(now_min)
        qx.client.api.realtime_price["EURUSD"] = [
            {"time": now_min + 1, "price": 1.0}
        ]
        tc._drain(discard=True)
        assert tc.get_candles("EURUSD") == []
        assert qx.client.api.realtime_price["EURUSD"] == []

    def test_get_closed_candle(self):
        now_min = (int(time.time()) // 60) * 60
        # Use a past minute so time.time() >= ts + 60
        past = now_min - 120
        tc, qx = self._make(past)
        tc.candles["EURUSD"] = {
            past: {"time": past, "open": 1, "high": 2, "low": 0.5, "close": 1.5},
            now_min: {"time": now_min, "open": 1, "high": 1, "low": 1, "close": 1},
        }
        # Past minute -> returned
        c = tc.get_closed_candle("EURUSD", past)
        assert c is not None and c["time"] == past
        # Running (current) minute -> None
        assert tc.get_closed_candle("EURUSD", now_min) is None
        # Before started_at -> None
        assert tc.get_closed_candle("EURUSD", past - 60) is None


# =============================================================
# SessionManager._candles merge test
# =============================================================
class FakeQxForSessions:
    def __init__(self, hist):
        self._hist = hist

    async def get_candles_1m(self, code, count):
        return list(self._hist)


class FakeTicks:
    def __init__(self, tick_candles):
        self._c = tick_candles

    def get_candles(self, code):
        return list(self._c)


class TestSessionsMerge:
    def test_tick_overrides_history_and_running_last(self):
        now_min = (int(time.time()) // 60) * 60
        hist = [
            {"time": now_min - 180, "open": 1, "high": 1, "low": 1, "close": 1},
            {"time": now_min - 120, "open": 2, "high": 2, "low": 2, "close": 2},
            {"time": now_min - 60, "open": 3, "high": 3, "low": 3, "close": 3},  # will be overridden
        ]
        tick = [
            # Override same minute as history
            {"time": now_min - 60, "open": 99, "high": 99, "low": 99, "close": 99},
            # Running current candle (not in history)
            {"time": now_min, "open": 7, "high": 8, "low": 6, "close": 7.5},
        ]
        sm = SessionManager()
        sm.qx = FakeQxForSessions(hist)
        sm.ticks = FakeTicks(tick)
        out = asyncio.run(sm._candles("EURUSD", 60))
        # Sorted ascending
        times = [c["time"] for c in out]
        assert times == sorted(times)
        # Running candle is last
        assert out[-1]["time"] == now_min
        # Tick overrode history for now_min - 60
        overridden = next(c for c in out if c["time"] == now_min - 60)
        assert overridden["open"] == 99
        assert overridden["close"] == 99

    def test_history_only_when_no_ticks(self):
        now_min = (int(time.time()) // 60) * 60
        hist = [{"time": now_min - 60, "open": 1, "high": 1, "low": 1, "close": 1}]
        sm = SessionManager()
        sm.qx = FakeQxForSessions(hist)
        sm.ticks = None
        out = asyncio.run(sm._candles("X", 60))
        assert len(out) == 1
        assert out[0]["time"] == now_min - 60

    def test_history_exception_falls_back_to_ticks(self):
        now_min = (int(time.time()) // 60) * 60

        class BrokenQx:
            async def get_candles_1m(self, code, count):
                raise RuntimeError("api down")

        tick = [{"time": now_min, "open": 1, "high": 2, "low": 0.5, "close": 1.5}]
        sm = SessionManager()
        sm.qx = BrokenQx()
        sm.ticks = FakeTicks(tick)
        out = asyncio.run(sm._candles("X", 60))
        assert len(out) == 1
        assert out[0]["time"] == now_min



# =============================================================
# Auto Select % filter behaviour
# =============================================================


class FakeQxForAuto:
    def __init__(self, markets):
        self.markets = markets
        self.calls = 0

    async def get_markets(self, category):
        self.calls += 1
        return list(self.markets)


class TestAutoSelectFilter:
    def test_refresh_keeps_only_markets_at_or_above_threshold(self):
        sm = SessionManager()
        sm.qx = FakeQxForAuto([
            {"code": "A_otc", "display": "A OTC", "payout": 92},
            {"code": "B_otc", "display": "B OTC", "payout": 80},
            {"code": "C_otc", "display": "C OTC", "payout": 79},
            {"code": "D_otc", "display": "D OTC", "payout": 65},
        ])
        sm.auto_mode = True
        sm.auto_threshold = 80
        sm.auto_category = "all"
        sm.bot = None  # skip admin-view edit
        asyncio.run(sm._refresh_markets())
        codes = [m["code"] for m in sm.markets]
        assert codes == ["A_otc", "B_otc"]
        assert sm.markets[0]["payout"] == 92
        assert sm.markets[1]["payout"] == 80

    def test_refresh_updates_when_payouts_change(self):
        pool = [
            {"code": "A_otc", "display": "A", "payout": 75},
            {"code": "B_otc", "display": "B", "payout": 85},
        ]
        sm = SessionManager()
        sm.qx = FakeQxForAuto(pool)
        sm.auto_mode = True
        sm.auto_threshold = 80
        sm.bot = None
        asyncio.run(sm._refresh_markets())
        assert [m["code"] for m in sm.markets] == ["B_otc"]
        # payout on A rises, B drops below threshold
        pool[0]["payout"] = 90
        pool[1]["payout"] = 70
        asyncio.run(sm._refresh_markets())
        assert [m["code"] for m in sm.markets] == ["A_otc"]

    def test_running_status_text_mentions_mode_and_markets(self):
        sm = SessionManager()
        sm.auto_mode = True
        sm.auto_threshold = 82
        sm.channels = [{"id": 1, "title": "MyCH"}]
        sm.markets = [{"code": "X_otc", "display": "X OTC", "payout": 90}]
        txt = sm.running_status_text()
        assert "Auto Select" in txt
        assert "82%" in txt
        assert "X OTC" in txt
        assert "90%" in txt



# =============================================================
# Duplicate signal / result guard
# =============================================================


class FakeBot:
    def __init__(self):
        self.photos = []
        self.edited = []

    async def send_photo(self, chat_id, photo, caption):
        self.photos.append({"chat_id": chat_id, "caption": caption})

    async def edit_message_text(self, chat_id, message_id, text, reply_markup=None):
        self.edited.append({"chat_id": chat_id, "message_id": message_id})


class FakeQxForRun:
    def __init__(self, closed_candle_close):
        self.closed_candle_close = closed_candle_close

    async def get_candles_1m(self, code, count):
        base = ((int(time.time()) // 60) * 60)
        out = []
        for i in range(30):
            t = base - (30 - i) * 60
            out.append({
                "time": t, "open": 100.0, "high": 100.5, "low": 99.5,
                "close": 100.4 if i > 20 else 99.6,
            })
        # ensure a decisive last closed candle to feed analysis
        out[-1]["close"] = self.closed_candle_close
        return out


class FakeTicksForRun:
    def get_candles(self, code):
        return []

    def get_closed_candle(self, code, ts):
        # first candle result — direction that WINS to keep the flow short
        return {"time": ts, "open": 100.0, "high": 100.3, "low": 99.9, "close": 100.2}


class TestDuplicateGuard:
    def test_run_signal_dedup_skips_second_send_for_same_entry(self):
        sm = SessionManager()
        sm.bot = FakeBot()
        sm.qx = FakeQxForRun(closed_candle_close=100.4)
        sm.ticks = FakeTicksForRun()
        sm.active = True
        sm.channel_id = 111
        sm.session_id = "S1"
        # short-circuit the sleep-until helpers to run instantly
        async def _no_sleep(ts): return
        sm._sleep_until = _no_sleep

        market = {"code": "USDINR_otc", "display": "USD/INR OTC", "payout": 92}
        res = {"direction": "CALL", "confidence": 80.0, "reason": "test"}

        # first call sends 1 signal + 1 result (2 photos)
        asyncio.run(sm._run_signal(market, res))
        first_count = len(sm.bot.photos)
        assert first_count == 2

        # second call with same entry_ts must be skipped completely
        asyncio.run(sm._run_signal(market, res))
        assert len(sm.bot.photos) == first_count  # no new sends
        assert len(sm._sent_entries) == 1

    def test_pick_best_skips_already_signaled_markets(self):
        sm = SessionManager()
        sm.active = True
        sm.markets = [{"code": "A_otc", "display": "A", "payout": 90}]
        next_entry_ts = ((int(time.time()) // 60) + 1) * 60
        sm._sent_entries.add(("A_otc", next_entry_ts))

        async def fake_candles(code, count):
            raise AssertionError("should not fetch candles for a signaled market")

        sm._candles = fake_candles
        pick = asyncio.run(sm._pick_best())
        assert pick is None

    def test_run_signal_aborts_when_session_closed_mid_flow(self):
        """After the signal photo is sent, if session becomes inactive
        the result photo must NOT be sent."""
        sm = SessionManager()
        sm.bot = FakeBot()
        sm.qx = FakeQxForRun(closed_candle_close=100.4)
        sm.ticks = FakeTicksForRun()
        sm.active = True
        sm.channel_id = 222
        sm.session_id = "S2"

        # simulate close() being called between signal photo and result fetch
        async def _sleep_closes(ts):
            sm.active = False
        sm._sleep_until = _sleep_closes

        market = {"code": "EURUSD_otc", "display": "EUR/USD OTC", "payout": 91}
        res = {"direction": "CALL", "confidence": 80.0, "reason": "test"}
        asyncio.run(sm._run_signal(market, res))
        # only the initial signal photo should have been sent; result aborted
        assert len(sm.bot.photos) == 1


# =============================================================
# start()/close() task-lifecycle guards
# =============================================================


class TestSessionLifecycle:
    def test_close_cancels_both_task_and_refresh_task(self):
        sm = SessionManager()

        async def _run():
            async def _long():
                try:
                    await asyncio.sleep(100)
                except asyncio.CancelledError:
                    raise

            sm.task = asyncio.create_task(_long())
            sm.refresh_task = asyncio.create_task(_long())
            sm.active = True
            # give both tasks a tick to start
            await asyncio.sleep(0)

            sm.close()
            # both must be scheduled for cancellation
            assert sm.active is False
            # await them
            for t in (sm.task, sm.refresh_task):
                try:
                    await t
                except asyncio.CancelledError:
                    pass
            assert sm.task.cancelled()
            assert sm.refresh_task.cancelled()

        asyncio.run(_run())

    def test_start_cancels_previous_running_tasks(self):
        """A second start() call must cancel the old _loop/refresh tasks
        before spawning new ones."""
        sm = SessionManager()

        old_tasks = {}

        async def _run():
            async def _long():
                await asyncio.sleep(100)

            # simulate a prior session's tasks
            sm.task = asyncio.create_task(_long())
            sm.refresh_task = asyncio.create_task(_long())
            old_tasks["task"] = sm.task
            old_tasks["refresh"] = sm.refresh_task
            await asyncio.sleep(0)

            # Prevent the new _loop from doing anything meaningful
            async def _noop_loop():
                # keep the new task alive briefly so we can inspect it
                await asyncio.sleep(0.05)
            sm._loop = _noop_loop

            markets = [{"code": "X", "display": "X", "payout": 90}]
            await sm.start(
                bot=FakeBot(), qx=FakeQxForRun(100.4),
                markets=markets, channels=[{"id": 1, "title": "t"}],
                auto_mode=False,
            )

            # Old tasks must be cancelled
            assert old_tasks["task"].cancelled() or old_tasks["task"].done()
            assert old_tasks["refresh"].cancelled() or old_tasks["refresh"].done()
            # New task is different
            assert sm.task is not old_tasks["task"]
            # cleanup
            sm.close()
            try:
                await sm.task
            except (asyncio.CancelledError, Exception):
                pass

        asyncio.run(_run())



# =============================================================
# QuotexManager.get_markets — category filter (otc / real / otcreal / all)
# =============================================================


class TestGetMarketsCategoryFilter:
    def _fake_client_with(self, instruments):
        client = MagicMock()

        async def _instruments():
            return instruments

        client.get_instruments = _instruments
        return client

    def _instruments_row(self, code, display, payout=88, is_open=True):
        row = [0] * 20
        row[1] = code
        row[2] = display
        row[5] = payout
        row[-9] = payout
        row[14] = is_open
        return row

    def _run(self, category, instruments):
        qm = qx_mod.QuotexManager()
        qm.client = self._fake_client_with(instruments)
        qm.connected = True

        async def _ensure():
            return

        qm.ensure_connected = _ensure
        return asyncio.run(qm.get_markets(category))

    def test_otc_returns_only_non_crypto_otc(self):
        rows = [
            self._instruments_row("EURUSD_otc", "EUR/USD OTC"),
            self._instruments_row("EURUSD", "EUR/USD"),
            self._instruments_row("BTCUSD_otc", "BTC/USD OTC"),
        ]
        codes = [m["code"] for m in self._run("otc", rows)]
        assert codes == ["EURUSD_otc"]

    def test_real_returns_only_real(self):
        rows = [
            self._instruments_row("EURUSD_otc", "EUR/USD OTC"),
            self._instruments_row("EURUSD", "EUR/USD"),
            self._instruments_row("BTCUSD_otc", "BTC/USD OTC"),
        ]
        codes = [m["code"] for m in self._run("real", rows)]
        assert codes == ["EURUSD"]

    def test_otcreal_returns_otc_and_real_no_crypto(self):
        rows = [
            self._instruments_row("EURUSD_otc", "EUR/USD OTC", 90),
            self._instruments_row("USDJPY", "USD/JPY", 85),
            self._instruments_row("BTCUSD_otc", "BTC/USD OTC", 95),  # crypto excluded
            self._instruments_row("ETHUSD_otc", "ETH/USD OTC", 92),  # crypto excluded
        ]
        codes = sorted(m["code"] for m in self._run("otcreal", rows))
        assert codes == ["EURUSD_otc", "USDJPY"]

    def test_all_still_returns_everything(self):
        rows = [
            self._instruments_row("EURUSD_otc", "EUR/USD OTC"),
            self._instruments_row("USDJPY", "USD/JPY"),
            self._instruments_row("BTCUSD_otc", "BTC/USD OTC"),
        ]
        codes = sorted(m["code"] for m in self._run("all", rows))
        assert codes == ["BTCUSD_otc", "EURUSD_otc", "USDJPY"]

    def test_status_text_shows_category_label(self):
        sm = SessionManager()
        sm.auto_mode = True
        sm.auto_threshold = 80
        sm.auto_category = "otcreal"
        sm.channels = [{"id": 1, "title": "CH"}]
        sm.markets = []
        txt = sm.running_status_text()
        assert "OTC + Real" in txt
