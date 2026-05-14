import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from drawdown.generate_drawdown_report import CachedDailyCandle
from drawdown.position_strategy import StrategyInputs, run_longbridge_robust_leaderboard


def candles(*prices: float):
    start = datetime(2021, 1, 1)
    return [
        CachedDailyCandle(start + timedelta(days=index), price)
        for index, price in enumerate(prices)
    ]


class StrategyLabRobustTest(unittest.TestCase):
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
                    coarse_shortlist_size=4,
                    finalist_size=6,
                )

        self.assertEqual(len(result["tasks"]), 1)
        self.assertLessEqual(len(result["leaderboard"]), 5)
        self.assertGreater(result["candidate_counts"]["coarse"], 0)
        self.assertGreater(result["candidate_counts"]["fine"], 0)
        top = result["leaderboard"][0]
        self.assertIn("candidate", top)
        self.assertIn("robust_score", top)
        self.assertGreaterEqual(top["top10_rate"], 0)
        self.assertGreaterEqual(top["bottom10_rate"], 0)
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
                    coarse_shortlist_size=8,
                    finalist_size=10,
                )

        candidates = [row["candidate"] for row in result["leaderboard"]]
        self.assertTrue(any(candidate.get("step_pct") is not None for candidate in candidates))
        self.assertTrue(any(candidate.get("equal_slice_allocation_pct") is not None for candidate in candidates))

    def test_return_drawdown_mode_is_supported(self):
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
                    coarse_shortlist_size=3,
                    finalist_size=4,
                    score_mode="return_drawdown",
                )

        self.assertEqual(result["method"]["score_mode"], "return_drawdown")
        self.assertEqual(result["method"]["score_formula"], "80% return + 20% drawdown")
        self.assertLessEqual(len(result["leaderboard"]), 3)

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
                    coarse_shortlist_size=3,
                    finalist_size=4,
                )

        self.assertEqual([task["period_key"] for task in result["tasks"]], ["1y"])


if __name__ == "__main__":
    unittest.main()
