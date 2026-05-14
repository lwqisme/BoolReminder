#!/usr/bin/env python3
"""Offline checks for drawdown position strategy simulation."""

from __future__ import annotations

import unittest
from inspect import signature
from datetime import datetime, timedelta

from drawdown.generate_drawdown_report import build_price_points_from_series
from drawdown.position_strategy import (
    PortfolioTarget,
    StrategyInputs,
    _score_question_strategies,
    build_strategy_tranches,
    run_longbridge_strategy_scorecard,
    simulate_portfolio,
)


def points(*prices: float):
    series = [
        (datetime(2024, 1, index + 1), price)
        for index, price in enumerate(prices)
    ]
    return build_price_points_from_series(series)


def dated_points(*items: tuple[str, float]):
    series = [
        (datetime.fromisoformat(day), price)
        for day, price in items
    ]
    return build_price_points_from_series(series)


class PositionStrategyTest(unittest.TestCase):
    def test_pyramid_tranches_use_max_drawdown_anchor(self):
        tranches = build_strategy_tranches(
            StrategyInputs(max_drawdown_pct=50),
            "pyramid_3",
        )

        self.assertEqual([round(item.threshold_pct, 2) for item in tranches], [10, 25, 50])
        self.assertEqual([round(item.allocation_pct, 2) for item in tranches], [20, 30, 50])

    def test_equal_slice_uses_fixed_default_allocation(self):
        tranches = build_strategy_tranches(
            StrategyInputs(max_drawdown_pct=50, step_pct=5, equal_slice_allocation_pct=5),
            "equal_slice",
        )

        self.assertEqual(len(tranches), 10)
        self.assertEqual(tranches[0].threshold_pct, 5)
        self.assertEqual(tranches[-1].threshold_pct, 50)
        self.assertTrue(all(item.allocation_pct == 5 for item in tranches))

    def test_weighted_slice_allocates_full_budget(self):
        linear_tranches = build_strategy_tranches(
            StrategyInputs(max_drawdown_pct=50, step_pct=5),
            "linear_weighted_slice",
        )
        tranches = build_strategy_tranches(
            StrategyInputs(max_drawdown_pct=50, step_pct=5),
            "weighted_slice",
        )

        self.assertEqual(len(linear_tranches), 10)
        self.assertAlmostEqual(sum(item.allocation_pct for item in linear_tranches), 100.0)
        self.assertEqual(len(tranches), 10)
        self.assertAlmostEqual(sum(item.allocation_pct for item in tranches), 100.0)
        self.assertGreater(linear_tranches[-1].allocation_pct, linear_tranches[0].allocation_pct)
        self.assertGreater(tranches[-1].allocation_pct, tranches[0].allocation_pct)
        self.assertGreater(tranches[-1].allocation_pct, linear_tranches[-1].allocation_pct)

    def test_target_max_drawdown_overrides_global_anchor(self):
        inputs = StrategyInputs(
            initial_cash=1000,
            max_drawdown_pct=50,
            step_pct=10,
            trade_fee=0,
        )
        result = simulate_portfolio(
            {
                "TSM.US": points(100, 95, 90, 85, 80, 75, 70),
                "GOOGL.US": points(100, 95, 90, 85, 80, 75, 70),
            },
            [
                PortfolioTarget("TSM.US", 50, "TSM", max_drawdown_pct=30),
                PortfolioTarget("GOOGL.US", 50, "GOOGL", max_drawdown_pct=50),
            ],
            inputs,
            strategies=("pyramid_3",),
            sell_strategies=("none",),
        )

        tranches_by_symbol = result["strategies"][0]["tranches_by_symbol"]
        self.assertEqual(
            [round(item["threshold_pct"], 2) for item in tranches_by_symbol["TSM.US"]],
            [6.0, 15.0, 30.0],
        )
        self.assertEqual(
            [round(item["threshold_pct"], 2) for item in tranches_by_symbol["GOOGL.US"]],
            [10.0, 25.0, 50.0],
        )

    def test_rolling_120_drawdown_basis_changes_buy_triggers(self):
        long_sideways_series = [100] + [90] * 120 + [81]
        price_points = build_price_points_from_series(
            [
                (datetime(2024, 1, 1) + timedelta(days=index), price)
                for index, price in enumerate(long_sideways_series)
            ]
        )
        targets = [PortfolioTarget("TSLA.US", 100, "TSLA")]

        ath_result = simulate_portfolio(
            {"TSLA.US": price_points},
            targets,
            StrategyInputs(initial_cash=1000, max_drawdown_pct=30, trade_fee=0, drawdown_basis="ath"),
            strategies=("pyramid_3",),
            sell_strategies=("none",),
        )
        rolling_result = simulate_portfolio(
            {"TSLA.US": price_points},
            targets,
            StrategyInputs(initial_cash=1000, max_drawdown_pct=30, trade_fee=0, drawdown_basis="rolling_120"),
            strategies=("pyramid_3",),
            sell_strategies=("none",),
        )

        self.assertEqual(
            [round(trade["threshold_pct"], 2) for trade in ath_result["strategies"][0]["trades"]],
            [6.0, 15.0],
        )
        self.assertEqual(
            [round(trade["threshold_pct"], 2) for trade in rolling_result["strategies"][0]["trades"]],
            [6.0, 6.0],
        )

    def test_monthly_contribution_uses_cumulative_capital(self):
        result = simulate_portfolio(
            {
                "TSM.US": dated_points(
                    ("2024-01-02", 100),
                    ("2024-01-31", 100),
                    ("2024-02-01", 100),
                    ("2024-02-29", 100),
                    ("2024-03-01", 100),
                )
            },
            [PortfolioTarget("TSM.US", 100, "TSM")],
            StrategyInputs(initial_cash=1000, monthly_contribution=100, trade_fee=0),
            strategies=("pyramid_3",),
            sell_strategies=("none",),
        )

        strategy = result["strategies"][0]
        metrics = strategy["metrics"]
        symbol = strategy["symbols"][0]
        self.assertEqual(metrics["contribution_count"], 2)
        self.assertAlmostEqual(metrics["contributed_cash"], 200.0)
        self.assertAlmostEqual(metrics["total_contributed"], 1200.0)
        self.assertAlmostEqual(metrics["final_value"], 1200.0)
        self.assertAlmostEqual(metrics["return_pct"], 0.0)
        self.assertAlmostEqual(symbol["budget"], 1200.0)
        self.assertEqual(
            strategy["series"]["contribution_values"],
            [1000.0, 1000.0, 1100.0, 1100.0, 1200.0],
        )

    def test_weekly_dca_buys_first_trading_day_each_week(self):
        inputs = StrategyInputs(
            initial_cash=900,
            max_drawdown_pct=50,
            step_pct=5,
            equal_slice_allocation_pct=5,
            trade_fee=0,
        )
        result = simulate_portfolio(
            {
                "TSLA.US": dated_points(
                    ("2024-01-02", 100),
                    ("2024-01-05", 90),
                    ("2024-01-08", 80),
                    ("2024-01-12", 70),
                    ("2024-01-16", 60),
                )
            },
            [PortfolioTarget("TSLA.US", 100, "TSLA")],
            inputs,
            strategies=("weekly_dca",),
            sell_strategies=("none",),
        )

        strategy = result["strategies"][0]
        buy_trades = [trade for trade in strategy["trades"] if trade["action"] == "buy"]
        self.assertEqual(strategy["label"], "每周定投 / 不卖出")
        self.assertEqual([trade["date"] for trade in buy_trades], ["2024-01-02", "2024-01-08", "2024-01-16"])
        self.assertEqual([round(trade["gross_amount"], 2) for trade in buy_trades], [300.0, 300.0, 300.0])

    def test_simulation_applies_fees_and_generates_three_buy_accounts_without_sells(self):
        inputs = StrategyInputs(
            initial_cash=1000,
            max_drawdown_pct=50,
            step_pct=5,
            equal_slice_allocation_pct=5,
            trade_fee=0.35,
        )
        result = simulate_portfolio(
            {"TSLA.US": points(100, 90, 75, 50)},
            [PortfolioTarget("TSLA.US", 100, "TSLA")],
            inputs,
        )

        by_key = {item["key"]: item for item in result["strategies"]}
        self.assertIn("TSLA.US", result["price_series"])
        self.assertEqual(result["price_series"]["TSLA.US"]["dates"][0], "2024-01-01")
        self.assertEqual(
            set(by_key),
            {
                "pyramid_3__none",
                "equal_slice__none",
                "linear_weighted_slice__none",
                "weighted_slice__none",
                "weekly_dca__none",
                "salary_flow_dca__none",
            },
        )
        self.assertEqual(by_key["pyramid_3__none"]["metrics"]["trade_count"], 3)
        self.assertEqual(by_key["weekly_dca__none"]["metrics"]["trade_count"], 1)
        self.assertEqual(by_key["salary_flow_dca__none"]["metrics"]["trade_count"], 0)
        self.assertAlmostEqual(by_key["pyramid_3__none"]["metrics"]["total_fees"], 1.05)
        self.assertEqual(by_key["equal_slice__none"]["metrics"]["trade_count"], 10)
        equal_buy_prices = [
            trade["display_price"]
            for trade in by_key["equal_slice__none"]["trades"]
            if trade["action"] == "buy"
        ]
        self.assertEqual([round(value, 2) for value in equal_buy_prices[-2:]], [55.0, 50.0])
        self.assertLess(
            by_key["equal_slice__none"]["metrics"]["cash_usage_pct"],
            by_key["pyramid_3__none"]["metrics"]["cash_usage_pct"],
        )

    def test_salary_flow_dca_uses_monthly_cashflow_and_drawdown_boost(self):
        result = simulate_portfolio(
            {
                "TSLA.US": dated_points(
                    ("2024-01-02", 100),
                    ("2024-01-08", 96),
                    ("2024-01-15", 90),
                    ("2024-01-22", 82),
                    ("2024-01-29", 69),
                    ("2024-02-01", 70),
                    ("2024-02-05", 70),
                )
            },
            [PortfolioTarget("TSLA.US", 100, "TSLA")],
            StrategyInputs(initial_cash=1000, monthly_contribution=400, trade_fee=0),
            strategies=("salary_flow_dca",),
            sell_strategies=("none",),
        )

        strategy = result["strategies"][0]
        buys = [trade for trade in strategy["trades"] if trade["action"] == "buy"]
        self.assertEqual([trade["date"] for trade in buys], ["2024-01-02", "2024-01-08", "2024-01-15", "2024-01-22", "2024-01-29", "2024-02-05"])
        self.assertEqual([round(trade["drawdown_boost"], 1) for trade in buys], [1.0, 1.0, 1.4, 2.0, 4.0, 4.0])
        self.assertEqual([round(trade["gross_amount"], 2) for trade in buys], [264.0, 211.2, 246.68, 214.06, 64.06, 400.0])
        self.assertAlmostEqual(strategy["metrics"]["total_contributed"], 1400.0)
        self.assertAlmostEqual(strategy["metrics"]["cash_remaining"], 0.0)

    def test_simulation_can_cross_buy_and_sell_strategies(self):
        inputs = StrategyInputs(
            initial_cash=1000,
            max_drawdown_pct=50,
            step_pct=5,
            equal_slice_allocation_pct=5,
            trade_fee=0,
            reserve_position_pct=25,
            sell_min_profit_pct=5,
            repair_sell_cooldown_days=0,
            repair_stage_sell_pct=25,
        )
        result = simulate_portfolio(
            {"TSLA.US": points(100, 90, 75, 50, 60, 75, 90, 100, 110)},
            [PortfolioTarget("TSLA.US", 100, "TSLA")],
            inputs,
            strategies=("pyramid_3",),
            sell_strategies=("none", "repair_step", "grid_rebound", "cost_deleverage"),
        )

        by_key = {item["key"]: item for item in result["strategies"]}
        self.assertEqual(len(by_key), 4)
        self.assertEqual(by_key["pyramid_3__none"]["metrics"]["sell_trade_count"], 0)
        self.assertGreater(by_key["pyramid_3__repair_step"]["metrics"]["sell_trade_count"], 0)
        self.assertGreater(by_key["pyramid_3__cost_deleverage"]["metrics"]["sell_trade_count"], 0)
        self.assertTrue(
            any(trade["action"] == "sell" for trade in by_key["pyramid_3__repair_step"]["trades"])
        )

    def test_pyramid_rearms_after_repair_for_later_drawdown_cycle(self):
        inputs = StrategyInputs(
            initial_cash=10000,
            max_drawdown_pct=50,
            step_pct=5,
            equal_slice_allocation_pct=5,
            trade_fee=0,
            reserve_position_pct=25,
            sell_min_profit_pct=5,
            repair_sell_cooldown_days=0,
            repair_stage_sell_pct=25,
        )
        result = simulate_portfolio(
            {"TSLA.US": points(100, 90, 75, 50, 75, 90, 105, 94.5, 78.75, 52.5, 78.75, 95, 110)},
            [PortfolioTarget("TSLA.US", 100, "TSLA")],
            inputs,
            strategies=("pyramid_3",),
            sell_strategies=("repair_step",),
        )

        strategy = result["strategies"][0]
        self.assertEqual(strategy["metrics"]["buy_trade_count"], 5)
        self.assertGreater(strategy["metrics"]["sell_trade_count"], 4)
        second_cycle_buys = [
            trade for trade in strategy["trades"]
            if trade["action"] == "buy" and trade["date"] >= "2024-01-08"
        ]
        self.assertEqual(
            [round(trade["threshold_pct"], 2) for trade in second_cycle_buys],
            [10.0, 25.0],
        )
        actions_by_date = {}
        for trade in strategy["trades"]:
            actions_by_date.setdefault(trade["date"], set()).add(trade["action"])
        self.assertFalse(any(actions == {"buy", "sell"} for actions in actions_by_date.values()))

    def test_all_strategy_pairs_rearm_across_later_drawdown_cycle(self):
        inputs = StrategyInputs(
            initial_cash=10000,
            max_drawdown_pct=50,
            step_pct=25,
            equal_slice_allocation_pct=10,
            trade_fee=0,
            reserve_position_pct=25,
            sell_min_profit_pct=5,
            repair_sell_cooldown_days=0,
            repair_stage_sell_pct=25,
        )
        result = simulate_portfolio(
            {"TSLA.US": points(100, 90, 75, 50, 75, 90, 105, 94.5, 78.75, 52.5, 78.75, 95, 110)},
            [PortfolioTarget("TSLA.US", 100, "TSLA")],
            inputs,
            strategies=("pyramid_3", "equal_slice", "weighted_slice"),
            sell_strategies=("repair_step", "grid_rebound", "cost_deleverage"),
        )

        for strategy in result["strategies"]:
            with self.subTest(strategy=strategy["key"]):
                later_buys = [
                    trade for trade in strategy["trades"]
                    if trade["action"] == "buy" and trade["date"] >= "2024-01-08"
                ]
                later_sells = [
                    trade for trade in strategy["trades"]
                    if trade["action"] == "sell" and trade["date"] >= "2024-01-08"
                ]
                actions_by_date = {}
                for trade in strategy["trades"]:
                    actions_by_date.setdefault(trade["date"], set()).add(trade["action"])

                self.assertGreater(len(later_buys), 0)
                self.assertGreater(len(later_sells), 0)
                self.assertFalse(any(actions == {"buy", "sell"} for actions in actions_by_date.values()))

    def test_repair_step_sells_one_stage_then_cools_down(self):
        inputs = StrategyInputs(
            initial_cash=10000,
            max_drawdown_pct=50,
            step_pct=5,
            equal_slice_allocation_pct=5,
            trade_fee=0,
            reserve_position_pct=0,
            sell_min_profit_pct=0,
            repair_sell_cooldown_days=20,
            repair_stage_sell_pct=15,
        )
        result = simulate_portfolio(
            {"TSLA.US": points(100, 90, 75, 50, 90, 95, 100)},
            [PortfolioTarget("TSLA.US", 100, "TSLA")],
            inputs,
            strategies=("pyramid_3",),
            sell_strategies=("repair_step",),
        )

        sells = [trade for trade in result["strategies"][0]["trades"] if trade["action"] == "sell"]
        self.assertEqual(len(sells), 1)
        self.assertEqual(sells[0]["date"], "2024-01-05")
        self.assertAlmostEqual(sells[0]["shares"], 6.0)

    def test_repair_step_cooldown_counts_trading_days(self):
        inputs = StrategyInputs(
            initial_cash=10000,
            max_drawdown_pct=50,
            step_pct=5,
            equal_slice_allocation_pct=5,
            trade_fee=0,
            reserve_position_pct=0,
            sell_min_profit_pct=0,
            repair_sell_cooldown_days=2,
            repair_stage_sell_pct=25,
        )
        result = simulate_portfolio(
            {
                "TSLA.US": dated_points(
                    ("2024-01-01", 100),
                    ("2024-01-02", 80),
                    ("2024-01-05", 100),
                    ("2024-01-08", 110),
                    ("2024-01-09", 120),
                )
            },
            [PortfolioTarget("TSLA.US", 100, "TSLA")],
            inputs,
            strategies=("pyramid_3",),
            sell_strategies=("repair_step",),
        )

        sells = [trade for trade in result["strategies"][0]["trades"] if trade["action"] == "sell"]
        self.assertEqual([trade["date"] for trade in sells], ["2024-01-05", "2024-01-09"])

    def test_repair_step_uses_lot_profit_not_average_cost(self):
        inputs = StrategyInputs(
            initial_cash=10000,
            max_drawdown_pct=50,
            step_pct=5,
            equal_slice_allocation_pct=5,
            trade_fee=0,
            reserve_position_pct=0,
            sell_min_profit_pct=10,
        )
        result = simulate_portfolio(
            {"TSLA.US": points(100, 90, 75, 50, 75, 90, 105, 94.5, 100)},
            [PortfolioTarget("TSLA.US", 100, "TSLA")],
            inputs,
            strategies=("pyramid_3",),
            sell_strategies=("repair_step",),
        )

        trades = result["strategies"][0]["trades"]
        self.assertTrue(
            any(
                trade["action"] == "buy"
                and trade["date"] == "2024-01-08"
                and round(trade["price"], 2) == 94.5
                for trade in trades
            )
        )
        self.assertFalse(
            any(
                trade["action"] == "sell"
                and trade.get("lot_buy_price_usd") == 94.5
                for trade in trades
            )
        )

    def test_equal_slice_repair_step_sells_from_position_not_each_lot(self):
        inputs = StrategyInputs(
            initial_cash=10000,
            max_drawdown_pct=50,
            step_pct=5,
            equal_slice_allocation_pct=5,
            trade_fee=0,
            reserve_position_pct=0,
            sell_min_profit_pct=0,
            repair_sell_cooldown_days=0,
            repair_stage_sell_pct=25,
        )
        result = simulate_portfolio(
            {"TSLA.US": points(100, 95, 90, 85, 80, 100, 105)},
            [PortfolioTarget("TSLA.US", 100, "TSLA")],
            inputs,
            strategies=("equal_slice",),
            sell_strategies=("repair_step",),
        )

        sells = [trade for trade in result["strategies"][0]["trades"] if trade["action"] == "sell"]
        self.assertGreater(len(sells), 0)
        self.assertTrue(all(trade.get("lot_buy_price_usd") is None for trade in sells))
        self.assertGreater(min(trade["gross_amount"] for trade in sells), 400)

    def test_scorecard_weights_return_more_than_drawdown(self):
        scored = _score_question_strategies(
            [
                {
                    "key": "high_return_high_drawdown",
                    "label": "高收益高回撤",
                    "buy_strategy": "pyramid_3",
                    "sell_strategy": "none",
                    "metrics": {
                        "return_pct": 30.0,
                        "max_drawdown_pct": -40.0,
                        "final_value": 1300.0,
                        "trade_count": 1,
                        "buy_trade_count": 1,
                        "sell_trade_count": 0,
                    },
                },
                {
                    "key": "low_return_low_drawdown",
                    "label": "低收益低回撤",
                    "buy_strategy": "pyramid_3",
                    "sell_strategy": "repair_step",
                    "metrics": {
                        "return_pct": 10.0,
                        "max_drawdown_pct": -5.0,
                        "final_value": 1100.0,
                        "trade_count": 1,
                        "buy_trade_count": 1,
                        "sell_trade_count": 0,
                    },
                },
            ]
        )

        self.assertEqual(scored[0]["key"], "high_return_high_drawdown")
        self.assertEqual(scored[0]["rank"], 1)

    def test_scorecard_default_buy_strategies_include_salary_flow_dca(self):
        default_buy_strategies = signature(run_longbridge_strategy_scorecard).parameters[
            "buy_strategies"
        ].default

        self.assertIn("salary_flow_dca", default_buy_strategies)


if __name__ == "__main__":
    unittest.main()
