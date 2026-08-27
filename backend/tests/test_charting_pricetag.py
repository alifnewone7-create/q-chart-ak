"""Backend-only test for render_chart price-tag containment fix.

Verifies:
1. render_chart returns non-empty PNG bytes for both signal & result renders.
2. The last-price cyan tag's rendered bbox stays INSIDE the chart axes (right
   edge of the text bbox <= right edge of axes bbox). This is the core bug fix.
3. Edge cases (25 candles, ultra-tight price range) do not crash.
"""
import io
import re
import time
import struct
import zlib
import pytest
import matplotlib.pyplot as plt

# Patch plt.close BEFORE importing charting, so we capture the Figure object
# right before render_chart closes it and can introspect artists.
_captured = {"fig": None}
_orig_close = plt.close


def _capturing_close(arg=None):
    # charting.py calls plt.close(fig) with the figure it just built
    if hasattr(arg, "axes"):
        _captured["fig"] = arg
    return _orig_close(arg)


plt.close = _capturing_close  # noqa: E305

from backend.charting import render_chart, ACCENT  # noqa: E402


# ---------- helpers -------------------------------------------------------
def _make_candles(n=70, base=1.28000, step=0.00012, tight=False):
    t0 = int(time.time()) - n * 60
    out = []
    price = base
    for i in range(n):
        o = price
        if tight:
            c = o + ((-1) ** i) * 0.00002
            h = max(o, c) + 0.00001
            l = min(o, c) - 0.00001
        else:
            c = o + step * ((-1) ** (i // 3)) * (1 + (i % 4) * 0.3)
            h = max(o, c) + step * 0.6
            l = min(o, c) - step * 0.6
        out.append({
            "time": t0 + i * 60,
            "open": round(o, 6),
            "high": round(h, 6),
            "low": round(l, 6),
            "close": round(c, 6),
            "volume": 1000 + i * 7,
        })
        price = c
    return out


def _is_png(b):
    return isinstance(b, (bytes, bytearray)) and b[:8] == b"\x89PNG\r\n\x1a\n" and len(b) > 2000


def _png_size(b):
    # PNG IHDR at offset 8: length(4) + "IHDR"(4) + width(4) + height(4)
    w = struct.unpack(">I", b[16:20])[0]
    h = struct.unpack(">I", b[20:24])[0]
    return w, h


def _inspect_last_price_tag(fig):
    """Find the last-price text on the main chart axis and assert containment."""
    assert fig is not None, "Figure was not captured"
    # main chart ax is the one with candles - it has patches (Rectangle candles)
    main_ax = None
    for a in fig.axes:
        # look for the axis that has our accent-color tag text like " 23:36 "
        for t in a.texts:
            s = (t.get_text() or "").strip()
            if re.fullmatch(r"\d{2}:\d{2}", s):
                main_ax = a
                break
        if main_ax:
            break
    assert main_ax is not None, "Could not locate main chart axis / price tag text"

    renderer = fig.canvas.get_renderer()
    ax_bbox = main_ax.get_window_extent(renderer=renderer)

    # find the price-tag text specifically (ha=right, has bbox facecolor==ACCENT)
    tag = None
    for t in main_ax.texts:
        s = (t.get_text() or "").strip()
        if not s:
            continue
        # tag is running-candle time like 23:36
        if not re.fullmatch(r"\d{2}:\d{2}", s):
            continue
        bbox_patch = t.get_bbox_patch()
        if bbox_patch is None:
            continue
        fc = bbox_patch.get_facecolor()
        # ACCENT = "#22d3ee" -> approx rgba (0.133, 0.827, 0.933, 1)
        if abs(fc[0] - 0.133) < 0.05 and abs(fc[1] - 0.827) < 0.05 and abs(fc[2] - 0.933) < 0.05:
            tag = t
            break
    assert tag is not None, "Could not find the ACCENT-colored last-price tag"

    tag_bbox = tag.get_window_extent(renderer=renderer)
    # The tag's right edge (including its rounded box padding) must not exceed
    # the axes' right border.
    # Allow 1 pixel tolerance for anti-aliasing.
    assert tag_bbox.x1 <= ax_bbox.x1 + 1.0, (
        f"Price tag right edge ({tag_bbox.x1:.1f}) exceeds axes right edge "
        f"({ax_bbox.x1:.1f}) by {tag_bbox.x1 - ax_bbox.x1:.1f}px - tag is OUTSIDE chart!"
    )
    # And the tag should still be to the right of most of the axes (near right side)
    assert tag_bbox.x0 > ax_bbox.x0 + (ax_bbox.x1 - ax_bbox.x0) * 0.5, \
        "Price tag not on the right side of the chart"
    return tag_bbox, ax_bbox


# ---------- tests ---------------------------------------------------------
class TestRenderChartSignal:
    def test_signal_png_valid(self):
        _captured["fig"] = None
        candles = _make_candles(70)
        last_ts = candles[-1]["time"]
        png = render_chart(
            candles, "TRUUSD (OTC) \u00b7 PUT",
            badge="PUT", payout=85,
            entry_ts=last_ts + 60, entry_str="04:00",
            market_name="TRUUSD (OTC)",
        )
        assert _is_png(png), "signal render did not return valid PNG bytes"
        w, h = _png_size(png)
        assert w > 1000 and h > 800, f"unexpected PNG dims {w}x{h}"

    def test_signal_price_tag_inside_axes(self):
        _captured["fig"] = None
        candles = _make_candles(70)
        last_ts = candles[-1]["time"]
        # We need to run render but keep the figure. Monkeypatch plt.close to no-op
        # for this test so we can inspect.
        import matplotlib.pyplot as _plt
        saved = _plt.close
        held = {"fig": None}

        def hold(arg=None):
            if hasattr(arg, "axes"):
                held["fig"] = arg
            # do NOT actually close - we need renderer alive
        _plt.close = hold
        try:
            png = render_chart(
                candles, "TRUUSD (OTC) \u00b7 PUT",
                badge="PUT", payout=85,
                entry_ts=last_ts + 60, entry_str="04:00",
                market_name="TRUUSD (OTC)",
            )
            assert _is_png(png)
            tag_bbox, ax_bbox = _inspect_last_price_tag(held["fig"])
            print(f"tag bbox: x0={tag_bbox.x0:.1f} x1={tag_bbox.x1:.1f} "
                  f"| axes bbox: x0={ax_bbox.x0:.1f} x1={ax_bbox.x1:.1f} "
                  f"| slack={ax_bbox.x1 - tag_bbox.x1:.1f}px")
        finally:
            if held["fig"] is not None:
                saved(held["fig"])
            _plt.close = saved


class TestRenderChartResult:
    def test_result_png_valid(self):
        candles = _make_candles(70)
        last_ts = candles[-1]["time"]
        png = render_chart(
            candles, "TRUUSD (OTC) \u00b7 PUT",
            badge="PUT", payout=85,
            entry_ts=last_ts + 60, entry_str="04:00",
            market_name="TRUUSD (OTC)",
            result="WIN",
            stats={"wins": 2, "losses": 0, "total": 2, "total_pct": 170},
        )
        assert _is_png(png), "result render did not return valid PNG bytes"

    def test_result_price_tag_inside_axes(self):
        candles = _make_candles(70)
        last_ts = candles[-1]["time"]
        import matplotlib.pyplot as _plt
        saved = _plt.close
        held = {"fig": None}

        def hold(arg=None):
            if hasattr(arg, "axes"):
                held["fig"] = arg
        _plt.close = hold
        try:
            png = render_chart(
                candles, "TRUUSD (OTC) \u00b7 PUT",
                badge="PUT", payout=85,
                entry_ts=last_ts + 60, entry_str="04:00",
                market_name="TRUUSD (OTC)",
                result="WIN",
                stats={"wins": 2, "losses": 0, "total": 2, "total_pct": 170},
            )
            assert _is_png(png)
            _inspect_last_price_tag(held["fig"])
        finally:
            if held["fig"] is not None:
                saved(held["fig"])
            _plt.close = saved


class TestEdgeCases:
    def test_only_25_candles(self):
        candles = _make_candles(25)
        png = render_chart(candles, "EURUSD (OTC) \u00b7 CALL", badge="CALL",
                           payout=80, market_name="EURUSD (OTC)")
        assert _is_png(png)

    def test_tight_price_range(self):
        candles = _make_candles(70, base=1.20000, tight=True)
        png = render_chart(candles, "GBPUSD (OTC) \u00b7 CALL", badge="CALL",
                           payout=82, market_name="GBPUSD (OTC)")
        assert _is_png(png)

    def test_tight_range_price_tag_still_inside(self):
        candles = _make_candles(70, tight=True)
        import matplotlib.pyplot as _plt
        saved = _plt.close
        held = {"fig": None}

        def hold(arg=None):
            if hasattr(arg, "axes"):
                held["fig"] = arg
        _plt.close = hold
        try:
            png = render_chart(candles, "GBPUSD (OTC) \u00b7 CALL", badge="CALL",
                               payout=82, market_name="GBPUSD (OTC)")
            assert _is_png(png)
            _inspect_last_price_tag(held["fig"])
        finally:
            if held["fig"] is not None:
                saved(held["fig"])
            _plt.close = saved
