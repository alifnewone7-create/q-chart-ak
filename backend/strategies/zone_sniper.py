"""Zone Reversal Sniper — S/R, trendline and multi-rejection level engine."""
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

ZONE_MIN_CANDLES = 60
_Z_PIV = 2
_Z_TL_PIVOTS = 2


def _z_pivots(x, i, left=_Z_PIV, right=_Z_PIV):
    """Swing highs / lows confirmed by `left` and `right` neighbours."""
    hs, ls = [], []
    for j in range(left, i - right + 1):
        lo_w = min(x.l[j - left:j + right + 1])
        hi_w = max(x.h[j - left:j + right + 1])
        if x.h[j] >= hi_w:
            hs.append((j, x.h[j]))
        if x.l[j] <= lo_w:
            ls.append((j, x.l[j]))
    return hs, ls


def _z_touch_stats(x, price, tol, i, a):
    """How often price visited a level and how often it was rejected there."""
    touches = rejections = 0
    last = -1
    cool = -99
    for j in range(i + 1):
        if not (x.l[j] - tol <= price <= x.h[j] + tol):
            continue
        if j - cool < 3:          # same visit, not a new touch
            continue
        cool = j
        touches += 1
        last = j
        away = x.cl[j] - price
        if abs(away) < 0.15 * a:
            continue
        k = min(i, j + 2)
        moved = (x.cl[k] - price) if away > 0 else (price - x.cl[k])
        if moved > abs(away) * 0.8:
            rejections += 1
    return touches, rejections, last


def _z_levels(x, i):
    """Cluster swing pivots into levels and score each one."""
    a = x.atr(14)[i] or 1e-12
    tol = 0.45 * a
    hs, ls = _z_pivots(x, i)
    pts = sorted([p for _j, p in hs] + [p for _j, p in ls])
    if not pts:
        return [], tol, a

    clusters = [[pts[0]]]
    for p in pts[1:]:
        if abs(p - clusters[-1][-1]) <= tol:
            clusters[-1].append(p)
        else:
            clusters.append([p])

    levels = []
    for cl in clusters:
        price = sum(cl) / len(cl)
        touches, rejections, last = _z_touch_stats(x, price, tol, i, a)
        if touches == 0:
            continue
        levels.append({
            "price": price,
            "pivots": len(cl),
            "touches": touches,
            "rejections": rejections,
            "last": last,
            "side": "R" if price >= x.cl[i] else "S",
        })
    return levels, tol, a


def _z_fit(points):
    """Least-squares line through (index, price) points -> (slope, intercept)."""
    m = len(points)
    if m < 2:
        return None
    mx = sum(p[0] for p in points) / m
    my = sum(p[1] for p in points) / m
    num = sum((p[0] - mx) * (p[1] - my) for p in points)
    den = sum((p[0] - mx) ** 2 for p in points) or 1e-12
    slope = num / den
    return slope, my - slope * mx


def _z_context(x, i):
    """Everything the filters share: levels, nearest level, trendlines."""
    levels, tol, a = _z_levels(x, i)
    close = x.cl[i]

    near = None
    dist_atr = 9.9
    for lv in levels:
        d = abs(close - lv["price"]) / a
        # a multi-rejection level wins ties against a plain one
        rank = d - 0.10 * min(3, lv["rejections"])
        if near is None or rank < dist_atr:
            near, dist_atr = lv, rank
    near_dist = abs(close - near["price"]) / a if near else 9.9

    hs, ls = _z_pivots(x, i)
    tl_sup = _z_fit(ls[-4:]) if len(ls) >= _Z_TL_PIVOTS else None
    tl_res = _z_fit(hs[-4:]) if len(hs) >= _Z_TL_PIVOTS else None

    at_zone = _clamp(1.0 - max(0.0, near_dist - 0.20) / 0.90, 0.0, 1.0)
    return {
        "a": a, "tol": tol, "levels": levels, "near": near,
        "near_dist": near_dist, "at_zone": at_zone,
        "tl_sup": tl_sup, "tl_res": tl_res, "hs": hs, "ls": ls,
    }


def _f_sr_zone(x, i, z):
    """Price parked on a horizontal support/resistance level."""
    lv = z["near"]
    if not lv or z["near_dist"] > 1.10:
        return 0.0
    sign = 1.0 if lv["price"] <= x.cl[i] else -1.0     # level below = support
    q = min(1.0, 0.34 + 0.16 * lv["touches"])
    prox = _clamp(1.0 - z["near_dist"] / 1.10, 0.0, 1.0)
    return _clamp(sign * q * (0.45 + 0.55 * prox))


def _z_best_rejection_level(x, i, z):
    """Closest level that has already rejected price at least once."""
    best = None
    for lv in z["levels"]:
        if lv["rejections"] < 1:
            continue
        d = abs(x.cl[i] - lv["price"]) / z["a"]
        if d > 1.30:
            continue
        rank = lv["rejections"] - d
        if best is None or rank > best[0]:
            best = (rank, lv, d)
    return (best[1], best[2]) if best else (None, None)


def _f_multi_rejection(x, i, z):
    """THE key filter: levels that already reversed price several times."""
    lv, d = _z_best_rejection_level(x, i, z)
    if lv is None:
        return 0.0
    sign = 1.0 if lv["price"] <= x.cl[i] else -1.0
    mag = min(1.0, 0.42 + 0.26 * (lv["rejections"] - 1))
    prox = _clamp(1.0 - d / 1.30, 0.0, 1.0)
    fresh = 1.0 if (i - lv["last"]) <= 30 else 0.85
    return _clamp(sign * mag * (0.4 + 0.6 * prox) * fresh)


def _z_rev_confirm(x, i, z):
    """(value, level) for a CONFIRMED rejection reversal off a real level.

    Candle j (i-1 or i-2) must hit a level with a genuine wick and close away
    from it, and candle i must then confirm by closing further in the reversal
    direction. No confirmation candle -> no trade.
    """
    a = z["a"]
    best = (0.0, None)
    for lv in z["levels"]:
        if lv["touches"] < 2 and lv["rejections"] < 1:
            continue
        p = lv["price"]
        for j in range(max(1, i - 2), i):
            hit_up = x.h[j] >= p - z["tol"] and x.cl[j] < p - 0.20 * a
            hit_dn = x.l[j] <= p + z["tol"] and x.cl[j] > p + 0.20 * a
            if not (hit_up or hit_dn):
                continue
            lw, uw = _wick_pair(x, j)
            wick = uw if hit_up else lw
            if wick < 0.30:
                continue
            if hit_up:
                confirmed = x.body[i] < 0 and x.cl[i] < x.cl[j] - 0.10 * a
                sign = -1.0
            else:
                confirmed = x.body[i] > 0 and x.cl[i] > x.cl[j] + 0.10 * a
                sign = 1.0
            if not confirmed:
                continue
            mag = min(1.0, 0.55 + 0.12 * min(3, lv["rejections"])
                      + 0.35 * (wick - 0.30))
            recency = 1.0 if (i - j) == 1 else 0.85
            v = _clamp(sign * mag * recency)
            if abs(v) > abs(best[0]):
                best = (v, lv)
    return best


def _f_rev_confirm(x, i, z):
    """Confirmed rejection reversal — the highest-conviction level setup."""
    return _z_rev_confirm(x, i, z)[0]


def _f_trendline(x, i, z):
    """Rising trendline support / falling trendline resistance touch."""
    a = z["a"]
    out = 0.0
    if z["tl_sup"]:
        slope, b = z["tl_sup"]
        val = slope * i + b
        d = (x.cl[i] - val) / a
        if slope > 0 and -0.35 <= d <= 0.85:
            out += _clamp(0.55 + 0.45 * (1.0 - min(1.0, abs(d) / 0.85)))
    if z["tl_res"]:
        slope, b = z["tl_res"]
        val = slope * i + b
        d = (val - x.cl[i]) / a
        if slope < 0 and -0.35 <= d <= 0.85:
            out -= _clamp(0.55 + 0.45 * (1.0 - min(1.0, abs(d) / 0.85)))
    return _clamp(out)


def _f_sweep(x, i, z):
    """False break / liquidity sweep: wick through a level, close back inside."""
    a = z["a"]
    lv = z["near"]
    cands = [lv["price"]] if lv else []
    cands += [x.highest(20)[i - 1] if i >= 1 else x.h[i],
              x.lowest(20)[i - 1] if i >= 1 else x.l[i]]
    score = 0.0
    for p in cands:
        if x.h[i] - p > 0.10 * a and p - x.cl[i] > 0.15 * a:
            score -= 0.9
        if p - x.l[i] > 0.10 * a and x.cl[i] - p > 0.15 * a:
            score += 0.9
    return _clamp(score / 1.4)


def _f_zone_rejection_candle(x, i, z):
    """Pin bar / engulfing printed while sitting on the level."""
    if z["at_zone"] < 0.25:
        return 0.0
    lw, uw = _wick_pair(x, i)
    score = 0.0
    if lw > 0.45 and uw < 0.30:
        score += 0.8
    elif uw > 0.45 and lw < 0.30:
        score -= 0.8
    b = x.body
    if i >= 1:
        if b[i] > 0 and b[i - 1] < 0 and x.cl[i] > x.o[i - 1]:
            score += 0.6
        elif b[i] < 0 and b[i - 1] > 0 and x.cl[i] < x.o[i - 1]:
            score -= 0.6
    return _clamp(score * (0.5 + 0.5 * z["at_zone"]))


def _f_divergence(x, i, z):
    """RSI divergence against the last two swing extremes."""
    r = x.rsi(14)
    hs, ls = z["hs"], z["ls"]
    out = 0.0
    if len(ls) >= 2:
        (j1, p1), (j2, p2) = ls[-2], ls[-1]
        if p2 < p1 and r[j2] > r[j1] + 1.5:
            out += 0.9
    if len(hs) >= 2:
        (j1, p1), (j2, p2) = hs[-2], hs[-1]
        if p2 > p1 and r[j2] < r[j1] - 1.5:
            out -= 0.9
    return _clamp(out)


def _f_round_level(x, i, z):
    """Psychological round numbers — OTC feeds respect them."""
    import math as _math
    a = z["a"]
    target = 3.0 * a
    mag = 10 ** _math.floor(_math.log10(max(target, 1e-12)))
    step = min((mag, 2 * mag, 5 * mag, 10 * mag), key=lambda m: abs(m - target))
    lvl = round(x.cl[i] / step) * step
    d = abs(x.cl[i] - lvl) / a
    if d > 0.45:
        return 0.0
    sign = 1.0 if lvl <= x.cl[i] else -1.0
    return _clamp(sign * 0.45 * (1.0 - d / 0.45))


def _f_band_extreme(x, i, z):
    """Bollinger 20/2 extreme confirmed by an RSI14 extreme."""
    return _clamp(0.55 * _m_band(x, i) + 0.45 * _m_rsi_extreme(x, i))


def _f_trend(x, i, z):
    """EMA 8/21/50 stack + slope."""
    a = z["a"]
    e8, e21, e50 = x.ema(8)[i], x.ema(21)[i], x.ema(50)[i]
    return _clamp((_clamp((e8 - e21) / (0.6 * a)) +
                   _clamp((e21 - e50) / (1.2 * a)) +
                   _clamp(x.slope(12)[i] / (0.12 * a))) / 3.0)


def _f_break_retest(x, i, z):
    """Level broken in the last 12 candles and now retested from the far side."""
    a = z["a"]
    best = 0.0
    for lv in z["levels"]:
        p = lv["price"]
        for j in range(max(1, i - 12), i + 1):
            broke_up = x.cl[j] - p > 0.35 * a and x.cl[j - 1] <= p
            broke_dn = p - x.cl[j] > 0.35 * a and x.cl[j - 1] >= p
            if not (broke_up or broke_dn):
                continue
            d = abs(x.cl[i] - p) / a
            if d > 1.0:
                continue
            holding = (x.cl[i] > p) if broke_up else (x.cl[i] < p)
            if not holding:
                continue
            mag = (0.55 + 0.15 * min(3, lv["touches"])) * (1.0 - d / 1.0)
            v = mag if broke_up else -mag
            if abs(v) > abs(best):
                best = v
    return _clamp(best)


def _f_htf_zone(x, i, z):
    """5-minute trend rebuilt from the same candles (higher-timeframe sanity)."""
    return _m_htf(x, i)


def _f_momentum(x, i, z):
    return _clamp(0.55 * _m_momentum(x, i) + 0.45 * _m_macd(x, i))


def _f_close_pressure(x, i, z):
    """Where the last two candles closed inside their own range."""
    tot = 0.0
    for j in range(max(0, i - 1), i + 1):
        tot += ((x.cl[j] - x.l[j]) / x.rng[j] - 0.5) * 2.0
    body = _clamp(x.body[i] / (0.9 * z["a"]))
    return _clamp(0.6 * _clamp(tot / 2.0) + 0.4 * body)


def _f_exhaustion(x, i, z):
    return _m_exhaustion(x, i)


# key, label, fn, weight AT a level, weight in OPEN space
ZONE_FILTERS = [
    ("sr_zone",    "Horizontal S/R zone",        _f_sr_zone,               0.14, 0.03),
    ("multi_rej",  "Multi-rejection level",      _f_multi_rejection,       0.17, 0.04),
    ("rev_confirm", "Confirmed rejection reversal", _f_rev_confirm,        0.22, 0.05),
    ("trendline",  "Trendline S/R touch",        _f_trendline,             0.10, 0.04),
    ("sweep",      "False break / sweep",        _f_sweep,                 0.08, 0.03),
    ("zone_candle", "Rejection candle at zone",  _f_zone_rejection_candle, 0.07, 0.03),
    ("divergence", "RSI divergence at swing",    _f_divergence,            0.06, 0.02),
    ("round",      "Round-number level",         _f_round_level,           0.03, 0.01),
    ("band_ext",   "Bollinger + RSI extreme",    _f_band_extreme,          0.05, 0.03),
    ("trend",      "EMA 8/21/50 stack",          _f_trend,                 0.03, 0.19),
    ("break_rt",   "Break and retest hold",      _f_break_retest,          0.02, 0.13),
    ("htf",        "5-minute alignment",         _f_htf_zone,              0.01, 0.11),
    ("momentum",   "3-candle push + MACD",       _f_momentum,              0.01, 0.15),
    ("pressure",   "Close-position pressure",    _f_close_pressure,        0.01, 0.09),
    ("exhaust",    "Streak exhaustion",          _f_exhaustion,            0.00, 0.05),
]


_ZONE_PHRASES = {
    "sr_zone": ("price is holding a horizontal support zone",
                "price is capped by a horizontal resistance zone"),
    "multi_rej": ("this level already rejected price back up several times",
                  "this level already rejected price back down several times"),
    "rev_confirm": ("a rejection off the level was confirmed by the next candle closing up",
                    "a rejection off the level was confirmed by the next candle closing down"),
    "trendline": ("price is sitting on a rising trendline support",
                  "price is pressing into a falling trendline resistance"),
    "sweep": ("a downside sweep was bought back inside the range",
              "an upside sweep was sold back inside the range"),
    "zone_candle": ("a bullish rejection candle printed at the level",
                    "a bearish rejection candle printed at the level"),
    "divergence": ("a lower low printed with higher RSI (bullish divergence)",
                   "a higher high printed with lower RSI (bearish divergence)"),
    "round": ("a round-number level is acting as support",
              "a round-number level is acting as resistance"),
    "band_ext": ("Bollinger and RSI are both oversold",
                 "Bollinger and RSI are both overbought"),
    "trend": ("EMA 8/21/50 are stacked upward",
              "EMA 8/21/50 are stacked downward"),
    "break_rt": ("a broken level is holding as new support",
                 "a broken level is holding as new resistance"),
    "htf": ("the 5-minute view agrees bullish",
            "the 5-minute view agrees bearish"),
    "momentum": ("the 3-candle push and MACD point up",
                 "the 3-candle push and MACD point down"),
    "pressure": ("the last candles closed near their highs",
                 "the last candles closed near their lows"),
    "exhaust": ("the bearish streak looks exhausted",
                "the bullish streak looks exhausted"),
}


def _z_fmt(price):
    return f"{price:.5f}" if abs(price) < 20 else f"{price:.3f}"


def zone_levels(candles, min_touches=3, min_rejections=2, limit=3,
                max_dist_atr=6.0):
    """Public: only levels that are REAL support/resistance on this market.

    Used by the chart renderer — if a market has no level that has been touched
    and rejected repeatedly, this returns an empty list and nothing is drawn.
    """
    if not candles or len(candles) < 30:
        return []
    x = Ctx(candles)
    i = x.n - 1
    levels, _tol, a = _z_levels(x, i)
    close = x.cl[i]
    strong = [lv for lv in levels
              if lv["touches"] >= min_touches and lv["rejections"] >= min_rejections
              and abs(close - lv["price"]) / a <= max_dist_atr]
    out = []
    for lv in strong:
        out.append({
            "price": lv["price"],
            "touches": lv["touches"],
            "rejections": lv["rejections"],
            "kind": "R" if lv["price"] >= close else "S",
            "dist_atr": abs(close - lv["price"]) / a,
        })
    out.sort(key=lambda l: -(2 * l["rejections"] + l["touches"]))
    return out[:limit]


def zone_sniper(candles, entry_ts=None):
    if not candles or len(candles) < ZONE_MIN_CANDLES:
        return None
    x = Ctx(candles)
    i = x.n - 1
    z = _z_context(x, i)
    at_zone = z["at_zone"]

    contrib, w_sum, score = [], 0.0, 0.0
    for key, label, fn, wz, wo in ZONE_FILTERS:
        try:
            v = fn(x, i, z)
        except Exception:
            v = 0.0
        w = at_zone * wz + (1.0 - at_zone) * wo
        if w <= 0:
            continue
        w_sum += w
        score += w * v
        if abs(v) >= 0.1:
            contrib.append({"key": key, "label": label, "value": v, "weight": w})

    if w_sum <= 0 or not contrib:
        return None
    score /= w_sum
    d = 1 if score >= 0 else -1

    # A confirmed rejection reversal off a real level outranks the blended vote.
    rev_v, rev_lv = _z_rev_confirm(x, i, z)
    override = abs(rev_v) >= 0.60
    if override:
        d = 1 if rev_v > 0 else -1
    direction = "CALL" if d > 0 else "PUT"

    spoke = sum(c["weight"] for c in contrib) or 1e-12
    agree = sum(c["weight"] for c in contrib if (c["value"] > 0) == (d > 0)) / spoke

    lv = rev_lv if override else z["near"]
    if override:
        quality = min(1.0, (lv["touches"] + 1.5 * lv["rejections"]) / 6.0)
        mode = "rejection-reversal"
    elif at_zone >= 0.35 and lv:
        quality = min(1.0, (lv["touches"] + 1.5 * lv["rejections"]) / 6.0)
        mode = "zone-reversal"
    else:
        quality = _clamp(_efficiency(x, i) / 0.45, 0.0, 1.0)
        mode = "trend-continuation"

    strength = min(1.0, abs(score) * 2.3)
    if override:
        strength = max(strength, abs(rev_v))
    confidence = min(95.0, 100.0 * (0.48 * strength +
                                    0.32 * max(0.0, (agree - 0.5) * 2.0) +
                                    0.20 * quality))
    if override:
        confidence = min(95.0, max(confidence, 70.0 + 20.0 * (abs(rev_v) - 0.6) / 0.4))

    supporting = sorted(
        (c for c in contrib if (c["value"] > 0) == (d > 0)),
        key=lambda c: -abs(c["value"]) * c["weight"],
    )
    n_agree = len(supporting)
    top = supporting[:3]
    bits = [_ZONE_PHRASES[c["key"]][0 if d > 0 else 1] for c in top]

    if lv:
        lvl_txt = (f"nearest level {_z_fmt(lv['price'])} "
                   f"({lv['touches']} touches, {lv['rejections']} rejections, "
                   f"{abs(x.cl[i] - lv['price']) / z['a']:.2f} ATR away)")
    else:
        lvl_txt = "no clean level in range"
    if override:
        head = (f"Confirmed rejection reversal at {_z_fmt(lv['price'])} "
                f"({lv['touches']} touches, {lv['rejections']} prior rejections)")
    else:
        head = f"{mode.replace('-', ' ').capitalize()} setup \u2014 {lvl_txt}"
    reason = (
        head + ": " + ", ".join(bits) +
        f" \u2014 {n_agree}/{len(contrib)} active filters agree, so {direction} "
        f"is preferred for the next candle."
    )

    return {
        "direction": direction,
        "confidence": confidence,
        "reason": reason,
        "mode": mode,
        "at_zone": at_zone,
        "agree": agree,
        "zone_quality": quality,
        "reversal_confirmed": bool(override),
        "level": (_z_fmt(lv["price"]) if lv else None),
        "level_touches": (lv["touches"] if lv else 0),
        "level_rejections": (lv["rejections"] if lv else 0),
        "levels_found": len(z["levels"]),
        "filters_active": len(contrib),
        "top_filters": [c["label"] for c in top],
    }


META = {
    "key": "zone_sniper",
    "name": "Zone Reversal Sniper",
    "tagline": "S/R + trendline + multi-rejection levels \u00b7 15 filters",
    "min_confidence": 68.0,
    "min_candles": ZONE_MIN_CANDLES,
    "fn": zone_sniper,
    "about": (
        "\U0001f3f0 Zone Reversal Sniper \u2014 level-based next-candle engine\n\n"
        "This engine does not chase momentum. It first maps where price has actually "
        "reacted before, then asks 15 filters which way the NEXT 1-minute candle should "
        "go from that spot.\n\n"
        "\U0001f9f1 Level engine\n"
        "\u2022 Swing pivots (2 left / 2 right) are clustered into price zones with an "
        "ATR-scaled tolerance, so nearby pivots become one level instead of five.\n"
        "\u2022 Every level is scored twice: how many times price TOUCHED it, and how many "
        "of those touches actually REJECTED \u2014 wick into the level, close away from it, "
        "and follow-through on the next candles.\n"
        "\u2022 Multiple-rejection levels (the same level reversing price 2, 3, 4 times in "
        "the past) carry the single heaviest weight in the whole engine, because that is "
        "the most repeatable behaviour on 1-minute OTC feeds.\n"
        "\u2022 Trendline support and resistance are least-squares fitted through the recent "
        "swing lows and swing highs \u2014 the diagonal version of the same idea.\n\n"
        "\u2696\ufe0f Context blending\n"
        "Distance to the nearest level decides which side of the engine leads:\n"
        "\u2022 Price ON a level \u2192 reversal filters lead (zone, multi-rejection, trendline, "
        "sweep, rejection candle, divergence, round number, Bollinger+RSI extreme)\n"
        "\u2022 Price in open space \u2192 continuation filters lead (EMA 8/21/50 stack, "
        "break-and-retest hold, 5-minute alignment, 3-candle push + MACD, close-position "
        "pressure, streak exhaustion)\n"
        "Fading a level that is not there, and buying a breakout that is really a level "
        "test, are the two classic 1-minute mistakes \u2014 so neither style is hard-coded.\n\n"
        "\u26a1 Confirmed rejection reversal (highest conviction)\n"
        "If a candle hits a real level with a genuine wick, closes away from it, and the "
        "NEXT candle confirms by closing further in the reversal direction, that setup "
        "overrides the blended vote and the trade is taken against the level. No "
        "confirmation candle means no reversal trade \u2014 a single touch is never enough.\n\n"
        "\U0001f52c Filters in full (15)\n"
        "Horizontal S/R zone \u00b7 Multi-rejection level \u00b7 Confirmed rejection reversal \u00b7 "
        "Trendline S/R touch \u00b7 "
        "False break / sweep \u00b7 Rejection candle at zone \u00b7 RSI divergence at "
        "swing \u00b7 Round-number level \u00b7 Bollinger + RSI extreme \u00b7 EMA 8/21/50 stack \u00b7 "
        "Break and retest hold \u00b7 5-minute alignment \u00b7 3-candle push + MACD \u00b7 "
        "Close-position pressure \u00b7 Streak exhaustion\n\n"
        "\U0001f4c8 Confidence\n"
        "48% score strength + 32% filter agreement + 20% setup quality (touch and "
        "rejection count of the level in play, or trend efficiency in open space). "
        "Signals fire from 68%, and every signal reports the exact level, its touch and "
        "rejection count, and how far price is from it in ATR.\n\n"
        "\u2139\ufe0f Honest notes: needs 60 closed candles before it will speak, levels are "
        "built only from real candle history, and no engine can guarantee a win on "
        "1-minute binaries."
    ),
}
