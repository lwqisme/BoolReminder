"""Test: when already 60% invested, which tranches generate buy signals vs show as covered.

Validates _mark_consumed_tranches_from_position for pyramid_3, equal_slice,
and linear_weighted_slice strategies.
"""

import math
import unittest
from dataclasses import replace
from datetime import date, datetime, timedelta

from drawdown.position_strategy import (
    PortfolioTarget,
    PositionLot,
    StrategyInputs,
    StrategyTranche,
    SymbolState,
    _execute_crossed_tranches,
    _mark_consumed_tranches_from_position,
    _position_value_usd,
    _price_usd,
    _rearm_buy_tranches_after_repair,
    build_strategy_tranches,
)


def _make_price_point(close: float, drawdown_ath: float, day_offset: int = 0):
    """Minimal PricePoint-like object for drawdown testing."""
    from drawdown.generate_drawdown_report import PricePoint
    dt = datetime(2026, 1, 10) + timedelta(days=day_offset)
    peak = close / (1.0 + drawdown_ath) if drawdown_ath > -1.0 else close * 2
    return PricePoint(
        date=dt, close=close, is_buy=False, is_sell=False,
        rolling_peak=peak, drawdown_ath=drawdown_ath,
    )


def _make_state(cash: float, shares: float, price: float) -> SymbolState:
    """Create a SymbolState with given cash, shares, and last price."""
    state = SymbolState(
        symbol="TEST.US", name="TEST", weight=100.0,
        budget=cash + shares * price, cash=cash,
        shares=shares, last_price=price,
        last_value=shares * price,
        lots=[],
        sell_marks=set(),
    )
    # Add dummy lots to make avg_cost/lot logic happy
    if shares > 0:
        state.lots = [
            PositionLot(
                threshold_pct=6.0, buy_drawdown_pct=10.0,
                buy_price_usd=price, buy_date=date(2025, 1, 1),
                initial_shares=shares, remaining_shares=shares,
            )
        ]
    state.max_shares = shares
    return state


class ConsumedTrancheSignalTest(unittest.TestCase):
    """When 60% of portfolio is invested, verify which tranches fire."""

    def setUp(self):
        self.inputs = StrategyInputs(
            initial_cash=10000.0,
            step_pct=6.22,
            max_drawdown_pct=50.0,
            equal_slice_allocation_pct=6.22,
        )
        self.cash = 4000.0
        self.price = 200.0
        self.shares = 30.0  # market value = 6000, total = 10000, 60% invested

    # ── helpers ──────────────────────────────────────────────────────

    def _build_tranches(self, strategy: str) -> list[StrategyTranche]:
        return build_strategy_tranches(self.inputs, strategy)

    def _run_consumed_and_check(
        self, strategy: str, drawdown: float
    ) -> tuple[list[str], list[str]]:
        """Mark consumed tranches, then simulate crossing a drawdown threshold.

        Returns (fired_thresholds, suppressed_thresholds) as label strings.
        """
        tranches = self._build_tranches(strategy)
        state = _make_state(self.cash, self.shares, self.price)
        point = _make_price_point(self.price, drawdown / -100.0)  # negative = below ATH

        executed: dict[float, float] = {}
        _mark_consumed_tranches_from_position(state, tranches, executed)

        trade_log: list[dict[str, object]] = []
        _execute_crossed_tranches(
            state, point, tranches, executed, self.inputs,
            trade_log, strategy, "none",
        )

        fired = []
        for t in trade_log:
            if t["action"] == "buy":
                base_thr = t.get("base_threshold_pct", 0)
                alloc = t.get("allocation_pct", 0)
                fired.append(f"{base_thr:.2f}%/{alloc:.2f}%(${t['gross_amount']:.0f})")

        suppressed = []
        for t in sorted(tranches, key=lambda x: x.threshold_pct):
            key = round(t.threshold_pct, 8)
            if key in executed and executed[key] > 0 and not any(
                abs(tl.get("base_threshold_pct", 0) - t.threshold_pct) < 0.01
                for tl in trade_log if tl["action"] == "buy"
            ):
                suppressed.append(f"{t.threshold_pct:.2f}%/{t.allocation_pct:.2f}%")

        return fired, suppressed

    # ── pyramid_3 ────────────────────────────────────────────────────

    def test_pyramid_3_60pct_all_covered_no_buy(self):
        """Pyramid: 60% invested → first 2 tranches (cum 50%) covered.  3rd (cum 100%) fires."""
        tranches = self._build_tranches("pyramid_3")
        self.assertEqual(len(tranches), 3)
        # Cumulative: after 1st=20%, after 2nd=50%, after 3rd=100%
        # 60% > 50% → first two consumed.  60% < 100% → third fires.
        fired, suppressed = self._run_consumed_and_check("pyramid_3", drawdown=50.0)
        self.assertEqual(len(fired), 1, f"Expected 1 buy (50% tranche), got {fired}")
        self.assertIn("50.00%", fired[0])
        suppressed_thresholds = [s.split("/")[0] for s in suppressed]
        self.assertIn("10.00%", suppressed_thresholds)
        self.assertIn("25.00%", suppressed_thresholds)

    def test_pyramid_3_60pct_partial_cross(self):
        """Only 2nd tranche threshold crossed (drawdown=25%), should be suppressed."""
        fired, suppressed = self._run_consumed_and_check("pyramid_3", drawdown=25.0)
        self.assertEqual(len(fired), 0,
                         f"25% drawdown should NOT fire: first two tranches already covered. Got {fired}")
        self.assertGreaterEqual(len(suppressed), 2)

    # ── linear_weighted_slice ────────────────────────────────────────

    def test_linear_weighted_60pct_partial_coverage(self):
        """Linear weighted: 60% invested → cum after 6 slices = 46.7% → 6 consumed, 3 fire."""
        tranches = self._build_tranches("linear_weighted_slice")
        self.assertEqual(len(tranches), 9)
        fired, suppressed = self._run_consumed_and_check(
            "linear_weighted_slice", drawdown=50.0
        )
        # Cumulative allocs: 2.22,6.67,13.33,22.22,33.33,46.67,62.22,80.0,100.0
        # 60% >= 46.67% → 6 consumed.  60% < 62.22% → slices 7-9 fire.
        self.assertEqual(len(fired), 3,
                         f"60% covers 6 tranches (cum 46.7% < 60%), 3 should fire. Got: {fired}")
        self.assertEqual(len(suppressed), 6,
                         f"6 tranches should be suppressed. Got: {len(suppressed)}")

    def test_linear_weighted_delta_fill_suppressed(self):
        """With 60% invested (cum covers first 6 slices), executed[$target] → no top-up buy for those 6."""
        tranches = self._build_tranches("linear_weighted_slice")
        state = _make_state(self.cash, self.shares, self.price)
        point = _make_price_point(self.price, -0.50)  # 50% drawdown

        executed: dict[float, float] = {}
        _mark_consumed_tranches_from_position(state, tranches, executed)

        # First 6 tranches consumed (cumulative alloc 46.67% < 60%)
        # Slice 7 cum=62.22% > 60% → NOT consumed
        total = self.cash + self.shares * self.price
        cumulative_alloc = 0.0
        consumed_count = 0
        for t in sorted(tranches, key=lambda x: x.threshold_pct):
            key = round(t.threshold_pct, 8)
            cumulative_alloc += t.allocation_pct / 100.0
            if cumulative_alloc - 1e-9 <= 60.0 / 100.0:
                consumed_count += 1
                expected = total * t.allocation_pct / 100.0
                actual = executed.get(key, 0.0)
                self.assertAlmostEqual(actual, expected, delta=0.01,
                    msg=f"Tranche {t.threshold_pct}%: executed={actual:.2f} != target={expected:.2f}")
            else:
                self.assertNotIn(key, executed,
                    f"Tranche {t.threshold_pct}% should NOT be consumed (cum={cumulative_alloc*100:.1f}% > 60%)")
        self.assertEqual(consumed_count, 6)

        # Run execute — consumed tranches produce no buys, remaining 3 fire
        trade_log: list[dict[str, object]] = []
        _execute_crossed_tranches(
            state, point, tranches, executed, self.inputs,
            trade_log, "linear_weighted_slice", "none",
        )
        self.assertEqual(len(trade_log), 3,
                         f"6 consumed → 3 buys expected. Got: {len(trade_log)}")

    # ── equal_slice ──────────────────────────────────────────────────

    def test_equal_slice_60pct_all_covered(self):
        """Equal slice: 60% invested → covers first N tranches where N * alloc ≤ 60%."""
        tranches = self._build_tranches("equal_slice")
        self.assertEqual(len(tranches), 9)
        fired, suppressed = self._run_consumed_and_check("equal_slice", drawdown=50.0)
        # alloc=6.22% per slice, 60% invested → 9 * 6.22 = 55.98% < 60% → all covered
        self.assertEqual(len(fired), 0,
                         f"60% invested should cover all equal slices. Got: {fired}")
        self.assertEqual(len(suppressed), len(tranches))

    def test_equal_slice_25pct_partial_coverage(self):
        """25% invested → 4 of 9 slices covered (4*6.22=24.88 < 25 < 5*6.22=31.10)."""
        cash = 7500.0
        shares = 12.5  # market=2500, total=10000, 25% invested
        state = _make_state(cash, shares, self.price)
        tranches = self._build_tranches("equal_slice")
        point = _make_price_point(self.price, -0.50)

        executed: dict[float, float] = {}
        _mark_consumed_tranches_from_position(state, tranches, executed)

        trade_log: list[dict[str, object]] = []
        _execute_crossed_tranches(
            state, point, tranches, executed, self.inputs,
            trade_log, "equal_slice", "none",
        )

        # First 4 slices covered → suppressed.  Slices 5-9 should fire.
        fired_thresholds = [t.get("base_threshold_pct", 0) for t in trade_log if t["action"] == "buy"]
        # equal_slice thresholds: 6.22, 12.44, 18.66, 24.88, 31.10, ...
        covered = {6.22, 12.44, 18.66, 24.88}
        for ft in fired_thresholds:
            self.assertNotIn(round(ft, 2), covered,
                             f"Threshold {ft:.2f}% should be covered (25% invested), not bought")
        remaining_thresholds = {31.10, 37.32, 43.54, 49.76, 50.0}
        fired_set = {round(t, 2) for t in fired_thresholds}
        for rt in remaining_thresholds:
            self.assertIn(rt, fired_set,
                          f"Threshold {rt}% should fire (not covered), but didn't. Fired: {fired_set}")

    # ── edge cases ───────────────────────────────────────────────────

    def test_zero_shares_nothing_consumed(self):
        """No shares held → no tranches consumed, all fire normally."""
        state = _make_state(10000.0, 0.0, self.price)
        tranches = self._build_tranches("linear_weighted_slice")
        executed: dict[float, float] = {}
        _mark_consumed_tranches_from_position(state, tranches, executed)
        self.assertEqual(len(executed), 0,
                         "No shares → no tranches should be marked consumed")

    def test_fully_invested_all_consumed(self):
        """100% invested → all tranches covered for all strategies."""
        for strategy in ["pyramid_3", "equal_slice", "linear_weighted_slice"]:
            with self.subTest(strategy=strategy):
                state = _make_state(0.0, 50.0, self.price)  # 100% invested
                tranches = self._build_tranches(strategy)
                executed: dict[float, float] = {}
                _mark_consumed_tranches_from_position(state, tranches, executed)
                self.assertEqual(len(executed), len(tranches),
                                 f"{strategy}: all {len(tranches)} tranches should be consumed")

    # ── rearm + position respect ─────────────────────────────────────

    def test_rearm_after_repair_respects_position(self):
        """After repair rearm (drawdown ≤ 0.5%), position is re-evaluated."""
        state = _make_state(self.cash, self.shares, self.price)
        tranches = self._build_tranches("linear_weighted_slice")
        executed: dict[float, float] = {}
        # Simulate: engine built up position, then rearm at ATH
        point = _make_price_point(self.price, -0.003)  # 0.3% drawdown
        _rearm_buy_tranches_after_repair(state, point, executed,
                                          StrategyInputs(), tranches)
        # 60% invested → first 6 tranches should be consumed
        self.assertGreater(len(executed), 0,
                           "After rearm near ATH, consumed tranches should be re-marked")
        # Run execute — consumed tranches should NOT fire
        trade_log: list[dict[str, object]] = []
        _execute_crossed_tranches(
            state, point, tranches, executed, self.inputs,
            trade_log, "linear_weighted_slice", "none",
        )
        # At 0.3% drawdown, no thresholds crossed anyway
        self.assertEqual(len(trade_log), 0)


if __name__ == "__main__":
    unittest.main()
