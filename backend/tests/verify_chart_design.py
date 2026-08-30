import sys, re, struct, time
sys.path.insert(0, "/app/backend")
import matplotlib.pyplot as plt
held = {"fig": None}
_orig = plt.close
def hold(arg=None):
    if hasattr(arg, "axes"):
        held["fig"] = arg
plt.close = hold
import charting
from charting import render_chart, ACCENT

def make(n=70, base=1.28000, step=0.00012, tight=False):
    t0 = int(time.time()) - n * 60
    out, price = [], base
    for i in range(n):
        o = price
        if tight:
            c = o + ((-1) ** i) * 0.00002; h = max(o, c) + 0.00001; l = min(o, c) - 0.00001
        else:
            c = o + step * ((-1) ** (i // 3)) * (1 + (i % 4) * 0.3)
            h = max(o, c) + step * 0.6; l = min(o, c) - step * 0.6
        out.append({"time": t0 + i*60, "open": round(o,6), "high": round(h,6),
                    "low": round(l,6), "close": round(c,6), "volume": 1000 + i*7})
        price = c
    return out

def check(png, label):
    assert png[:8] == b"\x89PNG\r\n\x1a\n" and len(png) > 2000, label
    w = struct.unpack(">I", png[16:20])[0]; h = struct.unpack(">I", png[20:24])[0]
    fig = held["fig"]; assert fig is not None
    r = fig.canvas.get_renderer()
    main = None
    for a in fig.axes:
        for t in a.texts:
            if re.fullmatch(r"\d{2}:\d{2}", (t.get_text() or "").strip()):
                main = a; break
        if main: break
    assert main is not None, f"{label}: no price tag axis"
    axb = main.get_window_extent(renderer=r)
    tag = None
    for t in main.texts:
        s = (t.get_text() or "").strip()
        if not re.fullmatch(r"\d{2}:\d{2}", s): continue
        bp = t.get_bbox_patch()
        if bp is None: continue
        fc = bp.get_facecolor()
        if abs(fc[0]-0.133)<0.05 and abs(fc[1]-0.827)<0.05 and abs(fc[2]-0.933)<0.05:
            tag = t; break
    assert tag is not None, f"{label}: accent tag not found"
    tb = tag.get_window_extent(renderer=r)
    assert tb.x1 <= axb.x1 + 1.0, f"{label}: tag outside ({tb.x1:.1f} > {axb.x1:.1f})"
    assert tb.x0 > axb.x0 + (axb.x1-axb.x0)*0.5, f"{label}: tag not right side"
    _orig(fig); held["fig"] = None
    print(f"OK {label}  {w}x{h}  slack={axb.x1-tb.x1:.1f}px")
    assert w > 1000 and h > 800, f"{label}: dims {w}x{h}"

c = make(70); lt = c[-1]["time"]
check(render_chart(c, "TRUUSD (OTC) · PUT", badge="PUT", payout=85,
                   entry_ts=lt+60, entry_str="04:00", market_name="TRUUSD (OTC)"), "signal")
check(render_chart(c, "TRUUSD (OTC) · PUT", badge="PUT", payout=85, entry_ts=lt+60,
                   entry_str="04:00", market_name="TRUUSD (OTC)", result="WIN",
                   stats={"wins":2,"losses":0,"total":2}), "result-win")
check(render_chart(c, "X · PUT", badge="PUT", payout=85, entry_ts=lt+60,
                   market_name="AUDCAD (OTC)", result="LOSS",
                   stats={"wins":0,"losses":1,"total":1}), "result-loss")
check(render_chart(make(25), "EURUSD (OTC) · CALL", badge="CALL", payout=80,
                   market_name="EURUSD (OTC)"), "25-candles")
check(render_chart(make(70, tight=True), "GBPUSD (OTC) · CALL", badge="CALL",
                   payout=82, market_name="GBPUSD (OTC)"), "tight-range")

png = render_chart(make(1), "TINY", badge="CALL", payout=70, market_name="TINY")
assert png[:8] == b"\x89PNG\r\n\x1a\n"; print("OK 1-candle fallback")

# no S/R lines on a market without a real level
import matplotlib
import strategies
def clean_trend(n=90):
    t0 = int(time.time()) - n * 60
    out, p = [], 1.5000
    for i in range(n):
        o = p; c = o + 0.0004; h = c + 0.00005; l = o - 0.00005
        out.append({"time": t0+i*60, "open": o, "high": h, "low": l,
                    "close": c, "volume": 900})
        p = c
    return out
tr = clean_trend()
assert strategies.zone_levels(tr) == [], "levels found on a clean staircase trend"
png = render_chart(tr, "NOLEVEL", badge="CALL", payout=80, market_name="NOLEVEL")
fig = held["fig"]
sr_lines = 0
labels = set()
for a in fig.axes:
    for ln in a.lines:
        col = matplotlib.colors.to_hex(ln.get_color()).lower()
        if col in (charting.RES_COL.lower(), charting.SUP_COL.lower()):
            sr_lines += 1
    for t in a.texts:
        labels.add((t.get_text() or "").strip())
assert sr_lines == 0, f"{sr_lines} S/R lines drawn on a market with no level"
leg = [t.get_text() for a in fig.axes if a.get_legend() for t in a.get_legend().get_texts()]
assert "Resistance Zone" not in leg and "Support Zone" not in leg, leg
_orig(fig); held["fig"] = None
print("OK no-level market -> no S/R lines, legend:", leg)

# result label is drawn under the entry candle, not as a top-right ribbon
c = make(70); lt = c[-1]["time"]
png = render_chart(c, "X", badge="PUT", payout=85, entry_ts=c[-4]["time"],
                   market_name="X", result="WIN", stats={"wins":3,"losses":1,"total":4})
fig = held["fig"]
main = fig.axes[0]
tag = [t for t in main.texts if (t.get_text() or "").strip() == "WIN"]
assert tag, "WIN label missing from the price panel"
tx, ty = tag[0].get_position()
entry_lo = c[-4]["low"]
assert abs(tx - (len(c[-70:]) - 4)) < 0.6, f"WIN label not at the entry candle x ({tx})"
assert ty < entry_lo, "WIN label is not below the entry candle"
boxes = [p for p in main.patches if getattr(p, "get_linestyle", None)
         and p.get_linestyle() not in ("solid", "-")]
assert boxes, "entry candle highlight box missing"
_orig(fig); held["fig"] = None
print("OK result label sits under the marked entry candle")

src = open("/app/backend/charting.py").read()
import re as _re
code = _re.sub(r'"""[\s\S]*?"""', "", src)
code = _re.sub(r"#.*", "", code)
assert "LOSSES" not in code, "LOSSES text present"
assert "engine_section" not in code, "engine chip still drawn"
assert "Ultra Volt" not in src, "Ultra Volt chip still present"
assert "ACCURACY" not in src, "accuracy badge still on the chart"
assert "TaNix" not in src, "old brand present"
print("OK source assertions")
