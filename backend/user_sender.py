"""Channel posts sent as a Telegram Premium USER account (MTProto / Telethon).

Bots are not allowed to render custom (premium) emoji in channel posts, so when
TG_API_ID / TG_API_HASH / TG_SESSION are present in .env the signal and result
posts are sent by the premium user account instead of the bot. The account must
be an admin of the channel with "post messages" permission.

Custom emoji are attached as real MessageEntityCustomEmoji entities (UTF-16
offsets), which is the only reliable way to make them render.

Generate TG_SESSION once with:  python userbot_login.py
"""
import io
import logging

from telethon import TelegramClient, utils
from telethon.sessions import StringSession
from telethon.tl.types import MessageEntityBold, MessageEntityCustomEmoji

from config import TG_API_ID, TG_API_HASH, TG_SESSION
from premium_emojis import bold_entities, to_entities

log = logging.getLogger("user_sender")


def configured():
    return bool(TG_API_ID and TG_API_HASH and TG_SESSION)


def _entities(text):
    plain, raw = to_entities(text)
    ents = [MessageEntityCustomEmoji(offset=o, length=n, document_id=d)
            for o, n, d in raw]
    ents += [MessageEntityBold(offset=o, length=n) for o, n in bold_entities(text)]
    return plain, (ents or None)


class UserSender:
    def __init__(self):
        self._client = None

    async def client(self):
        if self._client is None:
            client = TelegramClient(
                StringSession(TG_SESSION), int(TG_API_ID), TG_API_HASH)
            await client.connect()
            if not await client.is_user_authorized():
                await client.disconnect()
                raise RuntimeError(
                    "TG_SESSION is not authorized — regenerate it with userbot_login.py")
            me = await client.get_me()
            log.info(f"user sender ready as {me.first_name} "
                     f"(premium={getattr(me, 'premium', None)})")
            # populates the entity cache so channel ids resolve without errors
            await client.get_dialogs(limit=100)
            self._client = client
        return self._client

    async def _peer(self, chat_id):
        client = await self.client()
        try:
            return await client.get_input_entity(chat_id)
        except (ValueError, TypeError):
            real_id, peer_cls = utils.resolve_id(int(chat_id))
            return peer_cls(real_id)

    async def send_message(self, chat_id, text):
        client = await self.client()
        plain, ents = _entities(text)
        return await client.send_message(
            await self._peer(chat_id), plain, formatting_entities=ents)

    async def send_photo(self, chat_id, png, caption):
        client = await self.client()
        plain, ents = _entities(caption)
        buf = io.BytesIO(png)
        buf.name = "chart.png"
        return await client.send_file(
            await self._peer(chat_id), buf, caption=plain,
            formatting_entities=ents)

    async def close(self):
        if self._client is not None:
            await self._client.disconnect()
            self._client = None


USER = UserSender()
