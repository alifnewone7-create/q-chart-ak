"""aiogram sender for channel posts — this is what makes premium (custom) emoji work.

The admin UI keeps running on python-telegram-bot; only the outgoing channel
messages go through aiogram with parse_mode=HTML so <tg-emoji> entities are
accepted. Nothing polls here, so the two libraries never fight over getUpdates.

Every outgoing message is passed through premium_emojis.premiumize(), which
upgrades any plain emoji listed in data/premium_emojis.json to a Telegram
premium (custom) emoji. If Telegram rejects the custom-emoji entities (the bot
is not allowed to use them in that chat), the same message is resent with the
plain emoji instead of failing.
"""
import logging

from aiogram import Bot
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode
from aiogram.exceptions import TelegramBadRequest
from aiogram.types import BufferedInputFile

from premium_emojis import plain_html, premiumize
from user_sender import USER, configured as user_sender_configured
from config import BOT_TOKEN

log = logging.getLogger("notifier")

_CUSTOM_EMOJI_ERRORS = ("custom emoji", "custom_emoji", "entity", "entities")


def _is_emoji_error(exc):
    msg = str(exc).lower()
    return any(k in msg for k in _CUSTOM_EMOJI_ERRORS)


def _kept_custom_emoji(msg):
    """True if Telegram kept at least one custom_emoji entity in the sent message."""
    ents = list(getattr(msg, "entities", None) or []) + \
        list(getattr(msg, "caption_entities", None) or [])
    return any(getattr(e, "type", "") == "custom_emoji" for e in ents)


class Notifier:
    def __init__(self):
        self._bot = None
        self.custom_emoji_ok = True
        self._warned = False

    def _verify(self, msg, sent_text):
        """Telegram silently drops custom-emoji entities when the bot is not
        allowed to use them (owner without Telegram Premium / no Fragment
        username) or when the emoji-id does not match the wrapping emoji.
        No error is raised in that case, so log it loudly once."""
        if self._warned or "<tg-emoji" not in (sent_text or ""):
            return
        if _kept_custom_emoji(msg):
            log.info("premium emoji OK — Telegram kept the custom_emoji entities")
        else:
            log.warning(
                "PREMIUM EMOJI DROPPED by Telegram (no error returned). Checklist: "
                "1) the account that owns the bot needs an active Telegram Premium "
                "subscription (or a Fragment username assigned to the bot); "
                "2) each emoji-id in data/premium_emojis.json must belong to a custom "
                "emoji whose own emoji is exactly the key emoji; "
                "3) channel posts are more restricted than private/group chats.")
        self._warned = True

    @property
    def bot(self):
        if self._bot is None:
            self._bot = Bot(
                token=BOT_TOKEN,
                default=DefaultBotProperties(parse_mode=ParseMode.HTML),
            )
        return self._bot

    async def send_photo(self, chat_id, png, caption):
        if user_sender_configured():
            try:
                return await USER.send_photo(chat_id, png, caption)
            except Exception as e:
                log.error(f"user-account send_photo failed, using bot: {e}")
        photo = BufferedInputFile(png, filename="chart.png")
        if self.custom_emoji_ok:
            cap = premiumize(caption)
            try:
                msg = await self.bot.send_photo(chat_id, photo, caption=cap)
                self._verify(msg, cap)
                return msg
            except TelegramBadRequest as e:
                if not _is_emoji_error(e):
                    raise
                self.custom_emoji_ok = False
                log.warning(f"custom emoji rejected, falling back to plain emoji: {e}")
        # aiogram consumes the upload buffer, so build a fresh one for the retry
        return await self.bot.send_photo(
            chat_id, BufferedInputFile(png, filename="chart.png"),
            caption=plain_html(caption))

    async def send_message(self, chat_id, text):
        if user_sender_configured():
            try:
                return await USER.send_message(chat_id, text)
            except Exception as e:
                log.error(f"user-account send_message failed, using bot: {e}")
        if self.custom_emoji_ok:
            body = premiumize(text)
            try:
                msg = await self.bot.send_message(chat_id, body)
                self._verify(msg, body)
                return msg
            except TelegramBadRequest as e:
                if not _is_emoji_error(e):
                    raise
                self.custom_emoji_ok = False
                log.warning(f"custom emoji rejected, falling back to plain emoji: {e}")
        return await self.bot.send_message(chat_id, plain_html(text))

    async def close(self):
        await USER.close()
        if self._bot is not None:
            await self._bot.session.close()
            self._bot = None


NOTIFY = Notifier()
