"""Next-candle (1m) prediction. Routes to the strategy chosen in Settings."""
import storage
import strategies


def active_strategy():
    key = storage.get_settings().get("strategy", strategies.DEFAULT_KEY)
    return strategies.get(key)


def analyze(candles, entry_ts=None, strategy=None):
    """Returns dict {direction, confidence, reason, ...} or None if no signal."""
    st = strategy or active_strategy()
    if not candles or len(candles) < st["min_candles"]:
        return None
    res = st["fn"](candles, entry_ts)
    if res:
        res["strategy"] = st["name"]
        res["strategy_key"] = st["key"]
    return res
