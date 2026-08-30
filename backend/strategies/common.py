"""Shared indicator helpers used by more than one strategy engine.

These are the primitives that BOTH confluence engines lean on; anything used by
a single engine stays in that engine's own file.
"""
import math


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


# ---------------------------------------------------------------------------
# Regime / no-trade primitives (used by every engine)
# ---------------------------------------------------------------------------

def _choppiness(x, i, p=14):
    """Choppiness Index 0-100: > ~61.8 = churning, < ~38.2 = travelling."""
    if i < p:
        return 50.0
    s = i - p + 1
    tr = 0.0
    for j in range(s, i + 1):
        tr += max(x.h[j] - x.l[j],
                  abs(x.h[j] - x.cl[j - 1]), abs(x.l[j] - x.cl[j - 1]))
    box = max(1e-12, max(x.h[s:i + 1]) - min(x.l[s:i + 1]))
    return 100.0 * math.log10(max(1.0, tr / box)) / math.log10(p)


def _squeeze_on(x, i, mult=1.5):
    """TTM squeeze: Bollinger 20/2 fully inside the Keltner 20/1.5 channel."""
    _, up, lo = x.bb(20, 2.0)
    mid = x.ema(20)[i]
    a = x.atr(20)[i]
    return up[i] < mid + mult * a and lo[i] > mid - mult * a


def no_trade(x, i):
    """(blocked, reason) — market states where NO engine should fire.

    Skipping untradeable conditions is the cheapest accuracy upgrade there is:
    news spikes, dead chop, an unresolved volatility squeeze and flat/dead
    EMAs are the classic 1-minute account killers.
    """
    if i < 20:
        return True, "not enough closed history"
    a = x.atr(14)[i] or 1e-12
    for j in range(max(1, i - 2), i + 1):
        if x.rng[j] > 3.2 * a:
            return True, "spike candle in the last 3 (possible news)"
    adx, _pdi, _mdi = x.adx(14)
    chop = _choppiness(x, i)
    if adx[i] < 13.0 and chop > 62.0:
        return True, f"dead chop (ADX {adx[i]:.0f}, CHOP {chop:.0f})"
    if i >= 24 and all(_squeeze_on(x, j) for j in range(i - 3, i + 1)):
        return True, "volatility squeeze building \u2014 direction unknown"
    e9, e21 = x.ema(9)[i], x.ema(21)[i]
    if abs(e9 - e21) < 0.07 * a and adx[i] < 15.0 and _efficiency(x, i) < 0.10:
        return True, "flat EMAs, no directional efficiency"
    return False, ""


# ---------------------------------------------------------------------------
# Extra shared directional modules
# ---------------------------------------------------------------------------

def _m_adx_dir(x, i):
    """DI+/DI- direction, gated by ADX trend strength."""
    adx, pdi, mdi = x.adx(14)
    strength = _clamp((adx[i] - 15.0) / 25.0, 0.0, 1.0)
    if strength <= 0:
        return 0.0
    return _clamp((pdi[i] - mdi[i]) / 18.0) * strength


def _m_cci(x, i):
    v = x.cci(20)[i]
    if v <= -190:
        return 1.0
    if v <= -120:
        return 0.6
    if v >= 190:
        return -1.0
    if v >= 120:
        return -0.6
    return 0.0


def _m_willr(x, i):
    w = x.wr(14)[i]
    if w <= -92:
        return 0.9
    if w <= -80:
        return 0.5
    if w >= -8:
        return -0.9
    if w >= -20:
        return -0.5
    return 0.0


def _m_heikin(x, i):
    """Consistency of the last 4 Heikin-Ashi candles."""
    ho, hc = x.heikin()
    s, n = 0.0, 0
    for j in range(max(0, i - 3), i + 1):
        n += 1
        s += 1.0 if hc[j] >= ho[j] else -1.0
    return _clamp(s / max(1, n))


def _m_accel(x, i):
    """Momentum plus its acceleration (is the push speeding up or fading?)."""
    a = x.atr(14)[i] or 1e-12
    m1 = x.cl[i] - x.cl[max(0, i - 3)]
    m2 = x.cl[max(0, i - 3)] - x.cl[max(0, i - 6)]
    return _clamp(0.6 * _clamp(m1 / (1.2 * a)) + 0.4 * _clamp((m1 - m2) / (1.6 * a)))


def _m_squeeze_break(x, i):
    """Squeeze released within the last 3 candles -> follow the release."""
    if i < 26 or _squeeze_on(x, i):
        return 0.0
    a = x.atr(14)[i] or 1e-12
    for j in range(i - 3, i):
        if _squeeze_on(x, j):
            return _clamp((x.cl[i] - x.cl[j]) / (1.0 * a))
    return 0.0
