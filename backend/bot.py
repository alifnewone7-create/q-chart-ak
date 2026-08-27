#!/usr/bin/env python3
"""TaNix Alpha 2.0 — admin-only Telegram signal bot (entry point)."""
import asyncio
import contextlib
import logging
import os
import sys
import threading
import time

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    KeyboardButtonRequestChat,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
)
from telegram.constants import ChatMemberStatus
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    ChatMemberHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

import premium_emojis
import storage
import strategies
import user_sender
from config import BOT_TOKEN, ADMIN_ID, ADMIN_IDS
from notifier import NOTIFY
from qx import QuotexManager
from sessions import SM
from ticks import TickCollector

logging.basicConfig(format="%(asctime)s %(levelname)s %(name)s: %(message)s", level=logging.INFO)
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("bot")

QX = QuotexManager()
TICKS = TickCollector(QX)

PAGE_SIZE = 16
CATEGORY_LABELS = {
    "otc": "\U0001f552 OTC Market",
    "real": "\U0001f3e6 Real Market",
    "crypto": "\u20bf Crypto Market",
    "all": "\U0001f310 All Markets",
}

# in-memory UI state (single admin bot)
UI = {"category": None, "markets": [], "page": 0, "selected": {},
      "auto": None, "auto_cat": None, "await_pct": False,
      "channels": {}}

MAX_SESSION_CHANNELS = 2


def is_admin(update: Update) -> bool:
    return bool(update.effective_user) and update.effective_user.id in ADMIN_IDS


# ---------- Main menu ----------

def main_menu_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\u2795 Add Channel", callback_data="m|add"),
         InlineKeyboardButton("\U0001f4e2 My Channels", callback_data="m|my")],
        [InlineKeyboardButton("\U0001f680 Start Session", callback_data="m|sess")],
        [InlineKeyboardButton("\u2699\ufe0f Settings", callback_data="m|set")],
    ])


MAIN_TEXT = "\u2728 TaNix Alpha 2.0 \u2728\n\nSelect an option below:"


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Capture custom % value when Auto Select is waiting for it."""
    if not is_admin(update):
        return

    if not UI.get("await_pct"):
        return
    txt = (update.message.text or "").strip().replace("%", "").strip()
    try:
        pct = int(float(txt))
    except ValueError:
        await update.message.reply_text(
            "\u26a0\ufe0f Please send a whole number like 78 or 85.")
        return
    if pct < 1 or pct > 100:
        await update.message.reply_text("\u26a0\ufe0f % must be between 1 and 100.")
        return
    UI["auto"] = pct
    UI["await_pct"] = False
    cat = UI.get("auto_cat") or "otcreal"
    await update.message.reply_text(
        f"\U0001f3af Auto Select \u2265 {pct}%\n"
        f"Category: {_auto_cat_label(cat)}\n\n"
        f"Every minute the bot will scan open markets in this category\n"
        f"and analyze only those with payout \u2265 {pct}%.\n\n"
        f"Tap Confirm to pick the channel and start.",
        reply_markup=auto_confirm_kb(pct))


async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        await update.message.reply_text("\u26d4 You are not authorized to use this bot.")
        return
    await update.message.reply_text(MAIN_TEXT, reply_markup=main_menu_kb())


# ---------- Add Channel ----------

async def show_add_channel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    kb = ReplyKeyboardMarkup(
        [[KeyboardButton(
            "\U0001f4e2 Select Channel",
            request_chat=KeyboardButtonRequestChat(request_id=100, chat_is_channel=True),
        )]],
        resize_keyboard=True, one_time_keyboard=True,
    )
    await context.bot.send_message(
        chat_id=update.effective_chat.id,
        text=("\u2795 Add Channel\n\n"
              "1\ufe0f\u20e3 First make this bot an ADMIN in your channel\n"
              "2\ufe0f\u20e3 Then tap the button below and select the channel"),
        reply_markup=kb,
    )


async def on_chat_shared(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_admin(update):
        return
    shared = update.message.chat_shared
    if not shared or shared.request_id != 100:
        return
    chat_id = shared.chat_id
    try:
        member = await context.bot.get_chat_member(chat_id, context.bot.id)
        if member.status not in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER):
            raise PermissionError("not admin")
        chat = await context.bot.get_chat(chat_id)
    except Exception:
        await update.message.reply_text(
            "\u274c Bot is NOT admin in that channel.\n\n"
            "\U0001f449 Please make the bot an admin in the channel first, then try Add Channel again.",
            reply_markup=ReplyKeyboardRemove(),
        )
        return
    added = storage.add_channel(chat_id, chat.title or str(chat_id))
    msg = "\u2705 Channel successfully added!" if added else "\u2139\ufe0f Channel already connected (updated)."
    await update.message.reply_text(
        f"{msg}\n\n\U0001f4e2 {chat.title}\n\U0001f194 {chat_id}",
        reply_markup=ReplyKeyboardRemove(),
    )
    await context.bot.send_message(chat_id=update.effective_chat.id, text=MAIN_TEXT, reply_markup=main_menu_kb())


async def on_my_chat_member(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Being promoted does NOT connect a channel — the admin must add it from
    the Add Channel menu. Losing admin rights disconnects it."""
    cm = update.my_chat_member
    if not cm or cm.chat.type != "channel":
        return
    if cm.new_chat_member.status == ChatMemberStatus.ADMINISTRATOR:
        known = any(c["id"] == cm.chat.id for c in storage.get_channels())
        note = ("It is already connected \u2705"
                if known else
                "It is NOT connected yet \u2014 use \u2795 Add Channel and select it.")
        for aid in ADMIN_IDS:
            try:
                await context.bot.send_message(
                    chat_id=aid,
                    text=f"\u2139\ufe0f Bot is now admin in \U0001f4e2 {cm.chat.title}.\n{note}",
                )
            except Exception:
                pass
    elif cm.new_chat_member.status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED, ChatMemberStatus.MEMBER):
        storage.remove_channel(cm.chat.id)


# ---------- My Channels ----------

def my_channels_view():
    channels = storage.get_channels()
    if not channels:
        text = "\U0001f4e2 My Channels\n\nNo channels connected yet.\nUse \u2795 Add Channel first."
        rows = []
    else:
        text = f"\U0001f4e2 My Channels ({len(channels)})\n\nTap \u274c to disconnect a channel."
        rows = [[InlineKeyboardButton(f"\U0001f4e2 {c['title']}", callback_data="noop"),
                 InlineKeyboardButton("\u274c", callback_data=f"rm|{c['id']}")] for c in channels]
    rows.append([InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="m|main")])
    return text, InlineKeyboardMarkup(rows)


# ---------- Start Session: categories & market selection ----------

def category_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(CATEGORY_LABELS["otc"], callback_data="c|otc")],
        [InlineKeyboardButton(CATEGORY_LABELS["real"], callback_data="c|real")],
        [InlineKeyboardButton(CATEGORY_LABELS["crypto"], callback_data="c|crypto")],
        [InlineKeyboardButton(CATEGORY_LABELS["all"], callback_data="c|all")],
        [InlineKeyboardButton("\U0001f3af Auto Select %", callback_data="c|auto")],
        [InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="m|main")],
    ])


AUTO_CATEGORIES = [
    ("otc",     "\U0001f552 Only OTC"),
    ("real",    "\U0001f3e6 Only Real"),
    ("otcreal", "\U0001f501 OTC + Real"),
]


def auto_cat_kb():
    rows = [[InlineKeyboardButton(lbl, callback_data=f"ac|{code}")]
            for code, lbl in AUTO_CATEGORIES]
    rows.append([InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="m|sess")])
    return InlineKeyboardMarkup(rows)


AUTO_PRESETS = [70, 75, 80, 85, 90, 92, 95]


def _auto_cat_label(code):
    return next((lbl for c, lbl in AUTO_CATEGORIES if c == code), code)


def auto_pct_kb(cat):
    rows = []
    row = []
    for p in AUTO_PRESETS:
        row.append(InlineKeyboardButton(f"\u2265 {p}%", callback_data=f"at|{p}"))
        if len(row) == 3:
            rows.append(row); row = []
    if row:
        rows.append(row)
    rows.append([InlineKeyboardButton("\u2328\ufe0f Custom %", callback_data="at|custom")])
    rows.append([InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="c|auto")])
    return InlineKeyboardMarkup(rows)


def auto_confirm_kb(pct):
    return InlineKeyboardMarkup([
        [InlineKeyboardButton(f"\u2705 Confirm \u2265 {pct}%", callback_data=f"aok|{pct}")],
        [InlineKeyboardButton("\u2b05\ufe0f Change", callback_data="c|auto")],
    ])


def market_page_view():
    markets = UI["markets"]
    page = UI["page"]
    selected = UI["selected"]
    total_pages = max(1, (len(markets) + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    UI["page"] = page
    chunk = markets[page * PAGE_SIZE:(page + 1) * PAGE_SIZE]

    rows = []
    for i in range(0, len(chunk), 2):
        row = []
        for m in chunk[i:i + 2]:
            tick = "\u2705 " if m["code"] in selected else ""
            row.append(InlineKeyboardButton(
                f"{tick}{m['code']} ({m['payout']}%)", callback_data=f"t|{m['code']}"))
        rows.append(row)

    nav = []
    if page > 0:
        nav.append(InlineKeyboardButton("\u2b05\ufe0f Prev", callback_data=f"p|{page - 1}"))
    nav.append(InlineKeyboardButton(f"\U0001f4c4 {page + 1}/{total_pages}", callback_data="noop"))
    if page < total_pages - 1:
        nav.append(InlineKeyboardButton("Next \u27a1\ufe0f", callback_data=f"p|{page + 1}"))
    rows.append(nav)
    rows.append([InlineKeyboardButton(
        f"\U0001f680 Start Session ({len(selected)})", callback_data="go")])
    rows.append([InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="m|sess")])

    text = f"{CATEGORY_LABELS[UI['category']]}\n\U0001f4ca Open markets: {len(markets)}\n"
    if selected:
        text += f"\n\u2705 Selected markets ({len(selected)}):\n"
        text += "\n".join(f"  \u2022 {d}" for d in selected.values())
    else:
        text += "\nSelect the markets you want signals for \U0001f447"
    return text, InlineKeyboardMarkup(rows)


def session_channel_kb():
    picked = UI["channels"]
    tick, mark = "\u2705", "\U0001f4e2"
    rows = [[InlineKeyboardButton(
        f"{tick if c['id'] in picked else mark} {c['title']}",
        callback_data=f"sc|{c['id']}")] for c in storage.get_channels()]
    rows.append([InlineKeyboardButton(
        f"\U0001f680 Start Session ({len(picked)}/{MAX_SESSION_CHANNELS})",
        callback_data="scgo")])
    rows.append([InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="c|back")])
    return InlineKeyboardMarkup(rows)


def channel_select_view(header=None):
    if header is not None:
        UI["ch_header"] = header
    picked = UI["channels"]
    text = (f"{UI.get('ch_header', '')}\n\n\U0001f4e2 Choose the channel(s) where signals "
            f"will be sent (min 1, max {MAX_SESSION_CHANNELS}):")
    if picked:
        text += "\n\n\u2705 Selected:\n" + "\n".join(f"  \u2022 {t}" for t in picked.values())
    return text, session_channel_kb()


def running_kb():
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("\u23f9 Close Session", callback_data="cx")],
        [InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="m|main")],
    ])


def running_view():
    return SM.running_status_text(), running_kb()


# ---------- Settings ----------

def settings_view():
    s = storage.get_settings()
    mtg = s.get("mtg", "MTG-1")
    st = strategies.get(s.get("strategy", strategies.DEFAULT_KEY))
    text = (f"\u2699\ufe0f Settings\n\n"
            f"\U0001f9e0 Strategy: {st['name']}\n"
            f"\U0001f6e1 MTG mode: {mtg}")
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton("\U0001f9e0 Strategy", callback_data="set|strat")],
        [InlineKeyboardButton("\U0001f6e1 MTG Settings", callback_data="set|mtg")],
        [InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="m|main")],
    ])
    return text, kb


def strategy_view():
    cur = storage.get_settings().get("strategy", strategies.DEFAULT_KEY)
    lines = ["\U0001f9e0 Strategy", "",
             "Choose the engine that generates signals.", ""]
    rows = []
    for key in strategies.ORDER:
        st = strategies.STRATEGIES[key]
        mark = "\u2705 " if key == cur else "\u25ab\ufe0f "
        lines.append(f"{mark}{st['name']}")
        lines.append(f"    {st['tagline']}")
        lines.append(f"    Min confidence: {st['min_confidence']:.0f}%")
        lines.append("")
        rows.append([
            InlineKeyboardButton(("\u2705 " if key == cur else "") + st["name"],
                                 callback_data=f"st|{key}"),
            InlineKeyboardButton("\u2139\ufe0f", callback_data=f"sti|{key}"),
        ])
    rows.append([InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="m|set")])
    return "\n".join(lines).rstrip(), InlineKeyboardMarkup(rows)


def strategy_about_view(key):
    st = strategies.get(key)
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(f"\u2705 Use {st['name']}", callback_data=f"st|{st['key']}")],
        [InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="set|strat")],
    ])
    return st["about"], kb


def mtg_view():
    mtg = storage.get_settings().get("mtg", "MTG-1")
    text = f"\U0001f6e1 MTG Settings\n\nCurrently set: {mtg}"
    kb = InlineKeyboardMarkup([
        [InlineKeyboardButton(("\u2705 " if mtg == "MTG-1" else "") + "MTG-1", callback_data="mtg|1")],
        [InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="m|set")],
    ])
    return text, kb


# ---------- Callback router ----------

async def on_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    q = update.callback_query
    if not is_admin(update):
        await q.answer("Not authorized", show_alert=True)
        return
    data = q.data

    if data == "noop":
        await q.answer()
        return

    if data == "m|main":
        await q.answer()
        await q.edit_message_text(MAIN_TEXT, reply_markup=main_menu_kb())
        return

    if data == "m|add":
        await q.answer()
        await show_add_channel(update, context)
        return

    if data == "m|my":
        await q.answer()
        text, kb = my_channels_view()
        await q.edit_message_text(text, reply_markup=kb)
        return

    if data.startswith("rm|"):
        storage.remove_channel(int(data[3:]))
        await q.answer("Channel removed")
        text, kb = my_channels_view()
        await q.edit_message_text(text, reply_markup=kb)
        return

    if data == "m|sess":
        await q.answer()
        if SM.is_running():
            text, kb = running_view()
            await q.edit_message_text(text, reply_markup=kb)
        else:
            await q.edit_message_text(
                "\U0001f680 Start Session\n\nChoose a market category:",
                reply_markup=category_kb())
        return

    if data.startswith("c|"):
        cat = data[2:]
        if cat == "back":
            await q.answer()
            text, kb = market_page_view()
            await q.edit_message_text(text, reply_markup=kb)
            return
        if cat == "auto":
            await q.answer()
            UI["auto"] = None
            UI["auto_cat"] = None
            UI["await_pct"] = False
            await q.edit_message_text(
                "\U0001f3af Auto Select %\n\n"
                "First choose which market category to scan:",
                reply_markup=auto_cat_kb())
            return
        await q.answer()
        await q.edit_message_text("\u23f3 Connecting to Quotex & loading market payouts...")
        try:
            markets = await QX.get_markets(cat)
        except Exception as e:
            log.error(f"quotex markets failed: {e}")
            await q.edit_message_text(
                f"\u274c Could not load markets from Quotex.\n\nError: {e}",
                reply_markup=InlineKeyboardMarkup([
                    [InlineKeyboardButton("\U0001f501 Retry", callback_data=f"c|{cat}")],
                    [InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="m|sess")],
                ]))
            return
        if not markets:
            await q.edit_message_text(
                "\u26a0\ufe0f No open markets found in this category right now.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="m|sess")]]))
            return
        UI.update({"category": cat, "markets": markets, "page": 0, "selected": {},
                   "auto": None, "auto_cat": None})
        text, kb = market_page_view()
        await q.edit_message_text(text, reply_markup=kb)
        return

    if data.startswith("ac|"):
        cat = data[3:]
        if cat not in {c for c, _ in AUTO_CATEGORIES}:
            await q.answer("Bad category", show_alert=True)
            return
        UI["auto_cat"] = cat
        UI["await_pct"] = False
        await q.answer()
        await q.edit_message_text(
            f"\U0001f3af Auto Select %\n\n"
            f"Category: {_auto_cat_label(cat)}\n\n"
            f"Only markets with payout \u2265 chosen % will be scanned.\n"
            f"List refreshes every minute \u2014 no market is locked.\n\n"
            f"Pick a threshold below:",
            reply_markup=auto_pct_kb(cat))
        return

    if data.startswith("at|"):
        val = data[3:]
        if val == "custom":
            UI["await_pct"] = True
            await q.answer()
            await q.edit_message_text(
                "\u2328\ufe0f Send a % value (e.g. `78`) as a reply.\n"
                "Only markets with payout \u2265 that value will be scanned.",
                reply_markup=InlineKeyboardMarkup(
                    [[InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="c|auto")]]))
            return
        try:
            pct = int(val)
        except ValueError:
            await q.answer("Bad value", show_alert=True)
            return
        UI["auto"] = pct
        UI["await_pct"] = False
        await q.answer()
        cat = UI.get("auto_cat") or "otcreal"
        await q.edit_message_text(
            f"\U0001f3af Auto Select \u2265 {pct}%\n"
            f"Category: {_auto_cat_label(cat)}\n\n"
            f"Every minute the bot will scan open markets in this category\n"
            f"and analyze only those with payout \u2265 {pct}%.\n\n"
            f"Tap Confirm to pick the channel and start.",
            reply_markup=auto_confirm_kb(pct))
        return

    if data.startswith("aok|"):
        try:
            pct = int(data[4:])
        except ValueError:
            await q.answer("Bad value", show_alert=True)
            return
        UI["auto"] = pct
        channels = storage.get_channels()
        if not channels:
            await q.answer("\u26a0\ufe0f No channel connected! Use Add Channel first.", show_alert=True)
            return
        UI["channels"] = {}
        await q.answer()
        text, kb = channel_select_view(f"\U0001f3af Auto Select \u2265 {pct}%")
        await q.edit_message_text(text, reply_markup=kb)
        return

    if data.startswith("t|"):
        code = data[2:]
        if code in UI["selected"]:
            UI["selected"].pop(code)
        else:
            m = next((x for x in UI["markets"] if x["code"] == code), None)
            if m:
                UI["selected"][code] = m["display"]
        await q.answer()
        text, kb = market_page_view()
        await q.edit_message_text(text, reply_markup=kb)
        return

    if data.startswith("p|"):
        UI["page"] = int(data[2:])
        await q.answer()
        text, kb = market_page_view()
        await q.edit_message_text(text, reply_markup=kb)
        return

    if data == "go":
        if not UI["selected"]:
            await q.answer("\u26a0\ufe0f Select at least 1 market first!", show_alert=True)
            return
        channels = storage.get_channels()
        if not channels:
            await q.answer("\u26a0\ufe0f No channel connected! Use Add Channel first.", show_alert=True)
            return
        await q.answer()
        sel = "\n".join(f"  \u2022 {d}" for d in UI["selected"].values())
        UI["channels"] = {}
        # manual flow: make sure an abandoned Auto Select threshold cannot hijack it
        UI["auto"] = None
        UI["auto_cat"] = None
        text, kb = channel_select_view(
            f"\u2705 Selected markets ({len(UI['selected'])}):\n{sel}")
        await q.edit_message_text(text, reply_markup=kb)
        return

    if data.startswith("sc|"):
        if SM.is_running():
            await q.answer("A session is already running!", show_alert=True)
            return
        cid = int(data[3:])
        if cid in UI["channels"]:
            UI["channels"].pop(cid)
            await q.answer()
        else:
            if len(UI["channels"]) >= MAX_SESSION_CHANNELS:
                await q.answer(
                    f"\u26a0\ufe0f Maximum {MAX_SESSION_CHANNELS} channels per session. "
                    f"Deselect one first.", show_alert=True)
                return
            ch = next((c for c in storage.get_channels() if c["id"] == cid), None)
            if not ch:
                await q.answer("Channel not found", show_alert=True)
                return
            UI["channels"][cid] = ch["title"]
            await q.answer()
        text, kb = channel_select_view()
        await q.edit_message_text(text, reply_markup=kb)
        return

    if data == "scgo":
        if SM.is_running():
            await q.answer("A session is already running!", show_alert=True)
            return
        if not UI["channels"]:
            await q.answer("\u26a0\ufe0f Select at least 1 channel!", show_alert=True)
            return
        channels = [{"id": cid, "title": title} for cid, title in UI["channels"].items()]
        titles = ", ".join(c["title"] for c in channels)
        await q.answer()

        auto_pct = UI.get("auto")
        if auto_pct:
            auto_cat = UI.get("auto_cat") or "otcreal"
            # start with empty list; refresher fills it right away
            await q.edit_message_text(
                f"\U0001f7e2 Starting Auto session \u2265 {auto_pct}% \u2026\n"
                f"Category: {_auto_cat_label(auto_cat)}\n"
                f"Scanning markets, please wait\u2026",
                reply_markup=running_kb())
            msg = q.message
            await SM.start(
                context.bot, QX, [], channels, ticks=TICKS,
                auto_mode=True, auto_threshold=auto_pct, auto_category=auto_cat,
                admin_chat_id=msg.chat_id, admin_msg_id=msg.message_id,
                admin_kb=running_kb(),
            )
            UI["auto"] = None
            UI["auto_cat"] = None
        else:
            markets = [{"code": c, "display": d,
                        "payout": next((m["payout"] for m in UI["markets"]
                                        if m["code"] == c), 0)}
                       for c, d in UI["selected"].items()]
            await q.edit_message_text(
                f"\u2705 Session STARTED!\n\nSignals will be sent to \U0001f4e2 {titles}",
                reply_markup=running_kb())
            msg = q.message
            await SM.start(
                context.bot, QX, markets, channels, ticks=TICKS,
                admin_chat_id=msg.chat_id, admin_msg_id=msg.message_id,
                admin_kb=running_kb(),
            )
        UI["channels"] = {}
        # trigger initial view refresh
        await SM._refresh_admin_view()
        return

    if data == "cx":
        SM.close()
        await q.answer("Session closed")
        await q.edit_message_text(
            f"\U0001f534 Session CLOSED\n\n"
            f"\U0001f4e8 Total signals: {len(SM.signals)}\n"
            f"No more signals will be sent to the channel.",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("\U0001f4e4 Send Partial", callback_data="pp")],
                [InlineKeyboardButton("\u2b05\ufe0f Back", callback_data="m|main")],
            ]))
        return

    if data == "pp":
        sent = False
        try:
            sent = await SM.send_partial()
        except Exception as e:
            log.error(f"partial failed: {e}")
        if sent:
            await q.answer("Partial sent to channel \u2705", show_alert=True)
        else:
            await q.answer("No signals in this session to report.", show_alert=True)
        return

    if data == "m|set":
        await q.answer()
        text, kb = settings_view()
        await q.edit_message_text(text, reply_markup=kb)
        return

    if data == "set|mtg":
        await q.answer()
        text, kb = mtg_view()
        await q.edit_message_text(text, reply_markup=kb)
        return

    if data == "set|strat":
        await q.answer()
        text, kb = strategy_view()
        await q.edit_message_text(text, reply_markup=kb)
        return

    if data.startswith("sti|"):
        await q.answer()
        text, kb = strategy_about_view(data[4:])
        await q.edit_message_text(text, reply_markup=kb)
        return

    if data.startswith("st|"):
        key = data[3:]
        if key not in strategies.STRATEGIES:
            await q.answer("Unknown strategy", show_alert=True)
            return
        s = storage.get_settings()
        s["strategy"] = key
        storage.save_settings(s)
        name = strategies.STRATEGIES[key]["name"]
        await q.answer(f"Saved: {name} \u2705")
        text, kb = strategy_view()
        with contextlib.suppress(Exception):
            await q.edit_message_text(text, reply_markup=kb)
        return

    if data == "mtg|1":
        s = storage.get_settings()
        s["mtg"] = "MTG-1"
        storage.save_settings(s)
        await q.answer("Saved: MTG-1 \u2705")
        text, kb = mtg_view()
        try:
            await q.edit_message_text(text, reply_markup=kb)
        except Exception:
            pass
        return

    await q.answer()


async def on_error(update, context):
    log.error(f"error: {context.error}")


# ---------- Premium emoji diagnostics ----------

async def cmd_emoji_test(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """/emojitest — reload data/premium_emojis.json, validate every emoji-id and
    send a test message here + to every connected channel, reporting whether
    Telegram actually rendered the premium (custom) emoji."""
    if not is_admin(update):
        return

    emojis = premium_emojis.reload_premium_emojis()
    if not emojis:
        await update.message.reply_text(
            "\u26a0\ufe0f data/premium_emojis.json is empty.")
        return

    lines = [f"\U0001f48e Premium emoji check ({len(emojis)} entries)", ""]
    tick, cross = "\u2705", "\u274c"
    if user_sender.configured():
        try:
            client = await user_sender.USER.client()
            me = await client.get_me()
            prem = tick if getattr(me, "premium", False) else cross
            name = me.username or me.first_name
            lines.append(f"Sender: user account {name} (Premium: {prem})")
        except Exception as e:
            lines.append(f"Sender: user account \u26a0\ufe0f not usable: {e}")
    else:
        lines.append("Sender: BOT (TG_API_ID / TG_API_HASH / TG_SESSION not set in .env "
                     "\u2014 Telegram blocks premium emoji in channel posts from bots)")
    lines.append("")
    ids = list(emojis.values())
    try:
        stickers = await NOTIFY.bot.get_custom_emoji_stickers(custom_emoji_ids=ids)
        by_id = {s.custom_emoji_id: s.emoji for s in stickers}
    except Exception as e:
        by_id = {}
        lines.append(f"\u26a0\ufe0f getCustomEmojiStickers failed: {e}")

    for char, eid in emojis.items():
        real = by_id.get(eid)
        if real is None:
            lines.append(f"{char} \u2192 {eid} : \u2753 id not found")
        elif real == char:
            lines.append(f"{char} \u2192 {eid} : \u2705 id matches emoji")
        else:
            lines.append(
                f"{char} \u2192 {eid} : \u274c id belongs to {real} "
                f"(Telegram will ignore it \u2014 use {real} as the JSON key)")

    sample = " ".join(emojis.keys()) + "  PREMIUM EMOJI TEST"
    ok_txt = "\u2705 premium emoji rendered"
    bad_txt = "\u274c sent as plain emoji"
    lines.append("")
    channels = storage.get_channels()
    if not channels:
        lines.append("No connected channel to test \u2014 add one first.")
    for ch in channels:
        try:
            msg = await NOTIFY.send_message(ch["id"], sample)
            ents = list(getattr(msg, "entities", None) or [])
            ok = any(getattr(e, "type", "") == "custom_emoji"
                     or type(e).__name__ == "MessageEntityCustomEmoji" for e in ents)
            lines.append(f"{ch['title']}: {ok_txt if ok else bad_txt}")
        except Exception as e:
            lines.append(f"{ch['title']}: \u26a0\ufe0f send failed: {e}")

    lines += [
        "",
        ("Channel post-এ premium emoji দেখাতে হলে: TG_API_ID / TG_API_HASH / TG_SESSION "
         "সেট থাকতে হবে, ওই account-এ active Telegram Premium থাকতে হবে এবং সে ওই "
         "channel-এ admin (post messages) হতে হবে।"),
    ]
    await update.message.reply_text("\n".join(lines))


async def post_init(app):
    # start collecting tick data for ALL markets as soon as the backend starts
    await TICKS.start()
    app.bot_data["heartbeat"] = asyncio.create_task(_heartbeat())
    app.bot_data["watchdog"] = asyncio.create_task(_watchdog())
    threading.Thread(target=_hard_watchdog, daemon=True).start()


WATCHDOG_INTERVAL = 60      # how often the async health check runs
TICK_STALL_LIMIT = 900      # 15 min with zero ticks => the process is wedged
HEARTBEAT_INTERVAL = 1      # how often the event loop proves it is alive
LOOP_STALL_LIMIT = 300      # 5 min without a heartbeat => the loop is blocked
HARD_WATCHDOG_INTERVAL = 30

# updated by the asyncio heartbeat, read by the plain-thread watchdog
_HEARTBEAT = {"loop": time.time()}


async def _heartbeat():
    """Prove the event loop is still turning."""
    while True:
        _HEARTBEAT["loop"] = time.time()
        await asyncio.sleep(HEARTBEAT_INTERVAL)


def _hard_watchdog():
    """Runs in a PLAIN THREAD on purpose.

    Parts of the vendored Quotex client are synchronous (`requests` during
    login). A synchronous call cannot be cancelled by asyncio.wait_for and
    blocks the whole event loop, which means an asyncio watchdog is frozen
    right along with everything else. A thread still runs, so this is the only
    supervisor that can rescue a loop-level freeze.
    """
    while True:
        time.sleep(HARD_WATCHDOG_INTERVAL)
        stalled = time.time() - _HEARTBEAT["loop"]
        if stalled > LOOP_STALL_LIMIT:
            log.critical(
                f"hard watchdog: event loop blocked for {int(stalled)}s "
                "(synchronous call hung) — exiting so systemd restarts the bot")
            logging.shutdown()
            os._exit(1)


async def _watchdog():
    """Guard against the silent freeze that used to need a manual
    `systemctl restart`.

    The tick collector, the signal loop and every Quotex call share one
    connection lock. If a socket stalls the process stays alive but stops doing
    anything, so systemd's Restart=always never fires. This watchdog notices the
    stall, revives a dead signal loop, and as a last resort exits hard so
    systemd brings the bot back.
    """
    while True:
        try:
            await asyncio.sleep(WATCHDOG_INTERVAL)

            # a signal session that lost its loop task: restart it in place
            if SM.active and SM.task is not None and SM.task.done():
                log.error("watchdog: session loop died — restarting it")
                SM.task = asyncio.create_task(SM._loop())

            # collection has begun but no tick arrived for a long time
            if TICKS.started_at is not None:
                stalled = time.time() - TICKS.last_tick
                if stalled > TICK_STALL_LIMIT:
                    log.critical(
                        f"watchdog: no Quotex ticks for {int(stalled)}s — "
                        "exiting so systemd restarts the bot")
                    sys.stdout.flush()
                    sys.stderr.flush()
                    os._exit(1)
        except asyncio.CancelledError:
            return
        except Exception as e:
            log.error(f"watchdog error: {e}")


async def post_shutdown(app):
    for key in ("watchdog", "heartbeat"):
        task = app.bot_data.pop(key, None)
        if task:
            task.cancel()
    TICKS.stop()
    await NOTIFY.close()
    if TICKS.task:
        with contextlib.suppress(asyncio.CancelledError):
            await TICKS.task


def main():
    app = (Application.builder().token(BOT_TOKEN)
           .post_init(post_init).post_shutdown(post_shutdown).build())
    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("emojitest", cmd_emoji_test))
    app.add_handler(CallbackQueryHandler(on_callback))
    app.add_handler(MessageHandler(filters.StatusUpdate.CHAT_SHARED, on_chat_shared))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))
    app.add_handler(ChatMemberHandler(on_my_chat_member, ChatMemberHandler.MY_CHAT_MEMBER))
    app.add_error_handler(on_error)
    print("\U0001f680 TaNix Alpha 2.0 bot is running (polling)...")
    app.run_polling(drop_pending_updates=True, allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
