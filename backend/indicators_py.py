"""Pure-python indicator series used by the strategy engine (no extra deps).

Every function returns a list aligned 1:1 with the candle list, seeded at the
start so there are no None holes. Callers must still make sure enough history
exists before trusting the early values.
"""


class Ctx:
    """Pre-computed indicator context for one candle list. Series are cached."""

    def __init__(self, candles):
        self.c = candles
        self.n = len(candles)
        self.o = [x["open"] for x in candles]
        self.h = [x["high"] for x in candles]
        self.l = [x["low"] for x in candles]
        self.cl = [x["close"] for x in candles]
        self.tp = [(a + b + d) / 3.0 for a, b, d in zip(self.h, self.l, self.cl)]
        self.rng = [max(1e-12, a - b) for a, b in zip(self.h, self.l)]
        self.body = [a - b for a, b in zip(self.cl, self.o)]
        self._cache = {}

    def _memo(self, key, fn):
        if key not in self._cache:
            self._cache[key] = fn()
        return self._cache[key]

    # ---------- averages ----------

    def sma(self, p, src=None):
        return self._memo(("sma", p, src), lambda: _sma(self._src(src), p))

    def ema(self, p, src=None):
        return self._memo(("ema", p, src), lambda: _ema(self._src(src), p))

    def _src(self, src):
        if src is None or src == "close":
            return self.cl
        if src == "tp":
            return self.tp
        if src == "high":
            return self.h
        if src == "low":
            return self.l
        return self.cl

    # ---------- oscillators ----------

    def rsi(self, p):
        return self._memo(("rsi", p), lambda: _rsi(self.cl, p))

    def macd(self, f, s, sig):
        def build():
            ef, es = _ema(self.cl, f), _ema(self.cl, s)
            line = [a - b for a, b in zip(ef, es)]
            sg = _ema(line, sig)
            hist = [a - b for a, b in zip(line, sg)]
            return line, sg, hist
        return self._memo(("macd", f, s, sig), build)

    def bb(self, p, k):
        def build():
            mid = _sma(self.cl, p)
            up, lo = [], []
            for i in range(self.n):
                s = max(0, i - p + 1)
                win = self.cl[s:i + 1]
                m = mid[i]
                var = sum((v - m) ** 2 for v in win) / len(win)
                sd = var ** 0.5
                up.append(m + k * sd)
                lo.append(m - k * sd)
            return mid, up, lo
        return self._memo(("bb", p, k), build)

    def stoch(self, p, d):
        def build():
            kk = []
            for i in range(self.n):
                s = max(0, i - p + 1)
                hi = max(self.h[s:i + 1])
                lo = min(self.l[s:i + 1])
                kk.append(100.0 * (self.cl[i] - lo) / max(1e-12, hi - lo))
            return kk, _sma(kk, d)
        return self._memo(("stoch", p, d), build)

    def atr(self, p):
        def build():
            tr = [self.rng[0]]
            for i in range(1, self.n):
                tr.append(max(self.h[i] - self.l[i],
                              abs(self.h[i] - self.cl[i - 1]),
                              abs(self.l[i] - self.cl[i - 1])))
            return _ema(tr, p)
        return self._memo(("atr", p), build)

    def roc(self, p):
        def build():
            out = []
            for i in range(self.n):
                prev = self.cl[max(0, i - p)]
                out.append((self.cl[i] - prev) / max(1e-12, abs(prev)) * 100.0)
            return out
        return self._memo(("roc", p), build)

    def wr(self, p):
        def build():
            out = []
            for i in range(self.n):
                s = max(0, i - p + 1)
                hi = max(self.h[s:i + 1])
                lo = min(self.l[s:i + 1])
                out.append(-100.0 * (hi - self.cl[i]) / max(1e-12, hi - lo))
            return out
        return self._memo(("wr", p), build)

    def cci(self, p):
        def build():
            base = _sma(self.tp, p)
            out = []
            for i in range(self.n):
                s = max(0, i - p + 1)
                win = self.tp[s:i + 1]
                m = base[i]
                dev = sum(abs(v - m) for v in win) / len(win)
                out.append((self.tp[i] - m) / max(1e-12, 0.015 * dev))
            return out
        return self._memo(("cci", p), build)

    def slope(self, p):
        """Least-squares slope of close over the last p candles, ATR-normalised."""
        def build():
            out = []
            for i in range(self.n):
                s = max(0, i - p + 1)
                win = self.cl[s:i + 1]
                m = len(win)
                if m < 2:
                    out.append(0.0)
                    continue
                mx = (m - 1) / 2.0
                my = sum(win) / m
                num = sum((j - mx) * (win[j] - my) for j in range(m))
                den = sum((j - mx) ** 2 for j in range(m)) or 1e-12
                out.append(num / den)
            return out
        return self._memo(("slope", p), build)

    def vwapish(self, p):
        """Range-weighted typical-price average — a stand-in for VWAP.

        Quotex 1m candles carry no volume, so candle range is used as the
        weight. It is a proxy, not real volume-weighted price.
        """
        def build():
            out = []
            for i in range(self.n):
                s = max(0, i - p + 1)
                num = sum(self.tp[j] * self.rng[j] for j in range(s, i + 1))
                den = sum(self.rng[j] for j in range(s, i + 1)) or 1e-12
                out.append(num / den)
            return out
        return self._memo(("vwapish", p), build)

    def highest(self, p):
        return self._memo(("hh", p), lambda: [max(self.h[max(0, i - p + 1):i + 1]) for i in range(self.n)])

    def lowest(self, p):
        return self._memo(("ll", p), lambda: [min(self.l[max(0, i - p + 1):i + 1]) for i in range(self.n)])

    def heikin(self):
        def build():
            ho, hc = [self.o[0]], [(self.o[0] + self.h[0] + self.l[0] + self.cl[0]) / 4.0]
            for i in range(1, self.n):
                ho.append((ho[-1] + hc[-1]) / 2.0)
                hc.append((self.o[i] + self.h[i] + self.l[i] + self.cl[i]) / 4.0)
            return ho, hc
        return self._memo(("heikin",), build)


def _sma(vals, p):
    out, run = [], 0.0
    for i, v in enumerate(vals):
        run += v
        if i >= p:
            run -= vals[i - p]
        out.append(run / min(i + 1, p))
    return out


def _ema(vals, p):
    if not vals:
        return []
    k = 2.0 / (p + 1)
    val = vals[0]
    out = [val]
    for v in vals[1:]:
        val = (v - val) * k + val
        out.append(val)
    return out


def _rsi(closes, p):
    if len(closes) < 2:
        return [50.0] * len(closes)
    gains, losses = [0.0], [0.0]
    for i in range(1, len(closes)):
        d = closes[i] - closes[i - 1]
        gains.append(max(0.0, d))
        losses.append(max(0.0, -d))
    ag, al = _ema(gains, p), _ema(losses, p)
    return [100.0 - 100.0 / (1.0 + (g / l if l > 1e-12 else 999.0)) for g, l in zip(ag, al)]
