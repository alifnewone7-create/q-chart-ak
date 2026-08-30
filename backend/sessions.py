"""Live signal session engine: analyze -> signal -> result -> partial report."""
import asyncio
import time
from datetime import datetime

import analysis
import charting
import messages
import storage
from config import OWNER_TAG
from notifier import NOTIFY

ANALYSIS_CANDLES = 130   # history depth used for strategy backtesting/selection
CHART_CANDLES = 60       # candles drawn on the chart image sent to the channel
SCAN_START_SEC = 3       # second of the minute the scan begins (earlier = earlier signal)
SCAN_RESERVE = 8         # stop scanning this many seconds before the entry minute
EARLY_EXIT_CONF = 88.0   # take a candidate immediately once it scores this high
DEEP_CONF_BONUS = 12.0   # extra confidence demanded right after a LOSS
DEEP_CONF_CAP = 90.0     # the deep gate never exceeds this
DEEP_MIN_AGREE = 0.70    # min filter agreement in deep-analysis mode
BROADCAST_GAP = 3        # seconds between channels when posting to more than one
PLINE = messages.PLINE


def _hhmm(ts):
    return time.strftime("%H:%M", time.localtime(ts))


def _partial_asset(code):
    return code.replace("_otc", "-OTC")


def signal_caption(display, direction, entry_str, reason, payout=0):
    return messages.signal_caption(display, direction, entry_str, payout, reason, OWNER_TAG)


def result_caption(display, direction, entry_str, result, wins=0, losses=0, total_pct=0.0):
    return messages.result_caption(display, direction, entry_str, result,
                                   wins=wins, losses=losses, total_pct=total_pct)


def compute_delta(neg_deficit, per_trade_pct, result):
    """P&L change for one signal using the martingale recovery model.

    `neg_deficit` is the outstanding session loss as a negative number
    (0 when nothing has to be recovered).

    * first-trade stake S = per_trade_pct when there is no deficit,
      otherwise S = 2 x deficit (recover the outstanding loss).
    * MTG stake M = 2 x (deficit + S)  (double the deficit after a first loss).
      WIN      -> +S
      WIN_MTG  -> -S + M
      LOSS     -> -S - M
    """
    deficit = -neg_deficit if neg_deficit < 0 else 0.0
    s = per_trade_pct if deficit <= 0 else 2 * deficit
    m = 2 * (deficit + s)
    if result == "WIN":
        return s
    if result == "WIN_MTG":
        return -s + m
    return -s - m


class SessionManager:
    def __init__(self):
        self.active = False
        self.markets = []
        self.channels = []
        self.bot = None
        self.qx = None
        self.ticks = None
        self.task = None
        self.refresh_task = None
        self.signals = []
        self.session_id = None
        self.pnl = 0.0
        self.deficit = 0.0
        # deep-analysis mode: ON right after a LOSS, next signal must pass
        # a strict multi-engine consensus so back-to-back losses are avoided
        self.deep_mode = False
        # auto-select mode
        self.auto_mode = False
        self.auto_threshold = 0
        self.auto_category = "all"
        # admin UI live-update
        self.admin_chat_id = None
        self.admin_msg_id = None
        self.admin_kb = None
        # per-session dedup: (code, entry_ts) already signaled
        self._sent_entries = set()
        self._loop_lock = asyncio.Lock()

    def is_running(self):
        return self.active

    async def _cancel_task(self, task):
        if task and not task.done():
            task.cancel()
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass

    async def start(self, bot, qx, markets, channels, ticks=None,
                    auto_mode=False, auto_threshold=0, auto_category="all",
                    admin_chat_id=None, admin_msg_id=None, admin_kb=None):
        # make sure no leftover task from a previous session is still running
        self.active = False
        await self._cancel_task(self.task)
        await self._cancel_task(self.refresh_task)
        self.task = None
        self.refresh_task = None

        self.bot = bot
        self.qx = qx
        self.ticks = ticks
        self.markets = markets
        self.channels = list(channels or [])
        self.session_id = f"S{int(time.time())}"
        self.signals = []
        self.pnl = 0.0
        self.deficit = 0.0
        self._sent_entries = set()
        self.auto_mode = auto_mode
        self.auto_threshold = auto_threshold
        self.auto_category = auto_category
        self.admin_chat_id = admin_chat_id
        self.admin_msg_id = admin_msg_id
        self.admin_kb = admin_kb
        self.active = True
        if self.auto_mode:
            await self._refresh_markets()
            self.refresh_task = asyncio.create_task(self._refresh_loop())
        self.task = asyncio.create_task(self._loop())

    def close(self):
        self.active = False
        if self.refresh_task and not self.refresh_task.done():
            self.refresh_task.cancel()
        if self.task and not self.task.done():
            self.task.cancel()

    # ---------- auto-select payout scanning ---------------------------------

    async def _refresh_markets(self):
        try:
            fresh = await self.qx.get_markets(self.auto_category)
        except Exception as e:
            print(f"[session] auto-refresh failed: {e}")
            return
        selected = [m for m in fresh if m.get("payout", 0) >= self.auto_threshold]
        # preserve stable order: highest payout first
        selected.sort(key=lambda m: (-m["payout"], m["code"]))
        self.markets = selected
        await self._refresh_admin_view()

    async def _refresh_loop(self):
        # kick off refreshes on every fresh minute boundary
        while self.active:
            wait = 60 - (time.time() % 60)
            try:
                await asyncio.sleep(max(1.0, wait))
            except asyncio.CancelledError:
                return
            if not self.active:
                return
            await self._refresh_markets()

    async def _refresh_admin_view(self):
        if not (self.admin_chat_id and self.admin_msg_id and self.bot):
            return
        try:
            await self.bot.edit_message_text(
                chat_id=self.admin_chat_id,
                message_id=self.admin_msg_id,
                text=self.running_status_text(),
                reply_markup=self.admin_kb,
            )
        except Exception as e:
            # ignore "message is not modified" and rate limit noise
            msg = str(e).lower()
            if "not modified" not in msg:
                print(f"[session] admin view edit failed: {e}")

    def channel_ids(self):
        return [c["id"] for c in self.channels]

    def channel_titles(self):
        return ", ".join(c["title"] for c in self.channels)

    async def _broadcast_photo(self, png, caption):
        for i, ch in enumerate(self.channels):
            if i:
                await asyncio.sleep(BROADCAST_GAP)
            try:
                await NOTIFY.send_photo(ch["id"], png, caption)
            except Exception as e:
                print(f"[session] send_photo to {ch['title']} failed: {e}")

    async def _broadcast_text(self, text):
        for i, ch in enumerate(self.channels):
            if i:
                await asyncio.sleep(BROADCAST_GAP)
            try:
                await NOTIFY.send_message(ch["id"], text)
            except Exception as e:
                print(f"[session] send_message to {ch['title']} failed: {e}")

    def running_status_text(self):
        lines = [
            "\U0001f7e2 Session RUNNING",
            "",
            f"\U0001f4e2 Channel: {self.channel_titles()}",
        ]
        if self.auto_mode:
            cat_label = {
                "otc": "Only OTC",
                "real": "Only Real",
                "otcreal": "OTC + Real",
                "crypto": "Crypto",
                "all": "All",
            }.get(self.auto_category, self.auto_category)
            lines.append(f"\U0001f3af Mode: Auto Select \u2265 {self.auto_threshold}% ({cat_label})")
        else:
            lines.append("\U0001f3af Mode: Manual")
        lines.append(f"\U0001f4ca Markets: {len(self.markets)}")
        lines.append(f"\U0001f4e8 Signals sent: {len(self.signals)}")
        if self.markets:
            lines.append("")
            lines.append("\U0001f4dd Active markets:")
            for m in self.markets[:30]:
                lines.append(f"  \u2022 {m['display']} \u2014 {m['payout']}%")
            if len(self.markets) > 30:
                lines.append(f"  \u2026 (+{len(self.markets) - 30} more)")
        else:
            lines.append("")
            lines.append("\u26a0\ufe0f No markets currently match the threshold.")
        return "\n".join(lines)

    async def _loop(self):
        while self.active:
            try:
                await self._wait_window()
                if not self.active:
                    break
                pick = await self._pick_best()
                if not pick:
                    await asyncio.sleep(10)
                    continue
                await self._run_signal(*pick)
            except asyncio.CancelledError:
                break
            except Exception as e:
                print(f"[session] loop error: {e}")
                await asyncio.sleep(10)

    async def _wait_window(self):
        # wake up early in the minute so the signal goes out ~55s before entry
        target = (SCAN_START_SEC - (time.time() % 60)) % 60
        end = time.time() + target
        while self.active and time.time() < end:
            await asyncio.sleep(min(1.0, max(0.05, end - time.time())))

    async def _candles(self, code, count=60):
        """History candles from API merged with live tick-built candles
        (tick data wins; includes the current RUNNING candle)."""
        try:
            hist = await self.qx.get_candles_1m(code, count)
        except Exception as e:
            print(f"[session] history fetch failed {code}: {e}")
            hist = []
        book = {c["time"]: c for c in hist}
        if self.ticks:
            for c in self.ticks.get_candles(code):
                book[c["time"]] = c
        return [book[k] for k in sorted(book)][-count:]

    async def _pick_best(self):
        best = None
        st = analysis.active_strategy()
        min_conf = st["min_confidence"]
        deep = self.deep_mode
        if deep:
            min_conf = min(min_conf + DEEP_CONF_BONUS, DEEP_CONF_CAP)
        # snapshot to avoid races with the auto-refresh task swapping the list
        markets_snapshot = list(self.markets)
        # skip markets whose next entry_ts is already signaled
        next_entry_ts = ((int(time.time()) // 60) + 1) * 60
        deadline = next_entry_ts - SCAN_RESERVE
        checked = 0
        thin = 0
        top_conf = 0.0
        top_code = None
        for m in markets_snapshot:
            if not self.active:
                return None
            if time.time() >= deadline:
                print(f"[session] scan deadline hit after {checked}/{len(markets_snapshot)} markets")
                break
            if (m["code"], next_entry_ts) in self._sent_entries:
                continue
            candles = await self._candles(m["code"], ANALYSIS_CANDLES)
            if not candles:
                continue
            # analyze only CLOSED candles (drop the running one)
            cur_min = (int(time.time()) // 60) * 60
            closed = [c for c in candles if c["time"] < cur_min]
            if len(closed) < st["min_candles"]:
                thin += 1
                continue
            res = analysis.analyze(closed, entry_ts=next_entry_ts, strategy=st)
            checked += 1
            if not res:
                continue
            if res["confidence"] > top_conf:
                top_conf, top_code = res["confidence"], m["code"]
            if res["confidence"] < min_conf:
                continue
            if deep and not self._deep_pass(closed, next_entry_ts, res, st):
                continue
            if best is None or res["confidence"] > best[1]["confidence"]:
                best = (m, res)
            if best[1]["confidence"] >= EARLY_EXIT_CONF:
                break
        if best is None:
            mode = " [DEEP ANALYSIS]" if deep else ""
            print(f"[session] {st['name']}{mode}: no signal \u2014 analysed {checked} markets "
                  f"({thin} had too little history), best was {top_code or 'n/a'} at "
                  f"{top_conf:.1f}% (need {min_conf:.0f}%)")
        return best

    def _deep_pass(self, closed, entry_ts, res, st):
        """Post-loss gate: strict agreement + every other engine must concur."""
        if res.get("agree", 1.0) < DEEP_MIN_AGREE:
            return False
        import strategies
        for key in strategies.ORDER:
            if key == st["key"]:
                continue
            other = strategies.get(key)
            if len(closed) < other["min_candles"]:
                return False
            try:
                r2 = other["fn"](closed, entry_ts)
            except Exception:
                return False
            if not r2 or r2["direction"] != res["direction"]:
                return False
        return True

    async def _run_signal(self, market, res):
        direction = res["direction"]
        entry_ts = ((int(time.time()) // 60) + 1) * 60
        entry_str = _hhmm(entry_ts)

        # dedup: never send the same (market, entry_ts) twice within a session,
        # regardless of retries, overlapping tasks or timing race conditions
        key = (market["code"], entry_ts)
        if key in self._sent_entries:
            print(f"[session] skip duplicate signal for {market['code']} @ {entry_str}")
            return
        self._sent_entries.add(key)

        if not self.active:
            return

        # chart includes the live RUNNING candle from tick data
        candles = await self._candles(market["code"], CHART_CANDLES)
        png = charting.render_chart(
            candles, f"{market['display']}  \u00b7  M1", badge=direction,
            payout=market.get("payout", 0), entry_ts=entry_ts, entry_str=entry_str,
            market_name=market["display"], result=None,
        )
        reason = res["reason"]
        if self.deep_mode:
            reason += (" \U0001f52c Deep analysis: after the previous loss this setup was "
                       "re-verified \u2014 every engine independently agrees on this direction.")
        await self._broadcast_photo(
            png,
            signal_caption(market["display"], direction, entry_str, reason,
                           market.get("payout", 0)),
        )

        await self._sleep_until(entry_ts + 63)
        if not self.active:
            return
        c1 = await self._get_candle(market["code"], entry_ts)
        result = None
        if c1 and self._wins(c1, direction):
            result = "WIN"
        else:
            await self._sleep_until(entry_ts + 123)
            if not self.active:
                return
            c2 = await self._get_candle(market["code"], entry_ts + 60)
            result = "WIN_MTG" if (c2 and self._wins(c2, direction)) else "LOSS"

        # deep-analysis mode: ON after a LOSS, OFF again after any win
        self.deep_mode = (result == "LOSS")

        # performance stats for THIS session, including the current result
        per_trade = float(storage.get_settings().get("per_trade_pct", 1.0))
        delta = compute_delta(-self.deficit, per_trade, result)
        self.pnl += delta
        # outstanding loss carried into the next signal of this session
        self.deficit = (self.deficit - delta) if result == "LOSS" else 0.0

        prior = [s.get("result") for s in self.signals]
        all_res = prior + [result]
        wins = sum(1 for r in all_res if r in ("WIN", "WIN_MTG"))
        losses = sum(1 for r in all_res if r == "LOSS")
        stats = {"wins": wins, "losses": losses, "total": len(all_res),
                 "total_pct": self.pnl}

        fresh = await self._candles(market["code"], CHART_CANDLES) or candles
        png = charting.render_chart(
            fresh, f"{market['display']}  \u00b7  M1", badge=direction,
            payout=market.get("payout", 0), entry_ts=entry_ts, entry_str=entry_str,
            market_name=market["display"], result=result, stats=stats,
        )
        await self._broadcast_photo(
            png,
            result_caption(market["display"], direction, entry_str, result,
                           wins=wins, losses=losses, total_pct=self.pnl),
        )

        record = {
            "session_id": self.session_id,
            "date": datetime.now().strftime("%d.%m.%Y"),
            "code": market["code"],
            "display": market["display"],
            "direction": direction,
            "entry": entry_str,
            "entry_ts": entry_ts,
            "result": result,
            "pnl_delta": delta,
            "pnl_total": self.pnl,
            "deficit_after": self.deficit,
            "channel_id": self.channel_ids()[0] if self.channels else None,
            "channel_ids": self.channel_ids(),
        }
        self.signals.append(record)
        storage.append_signal(record)
        await self._refresh_admin_view()

    @staticmethod
    def _wins(candle, direction):
        if direction == "CALL":
            return candle["close"] > candle["open"]
        return candle["close"] < candle["open"]

    async def _get_candle(self, code, ts):
        for _ in range(4):
            if self.ticks:
                c = self.ticks.get_closed_candle(code, ts)
                if c:
                    return c
            try:
                candles = await self.qx.get_candles_1m(code, 20)
                for c in candles:
                    if c["time"] == ts:
                        return c
            except Exception as e:
                print(f"[session] result fetch failed: {e}")
            await asyncio.sleep(3)
        return None

    async def _sleep_until(self, ts):
        while time.time() < ts:
            await asyncio.sleep(min(1.0, max(0.05, ts - time.time())))

    def partial_text(self):
        sigs = self.signals
        if not sigs:
            return None
        mono = messages.mono
        date_str = datetime.now().strftime("%d.%m. %Y")
        lines = [
            f"=========== {mono('PARTIAL')} ===========",
            PLINE,
            f"\U0001f5d3 {mono(date_str)}",
            PLINE,
            f"\u2620\ufe0f {mono('Total')} : {mono(len(sigs))}",
            PLINE,
        ]
        wins = losses = 0
        for s in sigs:
            mark = {"WIN": "\U0001f44d", "WIN_MTG": "\U0001f44d\u00b9", "LOSS": "\u274c"}[s["result"]]
            if s["result"] == "LOSS":
                losses += 1
            else:
                wins += 1
            asset = mono(_partial_asset(s["code"]))
            lines.append(
                f"{mono('M1')} {asset} {mono(s['entry'])} {mono(s['direction'])} {mark}")
        total = wins + losses
        pct = round(wins / total * 100) if total else 0
        lines += [
            PLINE,
            f"\U0001f525 {mono('Win')}: {mono(wins)} | \u274c {mono('Loss')}: {mono(losses)} "
            f"| \u2696\ufe0f > ({mono(pct)}%)",
            PLINE,
        ]
        return "\n".join(lines)

    async def send_partial(self):
        text = self.partial_text()
        if not text or not self.channels:
            return False
        await self._broadcast_text(text)
        return True


SM = SessionManager()
