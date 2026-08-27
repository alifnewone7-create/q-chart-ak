"""Realtime tick collector: subscribes to ALL open markets on one Quotex
websocket and builds live 1m OHLC candles (including the running candle)."""
import asyncio
import time

MAX_MINUTES = 240
RESUB_INTERVAL = 300
TICK_IDLE_LIMIT = 90


class TickCollector:
    def __init__(self, qx):
        self.qx = qx
        self.candles = {}       # code -> {minute_ts: candle}
        self.codes = []
        self.running = False
        self.started_at = None  # minute boundary when collection began
        self.last_tick = time.time()
        self.task = None

    async def start(self):
        if self.running:
            return
        self.running = True
        self.task = asyncio.create_task(self._run())

    def stop(self):
        self.running = False
        if self.task:
            self.task.cancel()

    async def _run(self):
        while self.running:
            try:
                await self.qx.ensure_connected()
                await self._subscribe_all()
                last_sub = time.time()

                # wait for the NEXT full minute before collecting (partial minute discarded)
                boundary = ((int(time.time()) // 60) + 1) * 60
                if self.started_at is None:
                    self.started_at = boundary
                print(f"[ticks] {len(self.codes)} markets subscribed \u2014 collecting from "
                      f"{time.strftime('%H:%M:%S', time.localtime(boundary))}")
                while self.running and time.time() < boundary:
                    self._drain(discard=self.started_at == boundary)
                    await asyncio.sleep(0.5)

                self.last_tick = time.time()
                while self.running:
                    self._drain()
                    if time.time() - self.last_tick > TICK_IDLE_LIMIT:
                        raise ConnectionError(f"no ticks for {TICK_IDLE_LIMIT}s")
                    if time.time() - last_sub > RESUB_INTERVAL:
                        await self._subscribe_all()
                        last_sub = time.time()
                    await asyncio.sleep(0.4)
            except asyncio.CancelledError:
                return
            except Exception as e:
                print(f"[ticks] collector error: {e} \u2014 retrying in 30s")
                self.qx.connected = False
                await asyncio.sleep(30)

    async def _subscribe_all(self):
        markets = await self.qx.get_markets("all")
        self.codes = [m["code"] for m in markets]
        for code in self.codes:
            try:
                self.qx.client.start_candles_stream(code, 60)
            except Exception:
                pass
            await asyncio.sleep(0.04)

    def _drain(self, discard=False):
        api = self.qx.client.api if self.qx.client else None
        if not api:
            return
        for code in list(api.realtime_price.keys()):
            buf = api.realtime_price.get(code)
            if not buf:
                continue
            n = len(buf)
            items = buf[:n]
            del buf[:n]
            self.last_tick = time.time()
            if discard:
                continue
            book = self.candles.setdefault(code, {})
            for t in items:
                try:
                    ts = float(t["time"])
                    price = float(t["price"])
                except (KeyError, TypeError, ValueError):
                    continue
                if ts < self.started_at:
                    continue
                m = (int(ts) // 60) * 60
                c = book.get(m)
                if c is None:
                    book[m] = {"time": m, "open": price, "high": price,
                               "low": price, "close": price}
                else:
                    if price > c["high"]:
                        c["high"] = price
                    if price < c["low"]:
                        c["low"] = price
                    c["close"] = price
            if len(book) > MAX_MINUTES:
                for k in sorted(book)[:-MAX_MINUTES]:
                    book.pop(k)

    def get_candles(self, code):
        book = self.candles.get(code, {})
        return [dict(book[k]) for k in sorted(book)]

    def get_closed_candle(self, code, ts):
        """Return the finished tick-built candle for minute ts, or None."""
        if self.started_at is None or ts < self.started_at:
            return None
        if time.time() < ts + 60:
            return None
        c = self.candles.get(code, {}).get(ts)
        return dict(c) if c else None
