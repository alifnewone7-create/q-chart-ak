"""Classic Momentum — the original engine, upgraded to a 12-filter confluence."""
from indicators_py import Ctx

from .common import (
    _clamp,
    _m_accel,
    _m_adx_dir,
    _m_band,
    _m_heikin,
    _m_htf,
    _m_macd,
    _streak,
    _wick_pair,
    no_trade,
)

CLASSIC_MIN_CANDLES = 30


def _c_pressure(x, i):
    s = max(0, i - 7)
    bull = sum(b for b in x.body[s:i + 1] if b > 0)
    bear = -sum(b for b in x.body[s:i + 1] if b < 0)
    return _clamp((bull - bear) / (bull + bear + 1e-12))


def _c_sma_momentum(x, i):
    sma5, sma10 = x.sma(5)[i], x.sma(10)[i]
    c = x.cl[i]
    if c > sma5 > sma10:
        return 1.0
    if c < sma5 < sma10:
        return -1.0
    if c > sma5:
        return 0.5
    if c < sma5:
        return -0.5
    return 0.0


def _c_wick(x, i):
    lw = uw = 0.0
    for j in range(max(0, i - 2), i + 1):
        a_, b_ = _wick_pair(x, j)
        lw += a_
        uw += b_
    return _clamp((lw - uw) / 2.0)


def _c_streak(x, i):
    return max(-3, min(3, _streak(x, i))) / 3.0


def _c_ema_trend(x, i):
    a = x.atr(14)[i] or 1e-12
    e9, e21 = x.ema(9)[i], x.ema(21)[i]
    return _clamp((_clamp((e9 - e21) / (0.6 * a)) +
                   _clamp((x.cl[i] - e9) / (0.8 * a)) +
                   _clamp(x.slope(10)[i] / (0.13 * a))) / 3.0)


def _c_rsi7(x, i):
    r = x.rsi(7)[i]
    if 42.0 <= r <= 58.0:
        return 0.0            # dead zone — no edge either way
    if r >= 88.0:
        return -0.35          # overstretched, lean against it
    if r <= 12.0:
        return 0.35
    return _clamp((r - 50.0) / 24.0)


# key, label, fn, weight
FILTERS = [
    ("pressure",  "Buy/sell pressure",        _c_pressure,     0.13),
    ("ema_trend", "EMA 9/21 trend stack",     _c_ema_trend,    0.13),
    ("sma_mom",   "Close vs SMA5/SMA10",      _c_sma_momentum, 0.11),
    ("macd",      "MACD 6/13/5 thrust",       _m_macd,         0.11),
    ("rsi7",      "RSI 7 momentum zone",      _c_rsi7,         0.09),
    ("adx",       "ADX 14 directional gate",  _m_adx_dir,      0.09),
    ("wick",      "Wick rejection",           _c_wick,         0.07),
    ("band",      "Bollinger 20/2 position",  _m_band,         0.07),
    ("heikin",    "Heikin-Ashi consistency",  _m_heikin,       0.06),
    ("streak",    "Candle streak",            _c_streak,       0.05),
    ("accel",     "Momentum acceleration",    _m_accel,        0.05),
    ("htf",       "5-minute alignment",       _m_htf,          0.04),
]

_PHRASES = {
    "pressure": ("Recent candles show stronger buying pressure",
                 "Recent candles show stronger selling pressure"),
    "ema_trend": ("Price is riding above a rising EMA 9/21 stack",
                  "Price is sliding below a falling EMA 9/21 stack"),
    "sma_mom": ("Price is holding above its short-term averages",
                "Price is trading below its short-term averages"),
    "macd": ("MACD momentum is thrusting up",
             "MACD momentum is thrusting down"),
    "rsi7": ("RSI 7 is pushing out of its dead zone to the upside",
             "RSI 7 is pushing out of its dead zone to the downside"),
    "adx": ("ADX confirms a real uptrend in progress",
            "ADX confirms a real downtrend in progress"),
    "wick": ("Long lower wicks show buyers rejecting lower prices",
             "Long upper wicks show sellers rejecting higher prices"),
    "band": ("Price is pinned to the lower Bollinger band",
             "Price is pinned to the upper Bollinger band"),
    "heikin": ("Heikin-Ashi candles are consistently bullish",
               "Heikin-Ashi candles are consistently bearish"),
    "streak": ("A strong bullish streak is controlling the last candles",
               "A strong bearish streak is controlling the last candles"),
    "accel": ("Upward momentum is accelerating candle over candle",
              "Downward momentum is accelerating candle over candle"),
    "htf": ("The 5-minute view agrees bullish",
            "The 5-minute view agrees bearish"),
}


def classic_momentum(candles, entry_ts=None):
    if not candles or len(candles) < CLASSIC_MIN_CANDLES:
        return None
    x = Ctx(candles)
    i = x.n - 1
    if no_trade(x, i)[0]:
        return None

    contrib, score = [], 0.0
    for key, label, fn, w in FILTERS:
        try:
            v = fn(x, i)
        except Exception:
            v = 0.0
        score += w * v
        if abs(v) >= 0.1:
            contrib.append({"key": key, "label": label, "value": v, "weight": w})
    if not contrib:
        return None

    d = 1 if score >= 0 else -1
    direction = "CALL" if d > 0 else "PUT"

    spoke = sum(c["weight"] for c in contrib) or 1e-12
    agree = sum(c["weight"] for c in contrib if (c["value"] > 0) == (d > 0)) / spoke
    strength = min(1.0, abs(score) * 2.4)
    confidence = min(95.0, 100.0 * (0.62 * strength +
                                    0.38 * max(0.0, (agree - 0.5) * 2.0)))

    supporting = sorted((c for c in contrib if (c["value"] > 0) == (d > 0)),
                        key=lambda c: -abs(c["value"]) * c["weight"])
    top = supporting[0] if supporting else contrib[0]
    phrase = _PHRASES[top["key"]][0 if d > 0 else 1]
    reason = (f"{phrase} \u2014 {len(supporting)}/{len(contrib)} active filters "
              f"agree, so {direction} is preferred.")
    return {"direction": direction, "confidence": confidence, "reason": reason,
            "agree": agree, "filters_active": len(contrib),
            "top_filters": [c["label"] for c in supporting[:3]]}


META = {
    "key": "classic",
    "name": "Classic Momentum",
    "tagline": "Original engine upgraded \u2014 12-filter confluence + no-trade gate",
    "min_confidence": 58.0,
    "min_candles": CLASSIC_MIN_CANDLES,
    "fn": classic_momentum,
    "about": (
        "\U0001f4d0 Classic Momentum (upgraded)\n\n"
        "The engine this bot shipped with, rebuilt as a weighted 12-filter confluence. "
        "The original four reads are all still here \u2014 they just vote alongside eight "
        "new ones:\n\n"
        "\u2022 13% Buy/sell pressure (net bodies of the last 8 candles)\n"
        "\u2022 13% EMA 9/21 trend stack + slope\n"
        "\u2022 11% Close vs SMA5 vs SMA10\n"
        "\u2022 11% MACD 6/13/5 thrust\n"
        "\u2022 9% RSI 7 with a 42-58 dead zone (chop never votes)\n"
        "\u2022 9% ADX 14 directional gate (only counts in a real trend)\n"
        "\u2022 7% Wick rejection (last 3 candles)\n"
        "\u2022 7% Bollinger 20/2 band position\n"
        "\u2022 6% Heikin-Ashi consistency\n"
        "\u2022 5% Candle streak \u00b7 5% Momentum acceleration \u00b7 4% 5-minute alignment\n\n"
        "\U0001f6ab No-trade gate\n"
        "News-spike candles, dead chop (ADX + Choppiness Index), an unresolved "
        "volatility squeeze and flat EMAs all block the signal entirely \u2014 the "
        "cheapest accuracy upgrade is not trading the untradeable.\n\n"
        "\U0001f4c8 Confidence = 62% blended score strength + 38% filter agreement. "
        "Signals fire from 58%. Needs 30 closed candles."
    ),
}
