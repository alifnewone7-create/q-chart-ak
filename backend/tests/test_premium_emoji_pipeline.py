"""Offline tests for the premium (custom) emoji pipeline.

Covers:
  * module imports (regression for the user's `ImportError: cannot import name
    to_entities from premium_emojis`)
  * premium_emojis: load_premium_emojis / p_emoji / premiumize /
    strip_custom_emoji / to_entities (UTF-16 offsets)
  * user_sender: configured() + _entities() -> MessageEntityCustomEmoji
  * notifier: USER-account routing, fallback to the aiogram bot, _verify warning

No network calls: aiogram Bot and telethon TelegramClient are always stubbed.
"""
import importlib
import json
import subprocess
import sys
from pathlib import Path

import pytest

BACKEND_DIR = Path(__file__).resolve().parents[1]

import premium_emojis as pe  # noqa: E402
import messages as M  # noqa: E402
import user_sender as us  # noqa: E402
import notifier as nf  # noqa: E402

TICK = "\u2705"
SPARK = "\u2728"
ROCKET = "\U0001f680"
TICK_ID = "6217660507575291616"
SPARK_ID = "5325547803936572038"


# --------------------------------------------------------------- imports
class TestImports:
    def test_bot_imports_in_clean_subprocess(self):
        """`python -c 'import bot'` with dummy env must not raise ImportError."""
        env = {
            "PATH": "/usr/local/bin:/usr/bin:/bin",
            "BOT_TOKEN": "123:abc",
            "ADMIN_ID": "1",
            "QUOTEX_EMAIL": "a",
            "QUOTEX_PASSWORD": "b",
        }
        proc = subprocess.run(
            [sys.executable, "-c", "import bot; print('IMPORT_OK')"],
            cwd=str(BACKEND_DIR), env=env, capture_output=True, text=True, timeout=180)
        assert proc.returncode == 0, f"stdout={proc.stdout}\nstderr={proc.stderr[-3000:]}"
        assert "IMPORT_OK" in proc.stdout
        assert "ImportError" not in proc.stderr

    def test_to_entities_importable_by_name(self):
        mod = importlib.import_module("premium_emojis")
        from premium_emojis import to_entities, premiumize, strip_custom_emoji, p_emoji
        assert callable(to_entities) and callable(premiumize)
        assert callable(strip_custom_emoji) and callable(p_emoji)
        assert mod.to_entities is to_entities

    def test_module_exports_used_by_other_modules(self):
        assert us.to_entities is pe.to_entities
        assert nf.premiumize is pe.premiumize
        assert nf.plain_html is pe.plain_html


# --------------------------------------------------------- load / defaults
class TestLoad:
    def test_json_file_content(self):
        data = json.loads((BACKEND_DIR / "data" / "premium_emojis.json").read_text("utf-8"))
        assert data == {TICK: TICK_ID, SPARK: SPARK_ID}

    def test_load_premium_emojis(self):
        assert pe.load_premium_emojis() == {TICK: TICK_ID, SPARK: SPARK_ID}
        assert pe.PREMIUM_EMOJIS == {TICK: TICK_ID, SPARK: SPARK_ID}

    def test_recreates_default_file_when_deleted(self):
        path = pe.PREMIUM_EMOJIS_FILE
        original = path.read_text("utf-8")
        try:
            path.unlink()
            assert not path.exists()
            loaded = pe.load_premium_emojis()
            assert loaded == pe.DEFAULT_PREMIUM_EMOJIS
            assert path.exists()
            assert json.loads(path.read_text("utf-8")) == pe.DEFAULT_PREMIUM_EMOJIS
        finally:
            path.write_text(original, encoding="utf-8")
            pe.reload_premium_emojis()

    def test_reload_picks_up_edits(self):
        path = pe.PREMIUM_EMOJIS_FILE
        original = path.read_text("utf-8")
        try:
            path.write_text(json.dumps({ROCKET: "111"}), encoding="utf-8")
            assert pe.reload_premium_emojis() == {ROCKET: "111"}
            assert pe.p_emoji(ROCKET) == f'<tg-emoji emoji-id="111">{ROCKET}</tg-emoji>'
        finally:
            path.write_text(original, encoding="utf-8")
            pe.reload_premium_emojis()
        assert pe.PREMIUM_EMOJIS == {TICK: TICK_ID, SPARK: SPARK_ID}


# --------------------------------------------------------------- p_emoji
class TestPEmoji:
    def test_known_emoji_wrapped(self):
        assert pe.p_emoji(TICK) == f'<tg-emoji emoji-id="{TICK_ID}">{TICK}</tg-emoji>'
        assert pe.p_emoji(SPARK) == f'<tg-emoji emoji-id="{SPARK_ID}">{SPARK}</tg-emoji>'

    def test_unknown_emoji_untouched(self):
        assert pe.p_emoji(ROCKET) == ROCKET
        assert pe.p_emoji("A") == "A"


# ------------------------------------------------------------- premiumize
SIGNAL = M.signal_caption("EUR/USD OTC", "CALL", "12:30:00", 92,
                          "trend up", "@owner")
RESULT_WIN = M.result_caption("EUR/USD OTC", "CALL", "12:30:00", "WIN",
                              wins=3, losses=1, total_pct=12.5)
RESULT_LOSS = M.result_caption("EUR/USD OTC", "PUT", "12:30:00", "LOSS",
                               wins=3, losses=2, total_pct=-4.0)


class TestPremiumize:
    @pytest.mark.parametrize("text", [SIGNAL, RESULT_WIN, RESULT_LOSS])
    def test_all_known_emoji_converted(self, text):
        out = pe.premiumize(text)
        assert pe.strip_custom_emoji(out) == text  # round trip
        for char, eid in pe.PREMIUM_EMOJIS.items():
            expected = text.count(char)
            assert out.count(f'<tg-emoji emoji-id="{eid}">{char}</tg-emoji>') == expected
            # no bare occurrence left outside a tag
            assert pe.strip_custom_emoji(out).count(char) == expected

    def test_unknown_emoji_untouched(self):
        text = f"{ROCKET} hi {TICK}"
        out = pe.premiumize(text)
        assert ROCKET in out
        assert f'<tg-emoji emoji-id="{TICK_ID}">{TICK}</tg-emoji>' in out
        assert '<tg-emoji' not in out.split(ROCKET)[0]

    def test_idempotent(self):
        once = pe.premiumize(SIGNAL)
        assert pe.premiumize(once) == once
        assert pe.premiumize(pe.premiumize(once)) == once

    def test_partially_tagged_text(self):
        text = f'<tg-emoji emoji-id="{TICK_ID}">{TICK}</tg-emoji> and {SPARK}'
        out = pe.premiumize(text)
        assert out.count("<tg-emoji") == 2
        assert pe.strip_custom_emoji(out) == f"{TICK} and {SPARK}"

    def test_empty_and_none(self):
        assert pe.premiumize("") == ""
        assert pe.premiumize(None) is None

    def test_strip_is_inverse(self):
        for text in (SIGNAL, RESULT_WIN, RESULT_LOSS, f"{TICK}{SPARK}{ROCKET}"):
            assert pe.strip_custom_emoji(pe.premiumize(text)) == text


# ------------------------------------------------------------- to_entities
def utf16_slice(plain, offset, length):
    return plain.encode("utf-16-le")[offset * 2:(offset + length) * 2].decode("utf-16-le")


def no_bold(text):
    """Captions carry the owner tag in plain <b>..</b> bold; the MTProto path
    strips those tags and re-attaches them as MessageEntityBold instead."""
    return text.replace("<b>", "").replace("</b>", "")


class TestToEntities:
    @pytest.mark.parametrize("text", [SIGNAL, RESULT_WIN, RESULT_LOSS])
    def test_offsets_on_real_captions(self, text):
        plain, ents = pe.to_entities(text)
        assert "<tg-emoji" not in plain
        assert plain == no_bold(text)
        id_to_char = {int(v): k for k, v in pe.PREMIUM_EMOJIS.items()}
        expected = sum(text.count(c) for c in pe.PREMIUM_EMOJIS)
        assert len(ents) == expected
        for off, ln, eid in ents:
            assert utf16_slice(plain, off, ln) == id_to_char[eid]

    def test_accepts_already_tagged_html(self):
        plain, ents = pe.to_entities(pe.premiumize(SIGNAL))
        assert plain == no_bold(SIGNAL)
        for off, ln, eid in ents:
            assert utf16_slice(plain, off, ln) in pe.PREMIUM_EMOJIS

    def test_bold_entities_cover_the_owner_tag(self):
        body = pe.premiumize(SIGNAL)
        plain, _ = pe.to_entities(body)
        spans = pe.bold_entities(body)
        assert spans, "the owner tag must come back as a bold entity"
        assert [utf16_slice(plain, o, n) for o, n in spans] == ["@owner"]

    def test_surrogate_pairs_shift_offsets(self):
        # mono() output is Mathematical Monospace => 2 UTF-16 units per char
        text = f"{M.mono('AB')}{TICK}{M.mono('C')}{SPARK}"
        plain, ents = pe.to_entities(text)
        assert plain == text
        assert [(o, n) for o, n, _ in ents] == [(4, 1), (7, 1)]
        assert utf16_slice(plain, 4, 1) == TICK
        assert utf16_slice(plain, 7, 1) == SPARK

    def test_emoji_ids_are_ints(self):
        _, ents = pe.to_entities(f"{TICK}{SPARK}")
        assert [e[2] for e in ents] == [int(TICK_ID), int(SPARK_ID)]
        assert all(isinstance(e[2], int) for e in ents)

    def test_no_entities_when_no_known_emoji(self):
        plain, ents = pe.to_entities(f"{ROCKET} plain text")
        assert ents == []
        assert plain == f"{ROCKET} plain text"

    def test_empty_map_returns_plain(self, monkeypatch):
        monkeypatch.setattr(pe, "PREMIUM_EMOJIS", {})
        plain, ents = pe.to_entities(pe.premiumize(f"{TICK} x"))
        assert ents == []
        assert plain == f"{TICK} x"


# ------------------------------------------------------------ user_sender
class TestUserSender:
    def test_configured_false_when_empty(self, monkeypatch):
        monkeypatch.setattr(us, "TG_API_ID", "")
        monkeypatch.setattr(us, "TG_API_HASH", "")
        monkeypatch.setattr(us, "TG_SESSION", "")
        assert us.configured() is False

    @pytest.mark.parametrize("missing", ["TG_API_ID", "TG_API_HASH", "TG_SESSION"])
    def test_configured_false_when_one_missing(self, monkeypatch, missing):
        for name, val in (("TG_API_ID", "123"), ("TG_API_HASH", "h"), ("TG_SESSION", "s")):
            monkeypatch.setattr(us, name, "" if name == missing else val)
        assert us.configured() is False

    def test_configured_true_when_all_set(self, monkeypatch):
        monkeypatch.setattr(us, "TG_API_ID", "123456")
        monkeypatch.setattr(us, "TG_API_HASH", "deadbeef")
        monkeypatch.setattr(us, "TG_SESSION", "1AbC...")
        assert us.configured() is True

    def test_entities_builds_telethon_objects(self):
        from telethon.tl.types import MessageEntityCustomEmoji
        text = f"{M.mono('AB')}{TICK} ok {SPARK}"
        plain, ents = us._entities(text)
        assert plain == text
        assert len(ents) == 2
        for e in ents:
            assert isinstance(e, MessageEntityCustomEmoji)
            assert utf16_slice(plain, e.offset, e.length) in pe.PREMIUM_EMOJIS
        assert ents[0].document_id == int(TICK_ID)
        assert ents[1].document_id == int(SPARK_ID)
        assert ents[0].offset == 4 and ents[0].length == 1

    def test_entities_returns_none_when_nothing_to_attach(self):
        plain, ents = us._entities(f"{ROCKET} nothing")
        assert plain == f"{ROCKET} nothing"
        assert ents is None

    @pytest.mark.asyncio
    async def test_send_message_uses_plain_text_and_entities(self, monkeypatch):
        calls = {}

        class FakeClient:
            async def send_message(self, peer, text, formatting_entities=None):
                calls["peer"] = peer
                calls["text"] = text
                calls["ents"] = formatting_entities
                return "sent"

        sender = us.UserSender()
        fake = FakeClient()

        async def client():
            return fake

        async def peer(chat_id):
            return f"peer:{chat_id}"

        monkeypatch.setattr(sender, "client", client)
        monkeypatch.setattr(sender, "_peer", peer)
        res = await sender.send_message(-1001, pe.premiumize(f"{TICK} hi"))
        assert res == "sent"
        assert calls["text"] == f"{TICK} hi"
        assert "<tg-emoji" not in calls["text"]
        assert len(calls["ents"]) == 1
        assert calls["ents"][0].document_id == int(TICK_ID)
        assert calls["peer"] == "peer:-1001"

    @pytest.mark.asyncio
    async def test_send_photo_uses_named_buffer(self, monkeypatch):
        calls = {}

        class FakeClient:
            async def send_file(self, peer, buf, caption=None, formatting_entities=None):
                calls["name"] = buf.name
                calls["bytes"] = buf.read()
                calls["caption"] = caption
                calls["ents"] = formatting_entities
                return "photo"

        sender = us.UserSender()
        fake = FakeClient()

        async def client():
            return fake

        async def peer(chat_id):
            return chat_id

        monkeypatch.setattr(sender, "client", client)
        monkeypatch.setattr(sender, "_peer", peer)
        assert await sender.send_photo(-100, b"PNGDATA", f"{SPARK} cap") == "photo"
        assert calls["name"] == "chart.png"
        assert calls["bytes"] == b"PNGDATA"
        assert calls["caption"] == f"{SPARK} cap"
        assert calls["ents"][0].document_id == int(SPARK_ID)

    @pytest.mark.asyncio
    async def test_close_disconnects_and_resets(self):
        disconnected = []

        class FakeClient:
            async def disconnect(self):
                disconnected.append(True)

        sender = us.UserSender()
        sender._client = FakeClient()
        await sender.close()
        assert disconnected == [True]
        assert sender._client is None
        await sender.close()  # idempotent
        assert disconnected == [True]


# ---------------------------------------------------------------- notifier
class FakeBotMsg:
    def __init__(self, entities=None, caption_entities=None):
        self.entities = entities
        self.caption_entities = caption_entities


class Ent:
    def __init__(self, type_):
        self.type = type_


class FakeBot:
    def __init__(self, msg=None):
        self.msg = msg or FakeBotMsg([Ent("custom_emoji")])
        self.messages = []
        self.photos = []

    async def send_message(self, chat_id, text):
        self.messages.append((chat_id, text))
        return self.msg

    async def send_photo(self, chat_id, photo, caption=None):
        self.photos.append((chat_id, caption))
        return self.msg


class FakeUser:
    def __init__(self, fail=False):
        self.fail = fail
        self.messages = []
        self.photos = []

    async def send_message(self, chat_id, text):
        if self.fail:
            raise RuntimeError("mtproto boom")
        self.messages.append((chat_id, text))
        return "user-msg"

    async def send_photo(self, chat_id, png, caption):
        if self.fail:
            raise RuntimeError("mtproto boom")
        self.photos.append((chat_id, caption))
        return "user-photo"

    async def close(self):
        pass


@pytest.fixture
def notifier_with(monkeypatch):
    def _make(configured, user_fail=False, bot_msg=None):
        n = nf.Notifier()
        bot = FakeBot(bot_msg)
        user = FakeUser(fail=user_fail)
        n._bot = bot
        monkeypatch.setattr(nf, "user_sender_configured", lambda: configured)
        monkeypatch.setattr(nf, "USER", user)
        return n, bot, user
    return _make


class TestNotifierRouting:
    @pytest.mark.asyncio
    async def test_message_goes_to_user_account_when_configured(self, notifier_with):
        n, bot, user = notifier_with(True)
        res = await n.send_message(-100, SIGNAL)
        assert res == "user-msg"
        assert user.messages == [(-100, SIGNAL)]
        assert bot.messages == []

    @pytest.mark.asyncio
    async def test_photo_goes_to_user_account_when_configured(self, notifier_with):
        n, bot, user = notifier_with(True)
        res = await n.send_photo(-100, b"PNG", SIGNAL)
        assert res == "user-photo"
        assert user.photos == [(-100, SIGNAL)]
        assert bot.photos == []

    @pytest.mark.asyncio
    async def test_message_falls_back_to_bot_when_user_fails(self, notifier_with):
        n, bot, user = notifier_with(True, user_fail=True)
        res = await n.send_message(-100, f"{TICK} hi")
        assert res is bot.msg
        assert len(bot.messages) == 1
        assert '<tg-emoji emoji-id="%s">' % TICK_ID in bot.messages[0][1]

    @pytest.mark.asyncio
    async def test_photo_falls_back_to_bot_when_user_fails(self, notifier_with):
        n, bot, user = notifier_with(True, user_fail=True)
        res = await n.send_photo(-100, b"PNG", f"{TICK} cap")
        assert res is bot.msg
        assert len(bot.photos) == 1
        assert "<tg-emoji" in bot.photos[0][1]

    @pytest.mark.asyncio
    async def test_bot_path_when_not_configured(self, notifier_with):
        n, bot, user = notifier_with(False)
        await n.send_message(-100, SIGNAL)
        await n.send_photo(-100, b"PNG", SIGNAL)
        assert user.messages == [] and user.photos == []
        assert bot.messages[0][1] == pe.premiumize(SIGNAL)
        assert bot.photos[0][1] == pe.premiumize(SIGNAL)

    @pytest.mark.asyncio
    async def test_emoji_error_falls_back_to_plain(self, notifier_with, monkeypatch):
        from aiogram.exceptions import TelegramBadRequest
        n, bot, user = notifier_with(False)
        first = {"done": False}
        real_send = bot.send_message

        async def flaky(chat_id, text):
            if not first["done"]:
                first["done"] = True
                raise TelegramBadRequest(method=None, message="CUSTOM_EMOJI_INVALID")
            return await real_send(chat_id, text)

        bot.send_message = flaky
        await n.send_message(-100, f"{TICK} hi")
        assert bot.messages == [(-100, f"{TICK} hi")]
        assert n.custom_emoji_ok is False

    @pytest.mark.asyncio
    async def test_non_emoji_error_reraised(self, notifier_with):
        from aiogram.exceptions import TelegramBadRequest
        n, bot, user = notifier_with(False)

        async def boom(chat_id, text):
            raise TelegramBadRequest(method=None, message="chat not found")

        bot.send_message = boom
        with pytest.raises(TelegramBadRequest):
            await n.send_message(-100, f"{TICK} hi")

    @pytest.mark.asyncio
    async def test_close_closes_user_and_bot(self, notifier_with):
        n, bot, user = notifier_with(True)
        closed = []

        class Session:
            async def close(self):
                closed.append(True)

        bot.session = Session()
        await n.close()
        assert closed == [True]
        assert n._bot is None


class TestHtmlSafetyOfBotPath:
    """The bot path sends with parse_mode=HTML, so raw '<' / '&' in dynamic
    values (owner tag, asset display, reason) must be escaped before sending."""

    def test_raw_lt_and_amp_are_escaped_in_bot_payload(self):
        cap = M.signal_caption("<A&B>", "CALL", "12:00", 90, "r < >", "<owner>&")
        body = pe.premiumize(cap)
        plain_only = pe.strip_custom_emoji(body)
        assert "&lt;" in plain_only and "&amp;" in plain_only and "&gt;" in plain_only
        # only the caption's own bold markup may survive as real HTML
        assert "<b>@" not in plain_only  # the injected value stayed escaped
        assert "<" not in no_bold(plain_only)

    def test_plain_html_fallback_is_escaped(self):
        cap = M.signal_caption("<A&B>", "CALL", "12:00", 90, "r", "<owner>&")
        out = pe.plain_html(cap)
        assert "<tg-emoji" not in out
        assert "&lt;" in out and "&amp;" in out
        assert "<" not in no_bold(out)

    def test_user_account_path_keeps_raw_characters(self):
        """MTProto sends plain text + entities, so no escaping there."""
        cap = M.signal_caption("<A&B>", "CALL", "12:00", 90, "r", "<owner>&")
        plain, _ents = pe.to_entities(cap)
        assert "<" in plain and "&" in plain and "&lt;" not in plain

    def test_parse_error_is_treated_as_emoji_error(self):
        """Telegram's HTML parse failure text contains 'entities', which the
        notifier's _is_emoji_error heuristic classifies as a custom-emoji
        problem -> custom_emoji_ok is disabled for the whole process."""
        assert nf._is_emoji_error(Exception("Bad Request: can't parse entities")) is True


class TestNotifierVerify:
    def test_kept_custom_emoji_detection(self):
        assert nf._kept_custom_emoji(FakeBotMsg([Ent("custom_emoji")])) is True
        assert nf._kept_custom_emoji(FakeBotMsg(None, [Ent("custom_emoji")])) is True
        assert nf._kept_custom_emoji(FakeBotMsg([Ent("bold")])) is False
        assert nf._kept_custom_emoji(FakeBotMsg(None, None)) is False

    @pytest.mark.asyncio
    async def test_warns_once_when_emoji_dropped(self, notifier_with, caplog):
        n, bot, user = notifier_with(False, bot_msg=FakeBotMsg([Ent("bold")]))
        with caplog.at_level("WARNING", logger="notifier"):
            await n.send_message(-100, f"{TICK} a")
            await n.send_message(-100, f"{TICK} b")
        dropped = [r for r in caplog.records if "PREMIUM EMOJI DROPPED" in r.message]
        assert len(dropped) == 1
        assert n._warned is True

    @pytest.mark.asyncio
    async def test_no_warning_when_kept(self, notifier_with, caplog):
        n, bot, user = notifier_with(False)
        with caplog.at_level("WARNING", logger="notifier"):
            await n.send_message(-100, f"{TICK} a")
        assert not [r for r in caplog.records if "PREMIUM EMOJI DROPPED" in r.message]

    @pytest.mark.asyncio
    async def test_no_verify_when_no_tags_in_text(self, notifier_with, caplog):
        n, bot, user = notifier_with(False, bot_msg=FakeBotMsg([Ent("bold")]))
        with caplog.at_level("WARNING", logger="notifier"):
            await n.send_message(-100, f"{ROCKET} nothing premium")
        assert n._warned is False
