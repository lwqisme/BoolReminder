import unittest
from datetime import datetime

from drawdown.generate_drawdown_report import build_price_points_from_series
from drawdown.position_strategy import PortfolioTarget, StrategyInputs, simulate_portfolio


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
