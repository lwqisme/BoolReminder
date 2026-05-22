import gzip
import json
import unittest
from datetime import datetime, timedelta
from unittest.mock import patch

from drawdown.generate_drawdown_report import CachedDailyCandle
from drawdown.position_strategy import StrategyInputs, prepare_robust_leaderboard_packet
from drawdown.strategy_parameter_registry import expand_strategy_candidate_payloads
from web.app import (
    PARAMETER_LAB_PAYLOAD_SCHEMA,
    _estimate_strategy_parameter_lab_payload,
    _estimate_robust_server_simulations,
    _prepare_strategy_parameter_lab_payload,
    _prepare_strategy_robust_client_payload,
    _run_strategy_robust_payload,
    _run_strategy_score_payload,
    app,
)


def synthetic_candles(count: int = 260) -> list[CachedDailyCandle]:
    start = datetime(2025, 1, 1)
    return [
        CachedDailyCandle(start + timedelta(days=index), 100 + (index % 17) - index * 0.03)
        for index in range(count)
    ]


def synthetic_parameter_lab_task(symbol: str = "TSLA.US") -> dict[str, object]:
    return {
        "key": "synthetic__1y",
        "portfolio_key": "synthetic",
        "portfolio_label": f"全仓 {symbol}",
        "period_key": "1y",
        "period_label": "近 1 年",
        "start": datetime(2025, 1, 1).date(),
        "end": datetime(2025, 12, 31).date(),
        "targets": [{"symbol": symbol, "weight": 100.0, "name": symbol, "max_drawdown_pct": None}],
        "price_points": {symbol: [{"date": "2025-01-01", "close": 100.0}]},
    }


class StrategyLabScorePayloadTest(unittest.TestCase):
    def test_removed_option_endpoints_return_404(self):
        with app.test_client() as client:
            quote_response = client.post("/api/" + "option-quote", json={})
            packet_response = client.post("/api/strategy-lab/parameter-lab/" + "option" + "-packet", json={})

        self.assertEqual(quote_response.status_code, 404)
        self.assertEqual(packet_response.status_code, 404)

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

    def test_parameter_lab_packet_endpoint_returns_v3_manifest_and_cache_metadata(self):
        with patch("web.app._fetch_scorecard_points", return_value=({"TSLA.US": []}, [])):
            with patch("web.app._build_robust_tasks", return_value=[synthetic_parameter_lab_task("TSLA.US")]):
                packet = _prepare_strategy_parameter_lab_payload({
                    "end": "2026-05-14",
                    "buy_strategies": ["salary_flow_dca"],
                    "sell_strategies": ["cost_deleverage"],
                    "run_id": "plab-test-run",
                    "parameter_lab_concurrency": 6,
                    "scorecard_portfolio_keys": ["tsla_100"],
                    "scorecard_periods": [{"key": "1y", "enabled": True}],
                    "targets": [{"symbol": "TSLA.US", "weight": 100}],
                })

        self.assertEqual(packet["parameter_lab_concurrency"], 6)
        self.assertEqual(packet["run_id"], "plab-test-run")
        self.assertEqual(packet["payload_schema"], PARAMETER_LAB_PAYLOAD_SCHEMA)
        self.assertEqual(packet["method"]["name"], "client_strategy_parameter_lab_packet")
        self.assertEqual(packet["method"]["aggregate_formula"], "average_topic_score")
        self.assertIn("registry", packet)
        self.assertIn("cache_metadata", packet)
        self.assertIn("market_data", packet)
        self.assertEqual(packet["candidate_schema"], ["candidate_id", "buy_variant_id", "sell_variant_id"])
        self.assertIn("buy_variants", packet)
        self.assertIn("sell_variants", packet)
        self.assertIn("candidate_rows", packet)
        self.assertNotIn("candidate_pool", packet)
        self.assertNotIn("price_points", packet["tasks"][0])

    def test_parameter_lab_packet_endpoint_gzips_large_packet_response(self):
        large_warning = "x" * 2048
        with patch("web.app._fetch_scorecard_points", return_value=({"TSLA.US": []}, [large_warning] * 8)):
            with patch("web.app._build_robust_tasks", return_value=[synthetic_parameter_lab_task("TSLA.US")]):
                with app.test_client() as client:
                    response = client.post(
                        "/api/strategy-lab/parameter-lab/packet",
                        json={
                            "end": "2026-05-14",
                            "buy_strategies": ["salary_flow_dca"],
                            "sell_strategies": ["cost_deleverage"],
                            "run_id": "plab-gzip-test",
                            "parameter_lab_concurrency": 6,
                            "scorecard_portfolio_keys": ["tsla_100"],
                            "scorecard_periods": [{"key": "1y", "enabled": True}],
                            "targets": [{"symbol": "TSLA.US", "weight": 100}],
                        },
                        headers={"Accept-Encoding": "gzip"},
                    )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.headers.get("Content-Encoding"), "gzip")
        payload = json.loads(gzip.decompress(response.data).decode("utf-8"))
        self.assertTrue(payload["success"])
        self.assertEqual(payload["run_id"], "plab-gzip-test")
        self.assertGreater(int(response.headers["X-Uncompressed-Length"]), len(response.data))
        self.assertIn("X-Payload-Hash", response.headers)

    def test_parameter_lab_estimate_uses_registry_candidate_expansion(self):
        payload = {
            "end": "2025-12-31",
            "buy_strategies": ["salary_flow_dca"],
            "sell_strategies": ["repair_step"],
            "scorecard_portfolio_keys": ["tsm_100"],
            "scorecard_periods": [{"key": "1y", "label": "一年", "start": "2025-01-01", "end": "2025-12-31"}],
            "targets": [{"symbol": "TSM.US", "weight": 100}],
        }

        estimate = _estimate_strategy_parameter_lab_payload(payload)
        expected_candidates = expand_strategy_candidate_payloads(
            ["salary_flow_dca"],
            ["repair_step"],
            StrategyInputs(),
        )

        self.assertEqual(estimate["payload_schema"], PARAMETER_LAB_PAYLOAD_SCHEMA)
        self.assertEqual(estimate["candidate_count"], len(expected_candidates))
        self.assertEqual(estimate["task_count"], 1)
        self.assertEqual(estimate["estimated_simulations"], len(expected_candidates))

    def test_parameter_lab_large_estimate_recommends_up_to_four_workers(self):
        payload = {
            "end": "2025-12-31",
            "buy_strategies": ["salary_flow_dca"],
            "sell_strategies": ["repair_step"],
            "parameter_lab_concurrency": 8,
            "scorecard_portfolio_keys": ["tsm_100"],
            "scorecard_periods": [{"key": "1y", "label": "一年", "start": "2025-01-01", "end": "2025-12-31"}],
            "targets": [{"symbol": "TSM.US", "weight": 100}],
        }

        with patch("web.app.PARAMETER_LAB_LARGE_RUN_GUARDRAIL", 1):
            estimate = _estimate_strategy_parameter_lab_payload(payload)

        self.assertTrue(estimate["requires_confirmation"])
        self.assertEqual(estimate["recommended_worker_count"], 4)

    def test_parameter_lab_packet_endpoint_is_deterministic_for_repeated_gzip_requests(self):
        payload = {
            "end": "2025-12-31",
            "buy_strategies": ["salary_flow_dca"],
            "sell_strategies": ["grid_rebound"],
            "run_id": "plab-stable-size",
            "parameter_lab_concurrency": 4,
            "scorecard_portfolio_keys": ["tsm_100"],
            "scorecard_periods": [{"key": "1y", "label": "一年", "start": "2025-01-01", "end": "2025-12-31"}],
            "targets": [{"symbol": "TSM.US", "weight": 100}],
        }

        with patch("drawdown.position_strategy.build_longbridge_quote_context", return_value=object()):
            with patch("drawdown.position_strategy.fetch_longbridge_daily_candles", return_value=synthetic_candles(365)):
                with app.test_client() as client:
                    first = client.post(
                        "/api/strategy-lab/parameter-lab/packet",
                        json=payload,
                        headers={"Accept-Encoding": "gzip"},
                    )
                    second = client.post(
                        "/api/strategy-lab/parameter-lab/packet",
                        json=payload,
                        headers={"Accept-Encoding": "gzip"},
                    )

        self.assertEqual(first.status_code, 200)
        self.assertEqual(second.status_code, 200)
        self.assertEqual(first.headers.get("X-Payload-Hash"), second.headers.get("X-Payload-Hash"))
        self.assertEqual(first.headers.get("X-Uncompressed-Length"), second.headers.get("X-Uncompressed-Length"))
        self.assertEqual(first.headers.get("X-Candidate-Count"), second.headers.get("X-Candidate-Count"))
        self.assertEqual(first.headers.get("X-Task-Count"), "1")
        self.assertGreater(int(first.headers.get("X-Price-Row-Count", "0")), 0)
        self.assertEqual(len(first.data), len(second.data))
        self.assertLessEqual(
            int(second.headers["X-Uncompressed-Length"]),
            int(first.headers["X-Uncompressed-Length"]),
        )
        packet = json.loads(gzip.decompress(first.data).decode("utf-8"))["packet"]
        self.assertEqual(packet["payload_schema"], PARAMETER_LAB_PAYLOAD_SCHEMA)
        self.assertIn("market_data", packet)
        self.assertGreater(packet["price_row_count"], 0)
        self.assertNotIn("price_points", packet["tasks"][0])
        self.assertIn("buy_variants", packet)
        self.assertIn("sell_variants", packet)
        self.assertIn("candidate_rows", packet)
        self.assertNotIn("candidate_pool", packet)

    def test_parameter_lab_v3_packet_is_substantially_smaller_than_v1_shape(self):
        payload = {
            "end": "2025-12-31",
            "buy_strategies": ["salary_flow_dca"],
            "sell_strategies": ["grid_rebound"],
            "parameter_lab_concurrency": 4,
            "scorecard_portfolio_keys": ["tsm_100"],
            "scorecard_periods": [{"key": "1y", "label": "一年", "start": "2025-01-01", "end": "2025-12-31"}],
            "targets": [{"symbol": "TSM.US", "weight": 100}],
        }

        with patch("drawdown.position_strategy.build_longbridge_quote_context", return_value=object()):
            with patch("drawdown.position_strategy.fetch_longbridge_daily_candles", return_value=synthetic_candles(365)):
                v1_packet = prepare_robust_leaderboard_packet(
                    StrategyInputs(),
                    end_date=datetime(2025, 12, 31).date(),
                    portfolio_keys=["tsm_100"],
                    scorecard_periods=payload["scorecard_periods"],
                    buy_strategies=["salary_flow_dca"],
                    sell_strategies=["grid_rebound"],
                    top_n=1000000,
                )
                v3_packet = _prepare_strategy_parameter_lab_payload(payload)

        v1_size = len(json.dumps(v1_packet, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))
        v3_size = len(json.dumps(v3_packet, ensure_ascii=False, separators=(",", ":")).encode("utf-8"))

        self.assertEqual(v3_packet["payload_schema"], PARAMETER_LAB_PAYLOAD_SCHEMA)
        self.assertNotIn("candidate_pool", v3_packet)
        self.assertLess(v3_size, v1_size * 0.30)


if __name__ == "__main__":
    unittest.main()
