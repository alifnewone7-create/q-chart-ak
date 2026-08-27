import os
import time
from pathlib import Path
from dotenv import load_dotenv

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

# force UTC+6 (Asia/Dhaka) so signal/entry times are always UTC+6
os.environ.setdefault("TZ", "Asia/Dhaka")
time.tzset()

BOT_TOKEN = os.environ["BOT_TOKEN"]
_admins = os.environ.get("ADMIN_IDS") or os.environ.get("ADMIN_ID") or ""
ADMIN_IDS = [int(x.strip()) for x in str(_admins).split(",") if x.strip()]
if not ADMIN_IDS:
    raise KeyError("ADMIN_IDS")
ADMIN_ID = ADMIN_IDS[0]
QUOTEX_EMAIL = os.environ["QUOTEX_EMAIL"]
QUOTEX_PASSWORD = os.environ["QUOTEX_PASSWORD"]
ACCOUNT_TYPE = os.environ.get("ACCOUNT_TYPE", "PRACTICE")
OWNER_TAG = os.environ.get("OWNER_TAG", "@BfsTraderQX")

# Optional Telegram Premium user account (MTProto) used for channel posts so
# that premium custom emoji render. Leave empty to post with the bot instead.
TG_API_ID = os.environ.get("TG_API_ID", "").strip()
TG_API_HASH = os.environ.get("TG_API_HASH", "").strip()
TG_SESSION = os.environ.get("TG_SESSION", "").strip()

DATA_DIR = BASE_DIR / "data"
DATA_DIR.mkdir(exist_ok=True)
