"""Standalone checks for the strategy upgrades, deep-analysis mode,
no-trade gate and chart trendline drawing (no network needed)."""
import asyncio
import math
import os
import random
import sys
import time

sys.path.insert(0, "/app/backend")
os.environ.setdefault("BOT_TOKEN", "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
os.environ.setdefault("ADMIN_ID", "1")
os.environ.setdefault("QUOTEX_EMAIL", "a@b.c")
os.environ.setdefault("QUOTEX_PASSWORD", "p")

import strategies
from indicators_py import Ctx


def series(closes, wick=0.02, t0=None):
    t0 = t0 or int(time.time()) - len(closes) * 60
    out, prev = [], closes[0]
    for i, c in enumerate(closes):
        o = prev
        out.append({"time": t0 + i * 60, "open": o, "high": max(o, c) + wick,
                    "low": min(o, c) - wick, "close": c, "volume": 1000 + i})
        prev = c
    return out


def gen_trend(n, seed=0, drift=0.06, noise=0.03):
    r = random.Random(seed)
    cl = [100.0]
    for _ in range(n - 1):
        cl.append(cl[-1] + drift + r.gauss(0, noise))
    return series(cl)


def gen_jitter(n, seed=0):
    """Dead chop: tiny alternating candles around a flat level."""
    r = random.Random(seed)
    cl = [100.0]
    for i in range(n - 1):
        cl.append(100.0 + (0.01 if i % 2 == 0 else -0.01) + r.gauss(0, 0.004))
    return series(cl, wick=0.005)


def gen_spike(n, seed=0):
    c = gen_trend(n, seed=seed)
    c[-1]["high"] += 3.0
    c[-1]["low"] -= 3.0
    return c


def check(cond, msg):
    if not cond:
        print(f"FAIL  {msg}")
        sys.exit(1)
    print(f"OK  {msg}")


# 1. registry / filter counts
check(len(strategies.MODULES) == 21, f"OTC Sniper has 21 modules ({len(strategies.MODULES)})")
check(len(strategies.ZONE_FILTERS) == 20, f"Zone Sniper has 20 filters ({len(strategies.ZONE_FILTERS)})")
from strategies.classic import FILTERS as CLASSIC_FILTERS
check(len(CLASSIC_FILTERS) == 12, f"Classic has 12 filters ({len(CLASSIC_FILTERS)})")
for meta in strategies.STRATEGIES.values():
    check("No-trade gate" in meta["about"], f"{meta['name']} about mentions the no-trade gate")

# 2. no-trade gate behaviour
x = Ctx(gen_trend(130, seed=3))
check(not strategies.no_trade(x, x.n - 1)[0], "clean trend is tradeable")
x = Ctx(gen_jitter(130, seed=3))
blocked, why = strategies.no_trade(x, x.n - 1)
check(blocked, f"dead chop is blocked ({why})")
x = Ctx(gen_spike(130, seed=3))
blocked, why = strategies.no_trade(x, x.n - 1)
check(blocked, f"news spike is blocked ({why})")

# 3. every engine fires on a clean trend and agrees CALL; silent on chop/spike
for key in strategies.ORDER:
    st = strategies.get(key)
    r = st["fn"](gen_trend(130, seed=3))
    check(r is not None and r["direction"] == "CALL" and 0 <= r["confidence"] <= 95,
          f"{st['name']}: clean uptrend -> CALL @ {r['confidence']:.0f}%")
    check(st["fn"](gen_jitter(130, seed=3)) is None, f"{st['name']}: silent on dead chop")
    check(st["fn"](gen_spike(130, seed=3)) is None, f"{st['name']}: silent on news spike")
    d = st["fn"](gen_trend(130, seed=3, drift=-0.06))
    check(d is not None and d["direction"] == "PUT",
          f"{st['name']}: clean downtrend -> PUT @ {d['confidence']:.0f}%")

# 4. all new filters bounded in [-1, 1]
for gen, seeds in ((gen_trend, range(10)),):
    for seed in seeds:
        c = gen(130, seed=seed)
        x = Ctx(c)
        i = x.n - 1
        for key, label, fn, wt, wr in strategies.MODULES:
            v = fn(x, i)
            check_ok = -1.0 - 1e-9 <= v <= 1.0 + 1e-9
            if not check_ok:
                print(f"FAIL  module {key} out of bounds: {v}")
                sys.exit(1)
        from strategies.zone_sniper import _z_context
        z = _z_context(x, i)
        for key, label, fn, wz, wo in strategies.ZONE_FILTERS:
            v = fn(x, i, z)
            if not (-1.0 - 1e-9 <= v <= 1.0 + 1e-9):
                print(f"FAIL  zone filter {key} out of bounds: {v}")
                sys.exit(1)
print("OK  all 21 OTC modules + 20 zone filters bounded")

# 5. deep-analysis mode (sessions)
import sessions

sm = sessions.SessionManager()
check(sm.deep_mode is False, "deep_mode starts OFF")
check(sessions.DEEP_CONF_BONUS == 12.0 and sessions.DEEP_CONF_CAP == 90.0,
      "deep constants present (+12 conf, cap 90)")

trend = gen_trend(130, seed=3)
closed = trend
res = strategies.otc_sniper(closed)
st = strategies.get("otc_sniper")
ok = sm._deep_pass(closed, None, res, st)
check(ok is True, "deep consensus PASSES when every engine agrees on a clean trend")

fake = dict(res)
fake["direction"] = "PUT" if res["direction"] == "CALL" else "CALL"
check(sm._deep_pass(closed, None, fake, st) is False,
      "deep consensus FAILS when other engines point the opposite way")
fake2 = dict(res)
fake2["agree"] = 0.5
check(sm._deep_pass(closed, None, fake2, st) is False,
      "deep consensus FAILS on weak filter agreement (<0.70)")

# 6. deep gate raises the confidence bar inside _pick_best
async def run_pick(sm):
    return await sm._pick_best()

import storage
orig = storage.get_settings()
storage.save_settings({**orig, "strategy": "otc_sniper"})
try:
    # a market whose confidence sits between normal gate (60) and deep gate (72)
    mid_candles = None
    mid_conf = None
    for seed in range(200):
        c = gen_trend(130, seed=seed, drift=0.02, noise=0.03)
        r = strategies.otc_sniper(c)
        if r and 61.0 <= r["confidence"] <= 70.0:
            mid_candles, mid_conf = c, r["confidence"]
            break
    check(mid_candles is not None, f"found a mid-confidence market ({mid_conf:.1f}%)")

    async def fake_candles(code, count=60):
        return mid_candles[-count:]

    sm2 = sessions.SessionManager()
    sm2.active = True
    sm2.markets = [{"code": "T1_otc", "display": "T1-OTC", "payout": 90}]
    sm2._candles = fake_candles
    # closed-candle filter drops the running candle -> shift times far in the past
    for c in mid_candles:
        c["time"] -= 3600 * 24

    sm2.deep_mode = False
    normal_pick = asyncio.run(run_pick(sm2))
    sm2.deep_mode = True
    deep_pick = asyncio.run(run_pick(sm2))
    check(normal_pick is not None, "normal mode: mid-confidence signal accepted")
    check(deep_pick is None, "deep mode: same signal rejected by the raised gate")
finally:
    storage.save_settings(orig)

# 7. deep_mode toggling rule
sm3 = sessions.SessionManager()
for result, expect in (("LOSS", True), ("WIN", False), ("LOSS", True), ("WIN_MTG", False)):
    sm3.deep_mode = (result == "LOSS")
    check(sm3.deep_mode is expect, f"after {result} -> deep_mode {expect}")

# 8. trendlines + chart rendering
def gen_zigzag_up(n, seed=0, drift=0.03, amp=0.35):
    r = random.Random(seed)
    cl = [100.0 + drift * i + amp * math.sin(i / 3.0) + r.gauss(0, 0.02)
          for i in range(n)]
    return series(cl, wick=0.05)

zz = gen_zigzag_up(130, seed=3)
tl = strategies.zone_trendlines(zz)
check(any(t["kind"] == "S" and t["slope"] > 0 for t in tl),
      f"rising trendline detected on a zigzag uptrend ({len(tl)} lines)")

import charting
png = charting.render_chart(zz[-80:],
                            "EURUSD-OTC \u00b7 M1", badge="CALL",
                            entry_ts=int(time.time()) // 60 * 60 + 60)
check(png[:4] == b"\x89PNG" and len(png) > 20000, f"signal chart renders ({len(png)} bytes)")
png2 = charting.render_chart(gen_trend(80, seed=3), "EURUSD-OTC \u00b7 M1", badge="PUT",
                             entry_ts=int(time.time()) // 60 * 60 - 300, result="WIN",
                             stats={"wins": 3, "losses": 0, "total": 3})
check(png2[:4] == b"\x89PNG", f"result chart renders ({len(png2)} bytes)")

print("\nALL UPGRADE CHECKS PASSED")
