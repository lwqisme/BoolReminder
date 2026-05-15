import unittest

from drawdown.strategy_lab_config import StrategyLabConfig, strategy_lab_default_dict


class StrategyLabConfigTest(unittest.TestCase):
    def test_saved_defaults_round_trip_legacy_keys(self):
        defaults = strategy_lab_default_dict()
        config = StrategyLabConfig.from_saved_defaults(defaults)
        self.assertEqual(config.to_legacy_defaults()["default_initial_cash"], 20000.0)
        self.assertEqual(config.to_legacy_defaults()["default_dca_rearm_drawdown_pct"], 5.0)
        self.assertEqual(config.drawdown_basis, "rolling_120")
        self.assertEqual(config.selected_scorecard_keys(), ["tsm_100", "googl_100", "tsla_100", "core_50_30_20"])

    def test_runtime_payload_builds_strategy_inputs_and_weights(self):
        config = StrategyLabConfig.from_runtime_payload(
            {
                "initial_cash": 50000,
                "monthly_contribution": 1200,
                "max_drawdown_pct": 45,
                "dca_rearm_drawdown_pct": 10,
                "return_weight": 0.8,
                "drawdown_weight": 0.2,
                "targets": [{"symbol": "TSM.US", "weight": 100, "name": "TSM"}],
            },
            strategy_lab_default_dict(),
        )

        inputs = config.to_strategy_inputs()
        self.assertEqual(inputs.initial_cash, 50000.0)
        self.assertEqual(inputs.monthly_contribution, 1200.0)
        self.assertEqual(inputs.max_drawdown_pct, 45.0)
        self.assertEqual(inputs.dca_rearm_drawdown_pct, 10.0)
        self.assertEqual(config.score_weights(), (0.8, 0.2))
        self.assertEqual(config.portfolio_or_default()[0]["symbol"], "TSM.US")

    def test_defaults_payload_sanitizes_scorecard_keys_and_periods(self):
        config = StrategyLabConfig.from_defaults_payload(
            {
                "default_scorecard_portfolio_keys": ["tsm_100", "bad_key"],
                "default_scorecard_periods": [
                    {"key": "1y", "label": "一年", "start": "2025-01-01", "end": "2025-12-31"},
                    {"key": "bad", "label": "bad"},
                ],
            },
            strategy_lab_default_dict(),
        )

        self.assertEqual(config.scorecard_portfolio_keys, ["tsm_100"])
        self.assertEqual(len(config.scorecard_periods), 1)
        self.assertEqual(config.scorecard_periods[0].label, "一年")

    def test_invalid_strategy_config_is_rejected(self):
        with self.assertRaises(ValueError):
            StrategyLabConfig.from_defaults_payload(
                {"default_option_moneyness": "deep_magic"},
                strategy_lab_default_dict(),
            )


if __name__ == "__main__":
    unittest.main()
