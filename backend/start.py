#!/usr/bin/env python3
"""SignalMaster Pro — clean launcher.

Runs the Telegram bot but keeps the console silent: only a single
"Bot running..." line is shown. All library / pyquotex / telegram logs
and prints are suppressed.
"""
import os
import sys
import logging
import warnings
from logging.handlers import RotatingFileHandler

# ---- keep the console silent, but keep a real log on disk ----
# (this used to be logging.disable(CRITICAL) + every logger disabled, which meant
# a crash or a stalled Quotex socket left absolutely no trace to debug)
warnings.filterwarnings("ignore")

LOG_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "bot.log")

_handler = RotatingFileHandler(LOG_FILE, maxBytes=5 * 1024 * 1024, backupCount=3)
_handler.setFormatter(logging.Formatter(
    "%(asctime)s %(levelname)s %(name)s: %(message)s"))
_root = logging.getLogger()
_root.handlers = [_handler]           # file only, nothing on the console
_root.setLevel(logging.WARNING)
for _noisy in ("httpx", "websocket", "telegram", "apscheduler", "asyncio"):
    logging.getLogger(_noisy).setLevel(logging.ERROR)


def _run():
    # the ONLY thing the user should ever see on the console
    sys.__stdout__.write("Bot running...\n")
    sys.__stdout__.flush()
    # from here on, hide all stdout noise (pyquotex uses print())
    class _Null:
        def write(self, *_a, **_k):
            return 0

        def flush(self):
            pass

    sys.stdout = _Null()

    import bot  # obfuscated module
    try:
        bot.main()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    _run()
