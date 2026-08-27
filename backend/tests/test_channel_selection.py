"""Offline unit tests for the two changes under review:

1. bot.on_my_chat_member  -> promotion never auto-connects a channel,
   losing admin (LEFT/BANNED/MEMBER) auto-removes it.
   bot.on_chat_shared     -> Add Channel flow only stores when bot is admin.
2. Channel multi-select at session start (min 1 / max 2) and multi-channel
   broadcasting in sessions.SessionManager.

No network calls: telegram Update/CallbackQuery/Bot, quotex client, charting
and sessions.NOTIFY are all replaced with local async stubs, and storage is
redirected to tmp files so /app/backend/data/*.json is never touched.
"""
import asyncio
import sys

import pytest

sys.path.insert(0, "/app/backend")

from telegram.constants import ChatMemberStatus  # noqa: E402

import bot as bot_mod  # noqa: E402
import sessions as sessions_mod  # noqa: E402
import storage as storage_mod  # noqa: E402
from sessions import SessionManager  # noqa: E402


# =============================================================
# Stubs
# =============================================================
class FakeBot:
    def __init__(self, member_status=ChatMemberStatus.ADMINISTRATOR, raise_on_member=False,
                 title="My Channel"):
        self.id = 999
        self.sent = []
        self._member_status = member_status
        self._raise = raise_on_member
        self._title = title

    async def send_message(self, chat_id=None, text=None, **kw):
        self.sent.append((chat_id, text))
        return type("Msg", (), {"message_id": 1, "chat_id": chat_id})()

    async def get_chat_member(self, chat_id, user_id):
        if self._raise:
            raise RuntimeError("boom")
        return type("CM", (), {"status": self._member_status})()

    async def get_chat(self, chat_id):
        return type("Chat", (), {"id": chat_id, "title": self._title})()


class FakeCtx:
    def __init__(self, bot):
        self.bot = bot


class FakeMessage:
    def __init__(self, chat_shared=None, text=None):
        self.chat_shared = chat_shared
        self.text = text
        self.chat_id = 1
        self.message_id = 55
        self.replies = []

    async def reply_text(self, text, **kw):
        self.replies.append(text)


class FakeQuery:
    def __init__(self, data, message=None):
        self.data = data
        self.message = message or FakeMessage()
        self.answers = []       # (text, show_alert)
        self.edits = []         # (text, reply_markup)

    async def answer(self, text=None, show_alert=False, **kw):
        self.answers.append((text, show_alert))

    async def edit_message_text(self, text, reply_markup=None, **kw):
        self.edits.append((text, reply_markup))


class FakeUpdate:
    def __init__(self, user_id=1, callback_query=None, message=None, my_chat_member=None):
        self.effective_user = type("U", (), {"id": user_id})()
        self.callback_query = callback_query
        self.message = message
        self.my_chat_member = my_chat_member


class FakeChatMemberUpdated:
    def __init__(self, chat_id, chat_type, status, title="Chan"):
        self.chat = type("C", (), {"id": chat_id, "type": chat_type, "title": title})()
        self.new_chat_member = type("M", (), {"status": status})()


class SMStub:
    def __init__(self, running=False):
        self.running = running
        self.start_calls = []
        self.refreshed = 0
        self.signals = []

    def is_running(self):
        return self.running

    async def start(self, *args, **kwargs):
        self.start_calls.append((args, kwargs))

    async def _refresh_admin_view(self):
        self.refreshed += 1


class NotifyStub:
    def __init__(self, fail_ids=()):
        self.photos = []
        self.texts = []
        self.fail_ids = set(fail_ids)

    async def send_photo(self, chat_id, png, caption=None, **kw):
        if chat_id in self.fail_ids:
            raise RuntimeError("send failed")
        self.photos.append((chat_id, caption))

    async def send_message(self, chat_id, text, **kw):
        if chat_id in self.fail_ids:
            raise RuntimeError("send failed")
        self.texts.append((chat_id, text))


# =============================================================
# Fixtures
# =============================================================
@pytest.fixture
def tmp_storage(monkeypatch, tmp_path):
    """Redirect all storage json files to a tmp dir."""
    monkeypatch.setattr(storage_mod, "CHANNELS_FILE", tmp_path / "channels.json")
    monkeypatch.setattr(storage_mod, "SETTINGS_FILE", tmp_path / "settings.json")
    monkeypatch.setattr(storage_mod, "SIGNALS_FILE", tmp_path / "signals.json")
    return tmp_path


@pytest.fixture(autouse=True)
def clean_ui():
    saved = dict(bot_mod.UI)
    bot_mod.UI.update({"category": None, "markets": [], "page": 0, "selected": {},
                       "auto": None, "auto_cat": None, "await_pct": False,
                       "await_pt": False, "channels": {}})
    bot_mod.UI.pop("ch_header", None)
    yield
    bot_mod.UI.clear()
    bot_mod.UI.update(saved)


def _channels(*items):
    return [{"id": i, "title": t} for i, t in items]


# =============================================================
# 1. on_my_chat_member — no auto-connect
# =============================================================
class TestOnMyChatMember:
    async def test_promotion_does_not_add_channel(self, tmp_storage, monkeypatch):
        added = []
        monkeypatch.setattr(storage_mod, "add_channel",
                            lambda *a: added.append(a) or True)
        b = FakeBot()
        upd = FakeUpdate(my_chat_member=FakeChatMemberUpdated(
            -100, "channel", ChatMemberStatus.ADMINISTRATOR, "Promo Chan"))
        await bot_mod.on_my_chat_member(upd, FakeCtx(b))

        assert added == [], "storage.add_channel must NOT be called on promotion"
        assert storage_mod.get_channels() == []
        assert len(b.sent) == 1
        chat_id, text = b.sent[0]
        assert chat_id == bot_mod.ADMIN_ID
        assert "Promo Chan" in text
        assert "Add Channel" in text
        assert "NOT connected" in text

    @pytest.mark.parametrize("status", [ChatMemberStatus.LEFT,
                                        ChatMemberStatus.BANNED,
                                        ChatMemberStatus.MEMBER])
    async def test_demotion_removes_channel(self, tmp_storage, status):
        storage_mod.add_channel(-100, "Chan")
        assert storage_mod.get_channels() == _channels((-100, "Chan"))
        upd = FakeUpdate(my_chat_member=FakeChatMemberUpdated(-100, "channel", status))
        b = FakeBot()
        await bot_mod.on_my_chat_member(upd, FakeCtx(b))
        assert storage_mod.get_channels() == []
        assert b.sent == []

    async def test_non_channel_chat_ignored(self, tmp_storage, monkeypatch):
        touched = []
        monkeypatch.setattr(storage_mod, "remove_channel", lambda cid: touched.append(cid))
        monkeypatch.setattr(storage_mod, "add_channel",
                            lambda *a: touched.append(a) or True)
        b = FakeBot()
        for ctype in ("group", "supergroup", "private"):
            upd = FakeUpdate(my_chat_member=FakeChatMemberUpdated(
                -1, ctype, ChatMemberStatus.LEFT))
            await bot_mod.on_my_chat_member(upd, FakeCtx(b))
        assert touched == []
        assert b.sent == []

    async def test_missing_my_chat_member_ignored(self, tmp_storage):
        b = FakeBot()
        await bot_mod.on_my_chat_member(FakeUpdate(), FakeCtx(b))
        assert b.sent == []


# =============================================================
# 2. on_chat_shared — Add Channel flow
# =============================================================
class TestOnChatShared:
    def _update(self, request_id=100, chat_id=-100):
        shared = type("S", (), {"request_id": request_id, "chat_id": chat_id})()
        msg = FakeMessage(chat_shared=shared)
        return FakeUpdate(message=msg), msg

    @pytest.mark.parametrize("status", [ChatMemberStatus.ADMINISTRATOR,
                                        ChatMemberStatus.OWNER])
    async def test_adds_when_bot_is_admin(self, tmp_storage, status):
        upd, msg = self._update()
        b = FakeBot(member_status=status, title="Alpha Chan")
        await bot_mod.on_chat_shared(upd, FakeCtx(b))
        assert storage_mod.get_channels() == _channels((-100, "Alpha Chan"))
        assert "successfully added" in msg.replies[0]
        # main menu resent to admin
        assert b.sent and b.sent[-1][0] == bot_mod.ADMIN_ID

    @pytest.mark.parametrize("status", [ChatMemberStatus.MEMBER,
                                        ChatMemberStatus.LEFT,
                                        ChatMemberStatus.RESTRICTED])
    async def test_rejected_when_not_admin(self, tmp_storage, status):
        upd, msg = self._update()
        b = FakeBot(member_status=status)
        await bot_mod.on_chat_shared(upd, FakeCtx(b))
        assert storage_mod.get_channels() == []
        assert "NOT admin" in msg.replies[0]

    async def test_rejected_when_get_chat_member_raises(self, tmp_storage):
        upd, msg = self._update()
        b = FakeBot(raise_on_member=True)
        await bot_mod.on_chat_shared(upd, FakeCtx(b))
        assert storage_mod.get_channels() == []
        assert "NOT admin" in msg.replies[0]

    async def test_wrong_request_id_ignored(self, tmp_storage):
        upd, msg = self._update(request_id=7)
        b = FakeBot()
        await bot_mod.on_chat_shared(upd, FakeCtx(b))
        assert storage_mod.get_channels() == []
        assert msg.replies == []

    async def test_duplicate_channel_updates_title(self, tmp_storage):
        storage_mod.add_channel(-100, "Old Title")
        upd, msg = self._update()
        b = FakeBot(title="New Title")
        await bot_mod.on_chat_shared(upd, FakeCtx(b))
        assert storage_mod.get_channels() == _channels((-100, "New Title"))
        assert "already connected" in msg.replies[0]

    async def test_non_admin_user_ignored(self, tmp_storage):
        upd, msg = self._update()
        upd.effective_user = type("U", (), {"id": 424242})()
        b = FakeBot()
        await bot_mod.on_chat_shared(upd, FakeCtx(b))
        assert storage_mod.get_channels() == []
        assert msg.replies == []


# =============================================================
# 3. Channel multi-select UI rendering
# =============================================================
class TestChannelSelectUI:
    def test_keyboard_marks_selection(self, tmp_storage, monkeypatch):
        monkeypatch.setattr(bot_mod.storage, "get_channels",
                            lambda: _channels((1, "One"), (2, "Two"), (3, "Three")))
        bot_mod.UI["channels"] = {2: "Two"}
        kb = bot_mod.session_channel_kb()
        rows = kb.inline_keyboard
        assert len(rows) == 5  # 3 channels + start + back
        labels = [r[0].text for r in rows[:3]]
        assert labels[0].startswith("\U0001f4e2")
        assert labels[1].startswith("\u2705")
        assert labels[2].startswith("\U0001f4e2")
        assert [r[0].callback_data for r in rows[:3]] == ["sc|1", "sc|2", "sc|3"]
        assert rows[3][0].callback_data == "scgo"
        assert "Start Session (1/2)" in rows[3][0].text
        assert rows[4][0].callback_data == "c|back"

    def test_view_header_and_hint(self, tmp_storage, monkeypatch):
        monkeypatch.setattr(bot_mod.storage, "get_channels",
                            lambda: _channels((1, "One"), (2, "Two")))
        bot_mod.UI["channels"] = {}
        text, _ = bot_mod.channel_select_view("MY HEADER")
        assert text.startswith("MY HEADER")
        assert "min 1, max 2" in text
        assert "Selected" not in text

        bot_mod.UI["channels"] = {1: "One", 2: "Two"}
        text2, _ = bot_mod.channel_select_view()   # keeps last header
        assert text2.startswith("MY HEADER")
        assert "One" in text2 and "Two" in text2

    def test_max_constant(self):
        assert bot_mod.MAX_SESSION_CHANNELS == 2


# =============================================================
# 4. 'sc|' toggle callback
# =============================================================
class TestScToggle:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_storage, monkeypatch):
        monkeypatch.setattr(bot_mod.storage, "get_channels",
                            lambda: _channels((1, "One"), (2, "Two"), (3, "Three")))
        self.sm = SMStub()
        monkeypatch.setattr(bot_mod, "SM", self.sm)

    async def _tap(self, cid):
        q = FakeQuery(f"sc|{cid}")
        await bot_mod.on_callback(FakeUpdate(callback_query=q), FakeCtx(FakeBot()))
        return q

    async def test_select_then_deselect(self):
        q = await self._tap(1)
        assert bot_mod.UI["channels"] == {1: "One"}
        assert q.answers == [(None, False)]
        assert q.edits, "view must be refreshed after toggle"

        q = await self._tap(1)
        assert bot_mod.UI["channels"] == {}

    async def test_third_channel_rejected(self):
        await self._tap(1)
        await self._tap(2)
        q = await self._tap(3)
        assert len(bot_mod.UI["channels"]) == 2
        assert 3 not in bot_mod.UI["channels"]
        text, alert = q.answers[0]
        assert alert is True
        assert "Maximum 2" in text
        assert q.edits == [], "no view refresh expected on rejection"

    async def test_unknown_channel(self):
        q = await self._tap(777)
        text, alert = q.answers[0]
        assert alert is True and "Channel not found" in text
        assert bot_mod.UI["channels"] == {}

    async def test_blocked_while_session_running(self):
        self.sm.running = True
        q = await self._tap(1)
        text, alert = q.answers[0]
        assert alert is True and "already running" in text
        assert bot_mod.UI["channels"] == {}


# =============================================================
# 5. 'scgo' start callback (manual + auto flows)
# =============================================================
class TestScGo:
    @pytest.fixture(autouse=True)
    def _setup(self, tmp_storage, monkeypatch):
        monkeypatch.setattr(bot_mod.storage, "get_channels",
                            lambda: _channels((1, "One"), (2, "Two")))
        self.sm = SMStub()
        monkeypatch.setattr(bot_mod, "SM", self.sm)

    async def _go(self):
        q = FakeQuery("scgo")
        await bot_mod.on_callback(FakeUpdate(callback_query=q), FakeCtx(FakeBot()))
        return q

    async def test_zero_channels_rejected(self):
        bot_mod.UI["channels"] = {}
        q = await self._go()
        text, alert = q.answers[0]
        assert alert is True and "at least 1 channel" in text
        assert self.sm.start_calls == []

    async def test_manual_flow_with_two_channels(self):
        bot_mod.UI.update({"category": "otc", "selected": {"EURUSD_otc": "EUR/USD-OTC"},
                           "markets": [{"code": "EURUSD_otc", "display": "EUR/USD-OTC",
                                        "payout": 91}],
                           "channels": {1: "One", 2: "Two"}})
        q = await self._go()
        assert len(self.sm.start_calls) == 1
        args, kwargs = self.sm.start_calls[0]
        # positional: bot, qx, markets, channels
        assert len(args) >= 4
        assert args[2] == [{"code": "EURUSD_otc", "display": "EUR/USD-OTC", "payout": 91}]
        assert args[3] == [{"id": 1, "title": "One"}, {"id": 2, "title": "Two"}]
        assert kwargs.get("auto_mode") in (None, False)
        assert bot_mod.UI["channels"] == {}, "selection must be cleared after start"
        assert "One, Two" in q.edits[0][0]
        assert self.sm.refreshed == 1

    async def test_manual_flow_with_one_channel(self):
        bot_mod.UI.update({"selected": {"EURUSD_otc": "EUR/USD-OTC"},
                           "markets": [{"code": "EURUSD_otc", "display": "EUR/USD-OTC",
                                        "payout": 91}],
                           "channels": {2: "Two"}})
        await self._go()
        args, _ = self.sm.start_calls[0]
        assert args[3] == [{"id": 2, "title": "Two"}]

    async def test_auto_flow(self):
        bot_mod.UI.update({"auto": 85, "auto_cat": "otc", "channels": {1: "One"}})
        await self._go()
        args, kwargs = self.sm.start_calls[0]
        assert args[2] == []                                # markets empty for auto
        assert args[3] == [{"id": 1, "title": "One"}]
        assert kwargs["auto_mode"] is True
        assert kwargs["auto_threshold"] == 85
        assert kwargs["auto_category"] == "otc"
        assert bot_mod.UI["auto"] is None
        assert bot_mod.UI["channels"] == {}

    async def test_stale_auto_does_not_hijack_manual_flow(self):
        # user picked a threshold in Auto Select, then went back and chose markets
        # manually; the manual 'go' step must clear the stale auto threshold
        bot_mod.UI.update({"auto": 85, "auto_cat": "otc", "category": "otc",
                           "selected": {"EURUSD_otc": "EUR/USD-OTC"},
                           "markets": [{"code": "EURUSD_otc", "display": "EUR/USD-OTC",
                                        "payout": 91}],
                           "channels": {}})
        await bot_mod.on_callback(FakeUpdate(callback_query=FakeQuery("go")),
                                  FakeCtx(FakeBot()))
        bot_mod.UI["channels"] = {1: "One"}
        await self._go()
        args, kwargs = self.sm.start_calls[0]
        assert kwargs.get("auto_mode") in (None, False)
        assert args[2] == [{"code": "EURUSD_otc", "display": "EUR/USD-OTC", "payout": 91}]

    async def test_blocked_while_running(self):
        self.sm.running = True
        bot_mod.UI["channels"] = {1: "One"}
        q = await self._go()
        assert "already running" in q.answers[0][0]
        assert self.sm.start_calls == []

    async def test_go_opens_channel_select(self):
        """Manual market flow 'go' -> channel select screen with cleared picks."""
        bot_mod.UI.update({"selected": {"EURUSD_otc": "EUR/USD-OTC"},
                           "channels": {1: "One"}})
        q = FakeQuery("go")
        await bot_mod.on_callback(FakeUpdate(callback_query=q), FakeCtx(FakeBot()))
        assert bot_mod.UI["channels"] == {}
        assert "min 1, max 2" in q.edits[0][0]

    async def test_aok_opens_channel_select(self):
        bot_mod.UI["channels"] = {1: "One"}
        q = FakeQuery("aok|90")
        await bot_mod.on_callback(FakeUpdate(callback_query=q), FakeCtx(FakeBot()))
        assert bot_mod.UI["auto"] == 90
        assert bot_mod.UI["channels"] == {}
        assert "Auto Select" in q.edits[0][0]
        assert "min 1, max 2" in q.edits[0][0]

    async def test_aok_without_channels_alerts(self, monkeypatch):
        monkeypatch.setattr(bot_mod.storage, "get_channels", lambda: [])
        q = FakeQuery("aok|90")
        await bot_mod.on_callback(FakeUpdate(callback_query=q), FakeCtx(FakeBot()))
        text, alert = q.answers[0]
        assert alert is True and "No channel connected" in text
        assert q.edits == []


# =============================================================
# 6. SessionManager channel plumbing
# =============================================================
class TestSessionManagerChannels:
    def test_defaults(self):
        sm = SessionManager()
        assert sm.channels == []
        assert sm.channel_ids() == []
        assert sm.channel_titles() == ""

    async def test_start_stores_channels(self, monkeypatch):
        sm = SessionManager()

        async def fake_loop():
            return None

        monkeypatch.setattr(sm, "_loop", fake_loop)
        chans = _channels((1, "One"), (2, "Two"))
        await sm.start(FakeBot(), object(), [{"code": "A", "display": "A", "payout": 90}],
                       chans)
        assert sm.channels == chans
        assert sm.channel_ids() == [1, 2]
        assert sm.channel_titles() == "One, Two"
        txt = sm.running_status_text()
        assert "One, Two" in txt
        sm.close()
        await asyncio.sleep(0)

    async def test_start_with_none_channels(self, monkeypatch):
        sm = SessionManager()

        async def fake_loop():
            return None

        monkeypatch.setattr(sm, "_loop", fake_loop)
        await sm.start(FakeBot(), object(), [], None)
        assert sm.channels == []
        sm.close()
        await asyncio.sleep(0)


# =============================================================
# 7. Broadcast fan-out with gap + failure isolation
# =============================================================
class TestBroadcast:
    @pytest.fixture(autouse=True)
    def _fast(self, monkeypatch):
        monkeypatch.setattr(sessions_mod, "BROADCAST_GAP", 0)

    async def test_photo_to_all_channels(self, monkeypatch):
        notify = NotifyStub()
        monkeypatch.setattr(sessions_mod, "NOTIFY", notify)
        sm = SessionManager()
        sm.channels = _channels((1, "One"), (2, "Two"))
        await sm._broadcast_photo(b"png", "cap")
        assert notify.photos == [(1, "cap"), (2, "cap")]

    async def test_text_to_all_channels(self, monkeypatch):
        notify = NotifyStub()
        monkeypatch.setattr(sessions_mod, "NOTIFY", notify)
        sm = SessionManager()
        sm.channels = _channels((1, "One"), (2, "Two"))
        await sm._broadcast_text("hello")
        assert notify.texts == [(1, "hello"), (2, "hello")]

    async def test_failure_on_first_does_not_block_second(self, monkeypatch):
        notify = NotifyStub(fail_ids=(1,))
        monkeypatch.setattr(sessions_mod, "NOTIFY", notify)
        sm = SessionManager()
        sm.channels = _channels((1, "One"), (2, "Two"))
        await sm._broadcast_photo(b"png", "cap")
        await sm._broadcast_text("hi")
        assert notify.photos == [(2, "cap")]
        assert notify.texts == [(2, "hi")]

    async def test_gap_only_between_channels(self, monkeypatch):
        notify = NotifyStub()
        monkeypatch.setattr(sessions_mod, "NOTIFY", notify)
        monkeypatch.setattr(sessions_mod, "BROADCAST_GAP", 3)
        sleeps = []

        async def fake_sleep(sec):
            sleeps.append(sec)

        monkeypatch.setattr(sessions_mod.asyncio, "sleep", fake_sleep)
        sm = SessionManager()
        sm.channels = _channels((1, "One"))
        await sm._broadcast_text("x")
        assert sleeps == []          # nothing before the first channel
        sm.channels = _channels((1, "One"), (2, "Two"))
        await sm._broadcast_text("x")
        assert sleeps == [3]         # exactly one gap between two channels

    async def test_no_channels_no_send(self, monkeypatch):
        notify = NotifyStub()
        monkeypatch.setattr(sessions_mod, "NOTIFY", notify)
        sm = SessionManager()
        sm.channels = []
        await sm._broadcast_text("x")
        await sm._broadcast_photo(b"p", "c")
        assert notify.texts == [] and notify.photos == []


# =============================================================
# 8. Signal record + partial report
# =============================================================
class TestRecordAndPartial:
    @pytest.fixture
    def sm(self, monkeypatch, tmp_storage):
        monkeypatch.setattr(sessions_mod, "BROADCAST_GAP", 0)
        self.notify = NotifyStub()
        monkeypatch.setattr(sessions_mod, "NOTIFY", self.notify)
        monkeypatch.setattr(sessions_mod.charting, "render_chart",
                            lambda *a, **kw: b"PNG")
        self.appended = []
        monkeypatch.setattr(sessions_mod.storage, "append_signal",
                            lambda rec: self.appended.append(rec))
        sm = SessionManager()
        sm.active = True
        sm.session_id = "S1"
        sm.channels = _channels((10, "Chan A"), (20, "Chan B"))

        async def no_sleep(ts):
            return None

        async def candles(code, count=60):
            return [{"time": 0, "open": 1, "high": 2, "low": 0, "close": 2}]

        async def win_candle(code, ts):
            return {"time": ts, "open": 1.0, "high": 2.0, "low": 0.5, "close": 1.5}

        async def refresh():
            return None

        monkeypatch.setattr(sm, "_sleep_until", no_sleep)
        monkeypatch.setattr(sm, "_candles", candles)
        monkeypatch.setattr(sm, "_get_candle", win_candle)
        monkeypatch.setattr(sm, "_refresh_admin_view", refresh)
        return sm

    async def test_record_has_channel_ids(self, sm):
        market = {"code": "EURUSD_otc", "display": "EUR/USD-OTC", "payout": 91}
        res = {"direction": "CALL", "reason": "test", "confidence": 95.0}
        await sm._run_signal(market, res)

        assert len(self.appended) == 1
        rec = self.appended[0]
        assert rec["channel_id"] == 10
        assert rec["channel_ids"] == [10, 20]
        assert rec["result"] == "WIN"
        # signal photo + result photo to BOTH channels
        assert [c for c, _ in self.notify.photos] == [10, 20, 10, 20]

    async def test_record_channel_id_none_without_channels(self, sm):
        sm.channels = []
        market = {"code": "EURUSD_otc", "display": "EUR/USD-OTC", "payout": 91}
        res = {"direction": "CALL", "reason": "test", "confidence": 95.0}
        await sm._run_signal(market, res)
        rec = self.appended[0]
        assert rec["channel_id"] is None
        assert rec["channel_ids"] == []

    async def test_send_partial_no_channels(self, sm):
        sm.channels = []
        sm.signals = [{"result": "WIN", "code": "EURUSD_otc", "entry": "10:01",
                       "direction": "CALL"}]
        assert await sm.send_partial() is False
        assert self.notify.texts == []

    async def test_send_partial_no_signals(self, sm):
        sm.signals = []
        assert await sm.send_partial() is False
        assert self.notify.texts == []

    async def test_send_partial_broadcasts_to_all(self, sm):
        sm.signals = [{"result": "WIN", "code": "EURUSD_otc", "entry": "10:01",
                       "direction": "CALL"},
                      {"result": "LOSS", "code": "GBPUSD_otc", "entry": "10:05",
                       "direction": "PUT"}]
        assert await sm.send_partial() is True
        assert [c for c, _ in self.notify.texts] == [10, 20]
        body = self.notify.texts[0][1]
        assert self.notify.texts[1][1] == body
        # body uses unicode-monospace styling (messages.mono), compare with it
        assert sessions_mod.messages.mono("PARTIAL") in body
        assert sessions_mod.messages.mono("EURUSD-OTC") in body
