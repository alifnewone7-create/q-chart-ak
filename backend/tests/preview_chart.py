import sys, time, random
sys.path.insert(0, "/app/backend")
import charting

random.seed(2)
now = int(time.time()) // 60 * 60
candles = []
price = 97.325
trend = -0.006
for i in range(200):
    price += trend + random.uniform(-0.004, 0.004)
    o = price
    h = o + abs(random.gauss(0, 0.006))
    l = o - abs(random.gauss(0, 0.006))
    c = o + random.uniform(-0.008, 0.006)
    if random.random() < 0.02:
        c = o
    h = max(h, o, c) + random.uniform(0, 0.002)
    l = min(l, o, c) - random.uniform(0, 0.002)
    candles.append({
        "time": now - (200 - i) * 60,
        "open": o, "high": h, "low": l, "close": c,
        "volume": random.randint(80, 250),
    })
    price = c
    if i in (60, 110, 150):
        trend = -trend * 0.4

png = charting.render_chart(candles, "USD/INR (OTC)  \u00b7  M1", badge="WIN")
open("/app/backend/tests/preview.png", "wb").write(png)
print("OK", len(png))
