#!/usr/bin/env python3
"""Offline checks for drawdown position strategy simulation."""

from __future__ import annotations

import unittest
from inspect import signature
from datetime import datetime, timedelta

from drawdown.generate_drawdown_report import build_price_points_from_series
from drawdown.position_strategy import (
    PortfolioTarget,
    SymbolState,
    StrategyInputs,
    _rearm_position_sell_cycle_after_dca_buy,
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

    def test_linear_weighted_slice_allocates_full_budget(self):
        linear_tranches = build_strategy_tranches(
            StrategyInputs(max_drawdown_pct=50, step_pct=5),
            "linear_weighted_slice",
        )

        self.assertEqual(len(linear_tranches), 10)
        self.assertAlmostEqual(sum(item.allocation_pct for item in linear_tranches), 100.0)
        self.assertGreater(linear_tranches[-1].allocation_pct, linear_tranches[0].allocation_pct)

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

    def test_weekly_dca_all_ins_initial_cash_and_paychecks(self):
        inputs = StrategyInputs(
            initial_cash=900,
            monthly_contribution=120,
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
                    ("2024-02-01", 65),
                    ("2024-02-05", 70),
                    ("2024-03-01", 75),
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
        self.assertEqual([trade["date"] for trade in buy_trades], ["2024-01-02", "2024-02-01", "2024-03-01"])
        self.assertEqual([round(trade["gross_amount"], 2) for trade in buy_trades], [900.0, 120.0, 120.0])

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
                "weekly_dca__none",
                "salary_flow_dca__none",
                "core_dip_dca__none",
            },
        )
        self.assertEqual(by_key["pyramid_3__none"]["metrics"]["trade_count"], 3)
        self.assertEqual(by_key["weekly_dca__none"]["metrics"]["trade_count"], 1)
        self.assertEqual(by_key["salary_flow_dca__none"]["metrics"]["trade_count"], 0)
        self.assertEqual(by_key["core_dip_dca__none"]["metrics"]["trade_count"], 1)
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

    def test_core_dip_dca_keeps_core_buying_and_releases_dip_budget(self):
        result = simulate_portfolio(
            {
                "TSLA.US": dated_points(
                    ("2024-01-02", 100),
                    ("2024-01-08", 95),
                    ("2024-01-15", 90),
                    ("2024-01-22", 80),
                    ("2024-01-29", 70),
                )
            },
            [PortfolioTarget("TSLA.US", 100, "TSLA")],
            StrategyInputs(initial_cash=1000, monthly_contribution=400, trade_fee=0),
            strategies=("core_dip_dca",),
            sell_strategies=("none",),
        )

        strategy = result["strategies"][0]
        buys = [trade for trade in strategy["trades"] if trade["action"] == "buy"]
        self.assertEqual(strategy["label"], "核心定投+回撤加仓 / 不卖出")
        self.assertEqual([trade["date"] for trade in buys], ["2024-01-02", "2024-01-08", "2024-01-15", "2024-01-22", "2024-01-29"])
        self.assertEqual([round(trade["core_amount"], 2) for trade in buys], [90.0, 90.0, 90.0, 90.0, 90.0])
        self.assertEqual([round(trade["initial_core_amount"], 2) for trade in buys], [800.0, 0.0, 0.0, 0.0, 0.0])
        self.assertEqual([round(trade["dip_boost_ratio"], 2) for trade in buys], [0.0, 0.0, 0.25, 0.75, 1.0])
        self.assertEqual([round(trade["dip_amount"], 2) for trade in buys], [0.0, 0.0, 2.5, 7.5, 10.0])
        self.assertEqual([round(trade["gross_amount"], 2) for trade in buys], [893.6, 26.4, 17.0, 34.0, 17.0])
        self.assertGreater(strategy["metrics"]["cash_usage_pct"], 98.0)

    def test_core_dip_dca_uses_initial_core_when_monthly_contribution_is_zero(self):
        result = simulate_portfolio(
            {
                "TSLA.US": dated_points(
                    ("2024-01-02", 100),
                    ("2024-01-08", 95),
                    ("2024-01-15", 90),
                    ("2024-01-22", 85),
                )
            },
            [PortfolioTarget("TSLA.US", 100, "TSLA")],
            StrategyInputs(initial_cash=1000, monthly_contribution=0, trade_fee=0),
            strategies=("core_dip_dca",),
            sell_strategies=("none",),
        )

        strategy = result["strategies"][0]
        buys = [trade for trade in strategy["trades"] if trade["action"] == "buy"]
        self.assertGreaterEqual(len(buys), 2)
        self.assertEqual(buys[0]["date"], "2024-01-02")
        self.assertAlmostEqual(buys[0]["scheduled_amount"], 0.0)
        self.assertAlmostEqual(buys[0]["new_core_amount"], 0.0)
        self.assertAlmostEqual(buys[0]["dip_amount"], 0.0)
        self.assertAlmostEqual(buys[0]["initial_core_amount"], 800.0)
        self.assertGreater(buys[0]["idle_cash_sweep"], 0.0)
        self.assertTrue(any(trade["idle_cash_sweep"] > 0 for trade in buys[1:]))
        self.assertNotEqual(strategy["series"]["cash_values"], strategy["series"]["contribution_values"])

    def test_core_dip_timing_defers_core_buy_after_sharp_rise(self):
        result = simulate_portfolio(
            {
                "TSLA.US": dated_points(
                    ("2024-01-02", 100),
                    ("2024-01-08", 104),
                    ("2024-01-09", 103),
                    ("2024-01-10", 102),
                )
            },
            [PortfolioTarget("TSLA.US", 100, "TSLA")],
            StrategyInputs(
                initial_cash=1000,
                monthly_contribution=400,
                trade_fee=0,
                core_dip_initial_core_pct=0,
                core_dip_weekly_core_pct=90,
                core_dip_cash_reserve_pct=0,
                core_dip_timing_enabled=True,
                core_dip_timing_max_delay_days=3,
                core_dip_timing_rise_threshold_pct=1.5,
                core_dip_timing_near_low_pct=2,
            ),
            strategies=("core_dip_dca",),
            sell_strategies=("none",),
        )

        buys = [trade for trade in result["strategies"][0]["trades"] if trade["action"] == "buy"]
        self.assertEqual([trade["date"] for trade in buys], ["2024-01-02", "2024-01-09"])
        self.assertEqual(buys[0]["timing_reason"], "initial_core")
        self.assertEqual(buys[1]["timing_reason"], "down_day")
        self.assertEqual(round(buys[1]["core_amount"], 2), 90.0)

    def test_core_dip_timing_forces_buy_after_delay(self):
        result = simulate_portfolio(
            {
                "TSLA.US": dated_points(
                    ("2024-01-02", 100),
                    ("2024-01-08", 104),
                    ("2024-01-09", 106),
                    ("2024-01-10", 108),
                )
            },
            [PortfolioTarget("TSLA.US", 100, "TSLA")],
            StrategyInputs(
                initial_cash=1000,
                monthly_contribution=400,
                trade_fee=0,
                core_dip_initial_core_pct=0,
                core_dip_weekly_core_pct=90,
                core_dip_cash_reserve_pct=0,
                core_dip_timing_enabled=True,
                core_dip_timing_max_delay_days=3,
                core_dip_timing_rise_threshold_pct=1.5,
                core_dip_timing_near_low_pct=0,
            ),
            strategies=("core_dip_dca",),
            sell_strategies=("none",),
        )

        buys = [trade for trade in result["strategies"][0]["trades"] if trade["action"] == "buy"]
        self.assertEqual([trade["date"] for trade in buys], ["2024-01-02", "2024-01-10"])
        self.assertEqual(buys[1]["timing_reason"], "delay_expired")
        self.assertEqual(round(buys[1]["core_amount"], 2), 90.0)

    def test_equal_slice_reuses_triggered_drawdown_for_new_cash(self):
        result = simulate_portfolio(
            {
                "PDD.US": dated_points(
                    ("2024-01-02", 100),
                    ("2024-01-03", 70),
                    ("2024-02-01", 68),
                    ("2024-03-01", 66),
                )
            },
            [PortfolioTarget("PDD.US", 100, "PDD")],
            StrategyInputs(
                initial_cash=1000,
                monthly_contribution=1000,
                max_drawdown_pct=30,
                step_pct=10,
                equal_slice_allocation_pct=10,
                trade_fee=0,
                drawdown_basis="ath",
            ),
            strategies=("equal_slice",),
            sell_strategies=("none",),
        )

        buys = [trade for trade in result["strategies"][0]["trades"] if trade["action"] == "buy"]
        self.assertEqual([trade["date"] for trade in buys], ["2024-01-03", "2024-01-03", "2024-01-03", "2024-02-01", "2024-02-01", "2024-02-01", "2024-03-01", "2024-03-01", "2024-03-01"])
        self.assertEqual([round(trade["gross_amount"], 2) for trade in buys], [100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0, 100.0])

    def test_pyramid_does_not_reuse_triggered_drawdown_for_new_cash(self):
        result = simulate_portfolio(
            {
                "TSLA.US": dated_points(
                    ("2024-01-02", 100),
                    ("2024-01-03", 50),
                    ("2024-02-01", 48),
                    ("2024-03-01", 46),
                )
            },
            [PortfolioTarget("TSLA.US", 100, "TSLA")],
            StrategyInputs(
                initial_cash=1000,
                monthly_contribution=1000,
                max_drawdown_pct=50,
                trade_fee=0,
                drawdown_basis="ath",
            ),
            strategies=("pyramid_3",),
            sell_strategies=("none",),
        )

        buys = [trade for trade in result["strategies"][0]["trades"] if trade["action"] == "buy"]
        self.assertEqual([trade["date"] for trade in buys], ["2024-01-03", "2024-01-03", "2024-01-03"])
        self.assertEqual([round(trade["gross_amount"], 2) for trade in buys], [200.0, 300.0, 500.0])

    def test_pyramid_rearms_after_position_sell_and_deeper_drawdown(self):
        result = simulate_portfolio(
            {
                "PDD.US": dated_points(
                    ("2024-01-02", 100),
                    ("2024-01-03", 60),
                    ("2024-01-04", 90),
                    ("2024-01-05", 78),
                    ("2024-01-08", 60),
                )
            },
            [PortfolioTarget("PDD.US", 100, "PDD")],
            StrategyInputs(
                initial_cash=10000,
                max_drawdown_pct=40,
                trade_fee=0,
                drawdown_basis="ath",
                sell_min_profit_pct=10,
                cost_first_profit_pct=10,
                cost_second_profit_pct=25,
                cost_third_profit_pct=40,
                cost_first_sell_pct=50,
                cost_second_sell_pct=0,
                cost_third_sell_pct=0,
                dca_rearm_drawdown_pct=5,
                reserve_position_pct=0,
            ),
            strategies=("pyramid_3",),
            sell_strategies=("cost_deleverage",),
        )

        trades = result["strategies"][0]["trades"]
        buys = [trade for trade in trades if trade["action"] == "buy"]
        sells = [trade for trade in trades if trade["action"] == "sell"]
        self.assertTrue(sells)
        self.assertGreater(len(buys), 3)
        self.assertTrue(any(trade["date"] == "2024-01-08" for trade in buys))

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
            strategies=("pyramid_3", "equal_slice", "linear_weighted_slice"),
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

    def test_equal_slice_grid_rebound_uses_position_sized_sells(self):
        inputs = StrategyInputs(
            initial_cash=10000,
            max_drawdown_pct=50,
            step_pct=5,
            equal_slice_allocation_pct=5,
            trade_fee=0,
            reserve_position_pct=0,
            sell_min_profit_pct=0,
            grid_rebound_step_pct=5,
            grid_first_sell_pct=40,
            grid_second_sell_pct=40,
            grid_min_sell_amount=0,
        )
        result = simulate_portfolio(
            {"TSLA.US": points(100, 95, 90, 85, 80, 100, 105)},
            [PortfolioTarget("TSLA.US", 100, "TSLA")],
            inputs,
            strategies=("equal_slice",),
            sell_strategies=("grid_rebound",),
        )

        sells = [trade for trade in result["strategies"][0]["trades"] if trade["action"] == "sell"]
        self.assertEqual(len(sells), 2)
        self.assertTrue(all(trade.get("lot_buy_price_usd") is None for trade in sells))
        self.assertAlmostEqual(sells[0]["shares"], 9.180426556587548)
        self.assertAlmostEqual(sells[1]["shares"], 5.508255933952529)

    def test_position_grid_rebound_respects_min_sell_amount(self):
        inputs = StrategyInputs(
            initial_cash=1000,
            max_drawdown_pct=50,
            step_pct=5,
            equal_slice_allocation_pct=5,
            trade_fee=0,
            reserve_position_pct=0,
            sell_min_profit_pct=0,
            grid_rebound_step_pct=5,
            grid_first_sell_pct=40,
            grid_second_sell_pct=40,
            grid_min_sell_amount=500,
        )
        result = simulate_portfolio(
            {"TSLA.US": points(100, 95, 90, 85, 80, 100, 105)},
            [PortfolioTarget("TSLA.US", 100, "TSLA")],
            inputs,
            strategies=("equal_slice",),
            sell_strategies=("grid_rebound",),
        )

        sells = [trade for trade in result["strategies"][0]["trades"] if trade["action"] == "sell"]
        self.assertEqual(sells, [])

    def test_position_grid_rebound_starts_next_cycle_after_second_grid_sell(self):
        inputs = StrategyInputs(
            initial_cash=10000,
            max_drawdown_pct=60,
            step_pct=10,
            equal_slice_allocation_pct=100,
            trade_fee=0,
            reserve_position_pct=0,
            sell_min_profit_pct=0,
            grid_rebound_step_pct=5,
            grid_first_sell_pct=10,
            grid_second_sell_pct=10,
            grid_min_sell_amount=0,
        )
        result = simulate_portfolio(
            {"TSLA.US": points(200, 100, 110, 120, 130, 140)},
            [PortfolioTarget("TSLA.US", 100, "TSLA")],
            inputs,
            strategies=("equal_slice",),
            sell_strategies=("grid_rebound",),
        )

        sells = [trade for trade in result["strategies"][0]["trades"] if trade["action"] == "sell"]
        self.assertEqual([trade["date"] for trade in sells], ["2024-01-03", "2024-01-04", "2024-01-05", "2024-01-06"])
        self.assertEqual([trade["trigger_value"] for trade in sells], [45, 40, 35, 30])

    def test_position_grid_rebound_stops_opening_cycles_after_ath_grid_two(self):
        inputs = StrategyInputs(
            initial_cash=10000,
            max_drawdown_pct=60,
            step_pct=10,
            equal_slice_allocation_pct=100,
            trade_fee=0,
            reserve_position_pct=0,
            sell_min_profit_pct=0,
            grid_rebound_step_pct=5,
            grid_first_sell_pct=10,
            grid_second_sell_pct=10,
            grid_min_sell_amount=0,
        )
        result = simulate_portfolio(
            {"TSLA.US": points(200, 100, 110, 120, 200, 210, 220)},
            [PortfolioTarget("TSLA.US", 100, "TSLA")],
            inputs,
            strategies=("equal_slice",),
            sell_strategies=("grid_rebound",),
        )

        sells = [trade for trade in result["strategies"][0]["trades"] if trade["action"] == "sell"]
        self.assertEqual([trade["trigger_value"] for trade in sells], [45, 40, 35, 30])

    def test_cost_deleverage_uses_configured_position_sized_stages(self):
        inputs = StrategyInputs(
            initial_cash=10000,
            max_drawdown_pct=50,
            step_pct=5,
            equal_slice_allocation_pct=5,
            trade_fee=0,
            reserve_position_pct=0,
            sell_min_profit_pct=0,
            cost_first_profit_pct=5,
            cost_second_profit_pct=10,
            cost_third_profit_pct=20,
            cost_first_sell_pct=20,
            cost_second_sell_pct=30,
            cost_third_sell_pct=40,
            cost_deleverage_cooldown_days=0,
            cost_min_sell_amount=0,
        )
        result = simulate_portfolio(
            {"TSLA.US": points(100, 95, 90, 85, 100, 110, 125)},
            [PortfolioTarget("TSLA.US", 100, "TSLA")],
            inputs,
            strategies=("equal_slice",),
            sell_strategies=("cost_deleverage",),
        )

        sells = [trade for trade in result["strategies"][0]["trades"] if trade["action"] == "sell"]
        self.assertEqual([trade["trigger_value"] for trade in sells], [5, 10, 20])
        self.assertTrue(all(trade.get("lot_buy_price_usd") is None for trade in sells))
        self.assertGreater(sells[0]["gross_amount"], 300)

    def test_cost_deleverage_respects_cooldown_and_min_sell_amount(self):
        common = dict(
            initial_cash=1000,
            max_drawdown_pct=50,
            step_pct=5,
            equal_slice_allocation_pct=5,
            trade_fee=0,
            reserve_position_pct=0,
            sell_min_profit_pct=0,
            cost_first_profit_pct=5,
            cost_second_profit_pct=10,
            cost_third_profit_pct=20,
            cost_first_sell_pct=30,
            cost_second_sell_pct=30,
            cost_third_sell_pct=30,
        )
        blocked_by_min_amount = simulate_portfolio(
            {"TSLA.US": points(100, 95, 90, 85, 100, 110, 125)},
            [PortfolioTarget("TSLA.US", 100, "TSLA")],
            StrategyInputs(**common, cost_deleverage_cooldown_days=0, cost_min_sell_amount=500),
            strategies=("equal_slice",),
            sell_strategies=("cost_deleverage",),
        )
        self.assertEqual(
            [trade for trade in blocked_by_min_amount["strategies"][0]["trades"] if trade["action"] == "sell"],
            [],
        )

        cooled_down = simulate_portfolio(
            {"TSLA.US": points(100, 95, 90, 85, 100, 110, 125)},
            [PortfolioTarget("TSLA.US", 100, "TSLA")],
            StrategyInputs(**common, cost_deleverage_cooldown_days=3, cost_min_sell_amount=0),
            strategies=("equal_slice",),
            sell_strategies=("cost_deleverage",),
        )
        sells = [trade for trade in cooled_down["strategies"][0]["trades"] if trade["action"] == "sell"]
        self.assertEqual([trade["trigger_value"] for trade in sells], [5])

    def test_cost_deleverage_default_skips_buy_day_sell_until_next_trading_day(self):
        result = simulate_portfolio(
            {
                "TSLA.US": dated_points(
                    ("2024-01-02", 100),
                    ("2024-02-01", 115),
                    ("2024-02-02", 115),
                )
            },
            [PortfolioTarget("TSLA.US", 100, "TSLA")],
            StrategyInputs(
                initial_cash=10000,
                monthly_contribution=1000,
                trade_fee=0,
                reserve_position_pct=0,
                sell_min_profit_pct=0,
                cost_first_profit_pct=5,
                cost_second_profit_pct=10,
                cost_third_profit_pct=20,
                cost_first_sell_pct=10,
                cost_second_sell_pct=10,
                cost_third_sell_pct=10,
                sell_allow_same_day_sell=False,
            ),
            strategies=("weekly_dca",),
            sell_strategies=("cost_deleverage",),
        )

        sells = [trade for trade in result["strategies"][0]["trades"] if trade["action"] == "sell"]
        self.assertEqual([trade["date"] for trade in sells], ["2024-02-02"])

    def test_cost_deleverage_can_sell_after_same_day_buy_when_enabled(self):
        result = simulate_portfolio(
            {
                "TSLA.US": dated_points(
                    ("2024-01-02", 100),
                    ("2024-02-01", 115),
                    ("2024-02-02", 115),
                )
            },
            [PortfolioTarget("TSLA.US", 100, "TSLA")],
            StrategyInputs(
                initial_cash=10000,
                monthly_contribution=1000,
                trade_fee=0,
                reserve_position_pct=0,
                sell_min_profit_pct=0,
                cost_first_profit_pct=5,
                cost_second_profit_pct=10,
                cost_third_profit_pct=20,
                cost_first_sell_pct=10,
                cost_second_sell_pct=10,
                cost_third_sell_pct=10,
                sell_allow_same_day_sell=True,
            ),
            strategies=("weekly_dca",),
            sell_strategies=("cost_deleverage",),
        )

        trades = result["strategies"][0]["trades"]
        sells = [trade for trade in trades if trade["action"] == "sell"]
        self.assertEqual(sells[0]["date"], "2024-02-01")
        self.assertEqual([trade["action"] for trade in trades if trade["date"] == "2024-02-01"], ["buy", "sell"])

    def test_salary_flow_rearms_position_repair_after_drawdown_buy(self):
        inputs = StrategyInputs(
            initial_cash=10000,
            monthly_contribution=1000,
            max_drawdown_pct=50,
            drawdown_basis="ath",
            step_pct=5,
            trade_fee=0,
            reserve_position_pct=0,
            sell_min_profit_pct=0,
            repair_sell_cooldown_days=0,
            repair_stage_sell_pct=25,
        )
        result = simulate_portfolio(
            {
                "TSLA.US": dated_points(
                    ("2024-01-01", 100),
                    ("2024-01-02", 98),
                    ("2024-01-08", 80),
                    ("2024-01-09", 100),
                    ("2024-01-10", 105),
                    ("2024-01-11", 110),
                    ("2024-01-15", 95),
                    ("2024-01-16", 115),
                    ("2024-01-17", 120),
                    ("2024-01-18", 125),
                )
            },
            [PortfolioTarget("TSLA.US", 100, "TSLA")],
            inputs,
            strategies=("salary_flow_dca",),
            sell_strategies=("repair_step",),
        )

        trades = result["strategies"][0]["trades"]
        sells = [trade for trade in trades if trade["action"] == "sell"]
        rearming_buys = [
            trade for trade in trades
            if trade["action"] == "buy" and trade.get("sell_cycle_rearmed")
        ]
        self.assertEqual(len(sells), 6)
        self.assertEqual([trade["date"] for trade in rearming_buys], ["2024-01-15"])
        self.assertEqual([trade["date"] for trade in sells[-3:]], ["2024-01-16", "2024-01-17", "2024-01-18"])

    def test_dca_rearm_drawdown_threshold_can_delay_repair_cycle_reset(self):
        inputs = StrategyInputs(
            initial_cash=10000,
            monthly_contribution=1000,
            max_drawdown_pct=50,
            drawdown_basis="ath",
            step_pct=5,
            trade_fee=0,
            reserve_position_pct=0,
            sell_min_profit_pct=0,
            repair_sell_cooldown_days=0,
            repair_stage_sell_pct=25,
            dca_rearm_drawdown_pct=25,
        )
        result = simulate_portfolio(
            {
                "TSLA.US": dated_points(
                    ("2024-01-01", 100),
                    ("2024-01-02", 98),
                    ("2024-01-08", 80),
                    ("2024-01-09", 100),
                    ("2024-01-10", 105),
                    ("2024-01-11", 110),
                    ("2024-01-15", 95),
                    ("2024-01-16", 115),
                    ("2024-01-17", 120),
                    ("2024-01-18", 125),
                )
            },
            [PortfolioTarget("TSLA.US", 100, "TSLA")],
            inputs,
            strategies=("salary_flow_dca",),
            sell_strategies=("repair_step",),
        )

        trades = result["strategies"][0]["trades"]
        sells = [trade for trade in trades if trade["action"] == "sell"]
        rearming_buys = [
            trade for trade in trades
            if trade["action"] == "buy" and trade.get("sell_cycle_rearmed")
        ]
        self.assertEqual(len(sells), 3)
        self.assertEqual(rearming_buys, [])

    def test_sell_stage_rearm_defaults_to_dca_rearm_threshold(self):
        state = SymbolState(symbol="TSLA.US", name="TSLA", weight=100, budget=10000, cash=0, sell_marks={"cost_1"})
        inputs = StrategyInputs(max_drawdown_pct=50, dca_rearm_drawdown_pct=5)

        rearmed = _rearm_position_sell_cycle_after_dca_buy(state, 5, inputs, "cost_deleverage")

        self.assertTrue(rearmed)
        self.assertEqual(state.sell_marks, set())

    def test_grid_rebound_rearm_resets_cycle_anchor_after_dca_buy(self):
        state = SymbolState(
            symbol="TSLA.US",
            name="TSLA",
            weight=100,
            budget=10000,
            cash=0,
            sell_marks={"grid_1"},
            grid_rebound_cycle_anchor_drawdown_pct=25,
        )
        inputs = StrategyInputs(max_drawdown_pct=50, dca_rearm_drawdown_pct=10)

        rearmed = _rearm_position_sell_cycle_after_dca_buy(state, 10, inputs, "grid_rebound")

        self.assertTrue(rearmed)
        self.assertEqual(state.sell_marks, set())
        self.assertIsNone(state.grid_rebound_cycle_anchor_drawdown_pct)

    def test_sell_stage_rearm_can_delay_cost_mark_reset_after_dca_buy(self):
        state = SymbolState(symbol="TSLA.US", name="TSLA", weight=100, budget=10000, cash=0, sell_marks={"cost_1"})
        inputs = StrategyInputs(
            max_drawdown_pct=50,
            dca_rearm_drawdown_pct=5,
            sell_stage_rearm_drawdown_pct=15,
        )

        shallow_rearmed = _rearm_position_sell_cycle_after_dca_buy(state, 5, inputs, "cost_deleverage")
        deep_rearmed = _rearm_position_sell_cycle_after_dca_buy(state, 15, inputs, "cost_deleverage")

        self.assertFalse(shallow_rearmed)
        self.assertTrue(deep_rearmed)
        self.assertEqual(state.sell_marks, set())

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
