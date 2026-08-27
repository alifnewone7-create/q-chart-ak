#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
get_chat_id.py — one-time helper to find your Telegram chat_id

A bot token alone is NOT enough to send you messages — Telegram also needs
to know WHERE to deliver them (your personal chat, a group, or a channel).
That destination is the chat_id, and this script finds it for you.

Steps:
  1. Open Telegram and search for your bot (BotFather gave you its @username
     when you created it).
  2. Send the bot any message, e.g. "hi" or "/start".
  3. Run this script:  python get_chat_id.py
  4. Copy the printed chat_id into TELEGRAM_CHAT_ID near the top of engine.py.
"""
import requests

# Same token you're using in engine.py
TELEGRAM_BOT_TOKEN = "8940978558:AAF3YQ7HRtCL4dqlOR-Ry1oPFf0ZQkhyG8o"


def main():
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/getUpdates"
    try:
        resp = requests.get(url, timeout=15)
        data = resp.json()
    except Exception as e:
        print(f"❌ Could not reach Telegram: {e}")
        return

    if not data.get("ok"):
        print("❌ Telegram API error:", data.get("description", data))
        print("   Double-check TELEGRAM_BOT_TOKEN is correct and not revoked.")
        return

    results = data.get("result", [])
    if not results:
        print("⚠️  No messages found yet.")
        print("   1. Open Telegram, find your bot, and send it any message.")
        print("   2. Then run this script again.")
        return

    seen = {}
    for item in results:
        msg = item.get("message") or item.get("channel_post") or item.get("my_chat_member")
        if not msg or "chat" not in msg:
            continue
        chat = msg["chat"]
        seen[chat["id"]] = chat

    if not seen:
        print("⚠️  Updates were found, but none contained a usable chat. Try messaging the bot again.")
        return

    print("✅ Found the following chat(s):\n")
    for chat_id, chat in seen.items():
        kind = chat.get("type")
        name = chat.get("title") or chat.get("username") or chat.get("first_name") or "unknown"
        print(f"   chat_id: {chat_id}    type: {kind}    name: {name}")

    print("\n👉 Copy the chat_id you want into TELEGRAM_CHAT_ID inside engine.py")


if __name__ == "__main__":
    main()
