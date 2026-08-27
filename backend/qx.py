"""Quotex connection manager built on the bundled pyquotex package."""
import asyncio
import os
import time
from pathlib import Path

import certifi

os.environ.setdefault("SSL_CERT_FILE", certifi.where())
os.environ.setdefault("WEBSOCKET_CLIENT_CA_BUNDLE", certifi.where())

from pyquotex.stable_api import Quotex

from config import QUOTEX_EMAIL, QUOTEX_PASSWORD, ACCOUNT_TYPE

CRYPTO_CODES = {
    "ADAUSD_otc", "APTUSD_otc", "ARBUSD_otc", "ATOUSD_otc", "AVAUSD_otc",
    "AXSUSD_otc", "BCHUSD_otc", "BNBUSD_otc", "BONUSD_otc", "BTCUSD_otc",
    "DASUSD_otc", "DOGUSD_otc", "DOTUSD_otc", "ETCUSD_otc", "ETHUSD_otc",
    "FLOUSD_otc", "GALUSD_otc", "HMSUSD_otc", "LINUSD_otc", "LTCUSD_otc",
    "MELUSD_otc", "SHIBUSD_otc", "SOLUSD_otc", "TIAUSD_otc", "TONUSD_otc",
    "TRUUSD_otc", "TRXUSD_otc", "WIFUSD_otc", "XRPUSD_otc", "ZECUSD_otc",
}
CRYPTO_BASES = {c.replace("_otc", "") for c in CRYPTO_CODES}

# every network step gets a hard ceiling so a stalled Quotex socket can never
# hold the connection lock (and therefore the whole bot) forever
CHECK_TIMEOUT = 15
CONNECT_TIMEOUT = 120
CLOSE_TIMEOUT = 20


def category_of(code):
    if code in CRYPTO_CODES or code.replace("_otc", "") in CRYPTO_BASES:
        return "crypto"
    return "otc" if code.endswith("_otc") else "real"


def _session_file():
    """Path of pyquotex's session.json (handles frozen/PyInstaller builds)."""
    try:
        from pyquotex.config import resource_path
        return Path(resource_path("session.json"))
    except Exception:
        return Path("session.json")


class QuotexManager:
    def __init__(self):
        self.client = None
        self.connected = False
        self._lock = asyncio.Lock()
        # a forced brand-new login already succeeded during this run
        self._fresh_done = False
        # last moment the connection was verified healthy (watchdog input)
        self.last_ok = time.time()

    async def _discard(self, client):
        """Close a client we are about to throw away — otherwise its websocket
        thread stays alive for the life of the process, keeps receiving ticks
        into buffers nobody drains, and leaks a socket on every reconnect."""
        if client is None:
            return
        try:
            await asyncio.wait_for(client.close(), CLOSE_TIMEOUT)
        except Exception as e:
            print(f"[qx] closing old client failed: {e}")

    def _make_client(self, fresh):
        client = Quotex(
            email=QUOTEX_EMAIL, password=QUOTEX_PASSWORD,
            host="qxbroker.com", lang="en",
        )
        if fresh:
            # Blank only the stored SSID/cookies (keep the user-agent) so
            # pyquotex runs a full credential login instead of reusing a
            # possibly expired token.
            try:
                sd = dict(client.session_data or {})
            except Exception:
                sd = {}
            sd["token"] = None
            sd["cookies"] = None
            client.session_data = sd
        return client

    async def ensure_connected(self):
        async with self._lock:
            if self.connected and self.client:
                try:
                    if await asyncio.wait_for(self.client.check_connect(), CHECK_TIMEOUT):
                        self.last_ok = time.time()
                        return
                except Exception:
                    pass
            self.connected = False

            # First connection of this run -> try a brand-new login first.
            # Later reconnects reuse the saved session first, because firing a
            # full credential login on every retry makes Quotex reject it
            # ("Login failed. Unknown error").
            modes = (True, False) if not self._fresh_done else (False, True)
            reason = "not attempted"
            for fresh in modes:
                old, self.client = self.client, self._make_client(fresh)
                await self._discard(old)
                try:
                    ok, reason = await asyncio.wait_for(
                        self.client.connect(), CONNECT_TIMEOUT)
                except asyncio.TimeoutError:
                    ok, reason = False, f"connect timed out after {CONNECT_TIMEOUT}s"
                except Exception as e:
                    ok, reason = False, f"connect error: {e}"
                if ok:
                    self._fresh_done = True
                    try:
                        await asyncio.wait_for(
                            self.client.change_account(ACCOUNT_TYPE), CHECK_TIMEOUT)
                        await asyncio.wait_for(
                            self.client.get_instruments(), CONNECT_TIMEOUT)
                    except Exception as e:
                        raise ConnectionError(f"Quotex setup failed: {e}")
                    self.connected = True
                    self.last_ok = time.time()
                    return
                await asyncio.sleep(1)

            # both a fresh login and the saved session failed -> clear the
            # stored session so the next attempt starts clean
            try:
                _session_file().unlink(missing_ok=True)
            except Exception:
                pass
            raise ConnectionError(f"Quotex login failed: {reason}")

    async def get_markets(self, category):
        """Returns open markets for a category: [{code, display, payout}] sorted by payout desc."""
        await self.ensure_connected()
        instruments = await self.client.get_instruments()
        markets = []
        for i in instruments:
            try:
                code = i[1]
                display = i[2].replace("\n", "")
                is_open = bool(i[14])
                payout = i[-9]
                if not isinstance(payout, (int, float)):
                    payout = i[5]
            except (IndexError, TypeError):
                continue
            if not code or not is_open:
                continue
            if not isinstance(payout, (int, float)) or payout <= 0:
                continue
            if category not in ("all", "otcreal") and category_of(code) != category:
                continue
            if category == "otcreal" and category_of(code) == "crypto":
                continue
            markets.append({"code": code, "display": display, "payout": int(payout)})
        markets.sort(key=lambda m: (-m["payout"], m["code"]))
        return markets

    async def get_candles_1m(self, code, count=60):
        """Returns normalized 1m candles sorted by time: [{time, open, high, low, close}]."""
        await self.ensure_connected()
        raw = await self.client.get_candles(code, time.time(), count * 60, 60)
        out = {}
        for c in raw or []:
            if not isinstance(c, dict):
                continue
            if not all(k in c for k in ("time", "open", "high", "low", "close")):
                continue
            ts = (int(float(c["time"])) // 60) * 60
            out[ts] = {
                "time": ts, "open": float(c["open"]), "high": float(c["high"]),
                "low": float(c["low"]), "close": float(c["close"]),
            }
        return [out[k] for k in sorted(out)]
