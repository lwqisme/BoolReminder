import unittest
from unittest.mock import patch

from web.app import _run_strategy_score_payload


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


if __name__ == "__main__":
    unittest.main()
