#!/usr/bin/env python3
"""One-time login for the Telegram Premium account used to post channel signals.

Run it once on a machine where you can receive the Telegram login code:

    python userbot_login.py

It asks for the phone number, the login code and (if enabled) the 2FA password,
then prints a TG_SESSION string. Copy that string into backend/.env:

    TG_API_ID=1234567
    TG_API_HASH=your_api_hash
    TG_SESSION=the_printed_string

Keep the session string secret — it grants full access to the account.
"""
import asyncio
import os

from dotenv import load_dotenv
from telethon import TelegramClient
from telethon.sessions import StringSession

load_dotenv(os.path.join(os.path.dirname(__file__), ".env"))


async def main():
    api_id = os.environ.get("TG_API_ID") or input("api_id: ").strip()
    api_hash = os.environ.get("TG_API_HASH") or input("api_hash: ").strip()

    client = TelegramClient(StringSession(), int(api_id), api_hash)
    await client.start()
    me = await client.get_me()
    print("\nLogged in as:", me.first_name, f"(@{me.username})" if me.username else "")
    print("Telegram Premium:", bool(getattr(me, "premium", False)))
    if not getattr(me, "premium", False):
        print("WARNING: this account has no Telegram Premium — "
              "custom (premium) emoji will not render.")
    print("\nTG_SESSION=" + client.session.save() + "\n")
    print("Put the line above in backend/.env, then restart the bot.")
    await client.disconnect()


if __name__ == "__main__":
    asyncio.run(main())
