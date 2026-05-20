import unittest

from drawdown.strategy_lab_config import StrategyLabConfig, strategy_lab_default_dict


class StrategyLabConfigTest(unittest.TestCase):
    def test_saved_defaults_round_trip_legacy_keys(self):
        defaults = strategy_lab_default_dict()
        config = StrategyLabConfig.from_saved_defaults(defaults)
        self.assertEqual(config.to_legacy_defaults()["default_initial_cash"], 20000.0)
        self.assertEqual(config.to_legacy_defaults()["default_dca_rearm_drawdown_pct"], 5.0)
        self.assertEqual(config.to_legacy_defaults()["default_grid_rebound_step_pct"], 5.0)
        self.assertEqual(config.to_legacy_defaults()["default_grid_first_sell_pct"], 40.0)
        self.assertEqual(config.to_legacy_defaults()["default_grid_second_sell_pct"], 40.0)
        self.assertEqual(config.to_legacy_defaults()["default_grid_min_sell_amount"], 200.0)
        self.assertEqual(config.to_legacy_defaults()["default_cost_first_profit_pct"], 8.0)
        self.assertEqual(config.to_legacy_defaults()["default_cost_third_sell_pct"], 30.0)
        self.assertEqual(config.to_legacy_defaults()["default_cost_deleverage_cooldown_days"], 0)
        self.assertEqual(config.to_legacy_defaults()["default_cost_min_sell_amount"], 0.0)
        self.assertEqual(config.to_legacy_defaults()["default_core_dip_initial_core_pct"], 80.0)
        self.assertEqual(config.to_legacy_defaults()["default_core_dip_weekly_core_pct"], 90.0)
        self.assertEqual(config.to_legacy_defaults()["default_core_dip_cash_reserve_pct"], 8.0)
        self.assertEqual(config.to_legacy_defaults()["default_core_dip_start_drawdown_pct"], 5.0)
        self.assertEqual(config.to_legacy_defaults()["default_core_dip_full_drawdown_pct"], 25.0)
        self.assertFalse(config.to_legacy_defaults()["default_core_dip_timing_enabled"])
        self.assertEqual(config.to_legacy_defaults()["default_core_dip_timing_max_delay_days"], 3)
        self.assertEqual(config.to_legacy_defaults()["default_core_dip_timing_rise_threshold_pct"], 1.5)
        self.assertEqual(config.to_legacy_defaults()["default_core_dip_timing_near_low_pct"], 2.0)
        self.assertEqual(config.drawdown_basis, "rolling_120")
        self.assertEqual(config.selected_scorecard_keys(), ["tsm_100", "googl_100", "tsla_100", "core_50_30_20"])
        self.assertTrue(config.investment_universe_or_default())

    def test_runtime_payload_builds_strategy_inputs_and_weights(self):
        config = StrategyLabConfig.from_runtime_payload(
            {
                "initial_cash": 50000,
                "monthly_contribution": 1200,
                "max_drawdown_pct": 45,
                "dca_rearm_drawdown_pct": 10,
                "grid_rebound_step_pct": 7.5,
                "grid_first_sell_pct": 35,
                "grid_second_sell_pct": 30,
                "grid_min_sell_amount": 300,
                "cost_first_profit_pct": 9,
                "cost_second_profit_pct": 18,
                "cost_third_profit_pct": 28,
                "cost_first_sell_pct": 20,
                "cost_second_sell_pct": 25,
                "cost_third_sell_pct": 35,
                "cost_deleverage_cooldown_days": 15,
                "cost_min_sell_amount": 250,
                "core_dip_initial_core_pct": 90,
                "core_dip_weekly_core_pct": 95,
                "core_dip_cash_reserve_pct": 5,
                "core_dip_start_drawdown_pct": 3,
                "core_dip_full_drawdown_pct": 20,
                "core_dip_timing_enabled": True,
                "core_dip_timing_max_delay_days": 4,
                "core_dip_timing_rise_threshold_pct": 2.5,
                "core_dip_timing_near_low_pct": 1.5,
                "return_weight": 0.91,
                "drawdown_weight": 0.09,
                "targets": [{"symbol": "TSM.US", "weight": 100, "name": "TSM"}],
            },
            strategy_lab_default_dict(),
        )

        inputs = config.to_strategy_inputs()
        self.assertEqual(inputs.initial_cash, 50000.0)
        self.assertEqual(inputs.monthly_contribution, 1200.0)
        self.assertEqual(inputs.max_drawdown_pct, 45.0)
        self.assertEqual(inputs.dca_rearm_drawdown_pct, 10.0)
        self.assertEqual(inputs.grid_rebound_step_pct, 7.5)
        self.assertEqual(inputs.grid_first_sell_pct, 35.0)
        self.assertEqual(inputs.grid_second_sell_pct, 30.0)
        self.assertEqual(inputs.grid_min_sell_amount, 300.0)
        self.assertEqual(inputs.cost_first_profit_pct, 9.0)
        self.assertEqual(inputs.cost_second_profit_pct, 18.0)
        self.assertEqual(inputs.cost_third_profit_pct, 28.0)
        self.assertEqual(inputs.cost_first_sell_pct, 20.0)
        self.assertEqual(inputs.cost_second_sell_pct, 25.0)
        self.assertEqual(inputs.cost_third_sell_pct, 35.0)
        self.assertEqual(inputs.cost_deleverage_cooldown_days, 15)
        self.assertEqual(inputs.cost_min_sell_amount, 250.0)
        self.assertEqual(inputs.core_dip_initial_core_pct, 90.0)
        self.assertEqual(inputs.core_dip_weekly_core_pct, 95.0)
        self.assertEqual(inputs.core_dip_cash_reserve_pct, 5.0)
        self.assertEqual(inputs.core_dip_start_drawdown_pct, 3.0)
        self.assertEqual(inputs.core_dip_full_drawdown_pct, 20.0)
        self.assertTrue(inputs.core_dip_timing_enabled)
        self.assertEqual(inputs.core_dip_timing_max_delay_days, 4)
        self.assertEqual(inputs.core_dip_timing_rise_threshold_pct, 2.5)
        self.assertEqual(inputs.core_dip_timing_near_low_pct, 1.5)
        self.assertEqual(config.score_weights(), (0.9, 0.1))
        self.assertEqual(config.portfolio_or_default()[0]["symbol"], "TSM.US")

    def test_investment_universe_round_trips_and_dedupes(self):
        config = StrategyLabConfig.from_defaults_payload(
            {
                "default_investment_universe": [
                    {"symbol": "MSFT.US", "name": "Microsoft", "max_drawdown_pct": 35},
                    {"symbol": "msft.us", "name": "Duplicate", "max_drawdown_pct": 45},
                ],
            },
            strategy_lab_default_dict(),
        )

        self.assertEqual(config.investment_universe, [{"symbol": "MSFT.US", "name": "Microsoft", "max_drawdown_pct": 35.0}])
        self.assertIn("default_investment_universe", config.to_legacy_defaults())
        merged_symbols = [item["symbol"] for item in config.investment_universe_or_default()]
        self.assertIn("MSFT.US", merged_symbols)

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

    def test_stale_removed_strategy_defaults_fall_back(self):
        config = StrategyLabConfig.from_saved_defaults(
            {
                **strategy_lab_default_dict(),
                "default_buy_strategy": "weighted_slice",
                "default_scan_buy_strategy": "weighted_slice",
            }
        )

        self.assertEqual(config.buy_strategy, "all")
        self.assertEqual(config.scan_buy_strategy, "pyramid_3")

    def test_invalid_strategy_config_is_rejected(self):
        with self.assertRaises(ValueError):
            StrategyLabConfig.from_defaults_payload(
                {"default_option_moneyness": "deep_magic"},
                strategy_lab_default_dict(),
            )

    def test_legacy_option_allocation_default_maps_to_wallet_pct(self):
        config = StrategyLabConfig.from_saved_defaults(
            {
                **strategy_lab_default_dict(),
                "default_option_wallet_pct": "",
                "default_option_allocation_pct": 35,
            }
        )

        self.assertEqual(config.option_wallet_pct, 35.0)


if __name__ == "__main__":
    unittest.main()
