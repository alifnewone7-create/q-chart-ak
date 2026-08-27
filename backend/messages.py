"""Channel message templates.

Messages are written with plain emoji only. Outgoing channel posts pass through
premium_emojis.premiumize() in notifier.py, which turns every emoji listed in
data/premium_emojis.json into a Telegram custom emoji entity
(<tg-emoji emoji-id="...">emoji</tg-emoji>). If Telegram refuses the entities,
notifier.py resends the same text with the plain emoji.
"""
from premium_emojis import premiumize, strip_custom_emoji  # noqa: F401

# plain emoji used in the templates
EMOJI = {
    "brand":       "\u2728",
    "asset":       "\U0001f48e",
    "signal":      "\U0001f525",
    "call":        "\U0001f7e2",
    "put":         "\U0001f7e5",
    "entry":       "\u23f3",
    "payout":      "\U0001f4b5",
    "mtg":         "\U0001f6e1",
    "owner":       "\U0001f451",
    "analysis":    "\U0001f4a1",
    "result_head": "\U0001f4ca",
    "res_signal":  "\U0001f525",
    "time":        "\u23f3",
    "result":      "\U0001f3af",
    "win":         "\u2705",
    "loss":        "\u274c",
}

LINE = "\u2501" * 20
RLINE = "\u2501" * 21
PLINE = "\u2501" * 9 + "\u30fb" + "\u2501" * 9


def em(key):
    return EMOJI[key]


# ---- Mathematical Monospace font converter -------------------------------
# maps ASCII A-Z / a-z / 0-9 to the Unicode "Mathematical Monospace" block so
# messages render in the stylised font requested (e.g. TaNix Alpha 2.0).

def mono(text):
    out = []
    for ch in str(text):
        o = ord(ch)
        if 65 <= o <= 90:        # A-Z
            out.append(chr(0x1D670 + (o - 65)))
        elif 97 <= o <= 122:     # a-z
            out.append(chr(0x1D68A + (o - 97)))
        elif 48 <= o <= 57:      # 0-9
            out.append(chr(0x1D7F6 + (o - 48)))
        else:
            out.append(ch)
    return "".join(out)


def _fmt_pct(value):
    """Signed percentage, whole number when possible."""
    v = round(value, 1)
    if abs(v - round(v)) < 0.05:
        return f"{int(round(v)):+d}"
    return f"{v:+.1f}"


def _dir_emoji(direction):
    return em("call") if direction == "CALL" else em("put")


def signal_caption(display, direction, entry_str, payout, reason, owner_tag):
    payout_str = f"{int(payout)}%" if payout else "\u2014"
    return (
        f"{em('brand')} {mono('TaNix Alpha 2.0')} {em('brand')}\n"
        f"{LINE}\n\n"
        f"{em('asset')} {mono('Asset')} : {mono(display)}\n\n"
        f"{em('signal')} {mono('Signal')} : {_dir_emoji(direction)} {mono(direction)}\n"
        f"{em('entry')} {mono('Entry Time')} : {mono(entry_str)}\n"
        f"{em('payout')} {mono('Payout')} : {mono(payout_str)}\n"
        f"{em('mtg')} {mono('MTG')} : {mono('1 - Step')}\n\n"
        f"{em('owner')} {mono('Owner')} : <b>{owner_tag}</b>\n"
        f"{LINE}"
    )


def result_caption(display, direction, time_str, result,
                   wins=0, losses=0, total_pct=0.0):
    if result == "WIN":
        res = f"{em('win')} {mono('WIN')}"
    elif result == "WIN_MTG":
        res = f"{em('win')} {mono('MTG WIN')}"
    else:
        res = f"{em('loss')} {mono('LOSS')}"
    total = wins + losses
    rate = round(wins / total * 100) if total else 0
    return (
        f"{em('result_head')} {mono('SIGNAL RESULT')}\n"
        f"{RLINE}\n\n"
        f"{em('asset')} {mono('Asset')} : {mono(display)}\n"
        f"{em('res_signal')} {mono('Signal')} : {_dir_emoji(direction)} {mono(direction)}\n"
        f"{em('time')} {mono('Time')} : {mono(time_str)}\n\n"
        f"{em('result')} {mono('Result')} : {res}\n"
        f"{RLINE}"
    )
