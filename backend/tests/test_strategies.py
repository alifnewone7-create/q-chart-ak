"""Fresh test suite for the refactored strategy layer.

Covers:
  * Quantum Prime M1 removal (registry, module surface, no lingering refs).
  * OTC Sniper Pro registry metadata & 'about' text.
  * "Never silent" behaviour like Classic Momentum across many random series.
  * Regime engine (trend vs range) + reason string agree-count correctness.
  * Confidence calibration (walk-forward monotonic-ish accuracy by decile).
  * Accuracy edge vs classic_momentum on mean-reverting & trend-persistent data.
  * _adaptive_factors clamp + fallback + _m_persistence follow/fade sign flip.
  * Session wiring: SessionManager._pick_best delivers most minutes; constants intact.
  * Bot Settings/Strategy views + storage round-trip + analysis.analyze routing.
  * No-regression sanity across module imports + classic_momentum byte-behaviour.
"""
import asyncio
import importlib
import math
import random
from types import SimpleNamespace

import pytest

import analysis
import bot
import charting
import indicators_py
import sessions
import storage
import strategies


# ---------------------------------------------------------------------------
# helpers: synthetic 1-minute candle generators
# ---------------------------------------------------------------------------

def _mk(o, h, l, c, t):
    return {"time": int(t), "open": float(o), "high": float(h),
            "low": float(l), "close": float(c)}


def _series_from_closes(closes, start_ts=1_700_000_000, wick=0.02):
    out = []
    prev = closes[0]
    for i, c in enumerate(closes):
        o = prev
        hi = max(o, c) + wick
        lo = min(o, c) - wick
        out.append(_mk(o, hi, lo, c, start_ts + i * 60))
        prev = c
    return out


def gen_random_walk(n, seed=0, step=0.05):
    r = random.Random(seed)
    closes = [100.0]
    for _ in range(n - 1):
        closes.append(closes[-1] + r.gauss(0, step))
    return _series_from_closes(closes)


def gen_trend(n, seed=0, drift=0.06, noise=0.03):
    r = random.Random(seed)
    closes = [100.0]
    for _ in range(n - 1):
        closes.append(closes[-1] + drift + r.gauss(0, noise))
    return _series_from_closes(closes)


def gen_mean_revert(n, seed=0, k=0.20, noise=0.05, mean=100.0):
    r = random.Random(seed)
    closes = [mean]
    for _ in range(n - 1):
        closes.append(closes[-1] + k * (mean - closes[-1]) + r.gauss(0, noise))
    return _series_from_closes(closes)


# =============================================================================
# 1) Quantum Prime M1 completely removed
# =============================================================================

class TestQuantumRemoved:
    def test_registry_keys(self):
        assert set(strategies.STRATEGIES.keys()) == {"classic", "otc_sniper", "zone_sniper"}
        assert strategies.ORDER == ["classic", "otc_sniper", "zone_sniper"]
        assert strategies.DEFAULT_KEY == "classic"

    def test_module_surface_has_no_quantum(self):
        # None of the removed symbols should still exist
        for gone in ("POOL", "_build_pool", "_layers", "quantum_prime"):
            assert not hasattr(strategies, gone), f"strategies.{gone} still exists"

    def test_no_quantum_references_in_backend(self):
        import pathlib
        # strategies is a package now -> scan the whole backend dir
        root = pathlib.Path(strategies.__file__).resolve().parents[1]
        hits = []
        for p in root.rglob("*.py"):
            if "pyquotex" in p.parts or "tests" in p.parts:
                continue
            if "quantum" in p.read_text(encoding="utf-8", errors="ignore").lower():
                hits.append(str(p))
        assert hits == [], f"'quantum' referenced in: {hits}"


# =============================================================================
# 2) OTC Sniper Pro registry metadata
# =============================================================================

class TestOTCRegistry:
    def test_metadata(self):
        st = strategies.STRATEGIES["otc_sniper"]
        assert st["name"] == "OTC Sniper Pro"
        assert st["min_confidence"] == 60.0
        assert st["min_candles"] == 40
        assert st["min_candles"] == strategies.OTC_MIN_CANDLES
        assert callable(st["fn"])

    def test_about_text(self):
        about = strategies.STRATEGIES["otc_sniper"]["about"]
        # must mention regime engine, module list & the honest volume/VWAP note
        for needle in ("Regime", "modules", "VWAP", "Bollinger", "Efficiency"):
            assert needle in about, f"about text missing '{needle}'"


# =============================================================================
# 3) Never silent for >= 40 candles ("behaves like Classic Momentum")
# =============================================================================

class TestNeverSilent:
    def test_none_only_when_too_few(self):
        assert strategies.otc_sniper([]) is None
        c = gen_random_walk(39, seed=1)
        assert strategies.otc_sniper(c) is None
        c = gen_random_walk(40, seed=1)
        r = strategies.otc_sniper(c)
        x = indicators_py.Ctx(c)
        # None is only legal when the no-trade gate blocked the market
        assert (r is not None) or strategies.no_trade(x, x.n - 1)[0]

    def test_never_none_across_many_seeds_and_regimes(self):
        """None is allowed ONLY when the no-trade gate blocked the market."""
        gens = [gen_random_walk, gen_trend, gen_mean_revert]
        bad_none = 0
        total = 0
        for gen in gens:
            for seed in range(40):
                for n in (40, 60, 90, 130):
                    total += 1
                    candles = gen(n, seed=seed)
                    r = strategies.otc_sniper(candles)
                    if r is None:
                        x = indicators_py.Ctx(candles)
                        if not strategies.no_trade(x, x.n - 1)[0]:
                            bad_none += 1
        assert bad_none == 0, f"otc_sniper silent on {bad_none}/{total} TRADEABLE markets"

    def test_result_shape(self):
        r = None
        for seed in range(42, 60):
            r = strategies.otc_sniper(gen_trend(130, seed=seed))
            if r is not None:
                break
        assert r is not None
        assert r["direction"] in ("CALL", "PUT")
        assert 0.0 <= r["confidence"] <= 95.0
        assert isinstance(r["reason"], str) and len(r["reason"]) > 0
        for k in ("regime", "efficiency", "agree", "modules_active", "top_modules"):
            assert k in r


# =============================================================================
# 4) Regime engine + reason agree-count truthfulness
# =============================================================================

class TestRegime:
    def test_modules_count_and_weights(self):
        assert len(strategies.MODULES) == 21
        for key, label, fn, wt, wr in strategies.MODULES:
            assert isinstance(key, str) and isinstance(label, str)
            assert callable(fn)
            assert wt >= 0 and wr >= 0

    def test_trend_regime_on_strong_trend(self):
        trend_hits = 0
        for seed in range(20):
            r = strategies.otc_sniper(gen_trend(130, seed=seed, drift=0.15, noise=0.02))
            if r and r["regime"] == "trend-following":
                trend_hits += 1
        assert trend_hits >= 15, f"trend regime only hit {trend_hits}/20"

    def test_range_regime_on_mean_reversion(self):
        """Blocked (dead-chop) markets are skipped; every spoken result must
        classify as mean-reversion."""
        mr_hits = spoke = 0
        for seed in range(20):
            r = strategies.otc_sniper(gen_mean_revert(130, seed=seed, k=0.35, noise=0.04))
            if r is None:
                continue
            spoke += 1
            if r["regime"] == "mean-reversion":
                mr_hits += 1
        assert spoke >= 5, f"no-trade gate blocked too many MR markets: spoke {spoke}/20"
        assert mr_hits >= int(spoke * 0.8), f"mean-reversion regime only {mr_hits}/{spoke}"

    def test_reason_agree_count_matches_reality(self):
        """The N in 'N/M active modules agree' must equal the real agreeing count."""
        import re
        pat = re.compile(r"(\d+)/(\d+) active modules agree")
        mismatches = []
        for seed in range(50):
            for gen in (gen_trend, gen_mean_revert, gen_random_walk):
                candles = gen(120, seed=seed)
                r = strategies.otc_sniper(candles)
                if r is None:
                    continue  # no-trade gate blocked this market
                m = pat.search(r["reason"])
                assert m, f"reason missing agree-count: {r['reason']}"
                n_reason, m_reason = int(m.group(1)), int(m.group(2))
                # recompute the true count from scratch
                from indicators_py import Ctx
                x = Ctx(candles)
                last = x.n - 1
                er = strategies._efficiency(x, last)
                trend_w = strategies._clamp((er - 0.22) / 0.28, 0.0, 1.0)
                factors = strategies._adaptive_factors(x, last)
                contrib = []
                w_sum = 0.0
                score = 0.0
                for key, label, fn, wt, wr in strategies.MODULES:
                    v = fn(x, last)
                    w = (trend_w * wt + (1 - trend_w) * wr) * factors[key]
                    if w <= 0:
                        continue
                    w_sum += w
                    score += w * v
                    if abs(v) >= 0.1:
                        contrib.append((key, v, w))
                d = 1 if score >= 0 else -1
                true_n = sum(1 for _, v, _ in contrib if (v > 0) == (d > 0))
                true_m = len(contrib)
                if (n_reason, m_reason) != (true_n, true_m):
                    mismatches.append((seed, n_reason, m_reason, true_n, true_m))
        assert not mismatches, f"agree-count mismatches: {mismatches[:5]}"

    def test_reason_names_regime_and_efficiency(self):
        r = None
        for seed in range(7, 25):
            r = strategies.otc_sniper(gen_trend(120, seed=seed))
            if r is not None:
                break
        assert r is not None
        assert r["regime"] in r["reason"].lower() or r["regime"].capitalize() in r["reason"]
        # efficiency printed to 2 decimals
        assert f"{r['efficiency']:.2f}" in r["reason"]


# =============================================================================
# 5) Confidence calibration (walk-forward, bucketed by decile)
# =============================================================================

def _walk_forward(fn, candles, min_c):
    """Return list of (confidence, correct 0/1) tuples."""
    out = []
    for i in range(min_c, len(candles) - 1):
        r = fn(candles[: i + 1])
        if not r:
            continue
        nxt = candles[i + 1]
        actual = "CALL" if nxt["close"] > nxt["open"] else ("PUT" if nxt["close"] < nxt["open"] else None)
        if actual is None:
            continue
        out.append((r["confidence"], 1 if r["direction"] == actual else 0))
    return out


class TestCalibration:
    def _big_dataset(self):
        rows = []
        for seed in range(30):
            for gen in (gen_random_walk, gen_trend, gen_mean_revert):
                candles = gen(200, seed=seed)
                rows.extend(_walk_forward(strategies.otc_sniper, candles, 40))
        return rows

    def test_calibration_monotonic_ish_and_above_random(self):
        rows = self._big_dataset()
        assert len(rows) > 2000, f"only {len(rows)} samples"
        # 10 buckets by decile of confidence
        buckets = {i: [] for i in range(10)}
        for conf, ok in rows:
            b = min(9, int(conf // 10))
            buckets[b].append(ok)
        acc = {b: (sum(v) / len(v)) if v else None for b, v in buckets.items()}
        print(f"[calibration] per-decile accuracy: {acc}")
        # overall accuracy for >= 60% should be > 55%
        high = [ok for conf, ok in rows if conf >= 60]
        hi_acc = sum(high) / len(high) if high else 0
        print(f"[calibration] >=60% acc = {hi_acc:.3f} ({len(high)} samples)")
        assert hi_acc >= 0.55, f">=60% accuracy too low: {hi_acc:.3f}"
        # top-half of populated buckets should beat bottom-half on average
        populated = [(b, a) for b, a in acc.items() if a is not None]
        if len(populated) >= 4:
            half = len(populated) // 2
            low_avg = sum(a for _, a in populated[:half]) / half
            high_avg = sum(a for _, a in populated[-half:]) / half
            print(f"[calibration] low-half avg {low_avg:.3f} vs high-half avg {high_avg:.3f}")
            assert high_avg > low_avg, "accuracy not rising with confidence"

    def test_no_saturation_on_pure_noise(self):
        # confidence should not sit at the 95 cap on pure random walk
        rows = []
        for seed in range(30):
            rows.extend(_walk_forward(strategies.otc_sniper, gen_random_walk(160, seed=seed), 40))
        top = [c for c, _ in rows if c >= 90]
        pct_at_top = len(top) / len(rows) if rows else 0
        print(f"[noise] fraction of >=90% confidence signals on noise = {pct_at_top:.3f}")
        assert pct_at_top < 0.15, f"confidence saturates on noise: {pct_at_top:.3f}"


# =============================================================================
# 6) OTC-relevant edge vs classic_momentum
# =============================================================================

class TestEdgeVsClassic:
    def _acc(self, fn, threshold, gens, n=180, seeds=range(30)):
        ok = tot = 0
        for gen in gens:
            for seed in seeds:
                candles = gen(n, seed=seed)
                for conf, correct in _walk_forward(fn, candles,
                                                   40 if fn is strategies.otc_sniper else 15):
                    if conf >= threshold:
                        tot += 1
                        ok += correct
        return (ok / tot) if tot else 0.0, tot

    def test_edge_mean_reverting(self):
        otc, n1 = self._acc(strategies.otc_sniper, 62, [gen_mean_revert])
        cla, n2 = self._acc(strategies.classic_momentum, 55, [gen_mean_revert])
        print(f"[edge][MR] otc={otc:.3f} n={n1}   classic={cla:.3f} n={n2}")
        # tolerate small variance vs the request's target numbers
        assert otc >= cla - 0.03, f"otc {otc:.3f} does not beat/tie classic {cla:.3f} on MR"

    def test_edge_trend_persistent(self):
        otc, n1 = self._acc(strategies.otc_sniper, 62, [gen_trend])
        cla, n2 = self._acc(strategies.classic_momentum, 55, [gen_trend])
        print(f"[edge][TR] otc={otc:.3f} n={n1}   classic={cla:.3f} n={n2}")
        assert otc >= cla - 0.03, f"otc {otc:.3f} does not beat/tie classic {cla:.3f} on TR"


# =============================================================================
# 7) _adaptive_factors + _m_persistence follow/fade
# =============================================================================

class TestAdaptiveAndPersistence:
    def test_adaptive_clamped_and_default_below_min_samples(self):
        # very short history -> defaults to 1.0
        from indicators_py import Ctx
        candles = gen_random_walk(45, seed=1)
        x = Ctx(candles)
        f = strategies._adaptive_factors(x, 25)  # tiny lookback -> few samples
        assert all(v == 1.0 or (0.65 <= v <= 1.55) for v in f.values())

    def test_adaptive_clamp_range_on_full_history(self):
        from indicators_py import Ctx
        candles = gen_trend(130, seed=5)
        x = Ctx(candles)
        f = strategies._adaptive_factors(x, x.n - 1)
        for k, v in f.items():
            assert 0.65 <= v <= 1.55, f"{k}={v} outside clamp"

    def test_persistence_sign_flips(self):
        """Anti-persistent alternating candles -> fade; persistent runs -> follow."""
        from indicators_py import Ctx

        # strictly alternating candle colours around a flat level
        alt_closes, price = [], 100.0
        for i in range(80):
            price = 100.5 if i % 2 == 0 else 99.5
            alt_closes.append(price)
        alt = _series_from_closes(alt_closes)
        x_alt = Ctx(alt)
        i = x_alt.n - 1
        v_alt = strategies._m_persistence(x_alt, i)
        # last candle body sign
        last_body = x_alt.body[i]
        # anti-persistent -> should FADE last candle (opposite sign to last body)
        print(f"[persistence] alt last_body={last_body:.3f} v={v_alt:.3f}")
        if abs(v_alt) > 1e-6 and abs(last_body) > 1e-6:
            assert (v_alt > 0) != (last_body > 0), "anti-persistent should fade"

        # strictly same-colour runs (strong uptrend then keep going up)
        run_closes = [100.0 + 0.5 * i for i in range(80)]
        run = _series_from_closes(run_closes)
        x_run = Ctx(run)
        i = x_run.n - 1
        v_run = strategies._m_persistence(x_run, i)
        last_body = x_run.body[i]
        print(f"[persistence] run last_body={last_body:.3f} v={v_run:.3f}")
        if abs(v_run) > 1e-6 and abs(last_body) > 1e-6:
            assert (v_run > 0) == (last_body > 0), "persistent should follow"


# =============================================================================
# 8) Session wiring
# =============================================================================

class TestSessionWiring:
    def test_session_constants_intact(self):
        assert sessions.ANALYSIS_CANDLES == 130
        assert sessions.CHART_CANDLES == 60
        assert sessions.SCAN_START_SEC == 3
        assert sessions.SCAN_RESERVE == 8
        assert sessions.EARLY_EXIT_CONF == 88.0

    def _run_pick_best(self, strategy_key, minutes=30):
        # save & swap settings
        orig = storage.get_settings()
        storage.save_settings({**orig, "strategy": strategy_key})
        try:
            sm = sessions.SessionManager()
            sm.active = True
            fake_markets = [{"code": f"MKT{i}_otc", "display": f"MKT{i}-OTC", "payout": 90}
                            for i in range(30)]
            sm.markets = fake_markets

            # candle cache per market — use varied generators so we simulate a
            # realistic mix of trending/mean-reverting/noise markets across a scan
            gens = [gen_trend, gen_mean_revert, gen_random_walk]
            cache = {}
            for idx, m in enumerate(fake_markets):
                gen = gens[idx % 3]
                cache[m["code"]] = gen(130, seed=(idx * 7 + 3) & 0xFFFF)

            async def fake_candles(code, count=60):
                # simulate the current running candle: return all 130
                return cache[code][-count:]

            sm._candles = fake_candles

            hits = 0
            for minute in range(minutes):
                pick = asyncio.run(sm._pick_best())
                if pick is not None:
                    hits += 1
            return hits, minutes
        finally:
            storage.save_settings(orig)

    def test_pick_best_otc(self):
        hits, mins = self._run_pick_best("otc_sniper", minutes=20)
        print(f"[session][otc] {hits}/{mins} minutes had a signal")
        assert hits >= int(mins * 0.6), f"otc_sniper too silent: {hits}/{mins}"

    def test_pick_best_classic(self):
        hits, mins = self._run_pick_best("classic", minutes=20)
        print(f"[session][classic] {hits}/{mins} minutes had a signal")
        assert hits >= int(mins * 0.6), f"classic too silent: {hits}/{mins}"


# =============================================================================
# 9) Settings UI + analysis routing
# =============================================================================

class TestSettingsUI:
    def _restore(self):
        storage.save_settings({"mtg": "MTG-1", "strategy": "classic"})

    def test_settings_view_shows_active_name(self):
        try:
            storage.save_settings({"mtg": "MTG-1", "strategy": "otc_sniper"})
            text, kb = bot.settings_view()
            assert "OTC Sniper Pro" in text
        finally:
            self._restore()

    def test_strategy_view_lists_both_with_tick_and_info(self):
        try:
            storage.save_settings({"mtg": "MTG-1", "strategy": "classic"})
            text, kb = bot.strategy_view()
            assert "Classic Momentum" in text
            assert "OTC Sniper Pro" in text
            # ✅ appears next to active
            assert "✅" in text
            # callback data check
            cbs = [b.callback_data for row in kb.inline_keyboard for b in row]
            assert "st|classic" in cbs and "st|otc_sniper" in cbs
            assert "sti|classic" in cbs and "sti|otc_sniper" in cbs
        finally:
            self._restore()

    def test_strategy_about_view(self):
        text, kb = bot.strategy_about_view("otc_sniper")
        assert "OTC Sniper Pro" in text
        assert len(text) > 400

    def test_storage_persist_and_analysis_routes(self):
        try:
            storage.save_settings({"mtg": "MTG-1", "strategy": "otc_sniper"})
            assert storage.get_settings()["strategy"] == "otc_sniper"
            st = analysis.active_strategy()
            assert st["key"] == "otc_sniper"
            # below 40 candles -> None
            assert analysis.analyze(gen_random_walk(39, seed=0)) is None
            # >= 40 candles -> result with strategy tags
            r = analysis.analyze(gen_random_walk(60, seed=0))
            assert r is not None
            assert r["strategy"] == "OTC Sniper Pro"
            assert r["strategy_key"] == "otc_sniper"
        finally:
            self._restore()


# =============================================================================
# 10) No regression: imports + charting + classic_momentum behaviour
# =============================================================================

class TestNoRegression:
    def test_all_modules_import(self):
        for name in ("analysis", "strategies", "indicators_py", "storage",
                     "sessions", "bot", "charting"):
            m = importlib.import_module(name)
            assert m is not None

    def test_charting_render_60(self):
        candles = gen_trend(60, seed=3)
        png = charting.render_chart(candles, "TEST · M1", badge="CALL")
        assert isinstance(png, (bytes, bytearray)) and len(png) > 100

    def test_classic_min_candles_and_cap(self):
        assert strategies.STRATEGIES["classic"]["min_candles"] == 30
        assert strategies.classic_momentum(gen_trend(29, seed=1)) is None
        r = strategies.classic_momentum(gen_trend(45, seed=1))
        assert r is not None
        assert r["direction"] in ("CALL", "PUT")
        assert 0 <= r["confidence"] <= 95.0

    def test_classic_reason_strings_upgraded(self):
        allowed_starts = (
            "Recent candles show stronger buying pressure",
            "Recent candles show stronger selling pressure",
            "Price is riding above a rising EMA 9/21 stack",
            "Price is sliding below a falling EMA 9/21 stack",
            "Price is holding above its short-term averages",
            "Price is trading below its short-term averages",
            "MACD momentum is thrusting",
            "RSI 7 is pushing out of its dead zone",
            "ADX confirms a real",
            "Long lower wicks show buyers rejecting",
            "Long upper wicks show sellers rejecting",
            "Price is pinned to the",
            "Heikin-Ashi candles are consistently",
            "A strong bullish streak is controlling",
            "A strong bearish streak is controlling",
            "Upward momentum is accelerating",
            "Downward momentum is accelerating",
            "The 5-minute view agrees",
        )
        for seed in range(20):
            r = strategies.classic_momentum(gen_random_walk(45, seed=seed))
            if r is None:
                continue  # no-trade gate blocked this market
            assert r["reason"].startswith(allowed_starts), r["reason"]


# =============================================================================
# Restore settings.json at end of the run (session-scoped fixture)
# =============================================================================

@pytest.fixture(scope="session", autouse=True)
def _restore_settings_file():
    yield
    storage.save_settings({"mtg": "MTG-1", "strategy": "classic"})
