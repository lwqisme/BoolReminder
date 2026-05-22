import unittest

from drawdown.position_strategy import StrategyInputs
from drawdown.strategy_parameter_registry import (
    BUY_PARAMETER_FIELDS,
    SELL_PARAMETER_FIELDS,
    STRATEGY_DEFINITION_VERSION,
    expand_buy_parameter_variants,
    expand_strategy_candidate_payloads,
    strategy_parameter_lab_manifest_payload,
    strategy_registry_payload,
)


class StrategyParameterRegistryTest(unittest.TestCase):
    def test_buy_variant_keys_are_stable_and_canonical(self):
        first = expand_buy_parameter_variants(["equal_slice"], StrategyInputs())
        second = expand_buy_parameter_variants(["equal_slice"], StrategyInputs())

        self.assertEqual([item.variant_key for item in first], [item.variant_key for item in second])
        self.assertEqual(len(first), 12)
        self.assertTrue(all(item.variant_key.startswith("equal_slice:") for item in first))

    def test_core_dip_timing_params_expand_as_candidate_grid(self):
        all_variants = expand_buy_parameter_variants(["core_dip_dca"], StrategyInputs())
        enabled_variants = expand_buy_parameter_variants(
            ["core_dip_dca"],
            StrategyInputs(),
            core_dip_timing_filter="enabled",
        )
        disabled_variants = expand_buy_parameter_variants(
            ["core_dip_dca"],
            StrategyInputs(),
            core_dip_timing_filter="disabled",
        )

        self.assertEqual(len(disabled_variants), 6)
        self.assertEqual(len(enabled_variants), 162)
        self.assertEqual(len(all_variants), 168)
        self.assertEqual(
            {item.parameters["core_dip_timing_max_delay_days"] for item in enabled_variants},
            {1, 3, 5},
        )
        self.assertEqual(
            {item.parameters["core_dip_timing_rise_threshold_pct"] for item in enabled_variants},
            {1.0, 1.5, 2.5},
        )
        self.assertEqual(
            {item.parameters["core_dip_timing_near_low_pct"] for item in enabled_variants},
            {1.0, 2.0, 3.0},
        )

    def test_default_strategy_is_represented_as_valid_variant(self):
        candidates = expand_strategy_candidate_payloads(
            ["pyramid_3"],
            ["none"],
            StrategyInputs(),
        )

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["buy_strategy"], "pyramid_3")
        self.assertEqual(candidate["sell_strategy"], "none")
        self.assertEqual(candidate["strategy_definition_version"], STRATEGY_DEFINITION_VERSION)
        self.assertIn(candidate["buy_variant_key"], candidate["combination_key"])
        self.assertIn(candidate["sell_variant_key"], candidate["combination_key"])

    def test_incompatible_repair_combinations_are_excluded(self):
        equal_slice_candidates = expand_strategy_candidate_payloads(
            ["equal_slice"],
            ["repair_step"],
            StrategyInputs(),
        )
        salary_candidates = expand_strategy_candidate_payloads(
            ["salary_flow_dca"],
            ["repair_step"],
            StrategyInputs(),
        )

        self.assertEqual(equal_slice_candidates, [])
        self.assertTrue(salary_candidates)
        self.assertTrue(all(item["sell_strategy"] == "repair_step" for item in salary_candidates))

    def test_registry_payload_exposes_definitions(self):
        payload = strategy_registry_payload()

        self.assertEqual(payload["version"], STRATEGY_DEFINITION_VERSION)
        self.assertIn("pyramid_3", payload["definitions"])
        self.assertIn("grid_rebound", payload["definitions"])
        self.assertEqual(payload["definitions"]["pyramid_3"]["strategy_type"], "buy")
        self.assertEqual(payload["definitions"]["grid_rebound"]["strategy_type"], "sell")
        self.assertEqual(payload["definitions"]["repair_step"]["parameter_space"]["sell_allow_same_day_sell"], [False, True])
        self.assertEqual(payload["definitions"]["grid_rebound"]["parameter_space"]["sell_allow_same_day_sell"], [False, True])
        cost_space = payload["definitions"]["cost_deleverage"]["parameter_space"]
        self.assertEqual(cost_space["sell_allow_same_day_sell"], [False, True])
        self.assertEqual(cost_space["sell_stage_rearm_drawdown_pct"], [10.0, 15.0])
        core_space = payload["definitions"]["core_dip_dca"]["parameter_space"]
        self.assertEqual(core_space["core_dip_timing_max_delay_days"], [1, 3, 5])
        self.assertEqual(core_space["core_dip_timing_rise_threshold_pct"], [1.0, 1.5, 2.5])
        self.assertEqual(core_space["core_dip_timing_near_low_pct"], [1.0, 2.0, 3.0])

    def test_manifest_candidate_rows_match_full_candidate_expansion(self):
        manifest = strategy_parameter_lab_manifest_payload(
            ["salary_flow_dca", "equal_slice"],
            ["grid_rebound"],
            StrategyInputs(),
        )
        candidates = expand_strategy_candidate_payloads(
            ["salary_flow_dca", "equal_slice"],
            ["grid_rebound"],
            StrategyInputs(),
        )

        self.assertEqual(manifest["candidate_schema"], ["candidate_id", "buy_variant_id", "sell_variant_id"])
        self.assertEqual(len(manifest["candidate_rows"]), len(candidates))
        self.assertLess(len(manifest["buy_variants"]), len(candidates))
        self.assertLess(len(manifest["sell_variants"]), len(candidates))
        self.assertIn("candidate_manifest_hash", manifest)

    def test_active_fields_omitted_preserves_full_candidate_expansion(self):
        default_candidates = expand_strategy_candidate_payloads(
            ["pyramid_3"],
            ["repair_step"],
            StrategyInputs(),
        )
        explicit_default_candidates = expand_strategy_candidate_payloads(
            ["pyramid_3"],
            ["repair_step"],
            StrategyInputs(),
            active_parameter_fields=None,
        )

        self.assertEqual(len(explicit_default_candidates), len(default_candidates))

    def test_selected_values_omitted_preserves_full_candidate_expansion(self):
        default_candidates = expand_strategy_candidate_payloads(
            ["core_dip_dca"],
            ["none"],
            StrategyInputs(),
        )
        selected_candidates = expand_strategy_candidate_payloads(
            ["core_dip_dca"],
            ["none"],
            StrategyInputs(),
            selected_parameter_values={
                "core_dip_initial_core_pct": [70, 80, 85, 90, 95],
                "core_dip_weekly_core_pct": [85, 90, 95, 100],
                "core_dip_cash_reserve_pct": [3, 5, 6, 8, 12],
                "core_dip_start_drawdown_pct": [3, 5, 8, 10],
                "core_dip_full_drawdown_pct": [15, 20, 25, 30],
                "core_dip_timing_enabled": [False, True],
                "core_dip_timing_max_delay_days": [1, 3, 5],
                "core_dip_timing_rise_threshold_pct": [1.0, 1.5, 2.5],
                "core_dip_timing_near_low_pct": [1.0, 2.0, 3.0],
            },
        )

        self.assertEqual(len(selected_candidates), len(default_candidates))

    def test_empty_active_fields_keeps_only_baseline_candidates(self):
        inputs = StrategyInputs(
            sell_min_profit_pct=12.5,
            repair_sell_cooldown_days=45,
            repair_stage_sell_pct=18.0,
            sell_allow_same_day_sell=True,
            dca_rearm_drawdown_pct=15.0,
            sell_stage_rearm_drawdown_pct=10.0,
        )
        candidates = expand_strategy_candidate_payloads(
            ["salary_flow_dca"],
            ["repair_step"],
            inputs,
            active_parameter_fields=[],
        )

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["sell_min_profit_pct"], 12.5)
        self.assertEqual(candidate["repair_sell_cooldown_days"], 45)
        self.assertEqual(candidate["repair_stage_sell_pct"], 18.0)
        self.assertTrue(candidate["sell_allow_same_day_sell"])
        self.assertEqual(candidate["dca_rearm_drawdown_pct"], 15.0)
        self.assertIsNone(candidate["sell_stage_rearm_drawdown_pct"])

    def test_empty_selected_values_keep_only_baseline_candidates(self):
        inputs = StrategyInputs(
            sell_min_profit_pct=12.5,
            repair_sell_cooldown_days=45,
            repair_stage_sell_pct=18.0,
            sell_allow_same_day_sell=True,
            dca_rearm_drawdown_pct=15.0,
            sell_stage_rearm_drawdown_pct=10.0,
        )
        candidates = expand_strategy_candidate_payloads(
            ["salary_flow_dca"],
            ["repair_step"],
            inputs,
            selected_parameter_values={field: [] for field in BUY_PARAMETER_FIELDS + SELL_PARAMETER_FIELDS},
        )

        self.assertEqual(len(candidates), 1)
        candidate = candidates[0]
        self.assertEqual(candidate["sell_min_profit_pct"], 12.5)
        self.assertEqual(candidate["repair_sell_cooldown_days"], 45)
        self.assertEqual(candidate["repair_stage_sell_pct"], 18.0)
        self.assertTrue(candidate["sell_allow_same_day_sell"])
        self.assertEqual(candidate["dca_rearm_drawdown_pct"], 15.0)
        self.assertIsNone(candidate["sell_stage_rearm_drawdown_pct"])

    def test_partial_active_fields_reduce_candidates_and_fix_inactive_values(self):
        inputs = StrategyInputs(step_pct=5.0, equal_slice_allocation_pct=7.5)
        full = expand_strategy_candidate_payloads(
            ["equal_slice"],
            ["none"],
            inputs,
        )
        partial = expand_strategy_candidate_payloads(
            ["equal_slice"],
            ["none"],
            inputs,
            active_parameter_fields=["step_pct"],
        )

        self.assertLess(len(partial), len(full))
        self.assertEqual({item["step_pct"] for item in partial}, {2.5, 5.0, 10.0})
        self.assertEqual({item["equal_slice_allocation_pct"] for item in partial}, {7.5})

    def test_partial_selected_values_reduce_candidates_and_fix_empty_fields(self):
        inputs = StrategyInputs(step_pct=5.0, equal_slice_allocation_pct=7.5)
        full = expand_strategy_candidate_payloads(
            ["equal_slice"],
            ["none"],
            inputs,
        )
        partial = expand_strategy_candidate_payloads(
            ["equal_slice"],
            ["none"],
            inputs,
            selected_parameter_values={
                "step_pct": [2.5, 10.0],
                "equal_slice_allocation_pct": [],
            },
        )

        self.assertLess(len(partial), len(full))
        self.assertEqual({item["step_pct"] for item in partial}, {2.5, 10.0})
        self.assertEqual({item["equal_slice_allocation_pct"] for item in partial}, {7.5})

    def test_grouped_selected_values_filter_existing_core_tuples_only(self):
        candidates = expand_strategy_candidate_payloads(
            ["core_dip_dca"],
            ["none"],
            StrategyInputs(),
            selected_parameter_values={
                "core_dip_initial_core_pct": [70.0, 80.0],
                "core_dip_weekly_core_pct": [85.0, 90.0],
                "core_dip_cash_reserve_pct": [12.0],
                "core_dip_start_drawdown_pct": [10.0],
                "core_dip_full_drawdown_pct": [30.0],
                "core_dip_timing_enabled": [False],
            },
        )

        self.assertEqual(
            {
                (
                    item["core_dip_initial_core_pct"],
                    item["core_dip_weekly_core_pct"],
                    item["core_dip_cash_reserve_pct"],
                    item["core_dip_start_drawdown_pct"],
                    item["core_dip_full_drawdown_pct"],
                )
                for item in candidates
            },
            {(70.0, 85.0, 12.0, 10.0, 30.0)},
        )

    def test_grouped_selected_values_filter_existing_cost_tuples_only(self):
        candidates = expand_strategy_candidate_payloads(
            ["weekly_dca"],
            ["cost_deleverage"],
            StrategyInputs(),
            selected_parameter_values={
                "cost_first_profit_pct": [8.0, 10.0],
                "cost_second_profit_pct": [15.0, 20.0],
                "cost_third_profit_pct": [25.0],
                "sell_allow_same_day_sell": [False],
                "dca_rearm_drawdown_pct": [5.0],
                "sell_stage_rearm_drawdown_pct": [None],
            },
        )

        self.assertEqual(
            {
                (
                    item["cost_first_profit_pct"],
                    item["cost_second_profit_pct"],
                    item["cost_third_profit_pct"],
                )
                for item in candidates
            },
            {(8.0, 15.0, 25.0)},
        )

    def test_selected_sell_stage_rearm_values_respect_dca_constraint(self):
        candidates = expand_strategy_candidate_payloads(
            ["salary_flow_dca"],
            ["repair_step"],
            StrategyInputs(),
            selected_parameter_values={
                "sell_allow_same_day_sell": [False],
                "dca_rearm_drawdown_pct": [10.0],
                "sell_stage_rearm_drawdown_pct": [None, 10.0, 15.0],
            },
        )

        self.assertTrue(candidates)
        self.assertEqual({item["dca_rearm_drawdown_pct"] for item in candidates}, {10.0})
        self.assertEqual({item["sell_stage_rearm_drawdown_pct"] for item in candidates}, {None, 15.0})

    def test_core_timing_details_do_not_expand_when_timing_enabled_is_fixed_off(self):
        candidates = expand_strategy_candidate_payloads(
            ["core_dip_dca"],
            ["none"],
            StrategyInputs(core_dip_timing_enabled=False),
            active_parameter_fields=["core_dip_timing_max_delay_days"],
        )

        self.assertEqual(len(candidates), 1)
        self.assertFalse(candidates[0]["core_dip_timing_enabled"])
        self.assertIsNone(candidates[0]["core_dip_timing_max_delay_days"])

    def test_cost_deleverage_candidates_include_same_day_sell_variants(self):
        candidates = expand_strategy_candidate_payloads(
            ["weekly_dca"],
            ["cost_deleverage"],
            StrategyInputs(),
        )

        same_day = [item for item in candidates if item["sell_allow_same_day_sell"]]
        default = [item for item in candidates if not item["sell_allow_same_day_sell"]]
        self.assertEqual(len(candidates), 540)
        self.assertEqual(len(same_day), len(default))
        self.assertTrue(all("买入日可卖" in item["label"] for item in same_day))
        self.assertTrue(all("same1" in item["key"] for item in same_day))
        protected = [item for item in candidates if item.get("sell_stage_rearm_drawdown_pct") is not None]
        self.assertTrue(protected)
        self.assertTrue(all("sellrearm" in item["key"] for item in protected))
        self.assertTrue(all("卖档重启" in item["label"] for item in protected))
        self.assertTrue(
            all(
                item["sell_stage_rearm_drawdown_pct"] > item["dca_rearm_drawdown_pct"]
                for item in protected
            )
        )
        self.assertNotIn(
            (10.0, 10.0),
            {
                (item.get("dca_rearm_drawdown_pct"), item.get("sell_stage_rearm_drawdown_pct"))
                for item in candidates
            },
        )

    def test_non_none_sell_candidates_include_same_day_sell_variants(self):
        candidates = expand_strategy_candidate_payloads(
            ["salary_flow_dca"],
            ["repair_step", "grid_rebound", "none"],
            StrategyInputs(),
        )

        for sell_strategy in ("repair_step", "grid_rebound"):
            strategy_candidates = [item for item in candidates if item["sell_strategy"] == sell_strategy]
            same_day = [item for item in strategy_candidates if item["sell_allow_same_day_sell"]]
            default = [item for item in strategy_candidates if not item["sell_allow_same_day_sell"]]
            self.assertTrue(same_day)
            self.assertEqual(len(same_day), len(default))
            self.assertTrue(all("same1" in item["key"] for item in same_day))
            self.assertTrue(all("买入日可卖" in item["label"] for item in same_day))

        none_candidates = [item for item in candidates if item["sell_strategy"] == "none"]
        self.assertEqual(len(none_candidates), 1)
        self.assertIsNone(none_candidates[0]["sell_allow_same_day_sell"])


if __name__ == "__main__":
    unittest.main()
