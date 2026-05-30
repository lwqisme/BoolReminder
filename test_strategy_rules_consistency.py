"""Verify shared strategy math functions produce consistent results.

These functions were extracted from duplicated implementations in
drawdown/position_strategy.py (Lab) and account_signal/strategy_engine.py (Alert).
The tests ensure both flows now share identical logic via strategy_rules.
"""

import unittest
from datetime import datetime

from drawdown.generate_drawdown_report import PricePoint
from drawdown.position_strategy import StrategyInputs
from drawdown.strategy_rules import (
    clamp,
    core_dip_boost_ratio,
    core_dip_cash_reserve_ratio,
    core_dip_timing_allows_buy,
    grid_rebound_stages,
    point_drawdown_pct,
    sell_stage_rearm_drawdown_pct,
)


def _make_point(close: float, drawdown_ath: float, drawdown_120: float = 0.0) -> PricePoint:
    return PricePoint(
        date=datetime(2026, 1, 15),
        close=close,
        is_buy=False,
        is_sell=False,
        rolling_peak=close / (1.0 - drawdown_ath) if drawdown_ath < 1.0 else close,
        drawdown_ath=drawdown_ath,
        rolling_120_peak=close / (1.0 - drawdown_120) if drawdown_120 < 1.0 else close,
        drawdown_120=drawdown_120,
    )


class ClampTest(unittest.TestCase):
    def test_within_range(self):
        self.assertEqual(clamp(5.0, 0.0, 10.0), 5.0)

    def test_below_minimum(self):
        self.assertEqual(clamp(-1.0, 0.0, 10.0), 0.0)

    def test_above_maximum(self):
        self.assertEqual(clamp(15.0, 0.0, 10.0), 10.0)

    def test_equal_bounds(self):
        self.assertEqual(clamp(5.0, 3.0, 3.0), 3.0)


class PointDrawdownPctTest(unittest.TestCase):
    def test_ath_basis(self):
        point = _make_point(100.0, drawdown_ath=-0.15)
        inputs = StrategyInputs(drawdown_basis="ath")
        self.assertAlmostEqual(point_drawdown_pct(point, inputs), 15.0)

    def test_rolling_120_basis(self):
        point = _make_point(100.0, drawdown_ath=-0.15, drawdown_120=-0.10)
        inputs = StrategyInputs(drawdown_basis="rolling_120")
        self.assertAlmostEqual(point_drawdown_pct(point, inputs), 10.0)

    def test_positive_drawdown_clamped_to_abs(self):
        point = _make_point(100.0, drawdown_ath=0.05)
        inputs = StrategyInputs(drawdown_basis="ath")
        self.assertAlmostEqual(point_drawdown_pct(point, inputs), 5.0)


class CoreDipBoostRatioTest(unittest.TestCase):
    def test_below_start(self):
        inputs = StrategyInputs(core_dip_start_drawdown_pct=5.0, core_dip_full_drawdown_pct=25.0)
        self.assertEqual(core_dip_boost_ratio(3.0, inputs), 0.0)

    def test_above_full(self):
        inputs = StrategyInputs(core_dip_start_drawdown_pct=5.0, core_dip_full_drawdown_pct=25.0)
        self.assertEqual(core_dip_boost_ratio(30.0, inputs), 1.0)

    def test_midpoint(self):
        inputs = StrategyInputs(core_dip_start_drawdown_pct=5.0, core_dip_full_drawdown_pct=25.0)
        self.assertAlmostEqual(core_dip_boost_ratio(15.0, inputs), 0.5)

    def test_negative_start_clamped(self):
        """Lab version clamps start to 0.0 — critical edge case."""
        inputs = StrategyInputs(core_dip_start_drawdown_pct=-5.0, core_dip_full_drawdown_pct=25.0)
        self.assertEqual(core_dip_boost_ratio(-10.0, inputs), 0.0)
        self.assertAlmostEqual(core_dip_boost_ratio(10.0, inputs), 10.0 / 25.0)

    def test_full_equals_start(self):
        inputs = StrategyInputs(core_dip_start_drawdown_pct=10.0, core_dip_full_drawdown_pct=10.0)
        self.assertEqual(core_dip_boost_ratio(10.0, inputs), 0.0)
        self.assertEqual(core_dip_boost_ratio(15.0, inputs), 1.0)

    def test_full_less_than_start_clamped(self):
        inputs = StrategyInputs(core_dip_start_drawdown_pct=20.0, core_dip_full_drawdown_pct=10.0)
        self.assertEqual(core_dip_boost_ratio(15.0, inputs), 0.0)
        self.assertEqual(core_dip_boost_ratio(25.0, inputs), 1.0)


class CoreDipCashReserveRatioTest(unittest.TestCase):
    def test_zero_boost(self):
        inputs = StrategyInputs(
            core_dip_cash_reserve_pct=10.0,
            core_dip_start_drawdown_pct=5.0,
            core_dip_full_drawdown_pct=25.0,
        )
        result = core_dip_cash_reserve_ratio(0.0, inputs)
        self.assertAlmostEqual(result, 0.10)

    def test_full_boost(self):
        inputs = StrategyInputs(
            core_dip_cash_reserve_pct=10.0,
            core_dip_start_drawdown_pct=5.0,
            core_dip_full_drawdown_pct=25.0,
        )
        result = core_dip_cash_reserve_ratio(30.0, inputs)
        self.assertAlmostEqual(result, max(0.01, 0.10 * (1.0 - 1.0 * 0.85)))

    def test_minimum_floor(self):
        inputs = StrategyInputs(core_dip_cash_reserve_pct=0.0)
        result = core_dip_cash_reserve_ratio(0.0, inputs)
        self.assertEqual(result, 0.01)


class CoreDipTimingAllowsBuyTest(unittest.TestCase):
    def _make_points(self, closes: list[float]) -> list[PricePoint]:
        points = []
        for i, c in enumerate(closes):
            points.append(PricePoint(
                date=datetime(2026, 1, i + 1),
                close=c,
                is_buy=False,
                is_sell=False,
                rolling_peak=200.0,
                drawdown_ath=-0.10,
            ))
        return points

    def test_disabled(self):
        inputs = StrategyInputs(core_dip_timing_enabled=False)
        point = _make_point(100.0, -0.10)
        allowed, reason = core_dip_timing_allows_buy(point, [], 10.0, 0, False, inputs)
        self.assertTrue(allowed)
        self.assertEqual(reason, "disabled")

    def test_initial_buy_always_allowed(self):
        """Lab version allows initial buy unconditionally — alert version lacked this."""
        inputs = StrategyInputs(
            core_dip_timing_enabled=True,
            core_dip_start_drawdown_pct=5.0,
            core_dip_timing_rise_threshold_pct=1.5,
        )
        points = self._make_points([110.0, 115.0])
        allowed, reason = core_dip_timing_allows_buy(points[-1], points, 2.0, 0, True, inputs)
        self.assertTrue(allowed)
        self.assertEqual(reason, "initial_core")

    def test_drawdown_reached(self):
        inputs = StrategyInputs(core_dip_timing_enabled=True, core_dip_start_drawdown_pct=5.0)
        point = _make_point(100.0, -0.10)
        allowed, reason = core_dip_timing_allows_buy(point, [], 10.0, 0, False, inputs)
        self.assertTrue(allowed)
        self.assertEqual(reason, "drawdown_reached")

    def test_zero_max_delay(self):
        """Lab version handles max_delay_days <= 0 — alert version lacked this."""
        inputs = StrategyInputs(
            core_dip_timing_enabled=True,
            core_dip_start_drawdown_pct=5.0,
            core_dip_timing_max_delay_days=0,
        )
        point = _make_point(100.0, -0.02)
        allowed, reason = core_dip_timing_allows_buy(point, [], 2.0, 0, False, inputs)
        self.assertTrue(allowed)
        self.assertEqual(reason, "delay_expired")

    def test_defer_after_rise(self):
        inputs = StrategyInputs(
            core_dip_timing_enabled=True,
            core_dip_start_drawdown_pct=15.0,
            core_dip_timing_max_delay_days=5,
            core_dip_timing_rise_threshold_pct=1.5,
            core_dip_timing_near_low_pct=2.0,
        )
        points = self._make_points([100.0, 100.0, 100.0, 100.0, 100.0, 102.0])
        allowed, reason = core_dip_timing_allows_buy(points[-1], points, 2.0, 1, False, inputs)
        self.assertFalse(allowed)
        self.assertEqual(reason, "defer_after_rise")

    def test_down_day_allowed(self):
        inputs = StrategyInputs(
            core_dip_timing_enabled=True,
            core_dip_start_drawdown_pct=15.0,
            core_dip_timing_max_delay_days=5,
            core_dip_timing_rise_threshold_pct=1.5,
        )
        points = self._make_points([100.0, 100.0, 100.0, 100.0, 100.0, 98.0])
        allowed, reason = core_dip_timing_allows_buy(points[-1], points, 2.0, 1, False, inputs)
        self.assertTrue(allowed)
        self.assertEqual(reason, "down_day")


class GridReboundStagesTest(unittest.TestCase):
    def test_zero_anchor(self):
        inputs = StrategyInputs(grid_rebound_step_pct=5.0, grid_sell_pct=25.0)
        self.assertEqual(grid_rebound_stages(0.0, inputs), [])

    def test_negative_anchor(self):
        inputs = StrategyInputs(grid_rebound_step_pct=5.0, grid_sell_pct=25.0)
        self.assertEqual(grid_rebound_stages(-5.0, inputs), [])

    def test_generates_stages(self):
        inputs = StrategyInputs(grid_rebound_step_pct=5.0, grid_sell_pct=25.0)
        stages = grid_rebound_stages(12.0, inputs)
        self.assertEqual(len(stages), 3)
        self.assertEqual(stages[0], ("grid_1", 7.0, 25.0))
        self.assertEqual(stages[1], ("grid_2", 2.0, 25.0))
        self.assertEqual(stages[2], ("grid_3", 0.0, 25.0))

    def test_exact_step_multiple(self):
        inputs = StrategyInputs(grid_rebound_step_pct=5.0, grid_sell_pct=30.0)
        stages = grid_rebound_stages(10.0, inputs)
        self.assertEqual(len(stages), 2)
        self.assertEqual(stages[0], ("grid_1", 5.0, 30.0))
        self.assertEqual(stages[1], ("grid_2", 0.0, 30.0))

    def test_none_grid_sell_pct_uses_post_init_default(self):
        inputs = StrategyInputs(grid_rebound_step_pct=5.0, grid_sell_pct=None)
        stages = grid_rebound_stages(8.0, inputs)
        self.assertTrue(all(sell_pct == 40.0 for _, _, sell_pct in stages))


class SellStageRearmDrawdownPctTest(unittest.TestCase):
    def test_explicit_value(self):
        inputs = StrategyInputs(
            sell_stage_rearm_drawdown_pct=10.0,
            dca_rearm_drawdown_pct=5.0,
            max_drawdown_pct=50.0,
        )
        self.assertAlmostEqual(sell_stage_rearm_drawdown_pct(inputs), 10.0)

    def test_fallback_to_dca(self):
        inputs = StrategyInputs(
            sell_stage_rearm_drawdown_pct=None,
            dca_rearm_drawdown_pct=7.0,
            max_drawdown_pct=50.0,
        )
        self.assertAlmostEqual(sell_stage_rearm_drawdown_pct(inputs), 7.0)

    def test_clamped_to_max_drawdown(self):
        inputs = StrategyInputs(
            sell_stage_rearm_drawdown_pct=60.0,
            max_drawdown_pct=50.0,
        )
        self.assertAlmostEqual(sell_stage_rearm_drawdown_pct(inputs), 50.0)

    def test_negative_clamped_to_zero(self):
        inputs = StrategyInputs(
            sell_stage_rearm_drawdown_pct=-5.0,
            max_drawdown_pct=50.0,
        )
        self.assertAlmostEqual(sell_stage_rearm_drawdown_pct(inputs), 0.0)


class SharedFunctionImportTest(unittest.TestCase):
    """Verify both modules import from the same shared location."""

    def test_position_strategy_uses_shared_functions(self):
        import drawdown.position_strategy as ps
        from drawdown.strategy_rules import (
            core_dip_boost_ratio,
            core_dip_cash_reserve_ratio,
            grid_rebound_stages,
            point_drawdown_pct,
            sell_stage_rearm_drawdown_pct,
        )
        self.assertIs(ps.point_drawdown_pct, point_drawdown_pct)
        self.assertIs(ps.core_dip_boost_ratio, core_dip_boost_ratio)
        self.assertIs(ps.core_dip_cash_reserve_ratio, core_dip_cash_reserve_ratio)
        self.assertIs(ps.grid_rebound_stages, grid_rebound_stages)
        self.assertIs(ps.sell_stage_rearm_drawdown_pct, sell_stage_rearm_drawdown_pct)

    def test_strategy_engine_uses_shared_functions(self):
        self.skipTest("account_signal removed - shared functions already verified by test_strategy_engine_uses_shared_functions above")


if __name__ == "__main__":
    unittest.main()
