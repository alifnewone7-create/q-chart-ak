"""SignalMaster Pro — professional HUD-style candlestick chart PNG generator.

A clean trading dashboard image sent to Telegram:
    * Top header bar  : logo + brand + market name/OTC | big CALL/PUT badge |
                        payout % + signal time (UTC+6)
    * Main price panel: candles + 3 MA lines + shaded band + ENTRY arrow +
                        last-price tag  (+ WIN/LOSS ribbon on the result image)
    * Volume panel    : coloured volume bars
    * Right panel     : SIGNAL DETAILS (CALL/PUT, Entry Time, Market, Martingale)
                        and, on the result image, RESULT + PERFORMANCE stats
    * Footer          : Developed by @iamhear1
"""
import io
import time
import math
from datetime import datetime, timezone, timedelta

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyBboxPatch, RegularPolygon
from matplotlib.gridspec import GridSpec

# ---- palette -------------------------------------------------------------
BG        = "#070b13"      # page background
PANEL     = "#0b1220"      # panel fill
PANEL2    = "#0e1727"      # inner box fill
BORDER    = "#1c2b40"      # subtle borders
GRID      = "#111c2b"
TEXT      = "#d7e2f0"
DIM       = "#7c8ba1"
FAINT     = "#4a5a70"

UP        = "#16c784"      # bullish candle / CALL
DOWN      = "#ea3943"      # bearish candle / PUT
CALL_COL  = "#16c784"
PUT_COL   = "#ea3943"

MA_FAST   = "#f5c542"      # yellow
MA_MID    = "#22d3ee"      # cyan
MA_SLOW   = "#a78bfa"      # purple
BAND_COL  = "#16324a"      # shaded band
ACCENT    = "#22d3ee"      # cyan accent
WIN_COL   = "#16c784"
MTG_COL   = "#3b82f6"
LOSS_COL  = "#ea3943"
ENGINE_COL = "#f0b429"     # engine card gold accent

FMONO = "DejaVu Sans Mono"
FSANS = "DejaVu Sans"


# ---- indicator helpers ---------------------------------------------------

def _ema(values, period):
    if not values:
        return []
    k = 2.0 / (period + 1)
    out = [values[0]]
    for v in values[1:]:
        out.append(out[-1] + k * (v - out[-1]))
    return out


def _sma(values, period):
    out = []
    for i in range(len(values)):
        if i + 1 < period:
            out.append(None)
        else:
            out.append(sum(values[i + 1 - period:i + 1]) / period)
    return out


def _stdev(values, period):
    out = []
    for i in range(len(values)):
        if i + 1 < period:
            out.append(None)
            continue
        w = values[i + 1 - period:i + 1]
        m = sum(w) / period
        var = sum((x - m) ** 2 for x in w) / period
        out.append(math.sqrt(var))
    return out


def _result_label(result):
    return {"WIN": "WIN", "WIN_MTG": "MTG WIN", "LOSS": "LOSS"}.get(result, str(result))


def _result_color(result):
    if result == "WIN":
        return WIN_COL
    if result == "WIN_MTG":
        return MTG_COL
    return LOSS_COL


# ---- small drawing helpers (axis-coordinate rounded boxes) ---------------

def _rbox(ax, x, y, w, h, fc, ec, lw=1.0, alpha=1.0, pad=0.008, z=3):
    ax.add_patch(FancyBboxPatch(
        (x, y), w, h, transform=ax.transAxes,
        boxstyle=f"round,pad={pad},rounding_size=0.02",
        facecolor=fc, edgecolor=ec, linewidth=lw, alpha=alpha,
        mutation_aspect=0.6, clip_on=False, zorder=z,
    ))


# ---- main render ---------------------------------------------------------

def render_chart(candles, title, badge=None, *, payout=0, entry_ts=None,
                 entry_str=None, market_name=None, result=None, stats=None):
    """Render the dashboard PNG and return raw bytes.

    badge        : "CALL" / "PUT"  (the signal direction)
    payout       : int payout %
    entry_ts     : unix ts of the entry candle (used to place the ENTRY arrow)
    entry_str    : "HH:MM" entry time
    market_name  : market display name (falls back to `title`)
    result       : None (signal image) or "WIN" / "WIN_MTG" / "LOSS"
    stats        : dict(wins=, losses=, total=)  -> shown on the result image
    """
    direction = (badge or "").upper() if badge in ("CALL", "PUT") else (badge or "")
    if market_name is None:
        market_name = title.split("\u00b7")[0].strip() if title else ""
    if entry_str is None and entry_ts:
        entry_str = time.strftime("%H:%M", time.localtime(entry_ts))
    is_result = result is not None

    data = candles[-70:] if len(candles) >= 20 else list(candles)
    n = len(data)

    fig = plt.figure(figsize=(15.5, 10.65), dpi=110, facecolor=BG)
    # main chart + volume on the left, info panel on the right
    gs = GridSpec(2, 2, figure=fig, width_ratios=[3.05, 1.0],
                  height_ratios=[8.2, 1.7], wspace=0.03, hspace=0.05,
                  left=0.018, right=0.985, top=0.885, bottom=0.075)
    ax = fig.add_subplot(gs[0, 0]); ax.set_facecolor(PANEL)
    axv = fig.add_subplot(gs[1, 0], sharex=ax); axv.set_facecolor(PANEL)
    side = fig.add_subplot(gs[:, 1]); side.set_facecolor(PANEL)
    side.set_xticks([]); side.set_yticks([]); side.set_xlim(0, 1); side.set_ylim(0, 1)

    for a in (ax, axv, side):
        for s in a.spines.values():
            s.set_color(BORDER); s.set_linewidth(1.0)

    if n < 2:
        ax.text(0.5, 0.5, "Insufficient data", color=DIM, ha="center", va="center",
                transform=ax.transAxes, family=FMONO)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=BG); plt.close(fig)
        return buf.getvalue()

    opens = [c["open"] for c in data]
    highs = [c["high"] for c in data]
    lows = [c["low"] for c in data]
    closes = [c["close"] for c in data]
    vols = [c.get("volume", abs(c["close"] - c["open"]) * 1e6) for c in data]

    span = (max(highs) - min(lows)) or 1e-9

    # ---- MA lines + shaded band ----------------------------------------
    ema_f = _ema(closes, 5)
    ema_m = _ema(closes, 13)
    ema_s = _ema(closes, 34)
    bb_mid = _sma(closes, 20)
    bb_sd = _stdev(closes, 20)
    bb_up = [(m + 2 * s) if m is not None and s is not None else None
             for m, s in zip(bb_mid, bb_sd)]
    bb_lo = [(m - 2 * s) if m is not None and s is not None else None
             for m, s in zip(bb_mid, bb_sd)]

    xs = list(range(n))
    valid = [i for i in xs if bb_up[i] is not None]
    if valid:
        ax.fill_between(valid, [bb_lo[i] for i in valid], [bb_up[i] for i in valid],
                        color=BAND_COL, alpha=0.45, zorder=1, linewidth=0)
    ax.grid(color=GRID, linewidth=0.6, alpha=0.8)
    ax.plot(xs, ema_s, color=MA_SLOW, linewidth=1.4, alpha=0.9, zorder=3)
    ax.plot(xs, ema_m, color=MA_MID, linewidth=1.4, alpha=0.95, zorder=3)
    ax.plot(xs, ema_f, color=MA_FAST, linewidth=1.6, alpha=0.95, zorder=4)

    # ---- candles --------------------------------------------------------
    tiny = span * 0.0015
    for i, c in enumerate(data):
        up = c["close"] >= c["open"]
        col = UP if up else DOWN
        ax.vlines(i, c["low"], c["high"], color=col, linewidth=1.1, zorder=5)
        body = abs(c["close"] - c["open"]) or tiny
        ax.add_patch(Rectangle((i - 0.32, min(c["open"], c["close"])), 0.64, body,
                     facecolor=col, edgecolor=col, linewidth=0.6, zorder=6))

    # ---- ENTRY arrow ----------------------------------------------------
    entry_x = None
    if entry_ts is not None:
        for i, c in enumerate(data):
            if int(c["time"]) == int(entry_ts):
                entry_x = i
                break
        if entry_x is None and entry_ts > data[-1]["time"]:
            entry_x = n  # upcoming candle (signal image)
    if entry_x is not None:
        ref_price = closes[-1]
        y_arrow = ref_price + span * 0.13
        # guide line down to the price zone + a bold arrow marker
        ax.vlines(entry_x, ref_price, y_arrow, color=ACCENT, linewidth=0.8,
                  alpha=0.6, linestyle=(0, (3, 2)), zorder=8)
        ax.add_patch(RegularPolygon((entry_x, y_arrow), numVertices=3, radius=span * 0.038,
                     orientation=math.pi, facecolor=ACCENT, edgecolor="#04121b",
                     linewidth=0.8, zorder=9))
        ax.text(entry_x, y_arrow + span * 0.055, "ENTRY", color=ACCENT, fontsize=9.5,
                fontweight="bold", ha="center", va="bottom", family=FMONO, zorder=9,
                bbox=dict(boxstyle="round,pad=0.2", facecolor="#04121b",
                          edgecolor=ACCENT, linewidth=0.8))

    # ---- last price tag -------------------------------------------------
    # ---- running candle countdown tag (seconds left of the 1m candle) ----
    last_price = closes[-1]
    rem = max(0, int(data[-1]["time"]) + 60 - int(time.time()))
    rem = min(rem, 60)
    run_time = f"{rem // 60:02d}:{rem % 60:02d}"
    ax.text(0.992, last_price, f" {run_time} ", transform=ax.get_yaxis_transform(),
            color="#04121b", fontsize=10,
            fontweight="bold", ha="right", va="center", family=FMONO, zorder=11,
            bbox=dict(boxstyle="round,pad=0.32", facecolor=ACCENT, edgecolor="none"),
            clip_on=False)
    ax.axhline(last_price, color=ACCENT, linewidth=0.6, alpha=0.5,
               linestyle=(0, (4, 3)), zorder=2)

    # ---- result ribbon (top-right of chart) -----------------------------
    if is_result:
        rc = _result_color(result)
        ax.text(0.985, 0.955, _result_label(result), transform=ax.transAxes,
                color="#04121b", fontsize=13, fontweight="bold", ha="right", va="center",
                family=FMONO, rotation=0, zorder=12,
                bbox=dict(boxstyle="round,pad=0.45", facecolor=rc, edgecolor="none"))

    # axis limits / ticks
    ax.set_xlim(-0.6, n + 5.6)
    pad = span * 0.16
    ax.set_ylim(min(lows) - pad, max(highs) + pad)
    ax.set_xticks([])
    ax.tick_params(colors=DIM, labelsize=9, length=0)
    ax.yaxis.tick_right(); ax.yaxis.set_label_position("right")
    ax.tick_params(axis="y", pad=-8)
    for lbl in ax.yaxis.get_ticklabels():
        lbl.set_ha("right")

    # ---- volume ---------------------------------------------------------
    vmax = max(vols) or 1
    for i, c in enumerate(data):
        col = UP if c["close"] >= c["open"] else DOWN
        axv.bar(i, vols[i], color=col, width=0.7, alpha=0.75, linewidth=0)
    axv.set_ylim(0, vmax * 1.15)
    axv.grid(color=GRID, linewidth=0.5, alpha=0.7, axis="y")
    axv.set_yticks([])
    axv.tick_params(colors=DIM, labelsize=8, length=0)
    step = max(1, n // 8)
    ticks = list(range(0, n, step))
    axv.set_xticks(ticks)
    axv.set_xticklabels(
        [time.strftime("%H:%M", time.localtime(data[i]["time"])) for i in ticks],
        color=DIM, fontsize=8.5, family=FMONO)

    # =====================================================================
    #  HEADER  (figure coordinates)
    # =====================================================================
    fig.patches.append(FancyBboxPatch(
        (0.018, 0.905), 0.967, 0.078, transform=fig.transFigure,
        boxstyle="round,pad=0.004,rounding_size=0.01",
        facecolor=PANEL, edgecolor=BORDER, linewidth=1.2))

    # -- left: logo icon + brand
    fig.patches.append(RegularPolygon(
        (0.034, 0.945), numVertices=6, radius=0.015, orientation=0,
        transform=fig.transFigure, facecolor="none", edgecolor=ACCENT, linewidth=1.6))
    fig.patches.append(RegularPolygon(
        (0.034, 0.945), numVertices=6, radius=0.0065, orientation=0,
        transform=fig.transFigure, facecolor=ACCENT, edgecolor="none"))
    fig.text(0.058, 0.945, "TaNix", color=TEXT, fontsize=16, fontweight="bold",
             va="center", ha="left", family=FSANS)
    fig.text(0.108, 0.945, "Alpha 2.0", color=ACCENT, fontsize=16, fontweight="bold",
             va="center", ha="left", family=FSANS)

    # -- center: big CALL/PUT badge (no confidence)
    d_col = CALL_COL if direction == "CALL" else PUT_COL
    tri = "\u25b2" if direction == "CALL" else "\u25bc"

    # -- right: payout + signal time (UTC+6)
    fig.text(0.982, 0.958, f"Payout  {int(payout)}%", color="#f0b429", fontsize=12.5,
             fontweight="bold", ha="right", va="center", family=FMONO)
    st = entry_str or time.strftime("%H:%M", time.localtime())
    fig.text(0.982, 0.930, f"Signal Time  {st} (UTC+6)", color=DIM, fontsize=10.5,
             fontweight="bold", ha="right", va="center", family=FMONO)

    # =====================================================================
    #  RIGHT PANEL  (side axis, 0..1 coords)
    # =====================================================================
    def stitle(y, txt, col=ACCENT):
        side.plot([0.06, 0.10], [y, y], color=col, linewidth=3, transform=side.transAxes,
                  solid_capstyle="round", zorder=5)
        side.text(0.5, y, txt, transform=side.transAxes, color=col, fontsize=11.5,
                  fontweight="bold", ha="center", va="center", family=FMONO, zorder=5)

    def engine_section(title_y, box_y, h=0.078):
        """Premium 'ENGINE' card — TaNix Ultra Volt."""
        stitle(title_y, "ENGINE", ENGINE_COL)
        # soft outer glow frame
        _rbox(side, 0.045, box_y - 0.007, 0.91, h + 0.014, "#121a2a", ENGINE_COL,
              lw=0.8, alpha=0.40, z=3)
        _rbox(side, 0.06, box_y, 0.88, h, PANEL2, ENGINE_COL, lw=1.6, z=4)
        cy = box_y + h / 2
        side.text(0.5, cy + 0.014, "TaNix Ultra Volt", transform=side.transAxes,
                  color=ENGINE_COL, fontsize=13, fontweight="bold",
                  ha="center", va="center", family=FSANS, zorder=5)
        side.text(0.5, cy - 0.020, "\u25c6  PRECISION ENGINE  \u25c6",
                  transform=side.transAxes, color=DIM, fontsize=7.5,
                  fontweight="bold", ha="center", va="center", family=FMONO, zorder=5)

    # SIGNAL DETAILS
    stitle(0.965, "SIGNAL DETAILS")
    _rbox(side, 0.06, 0.885, 0.88, 0.055, PANEL2, d_col, lw=1.6, z=4)
    side.text(0.5, 0.9125, f"{tri}  {direction}", transform=side.transAxes, color=d_col,
              fontsize=15, fontweight="bold", ha="center", va="center", family=FMONO, zorder=5)

    rows = [("Entry Time", entry_str or "--:--", TEXT),
            ("Market", market_name, TEXT),
            ("Martingale", "1 Step", ACCENT)]
    ry = 0.83
    for label, val, vcol in rows:
        side.text(0.09, ry, "\u2022", transform=side.transAxes, color=ACCENT,
                  fontsize=11, ha="left", va="center", zorder=5)
        side.text(0.16, ry, label, transform=side.transAxes, color=DIM, fontsize=10.5,
                  ha="left", va="center", family=FMONO, zorder=5)
        side.text(0.94, ry, str(val), transform=side.transAxes, color=vcol, fontsize=10.5,
                  fontweight="bold", ha="right", va="center", family=FMONO, zorder=5)
        ry -= 0.058

    # ENGINE card under Signal Details (signal image)
    if not is_result:
        engine_section(0.645, 0.535)

    # RESULT + PERFORMANCE (result image only)
    if is_result:
        rc = _result_color(result)
        stitle(0.60, "RESULT", rc)
        _rbox(side, 0.06, 0.505, 0.88, 0.065, PANEL2, rc, lw=1.8, z=4)
        side.text(0.5, 0.5375, _result_label(result), transform=side.transAxes, color=rc,
                  fontsize=17, fontweight="bold", ha="center", va="center", family=FMONO, zorder=5)

        st_ = stats or {}
        wins = int(st_.get("wins", 0)); losses = int(st_.get("losses", 0))
        total = int(st_.get("total", wins + losses))
        rate = (wins / total * 100) if total else 0

        stitle(0.44, "PERFORMANCE")
        side.text(0.09, 0.385, "WIN RATE", transform=side.transAxes, color=DIM,
                  fontsize=10, fontweight="bold", ha="left", va="center", family=FMONO)
        side.text(0.94, 0.385, f"{rate:.0f}%", transform=side.transAxes, color=WIN_COL,
                  fontsize=11, fontweight="bold", ha="right", va="center", family=FMONO)
        # progress bar
        _rbox(side, 0.06, 0.335, 0.88, 0.022, "#10202f", BORDER, lw=0.8, pad=0.004, z=4)
        side.add_patch(FancyBboxPatch(
            (0.07, 0.34), 0.86 * max(0.02, rate / 100), 0.012, transform=side.transAxes,
            boxstyle="round,pad=0.003,rounding_size=0.02",
            facecolor=WIN_COL, edgecolor="none", zorder=5))

        # single WINS card — losses are intentionally not shown
        _rbox(side, 0.06, 0.235, 0.88, 0.075, PANEL2, BORDER, lw=1.0, z=4)
        side.text(0.5, 0.293, str(wins), transform=side.transAxes, color=WIN_COL,
                  fontsize=16, fontweight="bold", ha="center", va="center",
                  family=FMONO, zorder=5)
        side.text(0.5, 0.253, "WINS", transform=side.transAxes, color=DIM,
                  fontsize=8, fontweight="bold", ha="center", va="center",
                  family=FMONO, zorder=5)

        # ENGINE card under Performance (result image)
        engine_section(0.165, 0.055)

    # =====================================================================
    #  FOOTER
    # =====================================================================
    from matplotlib.lines import Line2D
    fig.add_artist(Line2D([0.018, 0.985], [0.055, 0.055], transform=fig.transFigure,
                          color=BORDER, linewidth=1.2))
    fig.text(0.982, 0.03, "Developed by  @iamhear1", color=DIM, fontsize=10,
             fontweight="bold", ha="right", va="center", family=FMONO)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
