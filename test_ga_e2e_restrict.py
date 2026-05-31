"""Full end-to-end test: GA categorical restriction from payload to results."""

import unittest
from datetime import date, datetime, timedelta
from unittest.mock import patch, MagicMock

from drawdown.position_strategy import StrategyInputs
from drawdown.strategy_parameter_genetic import (
    EvolutionConfig,
    Individual,
    _random_individual,
    _mutate,
    _initialize_population,
    _relevant_buy_parameter_fields,
    _relevant_sell_parameter_fields,
    evolve_parameters,
)


class GaE2ECategoricalRestrictTest(unittest.TestCase):
    """Verify categorical restriction survives full GA pipeline."""

    def test_initial_population_all_respects_restriction(self):
        """Every individual in initial population has restricted buy_rearm_mode."""
        inputs = StrategyInputs()
        bs = ["equal_slice"]
        ss = ["price_rise_grid"]
        buy_f = {bs[0]: _relevant_buy_parameter_fields(bs[0])}
        sell_f = {ss[0]: _relevant_sell_parameter_fields(ss[0], inputs, bs[0])}

        config = EvolutionConfig(
            population_size=30,
            buy_rearm_mode="restart_from_rearm",
        )

        pop = _initialize_population(bs, ss, buy_f, sell_f, inputs, 30, cat_restrict=config)
        self.assertEqual(len(pop), 30)

        for ind in pop:
            if "buy_rearm_mode" in ind.sell_params:
                self.assertEqual(
                    ind.sell_params["buy_rearm_mode"], "restart_from_rearm",
                    f"Individual {ind.key} has {ind.sell_params.get('buy_rearm_mode')}"
                )

    def test_full_evolution_respects_restriction(self):
        """After evolution, all individuals still respect restriction."""
        inputs = StrategyInputs(
            initial_cash=10000.0,
            monthly_contribution=0.0,
            step_pct=5.0,
            sell_min_profit_pct=10.0,
            repair_stage_sell_pct=10.0,
        )
        bs = ["equal_slice"]
        ss = ["price_rise_grid"]

        config = EvolutionConfig(
            population_size=10,
            generations=3,
            mutation_rate=0.3,
            crossover_rate=0.5,
            buy_rearm_mode="restart_from_rearm",
            seed=42,  # deterministic
        )

        # Mock fitness function: random-ish but deterministic
        import random
        random.seed(42)
        fitness_values = {}

        def mock_fitness(ind):
            if ind.key not in fitness_values:
                fitness_values[ind.key] = random.uniform(0, 100)
            return fitness_values[ind.key]

        result = evolve_parameters(bs, ss, inputs, mock_fitness, config)

        final_pop = result.get("final_population", [])
        self.assertGreater(len(final_pop), 0, "Should have final population")

        for ind_data in final_pop:
            ind = ind_data.get("individual") if isinstance(ind_data, dict) else ind_data
            if isinstance(ind, Individual):
                mode = ind.sell_params.get("buy_rearm_mode")
            elif isinstance(ind, dict):
                mode = ind.get("sell_params", {}).get("buy_rearm_mode") or ind.get("buy_rearm_mode")
            else:
                continue
            if mode is not None:
                self.assertEqual(mode, "restart_from_rearm",
                    f"Evolution produced {mode} despite restriction")

    def test_ga_config_serialization_roundtrip(self):
        """gaConfig from packet includes buy_rearm_mode."""
        from drawdown.strategy_parameter_genetic import build_ga_client_manifest

        inputs = StrategyInputs()
        config = EvolutionConfig(
            population_size=5,
            buy_rearm_mode="restart_from_rearm",
            sell_allow_same_day_sell="true",
        )

        manifest = build_ga_client_manifest(["equal_slice"], ["price_rise_grid"], inputs, config)

        # Check sell variants all have restricted buy_rearm_mode
        sell_variants = manifest.get("sell_variants", [])
        self.assertGreater(len(sell_variants), 0)

        # sell_variant_schema: (variant_id, variant_key, strategy_key, *SELL_PARAMETER_FIELDS)
        schema = manifest.get("sell_variant_schema", [])
        try:
            brm_idx = schema.index("buy_rearm_mode")
        except ValueError:
            self.fail("buy_rearm_mode missing from sell_variant_schema")

        for variant in sell_variants:
            mode = variant[brm_idx]
            if mode is not None:
                self.assertEqual(mode, "restart_from_rearm",
                    f"Variant {variant[1]} has buy_rearm_mode={mode}")


if __name__ == "__main__":
    unittest.main()
