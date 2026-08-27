"""Offline unit tests for the per-trade-percentage martingale recovery model.

Covers:
  * sessions.compute_delta  (module: sessions)
  * the deficit/pnl chain exactly as sessions._handle_result applies it
  * SessionManager deficit instance state + reset on start()
  * signal record fields (pnl_delta / pnl_total / deficit_after)
  * messages.result_caption + SessionManager.partial_text rendering of total_pct

No Telegram / Quotex network calls: everything is stubbed.
"""
import asyncio

import pytest

import messages
import sessions
from sessions import SessionManager, compute_delta


# --------------------------------------------------------------------------
# compute_delta
# --------------------------------------------------------------------------
class TestComputeDelta:
    def test_legacy_sanity_asserts_still_hold(self):
        assert compute_delta(0, 1, "LOSS") == -3
        assert compute_delta(-3, 1, "WIN") == 6
        assert compute_delta(-3, 1, "WIN_MTG") == 12

    @pytest.mark.parametrize("deficit,expected", [
        (0.0, {"WIN": 1.0, "WIN_MTG": 1.0, "LOSS": -3.0}),
        (3.0, {"WIN": 6.0, "WIN_MTG": 12.0, "LOSS": -24.0}),
        (27.0, {"WIN": 54.0, "WIN_MTG": 108.0, "LOSS": -216.0}),
    ])
    def test_p1_all_results(self, deficit, expected):
        for result, exp in expected.items():
            got = compute_delta(-deficit, 1.0, result)
            assert got == pytest.approx(exp), f"deficit={deficit} {result}: {got} != {exp}"

    @pytest.mark.parametrize("p", [0.5, 1.0, 2.5])
    def test_scales_linearly_with_per_trade_pct(self, p):
        # no deficit: S = P, M = 2P
        assert compute_delta(0, p, "WIN") == pytest.approx(p)
        assert compute_delta(0, p, "WIN_MTG") == pytest.approx(p)
        assert compute_delta(0, p, "LOSS") == pytest.approx(-3 * p)
        # deficit 3P (the state after one loss of 3P)
        d = 3 * p
        assert compute_delta(-d, p, "WIN") == pytest.approx(6 * p)
        assert compute_delta(-d, p, "WIN_MTG") == pytest.approx(12 * p)
        assert compute_delta(-d, p, "LOSS") == pytest.approx(-24 * p)

    def test_wintmg_math_matches_documented_formula(self):
        # WIN_MTG = -S + M with M = 2*(deficit + S)
        for deficit in (0.0, 3.0, 27.0, 243.0):
            s = 1.0 if deficit <= 0 else 2 * deficit
            m = 2 * (deficit + s)
            assert compute_delta(-deficit, 1.0, "WIN_MTG") == pytest.approx(-s + m)
            assert compute_delta(-deficit, 1.0, "LOSS") == pytest.approx(-s - m)

    def test_positive_or_zero_neg_deficit_is_treated_as_no_deficit(self):
        assert compute_delta(0.0, 1.0, "LOSS") == -3
        assert compute_delta(5.0, 1.0, "LOSS") == -3  # defensive: wrong sign ignored


# --------------------------------------------------------------------------
# the chain, applied exactly like sessions.py does it
# --------------------------------------------------------------------------
def run_chain(results, per_trade=1.0):
    pnl = 0.0
    deficit = 0.0
    steps = []
    for result in results:
        delta = compute_delta(-deficit, per_trade, result)
        pnl += delta
        deficit = (deficit - delta) if result == "LOSS" else 0.0
        steps.append((delta, pnl, deficit))
    return pnl, deficit, steps


class TestMartingaleChain:
    def test_single_loss(self):
        pnl, deficit, _ = run_chain(["LOSS"])
        assert (pnl, deficit) == (-3.0, 3.0)

    def test_loss_then_win(self):
        pnl, deficit, _ = run_chain(["LOSS", "WIN"])
        assert pnl == pytest.approx(3.0)
        assert deficit == 0.0

    def test_loss_then_mtg_win(self):
        pnl, deficit, _ = run_chain(["LOSS", "WIN_MTG"])
        assert pnl == pytest.approx(9.0)
        assert deficit == 0.0

    def test_two_losses_then_win(self):
        pnl, deficit, steps = run_chain(["LOSS", "LOSS", "WIN"])
        assert steps[1][1] == pytest.approx(-27.0)   # pnl after 2 losses
        assert steps[1][2] == pytest.approx(27.0)    # deficit after 2 losses
        assert steps[2][0] == pytest.approx(54.0)    # winning stake = 2*27
        assert pnl == pytest.approx(27.0)
        assert deficit == 0.0

    def test_three_losses(self):
        pnl, deficit, _ = run_chain(["LOSS", "LOSS", "LOSS"])
        assert pnl == pytest.approx(-243.0)
        assert deficit == pytest.approx(243.0)

    def test_win_then_loss_then_win_bug_case(self):
        """Regression for the reported bug: after WIN(+1) then LOSS(-3) the
        deficit must be 3 (the lost amount) and NOT 2 (the net P&L)."""
        pnl, deficit, steps = run_chain(["WIN", "LOSS", "WIN"])
        assert steps[0][1:] == (1.0, 0.0)
        assert steps[1][1] == pytest.approx(-2.0)    # net pnl is -2 ...
        assert steps[1][2] == pytest.approx(3.0)     # ... but deficit is 3
        assert steps[2][0] == pytest.approx(6.0)     # so stake is 6
        assert pnl == pytest.approx(4.0)
        assert deficit == 0.0

    def test_every_loss_accumulates_not_just_one(self):
        _, deficit, steps = run_chain(["LOSS", "WIN", "LOSS", "LOSS"])
        # after WIN deficit clears, then two fresh losses accumulate again
        assert steps[1][2] == 0.0
        assert steps[2][2] == pytest.approx(3.0)
        assert steps[3][2] == pytest.approx(27.0)
        assert deficit == pytest.approx(27.0)

    def test_chain_scales_with_per_trade_pct(self):
        for p in (0.5, 2.5):
            pnl, deficit, _ = run_chain(["LOSS", "LOSS", "WIN"], per_trade=p)
            assert pnl == pytest.approx(27.0 * p)
            assert deficit == 0.0


# --------------------------------------------------------------------------
# SessionManager instance state + reset
# --------------------------------------------------------------------------
class _StubBot:
    async def send_message(self, *a, **k):
        return type("M", (), {"entities": None, "caption_entities": None})()

    async def send_photo(self, *a, **k):
        return type("M", (), {"entities": None, "caption_entities": None})()


class _StubQx:
    async def get_candles_1m(self, code, n):
        return []


class TestSessionManagerState:
    def test_init_defaults(self):
        sm = SessionManager()
        assert sm.deficit == 0.0
        assert sm.pnl == 0.0
        assert sm.signals == []

    async def test_start_resets_deficit_pnl_signals(self):
        sm = SessionManager()
        sm.deficit = 243.0
        sm.pnl = -243.0
        sm.signals = [{"result": "LOSS"}]
        try:
            await sm.start(_StubBot(), _StubQx(), None, ["EURUSD"], -100, "chan")
            assert sm.deficit == 0.0, f"deficit leaked into new session: {sm.deficit}"
            assert sm.pnl == 0.0, f"pnl leaked into new session: {sm.pnl}"
            assert sm.signals == []
            assert sm.active is True
        finally:
            sm.close()
            await asyncio.sleep(0)

    async def test_second_session_does_not_inherit_state(self):
        sm = SessionManager()
        try:
            await sm.start(_StubBot(), _StubQx(), None, ["EURUSD"], -100, "chan")
            first_id = sm.session_id
            # simulate a losing session
            sm.pnl, sm.deficit = -27.0, 27.0
            sm.signals = [{"result": "LOSS"}, {"result": "LOSS"}]
            await sm.start(_StubBot(), _StubQx(), None, ["EURUSD"], -100, "chan")
            assert (sm.pnl, sm.deficit, sm.signals) == (0.0, 0.0, [])
            assert sm.session_id is not None and first_id is not None
        finally:
            sm.close()
            await asyncio.sleep(0)


# --------------------------------------------------------------------------
# signal record fields
# --------------------------------------------------------------------------
class TestSignalRecordFields:
    def test_record_contains_pnl_and_deficit_fields(self):
        import inspect
        src = inspect.getsource(sessions.SessionManager._run_signal) \
            if hasattr(sessions.SessionManager, "_run_signal") \
            else inspect.getsource(sessions.SessionManager)
        for key in ('"pnl_delta"', '"pnl_total"', '"deficit_after"'):
            assert key in src, f"signal record missing {key}"
        assert "total_pct=self.pnl" in src, "result_caption/chart stats must use self.pnl"


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------
class TestRendering:
    @pytest.mark.parametrize("value,expected", [
        (27.0, "+27"), (-243.0, "-243"), (3.0, "+3"), (0.0, "+0"),
        (4.0, "+4"), (1.5, "+1.5"), (-2.0, "-2"),
    ])
    def test_fmt_pct(self, value, expected):
        assert messages._fmt_pct(value) == expected

    def test_result_caption_gain(self):
        cap = messages.result_caption("EUR/USD", "CALL", "12:00", "WIN",
                                      wins=1, losses=2, total_pct=27.0)
        m = messages.mono
        assert f"{m('+27')}%" in cap
        assert m("GAIN") in cap and m("Total") in cap

    def test_result_caption_loss(self):
        cap = messages.result_caption("EUR/USD", "PUT", "12:00", "LOSS",
                                      wins=0, losses=3, total_pct=-243.0)
        m = messages.mono
        assert f"{m('-243')}%" in cap
        assert cap.rstrip().endswith(m("LOSS"))

    def test_result_caption_templates_unchanged(self):
        cap = messages.result_caption("EUR/USD", "PUT", "12:00", "LOSS",
                                      wins=2, losses=1, total_pct=-3.0)
        assert "\U0001f3af" in cap          # result emoji
        assert "\U0001f7e5" in cap          # PUT
        assert "\U0001f44d" in cap          # win row
        assert "|" in cap.split("\U0001f44d")[1]
        assert f"({messages.mono('67')}%)" in cap

    def test_signal_caption_template_unchanged(self):
        cap = messages.signal_caption("EUR/USD", "PUT", "12:00", 90, "r", "@x")
        assert "\U0001f525" in cap          # brand fire
        assert "\U0001f7e5" in cap          # PUT

    def test_partial_text_uses_session_pnl(self):
        sm = SessionManager()
        sm.pnl = 27.0
        sm.signals = [
            {"result": "LOSS", "code": "EURUSD", "entry": "12:00", "direction": "PUT"},
            {"result": "LOSS", "code": "EURUSD", "entry": "12:05", "direction": "CALL"},
            {"result": "WIN", "code": "EURUSD", "entry": "12:10", "direction": "CALL"},
        ]
        text = sm.partial_text()
        m = messages.mono
        assert f"{m('+27')}%" in text
        assert m("GAIN") in text
        assert "\U0001f5d3" in text         # date
        assert "\u2620\ufe0f" in text       # Total
        assert "\u2696\ufe0f >" in text     # scale before percentage

    def test_partial_text_negative_total(self):
        sm = SessionManager()
        sm.pnl = -243.0
        sm.signals = [{"result": "LOSS", "code": "EURUSD", "entry": "12:00",
                       "direction": "PUT"}]
        text = sm.partial_text()
        m = messages.mono
        assert f"{m('-243')}%" in text
        assert m("LOSS") in text

    def test_partial_text_none_without_signals(self):
        assert SessionManager().partial_text() is None
