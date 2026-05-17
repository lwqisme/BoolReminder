import unittest
from unittest.mock import patch

from web.app import (
    _estimate_robust_server_simulations,
    _prepare_strategy_robust_client_payload,
    _run_strategy_robust_payload,
    _run_strategy_score_payload,
    app,
)


class StrategyLabScorePayloadTest(unittest.TestCase):
    def test_score_payload_passes_selected_sell_strategies(self):
        with patch("web.app.run_longbridge_strategy_scorecard", return_value={"summary": [], "questions": []}) as scorecard:
            _run_strategy_score_payload({
                "end": "2026-05-14",
                "buy_strategies": ["pyramid_3"],
                "score_sell_strategies": ["none", "repair_step", "grid_rebound", "cost_deleverage"],
                "scorecard_portfolio_keys": ["tsla_100"],
                "scorecard_periods": [{"key": "1y", "enabled": True}],
                "targets": [{"symbol": "TSLA.US", "weight": 100}],
            })

        kwargs = scorecard.call_args.kwargs
        self.assertEqual(kwargs["buy_strategies"], ["pyramid_3"])
        self.assertIn("grid_rebound", kwargs["sell_strategies"])

    def test_score_payload_passes_investment_universe(self):
        with patch("web.app.run_longbridge_strategy_scorecard", return_value={"summary": [], "questions": []}) as scorecard:
            _run_strategy_score_payload({
                "end": "2026-05-14",
                "buy_strategies": ["pyramid_3"],
                "score_sell_strategies": ["none"],
                "scorecard_portfolio_keys": ["symbol_msft_us"],
                "scorecard_periods": [{"key": "1y", "enabled": True}],
                "targets": [{"symbol": "MSFT.US", "weight": 100}],
                "investment_universe": [{"symbol": "MSFT.US", "name": "Microsoft", "max_drawdown_pct": 35}],
            })

        kwargs = scorecard.call_args.kwargs
        symbols = [item["symbol"] for item in kwargs["investment_universe"]]
        self.assertIn("MSFT.US", symbols)

    def test_robust_payload_passes_selected_sell_strategies(self):
        with patch("web.app.run_longbridge_robust_leaderboard", return_value={"leaderboard": []}) as robust:
            _run_strategy_robust_payload({
                "end": "2026-05-14",
                "buy_strategies": ["pyramid_3"],
                "sell_strategies": ["grid_rebound"],
                "score_sell_strategies": ["grid_rebound"],
                "scorecard_portfolio_keys": ["tsla_100"],
                "scorecard_periods": [{"key": "1y", "enabled": True}],
                "targets": [{"symbol": "TSLA.US", "weight": 100}],
            })

        kwargs = robust.call_args.kwargs
        self.assertEqual(kwargs["buy_strategies"], ["pyramid_3"])
        self.assertEqual(kwargs["sell_strategies"], ["grid_rebound"])

    def test_robust_payload_prefers_top10_sell_strategies_over_scorecard_filter(self):
        with patch("web.app.run_longbridge_robust_leaderboard", return_value={"leaderboard": []}) as robust:
            _run_strategy_robust_payload({
                "end": "2026-05-14",
                "buy_strategies": ["pyramid_3"],
                "sell_strategies": ["grid_rebound"],
                "score_sell_strategies": ["none", "repair_step", "grid_rebound", "cost_deleverage"],
                "scorecard_portfolio_keys": ["tsla_100"],
                "scorecard_periods": [{"key": "1y", "enabled": True}],
                "targets": [{"symbol": "TSLA.US", "weight": 100}],
            })

        kwargs = robust.call_args.kwargs
        self.assertEqual(kwargs["sell_strategies"], ["grid_rebound"])

    def test_robust_payload_rejects_large_server_top10_jobs(self):
        with patch("web.app.SERVER_ROBUST_MAX_SIMULATIONS", 10):
            with self.assertRaisesRegex(ValueError, "超过服务器保护阈值"):
                _run_strategy_robust_payload({
                    "end": "2026-05-14",
                    "buy_strategies": ["pyramid_3", "equal_slice", "linear_weighted_slice", "weekly_dca", "salary_flow_dca", "core_dip_dca"],
                    "sell_strategies": ["none", "repair_step", "grid_rebound", "cost_deleverage"],
                    "scorecard_portfolio_keys": ["tsm_100", "googl_100", "tsla_100", "tencent_100"],
                    "scorecard_periods": [
                        {"key": "1y", "enabled": True},
                        {"key": "3y", "enabled": True},
                        {"key": "5y", "enabled": True},
                    ],
                    "targets": [{"symbol": "TSLA.US", "weight": 100}],
                })

    def test_robust_server_estimate_matches_selected_single_sell_strategy(self):
        grid_only = _estimate_robust_server_simulations(
            ["pyramid_3"],
            ["grid_rebound"],
            ["tsla_100"],
            [{"key": "1y", "enabled": True}],
        )
        all_sells = _estimate_robust_server_simulations(
            ["pyramid_3"],
            ["none", "repair_step", "grid_rebound", "cost_deleverage"],
            ["tsla_100"],
            [{"key": "1y", "enabled": True}],
        )

        self.assertLess(grid_only, all_sells)

    def test_robust_client_packet_endpoint_returns_prepared_packet(self):
        with patch("web.app.prepare_robust_leaderboard_packet", return_value={"tasks": [], "candidate_pool": []}) as prepare:
            with app.test_client() as client:
                response = client.post(
                    "/api/strategy-lab/robust/client-packet",
                    json={
                        "end": "2026-05-14",
                        "buy_strategies": ["salary_flow_dca"],
                        "sell_strategies": ["cost_deleverage"],
                        "robust_concurrency": 4,
                        "scorecard_portfolio_keys": ["tsla_100"],
                        "scorecard_periods": [{"key": "1y", "enabled": True}],
                        "targets": [{"symbol": "TSLA.US", "weight": 100}],
                    },
                )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(
            response.get_json()["packet"],
            {"tasks": [], "candidate_pool": [], "robust_concurrency": 4},
        )
        kwargs = prepare.call_args.kwargs
        self.assertEqual(kwargs["buy_strategies"], ["salary_flow_dca"])
        self.assertEqual(kwargs["sell_strategies"], ["cost_deleverage"])
        self.assertNotIn("compute_mode", kwargs)

    def test_prepare_client_payload_bypasses_server_top10_guard(self):
        with patch("web.app.SERVER_ROBUST_MAX_SIMULATIONS", 1):
            with patch("web.app.prepare_robust_leaderboard_packet", return_value={"tasks": []}) as prepare:
                packet = _prepare_strategy_robust_client_payload({
                    "end": "2026-05-14",
                    "buy_strategies": ["salary_flow_dca"],
                    "sell_strategies": ["cost_deleverage"],
                    "robust_concurrency": 4,
                    "scorecard_portfolio_keys": ["tsla_100"],
                    "scorecard_periods": [{"key": "1y", "enabled": True}],
                    "targets": [{"symbol": "TSLA.US", "weight": 100}],
                })

        self.assertEqual(packet, {"tasks": [], "robust_concurrency": 4})
        self.assertTrue(prepare.called)


if __name__ == "__main__":
    unittest.main()
