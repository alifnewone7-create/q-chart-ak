"""Standalone checks for the Zone Reversal Sniper engine (no aiogram needed)."""
import os, sys, random, math, time
sys.path.insert(0, "/app/backend")
os.environ.setdefault("BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
os.environ.setdefault("ADMIN_ID", "1")
os.environ.setdefault("QUOTEX_EMAIL", "a@b.c")
os.environ.setdefault("QUOTEX_PASSWORD", "p")
import strategies
from strategies import zone_sniper, ZONE_FILTERS, ZONE_MIN_CANDLES
from indicators_py import Ctx


def series(closes, t0=None):
    t0 = t0 or int(time.time()) - len(closes) * 60
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        h = max(o, c) + abs(c - o) * 0.4 + 1e-6
        l = min(o, c) - abs(c - o) * 0.4 - 1e-6
        out.append({"time": t0 + i * 60, "open": o, "high": h, "low": l,
                    "close": c, "volume": 1000 + i})
        prev = c
    return out


def walk(n=90, seed=1, base=1.1000, vol=0.0004):
    r = random.Random(seed)
    cl, p = [], base
    for _ in range(n):
        p += r.gauss(0, vol)
        cl.append(p)
    return series(cl)


def bouncy(n=120, base=1.1000, amp=0.0030, seed=3):
    """Range that repeatedly rejects the same top and bottom."""
    r = random.Random(seed)
    cl = []
    for i in range(n):
        cl.append(base + amp * math.sin(i / 7.0) + r.gauss(0, amp * 0.06))
    return series(cl)


def realistic(n=140, seed=1, base=1.10, amp=0.0030):
    """Oscillating range with realistic wicks (what a real OTC feed looks like)."""
    r = random.Random(seed)
    t0 = int(time.time()) - n * 60
    out, p = [], base
    for i in range(n):
        target = base + amp * math.sin(i / 7.0)
        c = p + (target - p) * 0.45 + r.gauss(0, amp * 0.10)
        o = p
        w = abs(r.gauss(0, amp * 0.12))
        h = max(o, c) + abs(r.gauss(0, amp * 0.10)) + w * 0.5
        l = min(o, c) - abs(r.gauss(0, amp * 0.10)) - w * 0.5
        out.append({"time": t0 + i * 60, "open": o, "high": h, "low": l,
                    "close": c, "volume": 1000})
        p = c
    return out


def trending(n=100, base=1.1000, step=0.00025, seed=5):
    r = random.Random(seed)
    cl, p = [], base
    for _ in range(n):
        p += step + r.gauss(0, step * 1.6)
        cl.append(p)
    return series(cl)


# 1) registry
st = strategies.STRATEGIES["zone_sniper"]
assert st["name"] == "Zone Reversal Sniper"
assert st["min_candles"] == ZONE_MIN_CANDLES == 60
assert st["min_confidence"] == 68.0
assert strategies.ORDER == ["classic", "otc_sniper", "zone_sniper"]
for needle in ("Multi-rejection", "Trendline", "S/R", "divergence", "sweep",
               "Confidence", "15", "Confirmed rejection reversal"):
    assert needle in st["about"], needle
print("OK registry + about text")

# 2) silent only when too few candles
assert zone_sniper([]) is None
assert zone_sniper(walk(59)) is None
assert zone_sniper(walk(60)) is not None
print("OK min-candle gate")

# 3) never silent, sane output, across many random markets
seen = {"CALL": 0, "PUT": 0}
modes = set()
for seed in range(120):
    for gen in (walk, bouncy, trending):
        c = gen(seed=seed) if gen is not walk else walk(seed=seed)
        r = zone_sniper(c)
        assert r is not None, f"silent on {gen.__name__} seed {seed}"
        assert r["direction"] in ("CALL", "PUT")
        assert 0.0 <= r["confidence"] <= 95.0
        assert isinstance(r["reason"], str) and len(r["reason"]) > 40
        assert 0.0 <= r["at_zone"] <= 1.0
        assert 0.0 <= r["agree"] <= 1.0
        assert r["filters_active"] >= 1
        assert r["level_touches"] >= 0 and r["level_rejections"] >= 0
        seen[r["direction"]] += 1
        modes.add(r["mode"])
assert seen["CALL"] > 10 and seen["PUT"] > 10, seen
assert modes == {"zone-reversal", "trend-continuation"}, modes
print(f"OK 360 markets  {seen}  modes={sorted(modes)}")

# 4) every filter is finite and inside [-1, 1] on every market
for seed in range(40):
    for gen in (walk, bouncy, trending, realistic):
        c = gen(seed=seed)
        x = Ctx(c); i = x.n - 1
        z = strategies._z_context(x, i)
        for key, label, fn, wz, wo in ZONE_FILTERS:
            v = fn(x, i, z)
            assert isinstance(v, float) and -1.0 <= v <= 1.0 and v == v, (key, v)
print(f"OK all {len(ZONE_FILTERS)} filters bounded")

# 5) multi-rejection filter actually detects a repeatedly rejected level
c = bouncy(160, seed=11)
x = Ctx(c); i = x.n - 1
z = strategies._z_context(x, i)
lvls = [l for l in z["levels"] if l["rejections"] >= 2]
assert lvls, "no multi-rejection level found in an oscillating range"
top = max(lvls, key=lambda l: l["rejections"])
print(f"OK multi-rejection levels: {len(lvls)} "
      f"(best {top['price']:.5f}: {top['touches']} touches / {top['rejections']} rejections)")

# 6) support bounce leans CALL, resistance rejection leans PUT
sup_calls = res_puts = sup_n = res_n = 0
for seed in range(60):
    c = bouncy(140, seed=seed)
    x = Ctx(c); i = x.n - 1
    z = strategies._z_context(x, i)
    lv, d = strategies._z_best_rejection_level(x, i, z)
    if lv is None:
        continue
    v = strategies._f_multi_rejection(x, i, z)
    if v == 0:
        continue
    if lv["price"] <= x.cl[i]:
        sup_n += 1; sup_calls += (v > 0)
    else:
        res_n += 1; res_puts += (v < 0)
assert sup_n + res_n > 5, (sup_n, res_n)
assert sup_calls == sup_n and res_puts == res_n, (sup_calls, sup_n, res_puts, res_n)
print(f"OK zone polarity  support->CALL {sup_calls}/{sup_n}  resistance->PUT {res_puts}/{res_n}")

# 6b) confirmed rejection reversal: fires, and only with a confirmation candle
rev_hits = 0
for seed in range(200):
    c = realistic(seed=seed)
    r = zone_sniper(c)
    if r["mode"] == "rejection-reversal":
        rev_hits += 1
        assert r["reversal_confirmed"] is True
        assert r["confidence"] >= 70.0, r["confidence"]
        assert "Confirmed rejection reversal" in r["reason"]
assert rev_hits > 20, f"confirmed rejection reversal too rare: {rev_hits}/200"
print(f"OK confirmed rejection reversal fired on {rev_hits}/200 oscillating markets")

# 6c) a rejection with NO confirming candle must not trade the reversal
checked = 0
for seed in range(200):
    c = realistic(seed=seed)
    x = Ctx(c); i = x.n - 1
    z = strategies._z_context(x, i)
    v, lv = strategies._z_rev_confirm(x, i, z)
    if v == 0:
        continue
    checked += 1
    # the confirming candle must close in the reversal direction
    assert (x.body[i] > 0) == (v > 0), (v, x.body[i])
assert checked > 20, checked
print(f"OK reversal always has a confirming candle ({checked} cases)")

# 7) trendline filter fires on a clean rising channel low touch
c = trending(120, seed=9)
x = Ctx(c); i = x.n - 1
z = strategies._z_context(x, i)
assert z["tl_sup"] is not None or z["tl_res"] is not None, "no trendline fitted"
print("OK trendline fitted")

# 8) routes through analysis.analyze
import storage, analysis
prev = storage.get_settings()
try:
    storage.save_settings({**prev, "strategy": "zone_sniper"})
    r = analysis.analyze(bouncy(140, seed=2))
    assert r and r["strategy"] == "Zone Reversal Sniper" and r["strategy_key"] == "zone_sniper"
    assert analysis.analyze(walk(40)) is None  # below min_candles
    print("OK analysis routing:", r["direction"], f"{r['confidence']:.1f}%")
    print("   reason:", r["reason"][:150])
finally:
    storage.save_settings(prev)

# 9) other engines untouched
assert strategies.classic_momentum(walk(60)) is not None
assert strategies.otc_sniper(walk(60)) is not None
print("OK classic + otc_sniper still work")
print("\nALL ZONE SNIPER CHECKS PASSED")
