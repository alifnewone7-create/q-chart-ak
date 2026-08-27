import json
from config import DATA_DIR

CHANNELS_FILE = DATA_DIR / "channels.json"
SETTINGS_FILE = DATA_DIR / "settings.json"
SIGNALS_FILE = DATA_DIR / "signals.json"


def _load(path, default):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return default


def _save(path, data):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)


def get_channels():
    return _load(CHANNELS_FILE, [])


def add_channel(chat_id, title):
    channels = get_channels()
    for ch in channels:
        if ch["id"] == chat_id:
            ch["title"] = title
            _save(CHANNELS_FILE, channels)
            return False
    channels.append({"id": chat_id, "title": title})
    _save(CHANNELS_FILE, channels)
    return True


def remove_channel(chat_id):
    channels = [c for c in get_channels() if c["id"] != chat_id]
    _save(CHANNELS_FILE, channels)


DEFAULT_SETTINGS = {"mtg": "MTG-1", "strategy": "classic", "per_trade_pct": 1.0}


def get_settings():
    s = _load(SETTINGS_FILE, {})
    return {**DEFAULT_SETTINGS, **s}


def save_settings(settings):
    _save(SETTINGS_FILE, settings)


def append_signal(record):
    signals = _load(SIGNALS_FILE, [])
    signals.append(record)
    _save(SIGNALS_FILE, signals)


def get_signals():
    return _load(SIGNALS_FILE, [])
