"""Genetic Algorithm optimization for strategy parameters.

Evolves buy/sell strategy parameter combinations using selection, crossover,
and mutation. Uses existing simulate_portfolio for fitness evaluation.

Designed as a complementary optimization mode to the existing exhaustive grid
search in strategy_parameter_registry.py. Does not modify any existing modules.
"""

from __future__ import annotations

import json
import math
import random
from dataclasses import dataclass, field, replace
from typing import Callable, Iterable, Mapping

from drawdown.position_strategy import (
    BUY_REARM_MODE_CUMULATIVE,
    BUY_REARM_MODE_RESTART_FROM_REARM,
    BUY_REARM_MODES,
    REARM_BUY_STRATEGIES,
    ROBUST_BUY_STEP_VALUES,
    ROBUST_CORE_DIP_PARAM_SETS,
    ROBUST_CORE_DIP_TIMING_MAX_DELAY_DAYS,
    ROBUST_CORE_DIP_TIMING_NEAR_LOW_VALUES,
    ROBUST_CORE_DIP_TIMING_RISE_THRESHOLDS,
    ROBUST_COST_COOLDOWNS,
    ROBUST_COST_PROFIT_SETS,
    ROBUST_COST_SELL_SETS,
    ROBUST_DCA_REARM_DRAWDOWN_VALUES,
    ROBUST_EQUAL_SLICE_ALLOCATION_VALUES,
    ROBUST_GRID_REBOUND_STEPS,
    ROBUST_GRID_SELLS,
    ROBUST_REPAIR_COOLDOWNS,
    ROBUST_REPAIR_SELL_MIN_PROFITS,
    ROBUST_REPAIR_STAGE_SELLS,
    ROBUST_SELL_STAGE_REARM_DRAWDOWN_VALUES,
    SELL_STRATEGY_LABELS,
    STRATEGY_LABELS,
    StrategyInputs,
)
from drawdown.strategy_lab_scoring import (
    DEFAULT_RETURN_WEIGHT,
    DEFAULT_DRAWDOWN_WEIGHT,
)

GA_FORMULA_VERSION = "ga_return_90_drawdown_10_v1"

# ── Parameter mutation ranges ──────────────────────────────────────────────

_BUY_PARAM_RANGES: dict[str, list[object]] = {
    "step_pct": [float(v) for v in ROBUST_BUY_STEP_VALUES],
    "equal_slice_allocation_pct": [float(v) for v in ROBUST_EQUAL_SLICE_ALLOCATION_VALUES],
    "core_dip_initial_core_pct": sorted({s[0] for s in ROBUST_CORE_DIP_PARAM_SETS}),
    "core_dip_weekly_core_pct": sorted({s[1] for s in ROBUST_CORE_DIP_PARAM_SETS}),
    "core_dip_cash_reserve_pct": sorted({s[2] for s in ROBUST_CORE_DIP_PARAM_SETS}),
    "core_dip_start_drawdown_pct": sorted({s[3] for s in ROBUST_CORE_DIP_PARAM_SETS}),
    "core_dip_full_drawdown_pct": sorted({s[4] for s in ROBUST_CORE_DIP_PARAM_SETS}),
    "core_dip_timing_max_delay_days": [int(v) for v in ROBUST_CORE_DIP_TIMING_MAX_DELAY_DAYS],
    "core_dip_timing_rise_threshold_pct": [float(v) for v in ROBUST_CORE_DIP_TIMING_RISE_THRESHOLDS],
    "core_dip_timing_near_low_pct": [float(v) for v in ROBUST_CORE_DIP_TIMING_NEAR_LOW_VALUES],
}

_SELL_PARAM_RANGES: dict[str, list[object]] = {
    "sell_min_profit_pct": [float(v) for v in ROBUST_REPAIR_SELL_MIN_PROFITS],
    "repair_sell_cooldown_days": [int(v) for v in ROBUST_REPAIR_COOLDOWNS],
    "repair_stage_sell_pct": [float(v) for v in ROBUST_REPAIR_STAGE_SELLS],
    "grid_rebound_step_pct": [float(v) for v in ROBUST_GRID_REBOUND_STEPS],
    "grid_sell_pct": [float(v) for v in ROBUST_GRID_SELLS],
    "grid_min_sell_amount": [200.0, 500.0, 1000.0],
    "cost_first_profit_pct": sorted({s[0] for s in ROBUST_COST_PROFIT_SETS}),
    "cost_second_profit_pct": sorted({s[1] for s in ROBUST_COST_PROFIT_SETS}),
    "cost_third_profit_pct": sorted({s[2] for s in ROBUST_COST_PROFIT_SETS}),
    "cost_first_sell_pct": sorted({s[0] for s in ROBUST_COST_SELL_SETS}),
    "cost_second_sell_pct": sorted({s[1] for s in ROBUST_COST_SELL_SETS}),
    "cost_third_sell_pct": sorted({s[2] for s in ROBUST_COST_SELL_SETS}),
    "cost_deleverage_cooldown_days": [int(v) for v in ROBUST_COST_COOLDOWNS],
    "cost_min_sell_amount": [0.0, 200.0, 500.0],
    "dca_rearm_drawdown_pct": [float(v) for v in ROBUST_DCA_REARM_DRAWDOWN_VALUES],
    "sell_stage_rearm_drawdown_pct": [None, *[float(v) for v in ROBUST_SELL_STAGE_REARM_DRAWDOWN_VALUES]],
}

# Fields relevant per strategy type
_RELEVANT_BUY_FIELDS: dict[str, list[str]] = {
    "equal_slice": ["step_pct", "equal_slice_allocation_pct"],
    "linear_weighted_slice": ["step_pct"],
    "core_dip_dca": [
        "core_dip_initial_core_pct",
        "core_dip_weekly_core_pct",
        "core_dip_cash_reserve_pct",
        "core_dip_start_drawdown_pct",
        "core_dip_full_drawdown_pct",
        "core_dip_timing_max_delay_days",
        "core_dip_timing_rise_threshold_pct",
        "core_dip_timing_near_low_pct",
    ],
    "pyramid_3": [],
    "weekly_dca": [],
    "salary_flow_dca": [],
}

_RELEVANT_SELL_FIELDS: dict[str, list[str]] = {
    "repair_step": [
        "sell_min_profit_pct",
        "repair_sell_cooldown_days",
        "repair_stage_sell_pct",
    ],
    "grid_rebound": [
        "grid_rebound_step_pct",
        "grid_sell_pct",
        "grid_min_sell_amount",
    ],
    "cost_deleverage": [
        "cost_first_profit_pct",
        "cost_second_profit_pct",
        "cost_third_profit_pct",
        "cost_first_sell_pct",
        "cost_second_sell_pct",
        "cost_third_sell_pct",
        "cost_deleverage_cooldown_days",
        "cost_min_sell_amount",
    ],
    "none": [],
}

# Common sell fields that apply to all non-none sell strategies
_COMMON_SELL_FIELDS = [
    "sell_allow_same_day_sell",
    "dca_rearm_drawdown_pct",
    "sell_stage_rearm_drawdown_pct",
]

# Fields that use integer values
_INT_FIELDS = frozenset({
    "repair_sell_cooldown_days",
    "cost_deleverage_cooldown_days",
    "core_dip_timing_max_delay_days",
})


@dataclass(frozen=True)
class Individual:
    """A single candidate strategy parameter combination."""
    buy_strategy: str
    sell_strategy: str
    buy_params: dict[str, object] = field(default_factory=dict)
    sell_params: dict[str, object] = field(default_factory=dict)
    key: str = ""

    def __post_init__(self):
        if not self.key:
            object.__setattr__(self, "key", _individual_key(
                self.buy_strategy, self.sell_strategy,
                self.buy_params, self.sell_params,
            ))

    def to_params_dict(self) -> dict[str, object]:
        """Flatten all parameters into a single dict for StrategyInputs construction."""
        result: dict[str, object] = {}
        result.update(self.buy_params)
        result.update(self.sell_params)
        return result

    def to_candidate_payload(self) -> dict[str, object]:
        """Return a payload compatible with strategy_lab_scoring."""
        params = self.to_params_dict()
        return {
            "key": self.key,
            "combination_key": f"buy:{self.buy_strategy}__sell:{self.sell_strategy}",
            "label": f"{STRATEGY_LABELS.get(self.buy_strategy, self.buy_strategy)} / {SELL_STRATEGY_LABELS.get(self.sell_strategy, self.sell_strategy)}",
            "buy_strategy": self.buy_strategy,
            "sell_strategy": self.sell_strategy,
            "buy_variant_key": self.key,
            "sell_variant_key": self.key,
            "strategy_definition_version": "ga-v1",
            **params,
        }


@dataclass
class EvolutionConfig:
    """GA hyperparameters."""
    population_size: int = 50
    generations: int = 20
    mutation_rate: float = 0.15
    crossover_rate: float = 0.80
    elitism_count: int = 3
    tournament_size: int = 4
    seed: int | None = None
    cross_strategy: bool = False
    strategy_mutation_rate: float = 0.05

    def __post_init__(self):
        if self.elitism_count >= self.population_size:
            self.elitism_count = max(1, self.population_size // 5)
        if self.tournament_size >= self.population_size:
            self.tournament_size = max(2, self.population_size // 10)


@dataclass
class GenerationSnapshot:
    """Record of a single generation's state."""
    generation: int
    best_fitness: float
    avg_fitness: float
    worst_fitness: float
    best_individual: Individual
    population_size: int


def evolve_parameters(
    buy_strategy: str | list[str],
    sell_strategy: str | list[str],
    base_inputs: StrategyInputs,
    fitness_fn: Callable[[Individual], float],
    config: EvolutionConfig | None = None,
    *,
    progress_callback: Callable[[GenerationSnapshot], None] | None = None,
    cancel_checker: Callable[[], bool] | None = None,
) -> dict[str, object]:
    """Run genetic algorithm to optimize strategy parameters.

    Args:
        buy_strategy: Strategy key(s) (e.g. "equal_slice" or ["equal_slice", "core_dip_dca"]).
            When a list is passed with config.cross_strategy=True, the GA evolves
            strategy type alongside parameters.
        sell_strategy: Sell strategy key(s).
        base_inputs: Base StrategyInputs for default values.
        fitness_fn: Function that evaluates an Individual and returns its fitness.
        config: GA hyperparameters.
        progress_callback: Called after each generation with a snapshot.
        cancel_checker: If returns True, evolution is cancelled early.

    Returns:
        Dict with "snapshots", "best", "final_population", "config", "formula_version".
    """
    config = config or EvolutionConfig()
    if config.seed is not None:
        random.seed(config.seed)

    buy_strategies = [buy_strategy] if isinstance(buy_strategy, str) else (list(buy_strategy) or list(STRATEGY_LABELS.keys()))
    sell_strategies = [sell_strategy] if isinstance(sell_strategy, str) else (list(sell_strategy) or list(SELL_STRATEGY_LABELS.keys()))
    for bs in buy_strategies:
        if bs not in STRATEGY_LABELS:
            raise ValueError(f"未知买入策略: {bs}")
    for ss in sell_strategies:
        if ss not in SELL_STRATEGY_LABELS:
            raise ValueError(f"未知卖出策略: {ss}")

    all_buy_fields = {bs: _relevant_buy_parameter_fields(bs) for bs in buy_strategies}
    all_sell_fields = {ss: _relevant_sell_parameter_fields(ss, base_inputs, buy_strategies[0]) for ss in sell_strategies}

    population = _initialize_population(
        buy_strategies, sell_strategies, all_buy_fields, all_sell_fields,
        base_inputs, config.population_size, cross_strategy=config.cross_strategy,
    )

    fitnesses = _evaluate_population(population, fitness_fn, cancel_checker)
    if cancel_checker and cancel_checker():
        return _build_cancelled_result(config)

    snapshots: list[dict[str, object]] = []
    best_individual = population[0]
    best_fitness = fitnesses[0]
    all_evaluated: dict[str, tuple[Individual, float]] = {}

    for gen in range(config.generations):
        if cancel_checker and cancel_checker():
            break

        ranked = sorted(
            zip(population, fitnesses),
            key=lambda item: item[1],
            reverse=True,
        )
        population = [item[0] for item in ranked]
        fitnesses = [item[1] for item in ranked]

        for ind, fit in ranked:
            if ind.key not in all_evaluated or fit > all_evaluated[ind.key][1]:
                all_evaluated[ind.key] = (ind, fit)

        gen_best_fitness = fitnesses[0]
        gen_avg_fitness = sum(fitnesses) / len(fitnesses)
        gen_worst_fitness = fitnesses[-1]

        if gen_best_fitness > best_fitness:
            best_fitness = gen_best_fitness
            best_individual = population[0]

        snapshot = GenerationSnapshot(
            generation=gen + 1,
            best_fitness=gen_best_fitness,
            avg_fitness=gen_avg_fitness,
            worst_fitness=gen_worst_fitness,
            best_individual=population[0],
            population_size=len(population),
        )
        snapshots.append(_snapshot_to_dict(snapshot))

        if progress_callback:
            progress_callback(snapshot)

        elites = population[:config.elitism_count]
        next_population: list[Individual] = list(elites)

        while len(next_population) < config.population_size:
            if cancel_checker and cancel_checker():
                break

            parent1 = _tournament_select(population, fitnesses, config.tournament_size)
            parent2 = _tournament_select(population, fitnesses, config.tournament_size)

            if random.random() < config.crossover_rate:
                child = _crossover(parent1, parent2, all_buy_fields, all_sell_fields, base_inputs, cross_strategy=config.cross_strategy)
            else:
                child = parent1

            child = _mutate(child, all_buy_fields, all_sell_fields, base_inputs,
                config.mutation_rate,
                buy_strategies=buy_strategies, sell_strategies=sell_strategies,
                cross_strategy=config.cross_strategy, strategy_mutation_rate=config.strategy_mutation_rate)
            next_population.append(child)

        population = next_population[:config.population_size]
        fitnesses = _evaluate_population(population, fitness_fn, cancel_checker)

    ranked_final = sorted(all_evaluated.items(), key=lambda item: item[1][1], reverse=True)
    final_rows: list[dict[str, object]] = []
    for rank, (key, (ind, fit)) in enumerate(ranked_final, start=1):
        final_rows.append({
            "rank": rank,
            "key": ind.key,
            "buy_strategy": ind.buy_strategy,
            "sell_strategy": ind.sell_strategy,
            "buy_params": dict(ind.buy_params),
            "sell_params": dict(ind.sell_params),
            "fitness": fit,
            "label": ind.to_candidate_payload()["label"],
        })

    return {
        "formula_version": GA_FORMULA_VERSION,
        "weights": {
            "return": DEFAULT_RETURN_WEIGHT,
            "drawdown": DEFAULT_DRAWDOWN_WEIGHT,
        },
        "config": {
            "population_size": config.population_size,
            "generations": config.generations,
            "mutation_rate": config.mutation_rate,
            "crossover_rate": config.crossover_rate,
            "elitism_count": config.elitism_count,
            "tournament_size": config.tournament_size,
            "seed": config.seed,
            "cross_strategy": config.cross_strategy,
            "strategy_mutation_rate": config.strategy_mutation_rate,
        },
        "snapshots": snapshots,
        "best": final_rows[0] if final_rows else None,
        "final_population": final_rows,
        "total_evaluated": len(all_evaluated),
    }


def default_fitness_fn(
    individual: Individual,
    price_points_by_symbol: dict,
    targets: list,
    base_inputs: StrategyInputs,
    *,
    return_weight: float = DEFAULT_RETURN_WEIGHT,
    drawdown_weight: float = DEFAULT_DRAWDOWN_WEIGHT,
) -> float:
    """Default fitness: weighted combination of return and drawdown control.

    Requires simulate_portfolio to be imported at the call site to avoid
    circular imports. This is a pure utility that computes fitness from
    simulation results already available.
    """
    from drawdown.position_strategy import simulate_portfolio

    params = individual.to_params_dict()
    individual_inputs = _apply_params_to_inputs(base_inputs, individual.buy_strategy, individual.sell_strategy, params)
    result = simulate_portfolio(
        price_points_by_symbol,
        targets,
        individual_inputs,
        strategies=[individual.buy_strategy],
        sell_strategies=[individual.sell_strategy],
    )
    strategies = result.get("strategies")
    if isinstance(strategies, list) and strategies:
        strategy_result = strategies[0]
        metrics = strategy_result.get("metrics", {}) if isinstance(strategy_result, dict) else {}
        return_pct = float(metrics.get("return_pct", 0) or 0)
        max_dd = float(metrics.get("max_drawdown_pct", 0) or 0)
        # Fitness: higher return is better, less drawdown (more negative) is worse
        # Normalize drawdown to positive: max_drawdown_pct is negative (e.g. -30)
        # So -max_dd is positive (e.g. 30 means 30% drawdown control)
        fitness = return_pct * return_weight + max(-max_dd, 0) * drawdown_weight
        return fitness
    return 0.0


def make_fitness_fn(
    buy_strategies: str | list[str],
    sell_strategies: str | list[str],
    price_points_by_symbol: dict,
    targets: list,
    base_inputs: StrategyInputs,
    *,
    return_weight: float = DEFAULT_RETURN_WEIGHT,
    drawdown_weight: float = DEFAULT_DRAWDOWN_WEIGHT,
) -> Callable[[Individual], float]:
    """Create a fitness function closure for the given market data and inputs.

    In cross-strategy mode, this evaluates individuals with whatever buy/sell
    strategy they carry - different strategy types compete directly in the
    same population by normalized fitness.
    """
    from drawdown.position_strategy import simulate_portfolio

    def evaluate(individual: Individual) -> float:
        params = individual.to_params_dict()
        individual_inputs = _apply_params_to_inputs(
            base_inputs, individual.buy_strategy, individual.sell_strategy, params,
        )
        result = simulate_portfolio(
            price_points_by_symbol,
            targets,
            individual_inputs,
            strategies=[individual.buy_strategy],
            sell_strategies=[individual.sell_strategy],
        )
        strategies = result.get("strategies")
        if isinstance(strategies, list) and strategies:
            strategy_result = strategies[0]
            metrics = strategy_result.get("metrics", {}) if isinstance(strategy_result, dict) else {}
            return_pct = float(metrics.get("return_pct", 0) or 0)
            max_dd = float(metrics.get("max_drawdown_pct", 0) or 0)
            fitness = return_pct * return_weight + max(-max_dd, 0) * drawdown_weight
            return fitness
        return 0.0

    return evaluate


# ── Internal: Initialization ────────────────────────────────────────────────


def _initialize_population(
    buy_strategies: list[str],
    sell_strategies: list[str],
    all_buy_fields: dict[str, list[str]],
    all_sell_fields: dict[str, list[str]],
    base_inputs: StrategyInputs,
    population_size: int,
    *,
    cross_strategy: bool = False,
) -> list[Individual]:
    """Create initial population with heuristic seeding."""
    population: list[Individual] = []
    seen_keys: set[str] = set()

    def _pick_strategies() -> tuple[str, str]:
        if cross_strategy:
            return random.choice(buy_strategies), random.choice(sell_strategies)
        return buy_strategies[0], sell_strategies[0]

    # Seed 1: Default params for each strategy combination
    for bs in buy_strategies:
        for ss in sell_strategies:
            if not cross_strategy and (bs != buy_strategies[0] or ss != sell_strategies[0]):
                continue
            default_ind = Individual(
                buy_strategy=bs, sell_strategy=ss,
                buy_params=_default_buy_params(bs, base_inputs),
                sell_params=_default_sell_params(ss, base_inputs, bs),
            )
            if default_ind.key not in seen_keys:
                seen_keys.add(default_ind.key)
                population.append(default_ind)

    # Seed 2-4: Best known parameter sets (capped to avoid exceeding pop size)
    for bs in buy_strategies:
        if len(population) >= population_size:
            break
        for ss in sell_strategies:
            if not cross_strategy and (bs != buy_strategies[0] or ss != sell_strategies[0]):
                continue
            if len(population) >= population_size:
                break
            buy_fields = all_buy_fields.get(bs, [])
            sell_fields = all_sell_fields.get(ss, [])
            for seed_params in _generate_seeded_params(bs, ss, base_inputs, buy_fields, sell_fields):
                if len(population) >= population_size:
                    break
                ind = Individual(bs, ss,
                    buy_params={k: v for k, v in seed_params.items() if k in buy_fields},
                    sell_params={k: v for k, v in seed_params.items() if k in sell_fields})
                if ind.key not in seen_keys:
                    seen_keys.add(ind.key)
                    population.append(ind)

    # Fill rest with random
    max_attempts = population_size * 10
    attempts = 0
    while len(population) < population_size and attempts < max_attempts:
        attempts += 1
        bs, ss = _pick_strategies()
        ind = _random_individual(bs, ss,
            all_buy_fields.get(bs, []), all_sell_fields.get(ss, []), base_inputs)
        if ind.key not in seen_keys:
            seen_keys.add(ind.key)
            population.append(ind)

    suffix = 0
    while len(population) < population_size:
        suffix += 1
        bs, ss = _pick_strategies()
        ind = _random_individual(bs, ss,
            all_buy_fields.get(bs, []), all_sell_fields.get(ss, []), base_inputs)
        unique_key = f"{ind.key}__pad{suffix}"
        object.__setattr__(ind, "key", unique_key)
        population.append(ind)

    return population


def _random_individual(
    buy_strategy: str,
    sell_strategy: str,
    buy_fields: list[str],
    sell_fields: list[str],
    base_inputs: StrategyInputs,
) -> Individual:
    """Generate a completely random individual."""
    buy_params: dict[str, object] = {}
    for field in buy_fields:
        if field in _BUY_PARAM_RANGES:
            buy_params[field] = _random_param_value(field, _BUY_PARAM_RANGES[field])
        else:
            buy_params[field] = _get_default_for_field(base_inputs, field)

    sell_params: dict[str, object] = {}
    for field in sell_fields:
        if field in _SELL_PARAM_RANGES:
            sell_params[field] = _random_param_value(field, _SELL_PARAM_RANGES[field])
        elif field == "sell_allow_same_day_sell":
            sell_params[field] = random.choice([False, True])
        elif field == "buy_rearm_mode":
            sell_params[field] = random.choice(list(BUY_REARM_MODES))
        else:
            sell_params[field] = _get_default_for_field(base_inputs, field)

    # Validate core_dip constraints
    _enforce_core_dip_constraints(buy_params)

    return Individual(
        buy_strategy=buy_strategy,
        sell_strategy=sell_strategy,
        buy_params=buy_params,
        sell_params=sell_params,
    )


def _generate_seeded_params(
    buy_strategy: str,
    sell_strategy: str,
    base_inputs: StrategyInputs,
    buy_fields: list[str],
    sell_fields: list[str],
) -> Iterable[dict[str, object]]:
    """Generate known-good parameter sets as GA seeds."""
    if buy_strategy == "core_dip_dca":
        for core_set in ROBUST_CORE_DIP_PARAM_SETS:
            for timing_enabled in [False, True]:
                params: dict[str, object] = {
                    "core_dip_initial_core_pct": float(core_set[0]),
                    "core_dip_weekly_core_pct": float(core_set[1]),
                    "core_dip_cash_reserve_pct": float(core_set[2]),
                    "core_dip_start_drawdown_pct": float(core_set[3]),
                    "core_dip_full_drawdown_pct": float(core_set[4]),
                    "core_dip_timing_enabled": timing_enabled,
                }
                if timing_enabled:
                    params.update({
                        "core_dip_timing_max_delay_days": random.choice(ROBUST_CORE_DIP_TIMING_MAX_DELAY_DAYS),
                        "core_dip_timing_rise_threshold_pct": random.choice(ROBUST_CORE_DIP_TIMING_RISE_THRESHOLDS),
                        "core_dip_timing_near_low_pct": random.choice(ROBUST_CORE_DIP_TIMING_NEAR_LOW_VALUES),
                    })
                params.update(_default_sell_params(sell_strategy, base_inputs, buy_strategy))
                yield params

    elif buy_strategy == "equal_slice":
        for step in ROBUST_BUY_STEP_VALUES:
            for alloc in ROBUST_EQUAL_SLICE_ALLOCATION_VALUES:
                params: dict[str, object] = {
                    "step_pct": float(step),
                    "equal_slice_allocation_pct": float(alloc),
                }
                params.update(_default_sell_params(sell_strategy, base_inputs, buy_strategy))
                yield params

    if sell_strategy == "repair_step":
        for profit in ROBUST_REPAIR_SELL_MIN_PROFITS:
            for cooldown in ROBUST_REPAIR_COOLDOWNS:
                for stage in ROBUST_REPAIR_STAGE_SELLS:
                    params: dict[str, object] = {
                        "sell_min_profit_pct": float(profit),
                        "repair_sell_cooldown_days": int(cooldown),
                        "repair_stage_sell_pct": float(stage),
                    }
                    params.update(_default_buy_params(buy_strategy, base_inputs))
                    yield params

    elif sell_strategy == "grid_rebound":
        for step in ROBUST_GRID_REBOUND_STEPS:
            for sell_pct in ROBUST_GRID_SELLS:
                params: dict[str, object] = {
                    "grid_rebound_step_pct": float(step),
                    "grid_sell_pct": float(sell_pct),
                }
                params.update(_default_buy_params(buy_strategy, base_inputs))
                yield params

    elif sell_strategy == "cost_deleverage":
        for profit_set in ROBUST_COST_PROFIT_SETS:
            for sell_set in ROBUST_COST_SELL_SETS:
                for cooldown in ROBUST_COST_COOLDOWNS:
                    params: dict[str, object] = {
                        "cost_first_profit_pct": float(profit_set[0]),
                        "cost_second_profit_pct": float(profit_set[1]),
                        "cost_third_profit_pct": float(profit_set[2]),
                        "cost_first_sell_pct": float(sell_set[0]),
                        "cost_second_sell_pct": float(sell_set[1]),
                        "cost_third_sell_pct": float(sell_set[2]),
                        "cost_deleverage_cooldown_days": int(cooldown),
                    }
                    params.update(_default_buy_params(buy_strategy, base_inputs))
                    yield params


# ── Internal: Selection, Crossover, Mutation ────────────────────────────────


def _tournament_select(
    population: list[Individual],
    fitnesses: list[float],
    tournament_size: int,
) -> Individual:
    """Tournament selection: pick best of k random individuals."""
    indices = random.sample(range(len(population)), min(tournament_size, len(population)))
    best_idx = max(indices, key=lambda i: fitnesses[i])
    return population[best_idx]


def _crossover(
    parent1: Individual,
    parent2: Individual,
    all_buy_fields: dict[str, list[str]],
    all_sell_fields: dict[str, list[str]],
    base_inputs: StrategyInputs,
    *,
    cross_strategy: bool = False,
) -> Individual:
    """Uniform crossover: each gene independently from parent1 or parent2."""
    if cross_strategy and parent1.buy_strategy != parent2.buy_strategy:
        buy_strategy = parent1.buy_strategy if random.random() < 0.5 else parent2.buy_strategy
    else:
        buy_strategy = parent1.buy_strategy

    if cross_strategy and parent1.sell_strategy != parent2.sell_strategy:
        sell_strategy = parent1.sell_strategy if random.random() < 0.5 else parent2.sell_strategy
    else:
        sell_strategy = parent1.sell_strategy

    buy_fields = all_buy_fields.get(buy_strategy, [])
    sell_fields = all_sell_fields.get(sell_strategy, [])

    buy_params: dict[str, object] = {}
    for field in buy_fields:
        v1 = parent1.buy_params.get(field)
        v2 = parent2.buy_params.get(field)
        buy_params[field] = v1 if random.random() < 0.5 else v2

    sell_params: dict[str, object] = {}
    for field in sell_fields:
        v1 = parent1.sell_params.get(field)
        v2 = parent2.sell_params.get(field)
        sell_params[field] = v1 if random.random() < 0.5 else v2

    _enforce_core_dip_constraints(buy_params)

    return Individual(
        buy_strategy=buy_strategy,
        sell_strategy=sell_strategy,
        buy_params=buy_params,
        sell_params=sell_params,
    )


def _mutate(
    individual: Individual,
    all_buy_fields: dict[str, list[str]],
    all_sell_fields: dict[str, list[str]],
    base_inputs: StrategyInputs,
    mutation_rate: float,
    *,
    buy_strategies: list[str] | None = None,
    sell_strategies: list[str] | None = None,
    cross_strategy: bool = False,
    strategy_mutation_rate: float = 0.05,
) -> Individual:
    """Mutate each gene with probability mutation_rate. Optionally mutate strategy type."""
    buy_strategy = individual.buy_strategy
    sell_strategy = individual.sell_strategy

    if cross_strategy and buy_strategies and random.random() < strategy_mutation_rate:
        buy_strategy = random.choice(buy_strategies)
    if cross_strategy and sell_strategies and random.random() < strategy_mutation_rate:
        sell_strategy = random.choice(sell_strategies)

    buy_fields = all_buy_fields.get(buy_strategy, [])
    sell_fields = all_sell_fields.get(sell_strategy, [])

    buy_params = dict(individual.buy_params)
    for field in buy_fields:
        if random.random() < mutation_rate:
            if field in _BUY_PARAM_RANGES:
                buy_params[field] = _random_param_value(field, _BUY_PARAM_RANGES[field])

    sell_params = dict(individual.sell_params)
    for field in sell_fields:
        if random.random() < mutation_rate:
            if field in _SELL_PARAM_RANGES:
                sell_params[field] = _random_param_value(field, _SELL_PARAM_RANGES[field])
            elif field == "sell_allow_same_day_sell":
                sell_params[field] = random.choice([False, True])
            elif field == "buy_rearm_mode":
                sell_params[field] = random.choice(list(BUY_REARM_MODES))

    _enforce_core_dip_constraints(buy_params)

    return Individual(
        buy_strategy=buy_strategy,
        sell_strategy=sell_strategy,
        buy_params=buy_params,
        sell_params=sell_params,
    )


# ── Internal: Parameter helpers ─────────────────────────────────────────────


def _relevant_buy_parameter_fields(buy_strategy: str) -> list[str]:
    return _RELEVANT_BUY_FIELDS.get(buy_strategy, [])


def _relevant_sell_parameter_fields(
    sell_strategy: str,
    base_inputs: StrategyInputs,
    buy_strategy: str,
) -> list[str]:
    fields = list(_RELEVANT_SELL_FIELDS.get(sell_strategy, []))
    # Common sell fields for non-none strategies
    if sell_strategy != "none":
        fields.extend(["sell_allow_same_day_sell"])
        if buy_strategy in REARM_BUY_STRATEGIES:
            fields.extend(["dca_rearm_drawdown_pct", "sell_stage_rearm_drawdown_pct"])
    return fields


def _default_buy_params(buy_strategy: str, base_inputs: StrategyInputs) -> dict[str, object]:
    params: dict[str, object] = {}
    for field in _BUY_PARAM_RANGES:
        params[field] = _get_default_for_field(base_inputs, field)
    return params


def _default_sell_params(
    sell_strategy: str,
    base_inputs: StrategyInputs,
    buy_strategy: str,
) -> dict[str, object]:
    params: dict[str, object] = {}
    for field in _SELL_PARAM_RANGES:
        params[field] = _get_default_for_field(base_inputs, field)
    params["sell_allow_same_day_sell"] = base_inputs.sell_allow_same_day_sell
    if buy_strategy in REARM_BUY_STRATEGIES:
        params["dca_rearm_drawdown_pct"] = base_inputs.dca_rearm_drawdown_pct
        params["sell_stage_rearm_drawdown_pct"] = base_inputs.sell_stage_rearm_drawdown_pct
    return params


def _get_default_for_field(inputs: StrategyInputs, field: str) -> object:
    """Get default value for a field from StrategyInputs."""
    if hasattr(inputs, field):
        return getattr(inputs, field)
    return None


def _random_param_value(field: str, options: list[object]) -> object:
    """Pick a random value for a parameter field."""
    if not options:
        return None
    value = random.choice(options)
    if field in _INT_FIELDS and value is not None:
        return int(value)
    if isinstance(value, (int, float)):
        return float(value) if field not in _INT_FIELDS else int(value)
    return value


def _apply_params_to_inputs(
    base_inputs: StrategyInputs,
    buy_strategy: str,
    sell_strategy: str,
    params: dict[str, object],
) -> StrategyInputs:
    """Create a StrategyInputs with individual parameter values overlaid."""
    replacements: dict[str, object] = {}
    for field, value in params.items():
        if value is not None and hasattr(base_inputs, field):
            replacements[field] = value
    return replace(base_inputs, **replacements)


def _enforce_core_dip_constraints(params: dict[str, object]) -> None:
    """Ensure core_dip parameters satisfy ordering constraints."""
    start = params.get("core_dip_start_drawdown_pct")
    full = params.get("core_dip_full_drawdown_pct")
    if start is not None and full is not None:
        if float(start) > float(full):
            params["core_dip_full_drawdown_pct"] = float(start)


def _evaluate_population(
    population: list[Individual],
    fitness_fn: Callable[[Individual], float],
    cancel_checker: Callable[[], bool] | None,
) -> list[float]:
    """Evaluate fitness for the entire population."""
    fitnesses: list[float] = []
    for ind in population:
        if cancel_checker and cancel_checker():
            fitnesses.append(float("-inf"))
            continue
        fitnesses.append(fitness_fn(ind))
    return fitnesses


def _individual_key(
    buy_strategy: str,
    sell_strategy: str,
    buy_params: Mapping[str, object],
    sell_params: Mapping[str, object],
) -> str:
    """Generate a deterministic key for an individual."""
    parts = [buy_strategy]
    for field in sorted(buy_params.keys()):
        value = buy_params[field]
        if value is not None:
            if isinstance(value, bool):
                parts.append(f"{field}={1 if value else 0}")
            elif isinstance(value, float):
                parts.append(f"{field}={value:g}")
            else:
                parts.append(f"{field}={value}")
    parts.append(sell_strategy)
    for field in sorted(sell_params.keys()):
        value = sell_params[field]
        if value is not None:
            if isinstance(value, bool):
                parts.append(f"{field}={1 if value else 0}")
            elif isinstance(value, float):
                parts.append(f"{field}={value:g}")
            else:
                parts.append(f"{field}={value}")
    return "__".join(parts)


def _snapshot_to_dict(snapshot: GenerationSnapshot) -> dict[str, object]:
    return {
        "generation": snapshot.generation,
        "best_fitness": snapshot.best_fitness,
        "avg_fitness": snapshot.avg_fitness,
        "worst_fitness": snapshot.worst_fitness,
        "best_key": snapshot.best_individual.key,
        "best_buy_strategy": snapshot.best_individual.buy_strategy,
        "best_sell_strategy": snapshot.best_individual.sell_strategy,
        "best_buy_params": dict(snapshot.best_individual.buy_params),
        "best_sell_params": dict(snapshot.best_individual.sell_params),
        "population_size": snapshot.population_size,
    }


def _build_cancelled_result(config: EvolutionConfig) -> dict[str, object]:
    return {
        "formula_version": GA_FORMULA_VERSION,
        "weights": {
            "return": DEFAULT_RETURN_WEIGHT,
            "drawdown": DEFAULT_DRAWDOWN_WEIGHT,
        },
        "config": {
            "population_size": config.population_size,
            "generations": config.generations,
            "mutation_rate": config.mutation_rate,
            "crossover_rate": config.crossover_rate,
            "elitism_count": config.elitism_count,
            "tournament_size": config.tournament_size,
            "seed": config.seed,
            "cross_strategy": config.cross_strategy,
            "strategy_mutation_rate": config.strategy_mutation_rate,
        },
        "snapshots": [],
        "best": None,
        "final_population": [],
        "total_evaluated": 0,
        "cancelled": True,
    }

