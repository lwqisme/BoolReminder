import unittest
from datetime import date, datetime

from drawdown.generate_drawdown_report import build_price_points_from_series, PricePoint
from drawdown.position_strategy import (
    PortfolioTarget,
    PositionLot,
    StrategyInputs,
    SymbolState,
    _slice_efficiency,
    _weighted_slice_metric,
    simulate_portfolio,
)


def _make_price_points(prices: list[float]) -> list[PricePoint]:
    return build_price_points_from_series(
        [(datetime(2025, 1, day + 1), price) for day, price in enumerate(prices)]
    )


class SliceEfficiencyTest(unittest.TestCase):
    def test_perfect_sell_at_peak(self):
        """Buy at low, sell at high → both efficiencies = 1.0."""
        points = _make_price_points([80, 100, 140])
        state = SymbolState(symbol="TEST.US", name="Test", weight=100, budget=1000, cash=1000, price_history=points)
        lot = PositionLot(
            threshold_pct=20, buy_drawdown_pct=0, buy_price_usd=80,
            buy_date=date(2025, 1, 1), initial_shares=10, remaining_shares=10,
        )
        inputs = StrategyInputs()
        result = _slice_efficiency(state, points[2], inputs, lot, 10)
        self.assertAlmostEqual(result["price_spread_efficiency"], 1.0)
        self.assertAlmostEqual(result["sell_timing_efficiency"], 1.0)

    def test_flat_price_zero_amplitude(self):
        """All prices equal → amplitude=0 → both efficiencies = 0."""
        points = _make_price_points([100, 100, 100])
        state = SymbolState(symbol="TEST.US", name="Test", weight=100, budget=1000, cash=1000, price_history=points)
        lot = PositionLot(
            threshold_pct=20, buy_drawdown_pct=0, buy_price_usd=100,
            buy_date=date(2025, 1, 1), initial_shares=10, remaining_shares=10,
        )
        inputs = StrategyInputs()
        result = _slice_efficiency(state, points[2], inputs, lot, 10)
        self.assertAlmostEqual(result["price_spread_efficiency"], 0.0)
        self.assertAlmostEqual(result["sell_timing_efficiency"], 0.0)

    def test_negative_spread_sell_below_buy(self):
        """Sell below buy price → both efficiencies negative."""
        points = _make_price_points([100, 110, 90])
        state = SymbolState(symbol="TEST.US", name="Test", weight=100, budget=1000, cash=1000, price_history=points)
        lot = PositionLot(
            threshold_pct=20, buy_drawdown_pct=0, buy_price_usd=100,
            buy_date=date(2025, 1, 1), initial_shares=10, remaining_shares=10,
        )
        inputs = StrategyInputs()
        result = _slice_efficiency(state, points[2], inputs, lot, 10)
        self.assertAlmostEqual(result["price_spread_efficiency"], -10.0 / 20.0)
        self.assertAlmostEqual(result["sell_timing_efficiency"], -10.0 / 10.0)

    def test_partial_capture_sell_before_peak(self):
        """Buy near low, sell before peak → spread < amplitude, timing < 1."""
        points = _make_price_points([90, 80, 100, 105])
        state = SymbolState(symbol="TEST.US", name="Test", weight=100, budget=1000, cash=1000, price_history=points)
        lot = PositionLot(
            threshold_pct=20, buy_drawdown_pct=0, buy_price_usd=90,
            buy_date=date(2025, 1, 1), initial_shares=10, remaining_shares=10,
        )
        inputs = StrategyInputs()
        # buy at 90 (day1), dip to 80 (day2), rise to 100 (day3-sell), peak 105 (day4)
        # but sell at day3, so holding period is day1-3: 90, 80, 100
        result = _slice_efficiency(state, points[2], inputs, lot, 10)
        # period_high=100, period_low=80, spread=100-90=10, amplitude=100-80=20
        # price_spread_efficiency = 10/20 = 0.5
        # sell_timing_efficiency = 10/(100-90) = 10/10 = 1.0
        self.assertAlmostEqual(result["price_spread_efficiency"], 0.5)
        self.assertAlmostEqual(result["sell_timing_efficiency"], 1.0)


class WeightedSliceMetricTest(unittest.TestCase):
    def test_single_slice(self):
        self.assertAlmostEqual(
            _weighted_slice_metric(
                [{"shares": 10, "price_spread_efficiency": 0.7}],
                "price_spread_efficiency",
            ),
            0.7,
        )

    def test_weighted_average_two_slices(self):
        self.assertAlmostEqual(
            _weighted_slice_metric(
                [
                    {"shares": 10, "price_spread_efficiency": 0.8},
                    {"shares": 30, "price_spread_efficiency": 0.4},
                ],
                "price_spread_efficiency",
            ),
            (10 * 0.8 + 30 * 0.4) / 40,
        )

    def test_empty_slices_returns_zero(self):
        self.assertAlmostEqual(
            _weighted_slice_metric([], "price_spread_efficiency"), 0.0
        )

    def test_zero_total_shares_returns_zero(self):
        self.assertAlmostEqual(
            _weighted_slice_metric(
                [{"shares": 0, "price_spread_efficiency": 0.5}],
                "price_spread_efficiency",
            ),
            0.0,
        )


class StrategySellQualityTest(unittest.TestCase):
    def test_repair_sell_quality_uses_holding_period_spread_metrics(self):
        points = build_price_points_from_series(
            [
                (datetime(2025, 1, 1), 100),
                (datetime(2025, 1, 2), 80),
                (datetime(2025, 1, 3), 140),
            ]
        )
        inputs = StrategyInputs(
            initial_cash=1000,
            max_drawdown_pct=20,
            step_pct=20,
            equal_slice_allocation_pct=100,
            repair_stage_sell_pct=50,
            sell_min_profit_pct=0,
            reserve_position_pct=0,
            trade_fee=0,
        )

        result = simulate_portfolio(
            {"TEST.US": points},
            [PortfolioTarget("TEST.US", 100, "TEST", 20)],
            inputs,
            strategies=["equal_slice"],
            sell_strategies=["repair_step"],
        )
        strategy = result["strategies"][0]
        metrics = strategy["metrics"]
        sell = [trade for trade in strategy["trades"] if trade["action"] == "sell"][0]

        self.assertAlmostEqual(sell["price_spread_efficiency"], 1.0)
        self.assertAlmostEqual(sell["sell_timing_efficiency"], 1.0)
        self.assertEqual(sell["sold_lot_slices"][0]["holding_period_low_usd"], 80)
        self.assertEqual(sell["sold_lot_slices"][0]["holding_period_high_usd"], 140)
        self.assertAlmostEqual(metrics["avg_price_spread_efficiency"], 1.0)
        self.assertAlmostEqual(metrics["avg_sell_timing_efficiency"], 1.0)
        expected_idle_component = ((65 - metrics["avg_cash_pct"]) / 65) * 12
        self.assertAlmostEqual(metrics["sell_quality_score"], 40 + 30 + expected_idle_component)


if __name__ == "__main__":
    unittest.main()
