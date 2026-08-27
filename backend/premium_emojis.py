"""
=========================================================
💎 PREMIUM EMOJI SYSTEM 💎
=========================================================
Regular emoji in any outgoing channel message are converted to Telegram
Premium (custom) emoji when their emoji-id is present in data/premium_emojis.json.
"""
import html
import json
import logging
import re

from config import DATA_DIR

logger = logging.getLogger("premium_emojis")

PREMIUM_EMOJIS_FILE = DATA_DIR / "premium_emojis.json"

DEFAULT_PREMIUM_EMOJIS = {
    "✅": "6217660507575291616",
    "✨": "5325547803936572038",
}


def load_json_file(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def save_json_file(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def load_premium_emojis():
    """Load premium emojis from JSON file"""
    emojis = load_json_file(PREMIUM_EMOJIS_FILE, {})
    if not emojis:
        save_json_file(PREMIUM_EMOJIS_FILE, DEFAULT_PREMIUM_EMOJIS)
        logger.info(
            f"Created default {PREMIUM_EMOJIS_FILE} with {len(DEFAULT_PREMIUM_EMOJIS)} emojis")
        return DEFAULT_PREMIUM_EMOJIS
    return emojis


PREMIUM_EMOJIS = load_premium_emojis()


def reload_premium_emojis():
    """Re-read the JSON file at runtime (after editing data/premium_emojis.json)."""
    global PREMIUM_EMOJIS
    PREMIUM_EMOJIS = load_premium_emojis()
    return PREMIUM_EMOJIS


def p_emoji(char):
    """Convert regular emoji to premium Telegram emoji if available"""
    emoji_id = PREMIUM_EMOJIS.get(char)
    if emoji_id:
        return f'<tg-emoji emoji-id="{emoji_id}">{char}</tg-emoji>'
    return char


_TAG_RE = re.compile(r'<tg-emoji emoji-id="\d+">(.*?)</tg-emoji>', re.DOTALL)


def _strip_bold(text):
    """(text_without_b_tags, [(utf16_offset, utf16_length), ...]) for <b>..</b>."""
    out = []
    spans = []
    off = 0
    i = 0
    start = None
    while i < len(text):
        if text.startswith("<b>", i):
            start = off
            i += 3
            continue
        if text.startswith("</b>", i):
            if start is not None and off > start:
                spans.append((start, off - start))
            start = None
            i += 4
            continue
        ch = text[i]
        out.append(ch)
        off += len(ch.encode("utf-16-le")) // 2
        i += 1
    return "".join(out), spans


def bold_entities(text):
    """UTF-16 (offset, length) spans of every <b>..</b> run in the message."""
    return _strip_bold(strip_custom_emoji(text))[1]


def to_entities(text):
    """(plain_text, [(utf16_offset, utf16_length, custom_emoji_id), ...])

    Used by the MTProto user-account sender, which needs real
    MessageEntityCustomEmoji entities instead of HTML tags.
    """
    plain, _bold = _strip_bold(strip_custom_emoji(text))
    if not PREMIUM_EMOJIS:
        return plain, []
    keys = sorted(PREMIUM_EMOJIS, key=len, reverse=True)
    ents = []
    i = 0
    off = 0  # UTF-16 code units, as Telegram expects
    while i < len(plain):
        match = next((k for k in keys if k and plain.startswith(k, i)), None)
        if match:
            length = len(match.encode("utf-16-le")) // 2
            ents.append((off, length, int(PREMIUM_EMOJIS[match])))
            off += length
            i += len(match)
        else:
            off += len(plain[i].encode("utf-16-le")) // 2
            i += 1
    return plain, ents


def premiumize(text):
    """Replace every known plain emoji in the text with its premium version."""
    if not text or not PREMIUM_EMOJIS:
        return text
    # never touch emoji that already sit inside a <tg-emoji> tag
    parts = []
    last = 0
    for m in _TAG_RE.finditer(text):
        parts.append(_replace_all(text[last:m.start()]))
        parts.append(m.group(0))
        last = m.end()
    parts.append(_replace_all(text[last:]))
    return "".join(parts)


def _replace_all(chunk):
    chunk = html.escape(chunk, quote=False)
    # keep plain <b> bold markup usable (Telegram HTML parse_mode)
    chunk = chunk.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")
    for char in sorted(PREMIUM_EMOJIS, key=len, reverse=True):
        if char and char in chunk:
            chunk = chunk.replace(char, p_emoji(char))
    return chunk


def plain_html(text):
    """HTML-safe version of the message with the custom-emoji tags removed."""
    out = html.escape(strip_custom_emoji(text), quote=False)
    return out.replace("&lt;b&gt;", "<b>").replace("&lt;/b&gt;", "</b>")


def strip_custom_emoji(text):
    """Same message with the custom-emoji tags reduced to their plain emoji."""
    return _TAG_RE.sub(r"\1", text)
