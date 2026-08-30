"""Shared indicator helpers used by more than one strategy engine.

These are the primitives that BOTH confluence engines lean on; anything used by
a single engine stays in that engine's own file.
"""


def _clamp(v, lo=-1.0, hi=1.0):
    return max(lo, min(hi, v))


def _streak(x, i):
    st = 0
    j = i
    while j >= 0 and x.body[j] != 0:
        s = 1 if x.body[j] > 0 else -1
        if st == 0:
            st = s
        elif (st > 0) == (s > 0):
            st += s
        else:
            break
        j -= 1
    return st


def _wick_pair(x, j):
    top = max(x.o[j], x.cl[j])
    bot = min(x.o[j], x.cl[j])
    return (bot - x.l[j]) / x.rng[j], (x.h[j] - top) / x.rng[j]


def _efficiency(x, i, p=14):
    s = max(0, i - p)
    net = abs(x.cl[i] - x.cl[s])
    path = sum(abs(x.cl[j] - x.cl[j - 1]) for j in range(s + 1, i + 1)) or 1e-12
    return net / path


def _m_macd(x, i):
    a = x.atr(14)[i] or 1e-12
    hist = x.macd(6, 13, 5)[2]
    now = _clamp(hist[i] / (0.30 * a))
    slope = _clamp((hist[i] - hist[i - 1]) / (0.18 * a)) if i >= 1 else 0.0
    return _clamp(0.65 * now + 0.35 * slope)


def _m_momentum(x, i):
    a = x.atr(14)[i] or 1e-12
    push = _clamp((x.cl[i] - x.cl[max(0, i - 3)]) / (1.2 * a))
    rsi = _clamp((x.rsi(9)[i] - 50.0) / 22.0)
    return _clamp(0.55 * push + 0.45 * rsi)


def _m_band(x, i):
    _, up, lo = x.bb(20, 2.0)
    width = max(1e-12, up[i] - lo[i])
    pb = (x.cl[i] - lo[i]) / width
    if pb <= 0.04:
        return 1.0
    if pb <= 0.16:
        return 0.65
    if pb >= 0.96:
        return -1.0
    if pb >= 0.84:
        return -0.65
    return 0.0


def _m_rsi_extreme(x, i):
    r = x.rsi(14)[i]
    if r <= 24:
        return 1.0
    if r <= 32:
        return 0.6
    if r >= 76:
        return -1.0
    if r >= 68:
        return -0.6
    return 0.0


def _m_exhaustion(x, i):
    st = _streak(x, i)
    if abs(st) < 3:
        return 0.0
    base = min(1.0, 0.4 + (abs(st) - 2) * 0.3)
    stall = 1.0
    if i >= 3:
        prev = sum(x.rng[i - 3:i]) / 3.0
        if x.rng[i] < 0.6 * prev:
            stall = 1.25
    return _clamp(-(1 if st > 0 else -1) * base * stall)


def _m_htf(x, i):
    """5-minute view rebuilt from the same 1m candles (closed groups only)."""
    closes, group, key = [], None, None
    for j in range(i + 1):
        k = x.c[j]["time"] // 300
        if k != key:
            if group is not None:
                closes.append(group)
            key, group = k, x.cl[j]
        else:
            group = x.cl[j]
    if len(closes) < 10:
        return 0.0
    from indicators_py import _ema
    e3, e8 = _ema(closes, 3), _ema(closes, 8)
    a = x.atr(14)[i] or 1e-12
    trend = _clamp((e3[-1] - e8[-1]) / (0.9 * a))
    push = _clamp((closes[-1] - closes[-3]) / (2.2 * a))
    return _clamp(0.6 * trend + 0.4 * push)
