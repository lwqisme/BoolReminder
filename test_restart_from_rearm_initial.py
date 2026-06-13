"""Test: restart_from_rearm anchors initial buy cycle to current drawdown.

Without this anchor, a first-day drawdown that already crosses many tranches
fires all of them at once and exhausts cash. The "restart_from_rearm" mode
should treat the very first buy cycle the same as post-sell rearm: peg the
anchor so that only the first tranche fires today, and subsequent tranches
need additional drawdown of `step` each.
"""

import unittest
from datetime import date, datetime, timedelta

from drawdown.generate_drawdown_report import PricePoint
from drawdown.position_strategy import (
    PositionLot,
    StrategyInputs,
    SymbolState,
    _execute_crossed_tranches,
    build_strategy_tranches,
)


def _point(close: float, drawdown_pct: float, day_offset: int = 0) -> PricePoint:
    dt = datetime(2021, 5, 27) + timedelta(days=day_offset)
    dd = -drawdown_pct / 100.0
    peak = close / (1.0 + dd)
    return PricePoint(
        date=dt, close=close, is_buy=False, is_sell=False,
        rolling_peak=peak, drawdown_ath=dd,
        rolling_120_peak=peak, drawdown_120=dd,
    )


def _state(cash: float) -> SymbolState:
    return SymbolState(
        symbol="TSM.US", name="TSM", weight=100.0,
        budget=cash, cash=cash,
        shares=0.0, last_price=0.0, last_value=0.0,
        lots=[], sell_marks=set(),
    )


class RestartFromRearmInitialAnchorTest(unittest.TestCase):
    """Reproduce the 5-year TSM bug: first-day drawdown=17.09%, step=1.21%, max_dd=37.5%."""

    def _inputs(self, mode: str) -> StrategyInputs:
        return StrategyInputs(
            initial_cash=50000.0,
            step_pct=1.21,
            equal_slice_allocation_pct=20.0,
            max_drawdown_pct=37.5,
            drawdown_basis="rolling_120",
            buy_rearm_mode=mode,
            trade_fee=1.0,
        )

    def test_cumulative_first_day_fires_all_crossed_tranches(self):
        """Baseline: cumulative mode crosses ~14 tranches → cash exhausts in 5 buys."""
        inputs = self._inputs("cumulative")
        tranches = build_strategy_tranches(inputs, "equal_slice")
        state = _state(50000.0)
        executed: dict[float, float] = {}
        trade_log: list[dict[str, object]] = []
        _execute_crossed_tranches(
            state, _point(106.72, 17.09), tranches, executed,
            inputs, trade_log, "equal_slice", "price_rise_grid",
        )
        buys = [t for t in trade_log if t["action"] == "buy"]
        # cumulative: should fire many tranches first day (existing behaviour)
        self.assertGreaterEqual(len(buys), 5,
            f"cumulative baseline: expected 5+ buys on first day, got {len(buys)}")

    def test_restart_from_rearm_first_day_fires_only_first_tranche(self):
        """Fix: restart_from_rearm anchors initial cycle → only first tranche fires."""
        inputs = self._inputs("restart_from_rearm")
        tranches = build_strategy_tranches(inputs, "equal_slice")
        state = _state(50000.0)
        executed: dict[float, float] = {}
        trade_log: list[dict[str, object]] = []
        _execute_crossed_tranches(
            state, _point(106.72, 17.09), tranches, executed,
            inputs, trade_log, "equal_slice", "price_rise_grid",
        )
        buys = [t for t in trade_log if t["action"] == "buy"]
        self.assertEqual(len(buys), 1,
            f"restart_from_rearm: expected exactly 1 buy on first day, got {len(buys)}: {buys}")
        # anchor should now be set so next buy needs another step of drawdown
        self.assertIsNotNone(state.buy_rearm_anchor_drawdown_pct,
            "anchor should be set after first buy in restart_from_rearm mode")

    def test_restart_from_rearm_next_tranche_needs_one_step_more(self):
        """After first-day buy, next tranche fires when drawdown rises by step."""
        inputs = self._inputs("restart_from_rearm")
        tranches = build_strategy_tranches(inputs, "equal_slice")
        state = _state(50000.0)
        executed: dict[float, float] = {}
        trade_log: list[dict[str, object]] = []
        # day 0: drawdown 17.09 → fire first tranche
        _execute_crossed_tranches(
            state, _point(106.72, 17.09), tranches, executed,
            inputs, trade_log, "equal_slice", "price_rise_grid",
        )
        n_after_day0 = len([t for t in trade_log if t["action"] == "buy"])

        # day 1: drawdown 17.50 (less than +1.21 step) → no new buy
        _execute_crossed_tranches(
            state, _point(106.0, 17.50, 1), tranches, executed,
            inputs, trade_log, "equal_slice", "price_rise_grid",
        )
        self.assertEqual(
            len([t for t in trade_log if t["action"] == "buy"]), n_after_day0,
            "drawdown only +0.41 (< step 1.21) should not trigger next tranche",
        )

        # day 2: drawdown 18.50 (>= 17.09 + 1.21) → second tranche fires
        _execute_crossed_tranches(
            state, _point(105.0, 18.50, 2), tranches, executed,
            inputs, trade_log, "equal_slice", "price_rise_grid",
        )
        self.assertEqual(
            len([t for t in trade_log if t["action"] == "buy"]), n_after_day0 + 1,
            "drawdown 18.50 (>= anchor + step) should fire exactly one more tranche",
        )

    def test_restart_from_rearm_first_day_low_drawdown_unchanged(self):
        """If first-day drawdown < step, behaviour matches cumulative (no buy, no anchor)."""
        inputs = self._inputs("restart_from_rearm")
        tranches = build_strategy_tranches(inputs, "equal_slice")
        state = _state(50000.0)
        executed: dict[float, float] = {}
        trade_log: list[dict[str, object]] = []
        _execute_crossed_tranches(
            state, _point(150.0, 0.5), tranches, executed,
            inputs, trade_log, "equal_slice", "price_rise_grid",
        )
        self.assertEqual(len([t for t in trade_log if t["action"] == "buy"]), 0)


if __name__ == "__main__":
    unittest.main()
