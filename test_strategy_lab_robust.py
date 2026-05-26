import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from drawdown.generate_drawdown_report import CachedDailyCandle
from drawdown.position_strategy import (
    StrategyLabComputationCancelled,
    StrategyInputs,
    _cost_deleverage_candidates,
    _dca_repair_candidates,
    _grid_rebound_candidates,
    _non_repair_candidates,
    _repair_candidates,
    prepare_robust_leaderboard_packet,
    run_longbridge_robust_leaderboard,
)


def candles(*prices: float):
    start = datetime(2021, 1, 1)
    return [
        CachedDailyCandle(start + timedelta(days=index), price)
        for index, price in enumerate(prices)
    ]


class StrategyLabRobustTest(unittest.TestCase):
    def test_robust_leaderboard_honors_cancel_checker(self):
        def fake_fetch(_ctx, _symbol, _start, _end):
            return candles(100, 95, 105, 110)

        def cancel_checker():
            raise StrategyLabComputationCancelled("stop")

        with patch("drawdown.position_strategy.build_longbridge_quote_context", return_value=object()):
            with patch("drawdown.position_strategy.fetch_longbridge_daily_candles", side_effect=fake_fetch):
                with self.assertRaises(StrategyLabComputationCancelled):
                    run_longbridge_robust_leaderboard(
                        StrategyInputs(initial_cash=1000, monthly_contribution=0, trade_fee=0),
                        end_date=datetime(2021, 1, 4).date(),
                        portfolio_keys=["tsla_100"],
                        scorecard_periods=[
                            {"key": "1y", "label": "短期", "start": "2021-01-01", "end": "2021-01-04"},
                        ],
                        buy_strategies=["weekly_dca"],
                        sell_strategies=["none"],
                        control_checker=cancel_checker,
                    )

    def test_prepare_robust_leaderboard_packet_includes_tasks_and_candidates(self):
        def fake_fetch(_ctx, _symbol, _start, _end):
            return candles(100, 95, 105, 110)

        with patch("drawdown.position_strategy.build_longbridge_quote_context", return_value=object()):
            with patch("drawdown.position_strategy.fetch_longbridge_daily_candles", side_effect=fake_fetch):
                packet = prepare_robust_leaderboard_packet(
                    StrategyInputs(initial_cash=1000, monthly_contribution=0, trade_fee=0),
                    end_date=datetime(2021, 1, 4).date(),
                    portfolio_keys=["tsla_100"],
                    scorecard_periods=[
                        {"key": "1y", "label": "短期", "start": "2021-01-01", "end": "2021-01-04"},
                    ],
                    buy_strategies=["weekly_dca"],
                    sell_strategies=["none"],
                )

        self.assertEqual(len(packet["tasks"]), 1)
        self.assertIn("price_points", packet["tasks"][0])
        self.assertGreater(len(packet["candidate_pool"]), 0)
        self.assertNotIn("candidate_neighborhoods", packet)
        self.assertNotIn("coarse_candidates", packet)
        self.assertEqual(packet["method"]["name"], "client_full_candidate_return_90_drawdown_10_packet")
        self.assertNotIn("compute_mode", packet["method"])

    def test_robust_leaderboard_returns_top_candidates(self):
        def fake_fetch(_ctx, symbol, _start, _end):
            if symbol == "TSLA.US":
                return candles(100, 90, 82, 76, 90, 110, 95, 125, 140, 130)
            return candles(100, 98, 96, 101, 105, 104, 110, 116, 118, 122)

        with patch("drawdown.position_strategy.build_longbridge_quote_context", return_value=object()):
            with patch("drawdown.position_strategy.fetch_longbridge_daily_candles", side_effect=fake_fetch):
                result = run_longbridge_robust_leaderboard(
                    StrategyInputs(initial_cash=1000, monthly_contribution=0, trade_fee=0, max_drawdown_pct=40),
                    end_date=datetime(2021, 1, 10).date(),
                    portfolio_keys=["tsla_100"],
                    scorecard_periods=[
                        {"key": "1y", "label": "短期", "start": "2021-01-01", "end": "2021-01-10"},
                    ],
                    buy_strategies=["pyramid_3", "weekly_dca"],
                    top_n=5,
                )

        self.assertEqual(len(result["tasks"]), 1)
        self.assertLessEqual(len(result["leaderboard"]), 5)
        self.assertGreater(result["candidate_counts"]["total"], 0)
        top = result["leaderboard"][0]
        self.assertIn("candidate", top)
        self.assertIn("robust_score", top)
        self.assertNotIn("top10_rate", top)
        self.assertNotIn("bottom10_rate", top)
        self.assertNotIn("p25_score", top)
        self.assertEqual(top["task_count"], 1)

    def test_robust_leaderboard_includes_buy_parameter_candidates(self):
        def fake_fetch(_ctx, _symbol, _start, _end):
            return candles(100, 94, 88, 82, 96, 112, 108, 125)

        with patch("drawdown.position_strategy.build_longbridge_quote_context", return_value=object()):
            with patch("drawdown.position_strategy.fetch_longbridge_daily_candles", side_effect=fake_fetch):
                result = run_longbridge_robust_leaderboard(
                    StrategyInputs(initial_cash=1000, monthly_contribution=0, trade_fee=0, max_drawdown_pct=40),
                    end_date=datetime(2021, 1, 8).date(),
                    portfolio_keys=["tsla_100"],
                    buy_strategies=["equal_slice"],
                    top_n=10,
                )

        candidates = [row["candidate"] for row in result["leaderboard"]]
        self.assertTrue(any(candidate.get("step_pct") is not None for candidate in candidates))
        self.assertTrue(any(candidate.get("equal_slice_allocation_pct") is not None for candidate in candidates))
        self.assertTrue(all(candidate.get("sell_strategy") != "repair_step" for candidate in candidates))

    def test_robust_leaderboard_includes_core_dip_parameter_candidates(self):
        def fake_fetch(_ctx, _symbol, _start, _end):
            return candles(100, 98, 94, 90, 96, 108, 120, 132)

        with patch("drawdown.position_strategy.build_longbridge_quote_context", return_value=object()):
            with patch("drawdown.position_strategy.fetch_longbridge_daily_candles", side_effect=fake_fetch):
                result = run_longbridge_robust_leaderboard(
                    StrategyInputs(initial_cash=1000, monthly_contribution=100, trade_fee=0, max_drawdown_pct=40),
                    end_date=datetime(2021, 1, 8).date(),
                    portfolio_keys=["tsla_100"],
                    buy_strategies=["core_dip_dca"],
                    sell_strategies=["none"],
                    top_n=10,
                )

        candidates = [row["candidate"] for row in result["leaderboard"]]
        self.assertTrue(candidates)
        self.assertTrue(all(candidate["buy_strategy"] == "core_dip_dca" for candidate in candidates))
        self.assertTrue(any(candidate.get("core_dip_initial_core_pct") is not None for candidate in candidates))
        self.assertTrue(any(candidate.get("core_dip_timing_enabled") for candidate in candidates))
        self.assertTrue(any("买点优化" in candidate["label"] for candidate in candidates))
        self.assertEqual(result["candidate_counts"]["total"], 168)
        self.assertEqual(result["method"]["parameter_grid"]["core_dip_timing_max_delay_days"], [1, 3, 5])
        self.assertEqual(result["method"]["parameter_grid"]["core_dip_timing_rise_threshold_pct"], [1.0, 1.5, 2.5])
        self.assertEqual(result["method"]["parameter_grid"]["core_dip_timing_near_low_pct"], [1.0, 2.0, 3.0])

    def test_robust_leaderboard_can_filter_core_dip_timing_candidates(self):
        def fake_fetch(_ctx, _symbol, _start, _end):
            return candles(100, 98, 94, 90, 96, 108, 120, 132)

        with patch("drawdown.position_strategy.build_longbridge_quote_context", return_value=object()):
            with patch("drawdown.position_strategy.fetch_longbridge_daily_candles", side_effect=fake_fetch):
                result = run_longbridge_robust_leaderboard(
                    StrategyInputs(initial_cash=1000, monthly_contribution=100, trade_fee=0, max_drawdown_pct=40),
                    end_date=datetime(2021, 1, 8).date(),
                    portfolio_keys=["tsla_100"],
                    buy_strategies=["core_dip_dca"],
                    sell_strategies=["none"],
                    top_n=10,
                    core_dip_timing_filter="enabled",
                )

        candidates = [row["candidate"] for row in result["leaderboard"]]
        self.assertTrue(candidates)
        self.assertTrue(all(candidate.get("core_dip_timing_enabled") for candidate in candidates))
        self.assertTrue(all("买点优化" in candidate["label"] for candidate in candidates))
        self.assertEqual(result["candidate_counts"]["total"], 162)

    def test_robust_leaderboard_respects_selected_sell_strategies(self):
        def fake_fetch(_ctx, _symbol, _start, _end):
            return candles(100, 92, 84, 76, 94, 112, 106, 126)

        with patch("drawdown.position_strategy.build_longbridge_quote_context", return_value=object()):
            with patch("drawdown.position_strategy.fetch_longbridge_daily_candles", side_effect=fake_fetch):
                result = run_longbridge_robust_leaderboard(
                    StrategyInputs(initial_cash=1000, monthly_contribution=0, trade_fee=0, max_drawdown_pct=40),
                    end_date=datetime(2021, 1, 8).date(),
                    portfolio_keys=["tsla_100"],
                    buy_strategies=["pyramid_3", "equal_slice"],
                    sell_strategies=["grid_rebound"],
                    top_n=10,
                )

        self.assertEqual(result["sell_strategies"], ["grid_rebound"])
        self.assertEqual(result["method"]["parameter_grid"]["grid_rebound_step_pct"], [2.5, 5.0, 7.5, 10.0, 15.0])
        self.assertEqual(result["method"]["parameter_grid"]["grid_sell_pct"], [15.0, 25.0, 40.0, 50.0])
        self.assertEqual(result["method"]["parameter_grid"]["dca_rearm_drawdown_pct"], [0.0, 5.0, 10.0, 15.0, 20.0])
        candidates = [row["candidate"] for row in result["leaderboard"]]
        self.assertTrue(candidates)
        self.assertTrue(all(candidate["sell_strategy"] == "grid_rebound" for candidate in candidates))
        self.assertTrue(all(candidate.get("grid_rebound_step_pct") is not None for candidate in candidates))
        self.assertIn("simulation_counts", result)
        self.assertGreater(result["simulation_counts"]["total"], 0)

    def test_robust_leaderboard_includes_cost_deleverage_parameters(self):
        def fake_fetch(_ctx, _symbol, _start, _end):
            return candles(100, 94, 88, 80, 96, 112, 128, 140)

        with patch("drawdown.position_strategy.build_longbridge_quote_context", return_value=object()):
            with patch("drawdown.position_strategy.fetch_longbridge_daily_candles", side_effect=fake_fetch):
                result = run_longbridge_robust_leaderboard(
                    StrategyInputs(initial_cash=1000, monthly_contribution=0, trade_fee=0, max_drawdown_pct=40),
                    end_date=datetime(2021, 1, 8).date(),
                    portfolio_keys=["tsla_100"],
                    buy_strategies=["equal_slice"],
                    sell_strategies=["cost_deleverage"],
                    top_n=10,
                )

        self.assertEqual(result["sell_strategies"], ["cost_deleverage"])
        candidates = [row["candidate"] for row in result["leaderboard"]]
        self.assertTrue(candidates)
        self.assertTrue(all(candidate["sell_strategy"] == "cost_deleverage" for candidate in candidates))
        self.assertTrue(all(candidate.get("cost_first_profit_pct") is not None for candidate in candidates))
        self.assertIn("cost_profit_sets", result["method"]["parameter_grid"])
        self.assertGreater(result["simulation_counts"]["total"], 0)

    def test_repair_parameter_grid_is_only_generated_for_pyramid(self):
        params = [(5, 0, 8)]

        self.assertEqual(_repair_candidates(["equal_slice", "weekly_dca"], params), [])
        pyramid_candidates = _repair_candidates(["pyramid_3"], params)
        self.assertEqual(len(pyramid_candidates), 10)
        self.assertEqual(
            {candidate.get("dca_rearm_drawdown_pct") for candidate in pyramid_candidates},
            {0.0, 5.0, 10.0, 15.0, 20.0},
        )
        self.assertEqual({candidate.get("sell_allow_same_day_sell") for candidate in pyramid_candidates}, {False, True})

    def test_dca_rearm_candidates_are_generated_for_robust_top10(self):
        inputs = StrategyInputs(sell_min_profit_pct=10, repair_sell_cooldown_days=30, repair_stage_sell_pct=12)
        non_repair = _non_repair_candidates(["weekly_dca", "core_dip_dca"])
        pyramid_repair = _repair_candidates(["pyramid_3"], [(10, 30, 12)])
        repair = _dca_repair_candidates(["weekly_dca", "core_dip_dca", "pyramid_3"], inputs)
        grid = _grid_rebound_candidates(["weekly_dca", "core_dip_dca", "pyramid_3"], inputs)
        cost = _cost_deleverage_candidates(["weekly_dca", "core_dip_dca", "pyramid_3"], inputs)

        rearm_values = {
            candidate.get("dca_rearm_drawdown_pct")
            for candidate in non_repair + pyramid_repair + repair + grid + cost
            if candidate.get("dca_rearm_drawdown_pct") is not None
        }
        self.assertEqual(rearm_values, {0.0, 5.0, 10.0, 15.0, 20.0})
        self.assertTrue(any(candidate["sell_strategy"] == "repair_step" for candidate in repair))
        self.assertEqual({candidate["buy_strategy"] for candidate in repair}, {"weekly_dca", "core_dip_dca", "pyramid_3"})
        self.assertTrue(
            all(candidate.get("dca_rearm_drawdown_pct") is not None for candidate in pyramid_repair)
        )
        self.assertTrue(
            any(candidate["buy_strategy"] == "pyramid_3" and candidate.get("dca_rearm_drawdown_pct") == 20.0 for candidate in grid + cost)
        )
        self.assertTrue(any(candidate.get("core_dip_timing_enabled") for candidate in non_repair + repair + grid + cost))

    def test_robust_ranking_ignores_runtime_weight_overrides(self):
        def fake_fetch(_ctx, _symbol, _start, _end):
            return candles(100, 92, 85, 100, 118, 135, 128, 150)

        with patch("drawdown.position_strategy.build_longbridge_quote_context", return_value=object()):
            with patch("drawdown.position_strategy.fetch_longbridge_daily_candles", side_effect=fake_fetch):
                result = run_longbridge_robust_leaderboard(
                    StrategyInputs(initial_cash=1000, monthly_contribution=0, trade_fee=0, max_drawdown_pct=40),
                    end_date=datetime(2021, 1, 8).date(),
                    portfolio_keys=["tsla_100"],
                    buy_strategies=["pyramid_3"],
                    top_n=3,
                    return_weight=0.91,
                    drawdown_weight=0.09,
                )

        self.assertEqual(result["method"]["ranking_formula"], "return_90_drawdown_10")
        self.assertNotIn("score_mode", result["method"])
        self.assertEqual(result["method"]["score_formula"], "90% return + 10% drawdown")
        self.assertEqual(result["method"]["weights"], {"return": 0.9, "drawdown": 0.1})
        self.assertLessEqual(len(result["leaderboard"]), 3)

    def test_scorecard_mode_uses_scorecard_summary_formula(self):
        def fake_fetch(_ctx, _symbol, _start, _end):
            return candles(100, 92, 85, 100, 118, 135, 128, 150)

        with patch("drawdown.position_strategy.build_longbridge_quote_context", return_value=object()):
            with patch("drawdown.position_strategy.fetch_longbridge_daily_candles", side_effect=fake_fetch):
                result = run_longbridge_robust_leaderboard(
                    StrategyInputs(initial_cash=1000, monthly_contribution=0, trade_fee=0, max_drawdown_pct=40),
                    end_date=datetime(2021, 1, 8).date(),
                    portfolio_keys=["tsla_100"],
                    buy_strategies=["pyramid_3", "weekly_dca"],
                    sell_strategies=["none"],
                    top_n=3,
                    return_weight=0.9,
                    drawdown_weight=0.1,
                )

        self.assertEqual(result["method"]["ranking_formula"], "return_90_drawdown_10")
        self.assertEqual(result["method"]["score_formula"], "90% return + 10% drawdown")
        top = result["leaderboard"][0]
        expected = top["return_score"] * 0.9 + top["drawdown_score"] * 0.1
        self.assertAlmostEqual(top["robust_score"], expected)

    def test_disabled_scorecard_periods_are_not_scored(self):
        def fake_fetch(_ctx, _symbol, _start, _end):
            return candles(100, 96, 90, 105, 116, 111, 125, 138)

        with patch("drawdown.position_strategy.build_longbridge_quote_context", return_value=object()):
            with patch("drawdown.position_strategy.fetch_longbridge_daily_candles", side_effect=fake_fetch):
                result = run_longbridge_robust_leaderboard(
                    StrategyInputs(initial_cash=1000, monthly_contribution=0, trade_fee=0, max_drawdown_pct=40),
                    end_date=datetime(2021, 1, 8).date(),
                    portfolio_keys=["tsla_100"],
                    scorecard_periods=[
                        {"key": "1y", "label": "启用", "start": "2021-01-01", "end": "2021-01-08", "enabled": True},
                        {"key": "3y", "label": "关闭", "start": "2021-01-01", "end": "2021-01-08", "enabled": False},
                    ],
                    buy_strategies=["pyramid_3"],
                    top_n=3,
                )

        self.assertEqual([task["period_key"] for task in result["tasks"]], ["1y"])


if __name__ == "__main__":
    unittest.main()
