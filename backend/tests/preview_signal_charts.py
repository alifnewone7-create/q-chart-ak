"""Renders sample signal/result chart PNGs to /tmp for eyeballing."""
import os, sys, time, random, math
sys.path.insert(0, "/app/backend")
os.environ.setdefault("BOT_TOKEN", "123456:ABC")
import charting


def gen(n=90, base=0.20050, seed=7, bouncy=False):
    random.seed(seed)
    t0 = int(time.time()) - n * 60
    out, p = [], base
    for i in range(n):
        if bouncy:
            c = base + 0.0009 * math.sin(i / 6.5) + random.uniform(-0.00004, 0.00004)
            o = out[-1]["close"] if out else c
        else:
            drift = 0.000012 * math.sin(i / 9.0) + 0.0000075
            o = p
            c = o + drift + random.uniform(-0.00006, 0.00006)
        h = max(o, c) + random.uniform(0, 0.00005)
        l = min(o, c) - random.uniform(0, 0.00005)
        out.append({"time": t0 + i * 60, "open": o, "high": h, "low": l,
                    "close": c, "volume": random.uniform(400, 1400)})
        p = c
    return out


c = gen(bouncy=True)
open("/tmp/sig.png", "wb").write(charting.render_chart(
    c, "BRLUSD (OTC) \u00b7 M1", badge="PUT", payout=89,
    entry_ts=c[-1]["time"] + 60, entry_str="00:16", market_name="BRLUSD (OTC)"))

c2 = gen(seed=3)
open("/tmp/res.png", "wb").write(charting.render_chart(
    c2, "EURUSD \u00b7 M1", badge="CALL", payout=85,
    entry_ts=c2[-3]["time"], entry_str="00:16", market_name="EURUSD",
    result="WIN", stats={"wins": 7, "losses": 2, "total": 9}))

c3 = gen(seed=11)
open("/tmp/loss.png", "wb").write(charting.render_chart(
    c3, "AUDCAD (OTC) \u00b7 M1", badge="PUT", payout=80,
    entry_ts=c3[-2]["time"], entry_str="00:44", market_name="AUDCAD (OTC)",
    result="LOSS", stats={"wins": 4, "losses": 3, "total": 7}))
print("rendered /tmp/sig.png /tmp/res.png /tmp/loss.png")
