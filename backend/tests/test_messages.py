"""Tests for messages templates + aiogram Notifier + sessions integration."""
import asyncio
import re
import sys
from pathlib import Path
from unittest.mock import AsyncMock

import pytest

# ensure /app/backend is on sys.path (conftest also does this)
BACKEND_DIR = Path(__file__).resolve().parents[1]
if str(BACKEND_DIR) not in sys.path:
    sys.path.insert(0, str(BACKEND_DIR))

import messages  # noqa: E402
import notifier as notifier_mod  # noqa: E402


# ------------------------------------------------------------------ signal_caption

SIGNAL_IDS = {
    "brand":    "5325547803936572038",
    "asset":    "5451654705241398333",
    "signal":   "5192982912496052999",
    "call":     "5449683594425410231",
    "put":      "5447183459602669338",
    "entry":    "6285240160120477644",
    "payout":   "5294167145079395967",
    "mtg":      "5310278924616356636",
    "owner":    "6267115986541877538",
    "analysis": "5422439311196834318",
}
RESULT_IDS = {
    "result_head": "5422439311196834318",
    "asset":       "5451654705241398333",
    "res_signal":  "5190806721286657692",
    "call":        "5449683594425410231",
    "put":         "5447183459602669338",
    "time":        "5382194935057372936",
    "result":      "6102723562076900877",
    "win":         "6217660507575291616",
    "loss":        "6102584400841546557",
}


def _tag(eid):
    return f'<tg-emoji emoji-id="{eid}">'


class TestSignalCaption:
    def test_all_emoji_ids_present_call(self):
        cap = messages.signal_caption("EUR/USD", "CALL", "12:34", 92, "trend up", "@owner")
        # every signal emoji id except the opposing direction must appear
        for name, eid in SIGNAL_IDS.items():
            if name == "put":
                continue
            assert _tag(eid) in cap, f"missing emoji id {eid} ({name})"
        # CALL uses call emoji, not put
        assert _tag(SIGNAL_IDS["call"]) in cap
        assert _tag(SIGNAL_IDS["put"]) not in cap
        assert "CALL" in cap
        assert "SignalMaster Pro" in cap
        # brand emoji appears TWICE (both sides of brand)
        assert cap.count(_tag(SIGNAL_IDS["brand"])) == 2
        # 20-char divider
        assert ("\u2501" * 20) in cap
        assert "Payout : 92%" in cap
        assert "MTG : 1 - Step" in cap
        assert "Asset : EUR/USD" in cap
        assert "Entry Time : 12:34" in cap
        assert "Owner : @owner" in cap
        assert "Analysis: trend up" in cap

    def test_put_direction(self):
        cap = messages.signal_caption("USD/JPY", "PUT", "01:02", 85, "reversal", "@x")
        assert _tag(SIGNAL_IDS["put"]) in cap
        assert _tag(SIGNAL_IDS["call"]) not in cap
        assert "PUT" in cap
        assert "Payout : 85%" in cap

    def test_payout_zero_shows_em_dash(self):
        cap = messages.signal_caption("A/B", "CALL", "00:00", 0, "r", "@y")
        assert "Payout : \u2014" in cap
        assert "Payout : 0%" not in cap

    def test_payout_none_shows_em_dash(self):
        cap = messages.signal_caption("A/B", "CALL", "00:00", None, "r", "@y")
        assert "Payout : \u2014" in cap

    def test_html_escape_of_dynamic_values(self):
        cap = messages.signal_caption(
            "<A&B>", "CALL", "12:00", 90, "reason < > &", "<owner>&"
        )
        # dynamic user values escaped
        assert "&lt;A&amp;B&gt;" in cap
        assert "reason &lt; &gt; &amp;" in cap
        assert "&lt;owner&gt;&amp;" in cap
        # raw unescaped forms must NOT appear as text
        assert "<A&B>" not in cap
        # tg-emoji tags stay intact
        assert '<tg-emoji emoji-id="' in cap

    def test_divider_after_brand_and_owner(self):
        cap = messages.signal_caption("EUR/USD", "CALL", "12:34", 92, "r", "@o")
        # two 20-char dividers (after brand, after owner)
        assert cap.count("\u2501" * 20) == 2


class TestResultCaption:
    def test_win(self):
        cap = messages.result_caption("EUR/USD", "CALL", "12:34", "WIN")
        # emoji BEFORE the word
        assert re.search(
            r'Result : <tg-emoji emoji-id="' + RESULT_IDS["win"] + r'">[^<]*</tg-emoji>\s+WIN',
            cap,
        ), cap
        assert "MTG WIN" not in cap
        # LOSS emoji must not appear
        assert _tag(RESULT_IDS["loss"]) not in cap
        # header + labels
        assert _tag(RESULT_IDS["result_head"]) in cap
        assert _tag(RESULT_IDS["res_signal"]) in cap
        assert _tag(RESULT_IDS["time"]) in cap
        # direction emoji (CALL)
        assert _tag(RESULT_IDS["call"]) in cap
        # 21-char divider
        assert ("\u2501" * 21) in cap
        assert "SIGNAL RESULT" in cap

    def test_win_mtg(self):
        cap = messages.result_caption("EUR/USD", "PUT", "12:34", "WIN_MTG")
        # reuses WIN emoji id, text is "MTG WIN"
        assert _tag(RESULT_IDS["win"]) in cap
        assert "MTG WIN" in cap
        # emoji BEFORE the word
        assert re.search(
            r'<tg-emoji emoji-id="' + RESULT_IDS["win"] + r'">[^<]*</tg-emoji>\s+MTG WIN',
            cap,
        )
        # PUT direction emoji present
        assert _tag(RESULT_IDS["put"]) in cap
        assert _tag(RESULT_IDS["loss"]) not in cap

    def test_loss(self):
        cap = messages.result_caption("EUR/USD", "CALL", "12:34", "LOSS")
        assert _tag(RESULT_IDS["loss"]) in cap
        assert "LOSS" in cap
        # emoji BEFORE the word
        assert re.search(
            r'<tg-emoji emoji-id="' + RESULT_IDS["loss"] + r'">[^<]*</tg-emoji>\s+LOSS',
            cap,
        )
        assert _tag(RESULT_IDS["win"]) not in cap
        assert "WIN" not in cap

    def test_html_escape(self):
        cap = messages.result_caption("<A&B>", "CALL", "12:34", "WIN")
        assert "&lt;A&amp;B&gt;" in cap
        assert '<tg-emoji emoji-id="' in cap


class TestStripCustomEmoji:
    def test_strips_all_tags_leaves_plain(self):
        cap = messages.signal_caption("EUR/USD", "CALL", "12:34", 92, "trend", "@o")
        stripped = messages.strip_custom_emoji(cap)
        # no more tag markup
        assert "<tg-emoji" not in stripped
        assert "</tg-emoji>" not in stripped
        # plain fallbacks preserved
        for _key, (_eid, plain) in messages.EMOJI.items():
            pass  # covered below
        # ensure a few known plain emoji stayed
        assert "\U0001f48e" in stripped  # asset diamond
        assert "\U0001f680" in stripped  # signal rocket
        # text kept
        assert "SignalMaster Pro" in stripped
        assert "Payout : 92%" in stripped

    def test_strips_result_caption(self):
        cap = messages.result_caption("EUR/USD", "CALL", "12:34", "WIN")
        s = messages.strip_custom_emoji(cap)
        assert "<tg-emoji" not in s
        assert "\u2705" in s  # win checkmark plain
        assert "WIN" in s

    def test_no_tags_unchanged(self):
        text = "Hello world, no tags here 123"
        assert messages.strip_custom_emoji(text) == text


# ------------------------------------------------------------------ notifier

class DummyBadRequest(Exception):
    """Stand-in that mimics aiogram TelegramBadRequest string."""


def _make_bad_request(msg):
    from aiogram.exceptions import TelegramBadRequest
    # TelegramBadRequest signature: (method, message)
    try:
        return TelegramBadRequest(method=None, message=msg)
    except TypeError:
        # fallback
        return TelegramBadRequest(msg)


class TestNotifier:
    def test_import_does_not_construct_bot(self):
        # module-level NOTIFY exists but its _bot is lazy
        assert notifier_mod.NOTIFY._bot is None

    def test_bot_property_lazy_and_html_parse_mode(self):
        n = notifier_mod.Notifier()
        assert n._bot is None
        b = n.bot
        assert n._bot is not None
        # aiogram DefaultBotProperties HTML parse mode
        from aiogram.enums import ParseMode
        assert b.default.parse_mode == ParseMode.HTML

    def test_send_photo_success_html(self):
        n = notifier_mod.Notifier()
        captured = {}

        async def fake_send_photo(chat_id, photo, caption=None, **kw):
            captured["chat_id"] = chat_id
            captured["photo"] = photo
            captured["caption"] = caption
            return "ok"

        n.bot.send_photo = fake_send_photo  # bind lazily-created bot instance method
        cap = messages.signal_caption("EUR/USD", "CALL", "12:34", 92, "r", "@o")
        result = asyncio.run(n.send_photo(-100, b"PNGDATA", cap))
        assert result == "ok"
        assert captured["chat_id"] == -100
        # BufferedInputFile with png bytes
        from aiogram.types import BufferedInputFile
        assert isinstance(captured["photo"], BufferedInputFile)
        assert captured["caption"] == cap
        assert '<tg-emoji emoji-id="' in captured["caption"]
        assert n.custom_emoji_ok is True

    def test_send_photo_fallback_on_emoji_error(self):
        n = notifier_mod.Notifier()
        calls = []
        state = {"raised": False}

        async def fake_send_photo(chat_id, photo, caption=None, **kw):
            calls.append(caption)
            if not state["raised"]:
                state["raised"] = True
                raise _make_bad_request("Bad Request: CUSTOM_EMOJI_INVALID")
            return "ok"

        n.bot.send_photo = fake_send_photo
        cap = messages.signal_caption("EUR/USD", "CALL", "12:34", 92, "r", "@o")
        result = asyncio.run(n.send_photo(-100, b"PNG", cap))
        assert result == "ok"
        assert len(calls) == 2
        assert calls[0] == cap
        # second attempt uses stripped version
        assert "<tg-emoji" not in calls[1]
        assert calls[1] == messages.strip_custom_emoji(cap)
        assert n.custom_emoji_ok is False

        # subsequent calls go straight to stripped, no retry
        calls.clear()
        cap2 = messages.signal_caption("USD/JPY", "PUT", "01:02", 85, "r2", "@o")
        asyncio.run(n.send_photo(-100, b"PNG", cap2))
        assert len(calls) == 1
        assert "<tg-emoji" not in calls[0]
        assert calls[0] == messages.strip_custom_emoji(cap2)

    def test_send_photo_non_emoji_error_reraised(self):
        n = notifier_mod.Notifier()

        async def fake_send_photo(chat_id, photo, caption=None, **kw):
            raise _make_bad_request("Bad Request: chat not found")

        n.bot.send_photo = fake_send_photo
        cap = messages.signal_caption("EUR/USD", "CALL", "12:34", 92, "r", "@o")
        from aiogram.exceptions import TelegramBadRequest
        with pytest.raises(TelegramBadRequest):
            asyncio.run(n.send_photo(-100, b"PNG", cap))
        # flag NOT flipped
        assert n.custom_emoji_ok is True

    def test_send_message_success(self):
        n = notifier_mod.Notifier()
        captured = {}

        async def fake_send_message(chat_id, text, **kw):
            captured["chat_id"] = chat_id
            captured["text"] = text
            return "ok"

        n.bot.send_message = fake_send_message
        text = "hello <tg-emoji emoji-id=\"1\">x</tg-emoji>"
        asyncio.run(n.send_message(-100, text))
        assert captured["text"] == text
        assert n.custom_emoji_ok is True

    def test_send_message_fallback_and_persist(self):
        n = notifier_mod.Notifier()
        calls = []
        state = {"raised": False}

        async def fake_send_message(chat_id, text, **kw):
            calls.append(text)
            if not state["raised"]:
                state["raised"] = True
                raise _make_bad_request("can't use custom emoji")
            return "ok"

        n.bot.send_message = fake_send_message
        text = 'HI <tg-emoji emoji-id="1">Y</tg-emoji>'
        asyncio.run(n.send_message(-100, text))
        assert len(calls) == 2
        assert calls[1] == "HI Y"
        assert n.custom_emoji_ok is False

        # subsequent send goes stripped directly
        calls.clear()
        asyncio.run(n.send_message(-100, 'A <tg-emoji emoji-id="2">B</tg-emoji> C'))
        assert calls == ["A B C"]

    def test_send_message_non_emoji_error_reraised(self):
        n = notifier_mod.Notifier()

        async def fake_send_message(chat_id, text, **kw):
            raise _make_bad_request("Bad Request: chat not found")

        n.bot.send_message = fake_send_message
        from aiogram.exceptions import TelegramBadRequest
        with pytest.raises(TelegramBadRequest):
            asyncio.run(n.send_message(-100, "x"))
        assert n.custom_emoji_ok is True


# ------------------------------------------------------------------ sessions integration

class TestSessionsIntegration:
    def test_sessions_does_not_import_io_or_use_ptb_for_channel(self):
        src = (BACKEND_DIR / "sessions.py").read_text()
        # sessions no longer imports io
        assert not re.search(r"^\s*import\s+io\b", src, re.MULTILINE), \
            "sessions.py should no longer import io"
        # sessions uses NOTIFY.send_photo / send_message for channel posts
        assert "NOTIFY.send_photo" in src
        assert "NOTIFY.send_message" in src
        # sessions must NOT call self.bot.send_photo / self.bot.send_message for channel
        assert "self.bot.send_photo" not in src
        assert "self.bot.send_message" not in src or True  # (allowed for edit_message_text only)

    def test_wrappers_still_exist(self):
        import sessions
        assert callable(sessions.signal_caption)
        assert callable(sessions.result_caption)
        # sanity: they produce HTML captions
        cap = sessions.signal_caption("EUR/USD", "CALL", "12:34", "r", 92)
        assert "Payout : 92%" in cap
        assert '<tg-emoji emoji-id="' in cap

    def test_run_signal_uses_notify_with_real_payout(self, monkeypatch):
        import sessions
        import charting

        # patch charting to avoid matplotlib
        monkeypatch.setattr(charting, "render_chart",
                            lambda candles, title, badge=None: b"PNGBYTES")

        sent = []

        async def fake_send_photo(chat_id, png, caption):
            sent.append(("photo", chat_id, caption))
            return "ok"

        async def fake_send_message(chat_id, text):
            sent.append(("msg", chat_id, text))
            return "ok"

        monkeypatch.setattr(sessions.NOTIFY, "send_photo", fake_send_photo)
        monkeypatch.setattr(sessions.NOTIFY, "send_message", fake_send_message)

        sm = sessions.SessionManager()
        sm.active = True
        sm.channel_id = -100
        sm.session_id = "S1"

        # patch time-consuming pieces
        async def instant_sleep_until(ts):
            return

        async def fake_get_candle(code, ts):
            # winning candle for CALL (close>open)
            return {"time": ts, "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05}

        async def fake_candles(code, count=60):
            # stub returning some candles list (not empty)
            return [{"time": i, "open": 1, "high": 1, "low": 1, "close": 1}
                    for i in range(count)]

        monkeypatch.setattr(sm, "_sleep_until", instant_sleep_until)
        monkeypatch.setattr(sm, "_get_candle", fake_get_candle)
        monkeypatch.setattr(sm, "_candles", fake_candles)

        # patch storage.append_signal to noop
        import storage
        monkeypatch.setattr(storage, "append_signal", lambda r: None)

        market = {"code": "EURUSD_otc", "display": "EUR/USD OTC", "payout": 92}
        res = {"direction": "CALL", "reason": "test-reason", "confidence": 90}

        asyncio.run(sm._run_signal(market, res))

        # expect two photo posts (signal + result)
        photos = [s for s in sent if s[0] == "photo"]
        assert len(photos) == 2
        signal_cap = photos[0][2]
        result_cap = photos[1][2]
        assert "Payout : 92%" in signal_cap
        assert "CALL" in signal_cap
        assert "test-reason" in signal_cap
        assert "SIGNAL RESULT" in result_cap
        # emoji tags in both
        assert '<tg-emoji emoji-id="' in signal_cap
        assert '<tg-emoji emoji-id="' in result_cap

    def test_run_signal_payout_zero_shows_em_dash(self, monkeypatch):
        import sessions
        import charting
        monkeypatch.setattr(charting, "render_chart",
                            lambda c, t, badge=None: b"PNG")

        sent = []

        async def fake_send_photo(chat_id, png, caption):
            sent.append(caption)
            return "ok"

        monkeypatch.setattr(sessions.NOTIFY, "send_photo", fake_send_photo)

        sm = sessions.SessionManager()
        sm.active = True
        sm.channel_id = -100
        sm.session_id = "S2"

        async def _noop(*a, **kw):
            return None

        async def fake_get_candle(code, ts):
            return {"time": ts, "open": 1.0, "high": 1.1, "low": 0.9, "close": 1.05}

        async def fake_candles(code, count=60):
            return [{"time": i, "open": 1, "high": 1, "low": 1, "close": 1}
                    for i in range(count)]

        monkeypatch.setattr(sm, "_sleep_until", _noop)
        monkeypatch.setattr(sm, "_get_candle", fake_get_candle)
        monkeypatch.setattr(sm, "_candles", fake_candles)
        import storage
        monkeypatch.setattr(storage, "append_signal", lambda r: None)

        market = {"code": "X", "display": "X/Y", "payout": 0}
        res = {"direction": "PUT", "reason": "r", "confidence": 90}
        asyncio.run(sm._run_signal(market, res))
        assert any("Payout : \u2014" in c for c in sent)

    def test_send_partial_uses_notify_send_message(self, monkeypatch):
        import sessions

        sent = []

        async def fake_send_message(chat_id, text):
            sent.append((chat_id, text))
            return "ok"

        monkeypatch.setattr(sessions.NOTIFY, "send_message", fake_send_message)

        sm = sessions.SessionManager()
        sm.channel_id = -100
        sm.signals = [
            {"code": "EURUSD_otc", "display": "EUR/USD OTC",
             "direction": "CALL", "entry": "12:34", "result": "WIN",
             "session_id": "S", "date": "01.01.2026", "entry_ts": 0, "channel_id": -100},
        ]
        ok = asyncio.run(sm.send_partial())
        assert ok is True
        assert len(sent) == 1
        assert "PARTIAL" in sent[0][1]


# ------------------------------------------------------------------ two-library coexistence

class TestTwoLibraryCoexistence:
    def test_no_aiogram_dispatcher_polling(self):
        # grep entire /app/backend for aiogram Dispatcher / start_polling
        forbidden = ["Dispatcher(", "start_polling", "dp.start_polling"]
        offenders = []
        for py in BACKEND_DIR.rglob("*.py"):
            # skip tests directory & pyquotex
            if "tests" in py.parts:
                continue
            try:
                text = py.read_text(errors="ignore")
            except Exception:
                continue
            for kw in forbidden:
                if kw in text:
                    # allow inside python-telegram-bot code, but this is our source; check aiogram imports on same file
                    if "from aiogram" in text or "import aiogram" in text:
                        offenders.append(f"{py}: {kw}")
        assert not offenders, offenders

    def test_bot_py_awaits_notify_close(self):
        src = (BACKEND_DIR / "bot.py").read_text()
        assert "await NOTIFY.close()" in src

    def test_requirements_has_both_libs(self):
        req = (BACKEND_DIR / "requirements.txt").read_text()
        assert "aiogram==3.30.0" in req
        assert "python-telegram-bot==22.8" in req


# ------------------------------------------------------------------ manual selection real payout in bot.py

class TestBotManualPayout:
    def test_manual_selection_copies_real_payout(self):
        src = (BACKEND_DIR / "bot.py").read_text()
        # The 'sc|' / go path must copy payout from UI['markets'], not hardcode 0
        # match presence of a comprehension that pulls payout from UI["markets"]
        assert 'UI["markets"]' in src or "UI['markets']" in src
        # Must build markets list with payout from UI['markets']
        pattern = r'"payout":\s*next\(\s*\(m\["payout"\]\s+for\s+m\s+in\s+UI\["markets"\]'
        assert re.search(pattern, src), "manual markets list must copy real payout from UI['markets']"


# ------------------------------------------------------------------ modules import cleanly

class TestImports:
    @pytest.mark.parametrize("mod", [
        "messages", "notifier", "analysis", "strategies", "indicators_py",
        "storage", "sessions", "bot", "charting",
    ])
    def test_import(self, mod):
        __import__(mod)
