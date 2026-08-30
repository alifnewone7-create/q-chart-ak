"""OTC Sniper Pro — regime-adaptive, self-weighting confluence engine."""
from indicators_py import Ctx

from .common import (
    _clamp,
    _efficiency,
    _m_band,
    _m_exhaustion,
    _m_htf,
    _m_macd,
    _m_momentum,
    _m_rsi_extreme,
    _wick_pair,
)

OTC_MIN_CANDLES = 40
_ADAPT_LOOKBACK = 70   # candles used to re-weight modules on this market
_ADAPT_MIN_SAMPLES = 8


def _m_trend_align(x, i):
    a = x.atr(14)[i] or 1e-12
    e5, e13, e34 = x.ema(5)[i], x.ema(13)[i], x.ema(34)[i]
    parts = [
        _clamp((e5 - e13) / (0.55 * a)),
        _clamp((e13 - e34) / (1.10 * a)),
        _clamp(x.slope(10)[i] / (0.13 * a)),
    ]
    return _clamp(sum(parts) / 3.0)


def _m_pullback(x, i):
    a = x.atr(14)[i] or 1e-12
    e13, e34 = x.ema(13)[i], x.ema(34)[i]
    strength = _clamp(abs(e13 - e34) / (0.8 * a), 0.0, 1.0)
    if strength < 0.2:
        return 0.0
    dist = (x.cl[i] - e13) / a
    if e13 > e34 and -1.0 <= dist <= 0.30:
        return strength
    if e13 < e34 and -0.30 <= dist <= 1.0:
        return -strength
    return 0.0


def _m_structure(x, i):
    if i < 10:
        return 0.0
    hi_now, lo_now = max(x.h[i - 4:i + 1]), min(x.l[i - 4:i + 1])
    hi_prev, lo_prev = max(x.h[i - 9:i - 4]), min(x.l[i - 9:i - 4])
    up = (hi_now > hi_prev) + (lo_now > lo_prev)
    dn = (hi_now < hi_prev) + (lo_now < lo_prev)
    return _clamp((up - dn) / 2.0)


def _m_pattern(x, i):
    score = 0.0
    b = x.body
    # engulfing
    if i >= 1:
        if b[i] > 0 and b[i - 1] < 0 and x.cl[i] > x.o[i - 1] and x.o[i] < x.cl[i - 1]:
            score += 1.0
        elif b[i] < 0 and b[i - 1] > 0 and x.cl[i] < x.o[i - 1] and x.o[i] > x.cl[i - 1]:
            score -= 1.0
    # pin bar
    lw, uw = _wick_pair(x, i)
    if lw > 0.55 and uw < 0.25:
        score += 0.8
    elif uw > 0.55 and lw < 0.25:
        score -= 0.8
    # inside-bar break
    if i >= 2 and x.h[i - 1] <= x.h[i - 2] and x.l[i - 1] >= x.l[i - 2]:
        if x.cl[i] > x.h[i - 1]:
            score += 0.6
        elif x.cl[i] < x.l[i - 1]:
            score -= 0.6
    # star reversal (small middle candle between two opposite bodies)
    if i >= 2 and abs(b[i - 1]) < 0.35 * abs(b[i - 2] or 1e-12):
        if b[i - 2] < 0 and b[i] > 0 and x.cl[i] > x.cl[i - 1]:
            score += 0.7
        elif b[i - 2] > 0 and b[i] < 0 and x.cl[i] < x.cl[i - 1]:
            score -= 0.7
    # three soldiers / crows
    if i >= 2:
        if all(v > 0 for v in b[i - 2:i + 1]) and x.cl[i] > x.cl[i - 1] > x.cl[i - 2]:
            score += 0.5
        elif all(v < 0 for v in b[i - 2:i + 1]) and x.cl[i] < x.cl[i - 1] < x.cl[i - 2]:
            score -= 0.5
    # tweezer bottom / top
    if i >= 1:
        a = x.atr(14)[i] or 1e-12
        if abs(x.l[i] - x.l[i - 1]) < 0.12 * a and b[i] > 0:
            score += 0.5
        elif abs(x.h[i] - x.h[i - 1]) < 0.12 * a and b[i] < 0:
            score -= 0.5
    return _clamp(score / 1.8)


def _m_wick(x, i):
    lw = uw = 0.0
    for j in range(max(0, i - 1), i + 1):
        a_, b_ = _wick_pair(x, j)
        lw += a_
        uw += b_
    return _clamp((lw - uw) / 1.1)


def _m_stoch(x, i):
    kk, dd = x.stoch(9, 3)
    base = 0.0
    if kk[i] <= 15:
        base = 1.0
    elif kk[i] <= 25:
        base = 0.55
    elif kk[i] >= 85:
        base = -1.0
    elif kk[i] >= 75:
        base = -0.55
    cross = 0.25 if kk[i] > dd[i] else -0.25
    return _clamp(base + (cross if base != 0 else 0.0))


def _m_vwap(x, i):
    a = x.atr(14)[i] or 1e-12
    return _clamp((x.vwapish(20)[i] - x.cl[i]) / (1.3 * a))


def _m_sr(x, i):
    a = x.atr(14)[i] or 1e-12
    hi20, lo20 = x.highest(20)[i], x.lowest(20)[i]
    if x.cl[i] - lo20 < 0.30 * a:
        return 0.8
    if hi20 - x.cl[i] < 0.30 * a:
        return -0.8
    return 0.0


def _m_persistence(x, i):
    """Data-driven follow-or-fade of the last candle.

    Measures the lag-1 autocorrelation of candle bodies over the recent window.
    OTC feeds are frequently anti-persistent (a candle tends to be followed by
    the opposite colour); when that is what the data says, this module fades the
    last candle. When bodies are actually persistent, it follows them. Nothing
    is assumed about the market — the sign comes from the market itself.
    """
    s = max(1, i - 59)
    prev = cur = 0.0
    num = den = 0.0
    for j in range(s, i + 1):
        cur = x.body[j] / x.rng[j]
        if j > s:
            num += prev * cur
            den += prev * prev
        prev = cur
    if den <= 1e-12:
        return 0.0
    ac1 = num / den
    a = x.atr(14)[i] or 1e-12
    last = _clamp(x.body[i] / (0.9 * a))
    return _clamp(_clamp(ac1 / 0.18) * last)


# key, label, fn, weight in a TRENDING regime, weight in a RANGING regime
MODULES = [
    ("trend_align", "EMA 5/13/34 stack",        _m_trend_align, 0.20, 0.02),
    ("pullback",    "Pullback into EMA13",      _m_pullback,    0.12, 0.00),
    ("macd",        "MACD 6/13/5 thrust",       _m_macd,        0.12, 0.03),
    ("structure",   "HH/HL market structure",   _m_structure,   0.10, 0.02),
    ("momentum",    "3-candle push + RSI9",     _m_momentum,    0.10, 0.04),
    ("persistence", "Follow/fade autocorr",     _m_persistence, 0.09, 0.18),
    ("pattern",     "Candle patterns",          _m_pattern,     0.08, 0.10),
    ("wick",        "Wick rejection",           _m_wick,        0.05, 0.09),
    ("band",        "Bollinger 20/2 extreme",   _m_band,        0.04, 0.18),
    ("rsi",         "RSI14 30/70 extreme",      _m_rsi_extreme, 0.04, 0.15),
    ("stoch",       "Stochastic 9/3",           _m_stoch,       0.03, 0.10),
    ("vwap",        "Stretch from VWAP-proxy",  _m_vwap,        0.02, 0.09),
    ("exhaustion",  "Streak exhaustion",        _m_exhaustion,  0.02, 0.09),
    ("htf",         "5-minute alignment",       _m_htf,         0.06, 0.01),
    ("sr",          "S/R 20 rejection",         _m_sr,          0.02, 0.08),
]


_PHRASES = {
    "trend_align": ("EMA 5/13/34 are stacked upward", "EMA 5/13/34 are stacked downward"),
    "pullback": ("price pulled back into rising EMA13 support", "price rallied back into falling EMA13 resistance"),
    "macd": ("MACD momentum is thrusting up", "MACD momentum is thrusting down"),
    "structure": ("structure is printing higher highs and higher lows", "structure is printing lower highs and lower lows"),
    "momentum": ("the last candles pushed up with RSI above midline", "the last candles pushed down with RSI below midline"),
    "persistence": ("candle-to-candle autocorrelation favours an up candle next", "candle-to-candle autocorrelation favours a down candle next"),
    "pattern": ("a bullish reversal/continuation pattern printed", "a bearish reversal/continuation pattern printed"),
    "wick": ("long lower wicks show buyers defending", "long upper wicks show sellers capping"),
    "band": ("price is pinned to the lower Bollinger band", "price is pinned to the upper Bollinger band"),
    "rsi": ("RSI14 is oversold", "RSI14 is overbought"),
    "stoch": ("Stochastic is turning up from oversold", "Stochastic is turning down from overbought"),
    "vwap": ("price is stretched below its VWAP-proxy", "price is stretched above its VWAP-proxy"),
    "exhaustion": ("the bearish streak looks exhausted", "the bullish streak looks exhausted"),
    "htf": ("the 5-minute view agrees bullish", "the 5-minute view agrees bearish"),
    "sr": ("price is bouncing off 20-candle support", "price is rejecting 20-candle resistance"),
}


def _adaptive_factors(x, last):
    """Recent hit-rate of each module on THIS market -> weight multiplier."""
    start = max(20, last - _ADAPT_LOOKBACK)
    factors = {}
    for key, _label, fn, _wt, _wr in MODULES:
        wins = total = 0
        for j in range(start, last):
            try:
                v = fn(x, j)
            except Exception:
                v = 0.0
            if abs(v) < 0.25:
                continue
            total += 1
            up = x.cl[j + 1] > x.cl[j]
            if x.cl[j + 1] != x.cl[j] and (v > 0) == up:
                wins += 1
        if total < _ADAPT_MIN_SAMPLES:
            factors[key] = 1.0
            continue
        hit = wins / total
        factors[key] = _clamp(0.55 + hit, 0.65, 1.55)
    return factors


def otc_sniper(candles, entry_ts=None):
    if not candles or len(candles) < OTC_MIN_CANDLES:
        return None
    x = Ctx(candles)
    last = x.n - 1

    er = _efficiency(x, last)
    trend_w = _clamp((er - 0.22) / 0.28, 0.0, 1.0)   # 0 = ranging, 1 = trending
    factors = _adaptive_factors(x, last)

    contrib, w_sum, score = [], 0.0, 0.0
    for key, label, fn, wt, wr in MODULES:
        try:
            v = fn(x, last)
        except Exception:
            v = 0.0
        w = (trend_w * wt + (1.0 - trend_w) * wr) * factors[key]
        if w <= 0:
            continue
        w_sum += w
        score += w * v
        if abs(v) >= 0.1:
            contrib.append({"key": key, "label": label, "value": v, "weight": w})

    if w_sum <= 0 or not contrib:
        return None
    score /= w_sum
    direction = "CALL" if score >= 0 else "PUT"
    d = 1 if score >= 0 else -1

    spoke = sum(c["weight"] for c in contrib) or 1e-12
    agree = sum(c["weight"] for c in contrib if (c["value"] > 0) == (d > 0)) / spoke

    strength = min(1.0, abs(score) * 2.2)
    confidence = min(95.0, 100.0 * (0.60 * strength + 0.40 * max(0.0, (agree - 0.5) * 2.0)))

    supporting = sorted(
        (c for c in contrib if (c["value"] > 0) == (d > 0)),
        key=lambda c: -abs(c["value"]) * c["weight"],
    )
    n_agree = len(supporting)
    supporting = supporting[:3]
    regime = ("trend-following" if trend_w >= 0.66 else
              "mean-reversion" if trend_w <= 0.33 else "mixed")
    bits = [_PHRASES[c["key"]][0 if d > 0 else 1] for c in supporting]
    reason = (
        f"{regime.capitalize()} regime (efficiency {er:.2f}): " + ", ".join(bits) +
        f" \u2014 {n_agree}/{len(contrib)} active modules agree, so {direction} is preferred."
    )

    return {
        "direction": direction,
        "confidence": confidence,
        "reason": reason,
        "regime": regime,
        "efficiency": er,
        "agree": agree,
        "modules_active": len(contrib),
        "top_modules": [c["label"] for c in supporting],
    }


META = {
    "key": "otc_sniper",
    "name": "OTC Sniper Pro",
    "tagline": "OTC-tuned \u00b7 15 modules \u00b7 regime-adaptive \u00b7 self-weighting",
    "min_confidence": 60.0,
    "min_candles": OTC_MIN_CANDLES,
    "fn": otc_sniper,
    "about": (
        "\U0001f3af OTC Sniper Pro \u2014 built for 1-minute OTC markets\n\n"
        "Works exactly like Classic Momentum: every market is analysed every minute and the "
        "best-scoring one is sent. It just reads far more of the chart before deciding.\n\n"
        "\U0001f9ed Regime engine (this is the core idea)\n"
        "A Kaufman Efficiency Ratio measures whether the market is really travelling or just "
        "churning, then blends the module weights continuously:\n"
        "\u2022 Efficient / trending \u2192 continuation modules lead\n"
        "\u2022 Choppy / exhausted \u2192 mean-reversion modules lead\n"
        "Research on OTC feeds is consistent on this point: fading a running trend and chasing "
        "a chop are the two classic ways to lose, so no single style is hard-coded.\n\n"
        "\U0001f4ca 15 confluence modules\n"
        "Trend side: EMA 5/13/34 stack \u00b7 pullback into EMA13 \u00b7 MACD 6/13/5 thrust \u00b7 "
        "HH/HL structure \u00b7 3-candle push with RSI9 \u00b7 5-minute alignment\n"
        "Reversion side: Bollinger 20/2 extreme \u00b7 RSI14 30/70 \u00b7 Stochastic 9/3 \u00b7 "
        "stretch from VWAP-proxy \u00b7 streak exhaustion \u00b7 S/R 20 rejection\n"
        "Always on: wick rejection \u00b7 candle patterns (engulfing, pin bar, inside-bar break, "
        "star, three soldiers, tweezer)\n\n"
        "\u2699\ufe0f Self-weighting\n"
        "Each module's recent hit-rate on that specific market re-weights it on every scan, so "
        "modules that are currently reading the asset well count more.\n\n"
        "\U0001f4c8 Confidence\n"
        "60% from blended score strength + 40% from how strongly the active modules agree. "
        "Signals fire from 60%.\n\n"
        "\u2139\ufe0f Honest notes: Quotex 1m candles carry no volume, so the VWAP module uses "
        "candle range as a weight proxy. Everything is computed from real candles \u2014 nothing "
        "is faked, and no engine can guarantee a win on 1-minute binaries."
    ),
}
