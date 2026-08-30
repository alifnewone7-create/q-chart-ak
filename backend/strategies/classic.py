"""Classic Momentum — the original engine this bot shipped with."""


def _sma_last(values, n):
    return sum(values[-n:]) / n


def classic_momentum(candles, entry_ts=None):
    if not candles or len(candles) < 15:
        return None
    c = candles[-15:]
    bodies = [v["close"] - v["open"] for v in c]

    last8 = bodies[-8:]
    bull = sum(b for b in last8 if b > 0)
    bear = -sum(b for b in last8 if b < 0)
    pressure = (bull - bear) / (bull + bear + 1e-12)

    closes = [v["close"] for v in c]
    sma5 = _sma_last(closes, 5)
    sma10 = _sma_last(closes, 10)
    if closes[-1] > sma5 > sma10:
        momentum = 1.0
    elif closes[-1] < sma5 < sma10:
        momentum = -1.0
    elif closes[-1] > sma5:
        momentum = 0.5
    elif closes[-1] < sma5:
        momentum = -0.5
    else:
        momentum = 0.0

    lower_w = upper_w = 0.0
    for v in c[-3:]:
        rng = (v["high"] - v["low"]) or 1e-12
        body_top = max(v["open"], v["close"])
        body_bot = min(v["open"], v["close"])
        lower_w += (body_bot - v["low"]) / rng
        upper_w += (v["high"] - body_top) / rng
    wick = (lower_w - upper_w) / 3.0

    streak = 0
    for b in reversed(bodies):
        if b == 0:
            break
        sign = 1 if b > 0 else -1
        if streak == 0:
            streak = sign
        elif (streak > 0) == (sign > 0):
            streak += sign
        else:
            break
    streak_score = max(-3, min(3, streak)) / 3.0

    score = 0.45 * pressure + 0.30 * momentum + 0.15 * wick + 0.10 * streak_score
    direction = "CALL" if score >= 0 else "PUT"
    confidence = min(95.0, abs(score) * 160.0)

    parts = {
        "pressure": abs(0.45 * pressure),
        "momentum": abs(0.30 * momentum),
        "wick": abs(0.15 * wick),
        "streak": abs(0.10 * streak_score),
    }
    dominant = max(parts, key=parts.get)
    up = direction == "CALL"
    reasons = {
        "pressure": (
            "Recent candles show stronger bullish pressure than selling pressure, so CALL is preferred."
            if up else
            "Recent candles show stronger selling pressure than buying pressure, so PUT is preferred."
        ),
        "momentum": (
            "Price is holding above its short-term averages with steady upward momentum, so CALL is preferred."
            if up else
            "Price is trading below its short-term averages with steady downward momentum, so PUT is preferred."
        ),
        "wick": (
            "Long lower wicks show buyers rejecting lower prices, so CALL is preferred."
            if up else
            "Long upper wicks show sellers rejecting higher prices, so PUT is preferred."
        ),
        "streak": (
            "A strong bullish streak is controlling the last candles, so CALL is preferred."
            if up else
            "A strong bearish streak is controlling the last candles, so PUT is preferred."
        ),
    }
    return {"direction": direction, "confidence": confidence, "reason": reasons[dominant]}


META = {
    "key": "classic",
    "name": "Classic Momentum",
    "tagline": "Original engine \u2014 pressure + momentum + wick + streak",
    "min_confidence": 55.0,
    "min_candles": 15,
    "fn": classic_momentum,
    "about": (
        "\U0001f4d0 Classic Momentum\n\n"
        "The engine this bot shipped with. It reads the last 15 closed 1m candles and blends "
        "four weighted components:\n\n"
        "\u2022 45% Buying/selling pressure (net body size of the last 8 candles)\n"
        "\u2022 30% Short-term momentum (close vs SMA5 vs SMA10)\n"
        "\u2022 15% Wick rejection (last 3 candles)\n"
        "\u2022 10% Candle streak\n\n"
        "Signals fire when the blended score reaches 55% confidence.\n"
        "Fast and always talkative \u2014 it will find something on almost every market, "
        "which also means weaker setups get through."
    ),
}
