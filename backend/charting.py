"""SignalMaster Pro — neon HUD candlestick chart PNG generator.

Layout (matches the SignalMaster Pro dashboard design):
    * Top header bar  : target logo + SignalMaster Pro | market pill (+OTC badge)
                        | CALL/PUT badge
    * Main price panel: candles + EMA 7 / EMA 21 + real support/resistance levels
                        (drawn only when the market actually has one) + rejection
                        zone + ENTRY marker + BUY/SELL arrow + last-price tag.
                        On the result image the entry candle itself is boxed and
                        the WIN / MTG WIN / LOSS label sits right under it.
    * Volume panel    : coloured volume bars + time axis
    * Footer          : Powered by SignalMaster Pro | Developed by : @iamhear1
"""
import io
import math
import time

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle, FancyBboxPatch, RegularPolygon
from matplotlib.lines import Line2D
from matplotlib.gridspec import GridSpec

# ---- palette -------------------------------------------------------------
BG        = "#050810"      # page background
PANEL     = "#080e1b"      # panel fill
PANEL2    = "#0c1424"      # inner box fill
BORDER    = "#1b2a42"      # subtle borders
GRID      = "#0e1826"
TEXT      = "#e6eefc"
DIM       = "#7f90a8"
FAINT     = "#3d4c63"

UP        = "#22d17a"      # bullish candle / CALL
DOWN      = "#f0344f"      # bearish candle / PUT
CALL_COL  = "#22d17a"
PUT_COL   = "#ff3b5c"

MA_FAST   = "#f5a623"      # EMA 7  (orange)
MA_MID    = "#2bb2f5"      # EMA 21 (blue)
RES_COL   = "#ff3b6b"      # resistance zone (pink)
SUP_COL   = "#00c48c"      # support zone (green)
REJ_COL   = "#31d0aa"      # rejection zone (dotted)

ACCENT    = "#22d3ee"      # cyan accent (last-price tag)
NEON_PINK = "#e0457b"      # outer glow / accuracy badge
NEON_CYAN = "#28c8f0"      # header glow
GOLD      = "#f5c542"      # ENTRY marker
WIN_COL   = "#22d17a"
MTG_COL   = "#3b82f6"
LOSS_COL  = "#ff3b5c"
ENGINE_COL = "#f0b429"     # engine chip gold accent

FMONO = "DejaVu Sans Mono"
FSANS = "DejaVu Sans"

BRAND_A = "SignalMaster"
BRAND_B = "Pro"


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


# ---- small drawing helpers ----------------------------------------------

def _rbox(ax, x, y, w, h, fc, ec, lw=1.0, alpha=1.0, pad=0.008, z=3, rs=0.02):
    ax.add_patch(FancyBboxPatch(
        (x, y), w, h, transform=ax.transAxes,
        boxstyle=f"round,pad={pad},rounding_size={rs}",
        facecolor=fc, edgecolor=ec, linewidth=lw, alpha=alpha,
        mutation_aspect=0.6, clip_on=False, zorder=z,
    ))


def _fbox(fig, x, y, w, h, fc, ec, lw=1.2, alpha=1.0, pad=0.004, rs=0.012, z=2):
    """Rounded box in figure coordinates."""
    fig.patches.append(FancyBboxPatch(
        (x, y), w, h, transform=fig.transFigure,
        boxstyle=f"round,pad={pad},rounding_size={rs}",
        facecolor=fc, edgecolor=ec, linewidth=lw, alpha=alpha, zorder=z))


def _fglow(fig, x, y, w, h, col, layers=3, base_lw=1.4, spread=0.0035, z=1):
    """Soft neon glow around a figure-coordinate box."""
    for i in range(layers, 0, -1):
        g = spread * i
        _fbox(fig, x - g, y - g, w + 2 * g, h + 2 * g, "none", col,
              lw=base_lw + i * 0.8, alpha=0.06 + 0.02 * (layers - i),
              rs=0.014, z=z)


# ---- main render ---------------------------------------------------------

def render_chart(candles, title, badge=None, *, payout=0, entry_ts=None,
                 entry_str=None, market_name=None, result=None, stats=None):
    """Render the dashboard PNG and return raw bytes.

    badge        : "CALL" / "PUT"  (the signal direction)
    payout       : int payout % (accepted for compatibility, not drawn)
    entry_ts     : unix ts of the entry candle (used to place the ENTRY marker)
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

    fig = plt.figure(figsize=(15.5, 10.0), dpi=112, facecolor=BG)
    gs = GridSpec(2, 1, figure=fig, height_ratios=[8.0, 1.35], hspace=0.035,
                  left=0.035, right=0.945, top=0.855, bottom=0.105)
    ax = fig.add_subplot(gs[0, 0]); ax.set_facecolor(PANEL)
    axv = fig.add_subplot(gs[1, 0], sharex=ax); axv.set_facecolor(PANEL)

    for a in (ax, axv):
        for s in a.spines.values():
            s.set_visible(False)

    # ---- outer neon frame + chart panel frame ---------------------------
    _fglow(fig, 0.008, 0.012, 0.984, 0.976, NEON_PINK, layers=3, spread=0.0028, z=0.2)
    _fbox(fig, 0.008, 0.012, 0.984, 0.976, "none", "#243a56", lw=1.4, rs=0.012, z=0.3)

    _fglow(fig, 0.022, 0.075, 0.956, 0.815, NEON_PINK, layers=2, spread=0.0022, z=0.2)
    _fbox(fig, 0.022, 0.075, 0.956, 0.815, PANEL, "#2a3f5e", lw=1.3, rs=0.010, z=0.3)

    ax.set_zorder(3); axv.set_zorder(3)

    if n < 2:
        ax.text(0.5, 0.5, "Insufficient data", color=DIM, ha="center", va="center",
                transform=ax.transAxes, family=FMONO)
        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=BG); plt.close(fig)
        return buf.getvalue()

    highs = [c["high"] for c in data]
    lows = [c["low"] for c in data]
    closes = [c["close"] for c in data]
    vols = [c.get("volume", abs(c["close"] - c["open"]) * 1e6) for c in data]

    span = (max(highs) - min(lows)) or 1e-9
    xs = list(range(n))

    # ---- watermark ------------------------------------------------------
    ax.text(0.5, 0.5, f"{BRAND_A.upper()} {BRAND_B.upper()}", transform=ax.transAxes,
            color="#16233a", fontsize=54, fontweight="bold", ha="center", va="center",
            family=FSANS, alpha=0.55, zorder=1)

    # ---- EMAs + rejection zone (dotted band) ----------------------------
    ema_f = _ema(closes, 7)
    ema_m = _ema(closes, 21)
    bb_mid = _sma(closes, 20)
    bb_sd = _stdev(closes, 20)
    bb_up = [(m + 2 * s) if m is not None and s is not None else None
             for m, s in zip(bb_mid, bb_sd)]
    bb_lo = [(m - 2 * s) if m is not None and s is not None else None
             for m, s in zip(bb_mid, bb_sd)]

    ax.grid(color=GRID, linewidth=0.6, alpha=0.75)
    valid = [i for i in xs if bb_up[i] is not None]
    if valid:
        ax.plot(valid, [bb_up[i] for i in valid], color=REJ_COL, linewidth=0.9,
                linestyle=(0, (1, 2)), alpha=0.55, zorder=2)
        ax.plot(valid, [bb_lo[i] for i in valid], color=REJ_COL, linewidth=0.9,
                linestyle=(0, (1, 2)), alpha=0.55, zorder=2)

    # ---- support / resistance zones (only REAL levels) ------------------
    # A market with no repeatedly-rejected level gets no lines at all.
    zlevels = []
    try:
        from strategies import zone_levels
        zlevels = zone_levels(candles)
    except Exception:
        zlevels = []
    y_lo, y_hi = min(lows) - span * 0.05, max(highs) + span * 0.05
    zh = span * 0.012
    drew_res = drew_sup = False
    for lv in zlevels:
        p = lv["price"]
        if not (y_lo <= p <= y_hi):
            continue
        is_res = lv["kind"] == "R"
        col = RES_COL if is_res else SUP_COL
        ax.axhspan(p - zh, p + zh, color=col, alpha=0.10, zorder=1)
        ax.axhline(p, color=col, linewidth=0.9, linestyle=(0, (6, 4)),
                   alpha=0.75, zorder=2)
        ax.text(-0.4, p, f" {lv['kind']}", color=col, fontsize=7.5, ha="left",
                va="bottom", family=FMONO, alpha=0.9, zorder=3)
        drew_res = drew_res or is_res
        drew_sup = drew_sup or not is_res

    ax.plot(xs, ema_m, color=MA_MID, linewidth=2.6, alpha=0.95, zorder=3,
            solid_capstyle="round")
    ax.plot(xs, ema_f, color=MA_FAST, linewidth=2.6, alpha=0.95, zorder=4,
            solid_capstyle="round")

    # ---- candles --------------------------------------------------------
    tiny = span * 0.0015
    for i, c in enumerate(data):
        up = c["close"] >= c["open"]
        col = UP if up else DOWN
        ax.vlines(i, c["low"], c["high"], color=col, linewidth=1.3, zorder=5)
        body = abs(c["close"] - c["open"]) or tiny
        ax.add_patch(Rectangle((i - 0.34, min(c["open"], c["close"])), 0.68, body,
                     facecolor=col, edgecolor=col, linewidth=0.6, zorder=6))

    # ---- legend (top-left inside the chart) -----------------------------
    handles = [
        Line2D([], [], color=MA_FAST, lw=2.4, label="EMA 7"),
        Line2D([], [], color=MA_MID, lw=2.4, label="EMA 21"),
    ]
    if drew_res:
        handles.append(Line2D([], [], color=RES_COL, lw=2.4, label="Resistance Zone"))
    if drew_sup:
        handles.append(Line2D([], [], color=SUP_COL, lw=2.4, label="Support Zone"))
    handles.append(Line2D([], [], color=REJ_COL, lw=1.2, linestyle=(0, (1, 2)),
                          label="Rejection Zone"))
    leg = ax.legend(handles=handles, loc="upper left", bbox_to_anchor=(0.012, 0.955),
                    frameon=True, fontsize=8.5, labelspacing=0.42,
                    handlelength=1.9, borderpad=0.7)
    leg.get_frame().set_facecolor(PANEL2)
    leg.get_frame().set_edgecolor(BORDER)
    leg.get_frame().set_linewidth(0.9)
    leg.get_frame().set_alpha(0.88)
    leg.set_zorder(9)
    for t in leg.get_texts():
        t.set_color(DIM); t.set_family(FMONO)

    # ---- ENTRY marker + BUY/SELL arrow ----------------------------------
    entry_x = None
    if entry_ts is not None:
        for i, c in enumerate(data):
            if int(c["time"]) == int(entry_ts):
                entry_x = i
                break
        if entry_x is None and entry_ts > data[-1]["time"]:
            entry_x = n  # upcoming candle (signal image)
    ref_price = closes[-1]
    if entry_x is not None:
        y_arrow = ref_price + span * 0.085
        ax.vlines(entry_x, ref_price, y_arrow, color=GOLD, linewidth=0.9,
                  alpha=0.55, linestyle=(0, (3, 3)), zorder=8)
        ax.add_patch(RegularPolygon((entry_x, y_arrow), numVertices=3,
                     radius=span * 0.032, orientation=math.pi,
                     facecolor=GOLD, edgecolor="#1a1405", linewidth=0.8, zorder=9))
        ax.text(entry_x, y_arrow + span * 0.045, "ENTRY", color=GOLD, fontsize=9,
                fontweight="bold", ha="center", va="bottom", family=FMONO, zorder=9)

        # dotted directional arrow out to a BUY / SELL pill
        d_is_call = direction == "CALL"
        ar_col = CALL_COL if d_is_call else PUT_COL
        y_end = ref_price + (span * 0.10 if d_is_call else -span * 0.10)
        ax.annotate("", xy=(min(n + 4.2, n + 4.2), y_end),
                    xytext=(entry_x, ref_price),
                    arrowprops=dict(arrowstyle="-|>", color=ar_col, linewidth=1.3,
                                    linestyle=(0, (2, 2)), shrinkA=6, shrinkB=2,
                                    mutation_scale=14), zorder=9)
        ax.text(n + 4.9, y_end - span * 0.012, "BUY" if d_is_call else "SELL",
                color=ar_col, fontsize=9.5, fontweight="bold", ha="center",
                va="center", family=FMONO, zorder=10,
                bbox=dict(boxstyle="round,pad=0.35", facecolor="#140a10",
                          edgecolor=ar_col, linewidth=1.1))

    # ---- last price tag (running candle countdown) ----------------------
    last_price = closes[-1]
    rem = max(0, int(data[-1]["time"]) + 60 - int(time.time()))
    rem = min(rem, 60)
    run_time = f"{rem // 60:02d}:{rem % 60:02d}"
    ax.text(0.992, last_price, f" {run_time} ", transform=ax.get_yaxis_transform(),
            color="#04121b", fontsize=10.5,
            fontweight="bold", ha="right", va="center", family=FMONO, zorder=11,
            bbox=dict(boxstyle="round,pad=0.34", facecolor=ACCENT, edgecolor="none"),
            clip_on=False)
    ax.axhline(last_price, color=ACCENT, linewidth=0.7, alpha=0.5,
               linestyle=(0, (4, 3)), zorder=2)

    # ---- result: mark the entry candle + label under it ------------------
    if is_result:
        rc = _result_color(result)
        mx = entry_x if (entry_x is not None and 0 <= entry_x < n) else n - 1
        ec = data[mx]
        pad_y = span * 0.02
        ax.add_patch(Rectangle((mx - 0.6, ec["low"] - pad_y), 1.20,
                               (ec["high"] - ec["low"]) + 2 * pad_y,
                               facecolor=rc, alpha=0.18, edgecolor=rc,
                               linewidth=1.7, linestyle=(0, (3, 2)), zorder=7))
        y_lbl = ec["low"] - span * 0.115
        ax.vlines(mx, y_lbl + span * 0.028, ec["low"] - pad_y, color=rc,
                  linewidth=1.1, alpha=0.9, linestyle=(0, (3, 2)), zorder=8)
        ax.text(mx, y_lbl, _result_label(result), color="#04121b", fontsize=12,
                fontweight="bold", ha="center", va="center", family=FMONO, zorder=13,
                bbox=dict(boxstyle="round,pad=0.45", facecolor=rc,
                          edgecolor="#04121b", linewidth=1.6))

    # ---- axis limits / ticks --------------------------------------------
    ax.set_xlim(-0.6, n + 6.2)
    pad = span * 0.17
    ax.set_ylim(min(lows) - pad, max(highs) + pad)
    ax.set_xticks([])
    ax.tick_params(colors=DIM, labelsize=9.5, length=0)
    ax.yaxis.tick_right(); ax.yaxis.set_label_position("right")
    ax.tick_params(axis="y", pad=6)
    dec = 5 if last_price < 20 else 2
    ax.yaxis.set_major_formatter(
        matplotlib.ticker.FuncFormatter(lambda v, _p: f"{v:.{dec}f}"))
    for lbl in ax.yaxis.get_ticklabels():
        lbl.set_family(FMONO); lbl.set_color(DIM)

    # ---- volume ---------------------------------------------------------
    vmax = max(vols) or 1
    for i, c in enumerate(data):
        col = UP if c["close"] >= c["open"] else DOWN
        axv.bar(i, vols[i], color=col, width=0.66, alpha=0.65, linewidth=0)
    axv.set_ylim(0, vmax * 1.18)
    axv.set_yticks([])
    axv.grid(False)
    axv.tick_params(colors=DIM, labelsize=8.5, length=0)
    step = max(1, n // 12)
    ticks = list(range(0, n, step))
    axv.set_xticks(ticks)
    axv.set_xticklabels(
        [time.strftime("%H:%M", time.localtime(data[i]["time"])) for i in ticks],
        color=DIM, fontsize=8.5, family=FMONO)
    ax.tick_params(axis="x", labelbottom=False, length=0)

    # =====================================================================
    #  HEADER  (figure coordinates)
    # =====================================================================
    hx, hy, hw, hh = 0.022, 0.895, 0.956, 0.078
    _fglow(fig, hx, hy, hw, hh, NEON_CYAN, layers=2, spread=0.0022)
    _fbox(fig, hx, hy, hw, hh, PANEL, "#2f5a7a", lw=1.3, rs=0.010, z=2)

    # -- left: crosshair/target logo mark + brand
    from matplotlib.patches import Ellipse
    lx, ly = 0.045, 0.934
    ar = fig.get_figwidth() / fig.get_figheight()       # keep circles round
    r_out = 0.017
    fig.patches.append(Ellipse((lx, ly), 2 * r_out / ar, 2 * r_out,
                               transform=fig.transFigure, facecolor="#0d1c2c",
                               edgecolor=NEON_CYAN, linewidth=1.7, zorder=4))
    fig.patches.append(Ellipse((lx, ly), 2 * r_out * 0.52 / ar, 2 * r_out * 0.52,
                               transform=fig.transFigure, facecolor="none",
                               edgecolor=NEON_CYAN, linewidth=1.2, alpha=0.85,
                               zorder=5))
    fig.patches.append(Ellipse((lx, ly), 2 * r_out * 0.16 / ar, 2 * r_out * 0.16,
                               transform=fig.transFigure, facecolor=NEON_CYAN,
                               edgecolor="none", zorder=6))
    for dx, dy in ((1, 0), (-1, 0), (0, 1), (0, -1)):
        x0 = lx + dx * r_out * 0.62 / ar
        y0 = ly + dy * r_out * 0.62
        x1 = lx + dx * r_out * 1.12 / ar
        y1 = ly + dy * r_out * 1.12
        fig.add_artist(Line2D([x0, x1], [y0, y1], transform=fig.transFigure,
                              color=NEON_CYAN, linewidth=1.5, zorder=6))
    t_a = fig.text(0.068, 0.934, BRAND_A, color=TEXT, fontsize=19,
                   fontweight="bold", va="center", ha="left", family=FSANS, zorder=5)
    fig.canvas.draw()
    bb = t_a.get_window_extent(renderer=fig.canvas.get_renderer())
    bx_pro = bb.x1 / fig.bbox.width + 0.008
    fig.text(bx_pro, 0.934, BRAND_B, color=NEON_CYAN,
             fontsize=19, fontweight="bold", va="center", ha="left",
             family=FSANS, zorder=5)

    # -- center: market pill (+ OTC badge)
    mk = str(market_name or "").strip()
    is_otc = "OTC" in mk.upper()
    mk_clean = mk.replace("(OTC)", "").replace("OTC", "").replace("/", "").strip(" ·-")
    mk_show = (mk_clean or mk).upper()
    pill_w = max(0.135, 0.0135 * len(mk_show) + (0.045 if is_otc else 0.02))
    px = 0.5 - pill_w / 2
    _fglow(fig, px, 0.906, pill_w, 0.056, NEON_CYAN, layers=2, spread=0.0018)
    _fbox(fig, px, 0.906, pill_w, 0.056, "#081726", NEON_CYAN, lw=1.5, rs=0.014, z=3)
    tx = 0.5 - (0.019 if is_otc else 0.0)
    fig.text(tx, 0.934, mk_show, color=TEXT, fontsize=21, fontweight="bold",
             ha="center", va="center", family=FSANS, zorder=5)
    if is_otc:
        bx = tx + 0.0075 * len(mk_show) + 0.006
        _fbox(fig, bx, 0.9225, 0.030, 0.024, "#20160a", "#f5a623", lw=1.2,
              rs=0.012, z=4)
        fig.text(bx + 0.015, 0.9345, "OTC", color="#f5a623", fontsize=9.5,
                 fontweight="bold", ha="center", va="center", family=FMONO, zorder=5)

    # -- right: direction badge (no accuracy badge)
    d_col = CALL_COL if direction == "CALL" else PUT_COL
    tri = "\u25b2" if direction == "CALL" else "\u25bc"
    dbw = 0.108
    dbx = 0.966 - dbw
    _fglow(fig, dbx, 0.906, dbw, 0.056, d_col, layers=2, spread=0.0018)
    _fbox(fig, dbx, 0.906, dbw, 0.056, "#14070d", d_col, lw=1.5, rs=0.014, z=3)
    fig.text(dbx + dbw / 2, 0.934, f"{tri} {direction or '--'}", color=d_col,
             fontsize=17, fontweight="bold", ha="center", va="center",
             family=FSANS, zorder=5)

    # =====================================================================
    #  FOOTER
    # =====================================================================
    fig.add_artist(Line2D([0.022, 0.978], [0.062, 0.062], transform=fig.transFigure,
                          color="#2a3f5e", linewidth=1.1))
    fig.text(0.030, 0.038, f"Powered by  {BRAND_A} {BRAND_B}  \u00b7  @iamhear1",
             color=FAINT, fontsize=9.5, fontweight="bold", ha="left", va="center",
             family=FMONO)
    if is_result:
        st_ = stats or {}
        wins = int(st_.get("wins", 0))
        total = int(st_.get("total", wins + int(st_.get("losses", 0))))
        rate = (wins / total * 100) if total else 0
        fig.text(0.5, 0.038, f"WINS  {wins}   \u00b7   WIN RATE  {rate:.0f}%",
                 color=DIM, fontsize=9.5, fontweight="bold", ha="center",
                 va="center", family=FMONO)
    else:
        st = entry_str or time.strftime("%H:%M", time.localtime())
        fig.text(0.5, 0.038, f"ENTRY  {st}  (UTC+6)", color=DIM, fontsize=9.5,
                 fontweight="bold", ha="center", va="center", family=FMONO)
    fig.text(0.970, 0.038, "Developed by :  @iamhear1", color=DIM, fontsize=9.5,
             fontweight="bold", ha="right", va="center", family=FMONO)

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=BG)
    plt.close(fig)
    buf.seek(0)
    return buf.getvalue()
