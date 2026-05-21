"""Strategy parameter definitions and deterministic variant expansion."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Iterable, Mapping

from drawdown.position_strategy import (
    LOT_SELL_BUY_STRATEGIES,
    POSITION_SELL_REARM_STRATEGIES,
    REARM_BUY_STRATEGIES,
    ROBUST_BUY_STEP_VALUES,
    ROBUST_CORE_DIP_PARAM_SETS,
    ROBUST_COST_COOLDOWNS,
    ROBUST_COST_PROFIT_SETS,
    ROBUST_COST_SELL_SETS,
    ROBUST_CORE_DIP_TIMING_MAX_DELAY_DAYS,
    ROBUST_CORE_DIP_TIMING_NEAR_LOW_VALUES,
    ROBUST_CORE_DIP_TIMING_RISE_THRESHOLDS,
    ROBUST_DCA_REARM_DRAWDOWN_VALUES,
    ROBUST_EQUAL_SLICE_ALLOCATION_VALUES,
    ROBUST_GRID_FIRST_SELLS,
    ROBUST_GRID_REBOUND_STEPS,
    ROBUST_GRID_SECOND_SELLS,
    ROBUST_REPAIR_COOLDOWNS,
    ROBUST_REPAIR_SELL_MIN_PROFITS,
    ROBUST_REPAIR_STAGE_SELLS,
    SELL_STRATEGY_LABELS,
    STRATEGY_LABELS,
    StrategyInputs,
)


STRATEGY_DEFINITION_VERSION = "strategy-params-v3"

BUY_PARAMETER_FIELDS = (
    "step_pct",
    "equal_slice_allocation_pct",
    "core_dip_initial_core_pct",
    "core_dip_weekly_core_pct",
    "core_dip_cash_reserve_pct",
    "core_dip_start_drawdown_pct",
    "core_dip_full_drawdown_pct",
    "core_dip_timing_enabled",
    "core_dip_timing_max_delay_days",
    "core_dip_timing_rise_threshold_pct",
    "core_dip_timing_near_low_pct",
)

SELL_PARAMETER_FIELDS = (
    "sell_min_profit_pct",
    "repair_sell_cooldown_days",
    "repair_stage_sell_pct",
    "grid_rebound_step_pct",
    "grid_first_sell_pct",
    "grid_second_sell_pct",
    "grid_min_sell_amount",
    "cost_first_profit_pct",
    "cost_second_profit_pct",
    "cost_third_profit_pct",
    "cost_first_sell_pct",
    "cost_second_sell_pct",
    "cost_third_sell_pct",
    "cost_deleverage_cooldown_days",
    "sell_allow_same_day_sell",
    "cost_min_sell_amount",
    "dca_rearm_drawdown_pct",
)

PARAMETER_LAB_BUY_VARIANT_SCHEMA = ("variant_id", "variant_key", "strategy_key", *BUY_PARAMETER_FIELDS)
PARAMETER_LAB_SELL_VARIANT_SCHEMA = ("variant_id", "variant_key", "strategy_key", *SELL_PARAMETER_FIELDS)
PARAMETER_LAB_CANDIDATE_SCHEMA = ("candidate_id", "buy_variant_id", "sell_variant_id")


@dataclass(frozen=True)
class StrategyDefinition:
    strategy_key: str
    strategy_label: str
    strategy_type: str
    base_parameters: dict[str, object]
    parameter_space: dict[str, list[object]]
    compatible_buy_strategies: tuple[str, ...] = ()
    compatible_sell_strategies: tuple[str, ...] = ()
    source_definition_version: str = STRATEGY_DEFINITION_VERSION


@dataclass(frozen=True)
class ParameterVariant:
    variant_key: str
    strategy_key: str
    strategy_type: str
    parameters: dict[str, object]
    display_label: str
    compatible_buy_strategies: tuple[str, ...] = ()
    compatible_sell_strategies: tuple[str, ...] = ()
    source_definition_version: str = STRATEGY_DEFINITION_VERSION

    def to_payload(self) -> dict[str, object]:
        return {
            "variant_key": self.variant_key,
            "strategy_key": self.strategy_key,
            "strategy_type": self.strategy_type,
            "parameters": dict(self.parameters),
            "display_label": self.display_label,
            "compatible_buy_strategies": list(self.compatible_buy_strategies),
            "compatible_sell_strategies": list(self.compatible_sell_strategies),
            "source_definition_version": self.source_definition_version,
        }


@dataclass(frozen=True)
class StrategyCombination:
    combination_key: str
    buy_variant: ParameterVariant
    sell_variant: ParameterVariant
    label: str

    def to_candidate_payload(self) -> dict[str, object]:
        buy_params = _full_buy_params(self.buy_variant.parameters)
        sell_params = _full_sell_params(self.sell_variant.parameters)
        candidate = {
            "key": _candidate_key(
                self.buy_variant.strategy_key,
                self.sell_variant.strategy_key,
                buy_params,
                sell_params,
            ),
            "combination_key": self.combination_key,
            "label": self.label,
            "buy_strategy": self.buy_variant.strategy_key,
            "sell_strategy": self.sell_variant.strategy_key,
            "buy_variant_key": self.buy_variant.variant_key,
            "sell_variant_key": self.sell_variant.variant_key,
            "strategy_definition_version": STRATEGY_DEFINITION_VERSION,
            "parameter_snapshot": {
                "buy": dict(self.buy_variant.parameters),
                "sell": dict(self.sell_variant.parameters),
            },
            "buy_variant": self.buy_variant.to_payload(),
            "sell_variant": self.sell_variant.to_payload(),
        }
        candidate.update(buy_params)
        candidate.update(sell_params)
        return candidate


def strategy_definitions() -> dict[str, StrategyDefinition]:
    """Return all strategy definitions used by Strategy Lab parameter search."""
    return {
        **_buy_definitions(),
        **_sell_definitions(),
    }


def strategy_registry_payload() -> dict[str, object]:
    definitions = strategy_definitions()
    return {
        "version": STRATEGY_DEFINITION_VERSION,
        "definitions": {
            key: {
                "strategy_key": item.strategy_key,
                "strategy_label": item.strategy_label,
                "strategy_type": item.strategy_type,
                "base_parameters": dict(item.base_parameters),
                "parameter_space": {field: list(values) for field, values in item.parameter_space.items()},
                "compatible_buy_strategies": list(item.compatible_buy_strategies),
                "compatible_sell_strategies": list(item.compatible_sell_strategies),
                "source_definition_version": item.source_definition_version,
            }
            for key, item in definitions.items()
        },
    }


def expand_buy_parameter_variants(
    buy_strategies: Iterable[str] | None,
    inputs: StrategyInputs | None = None,
    *,
    core_dip_timing_filter: str = "all",
) -> list[ParameterVariant]:
    inputs = inputs or StrategyInputs()
    selected = _validate_keys(buy_strategies or STRATEGY_LABELS.keys(), STRATEGY_LABELS, "买入策略")
    if core_dip_timing_filter not in {"all", "enabled", "disabled"}:
        raise ValueError("核心买点优化候选必须是 all、enabled 或 disabled。")
    variants: list[ParameterVariant] = []
    for strategy_key in selected:
        for params in _buy_param_variants(strategy_key, core_dip_timing_filter):
            variants.append(
                ParameterVariant(
                    variant_key=_variant_key(strategy_key, params),
                    strategy_key=strategy_key,
                    strategy_type="buy",
                    parameters=params,
                    display_label=_buy_label(strategy_key, params),
                    compatible_sell_strategies=tuple(SELL_STRATEGY_LABELS.keys()),
                )
            )
    return variants


def expand_sell_parameter_variants(
    sell_strategies: Iterable[str] | None,
    inputs: StrategyInputs | None = None,
    *,
    buy_variant: ParameterVariant,
) -> list[ParameterVariant]:
    inputs = inputs or StrategyInputs()
    selected = _validate_keys(sell_strategies or SELL_STRATEGY_LABELS.keys(), SELL_STRATEGY_LABELS, "卖出策略")
    variants: list[ParameterVariant] = []
    for strategy_key in selected:
        for params in _sell_param_variants(strategy_key, buy_variant.strategy_key, inputs):
            variants.append(
                ParameterVariant(
                    variant_key=_variant_key(strategy_key, params),
                    strategy_key=strategy_key,
                    strategy_type="sell",
                    parameters=params,
                    display_label=_sell_label(strategy_key, params),
                    compatible_buy_strategies=(buy_variant.strategy_key,),
                )
            )
    return _dedupe_variants(variants)


def expand_strategy_combinations(
    buy_strategies: Iterable[str] | None,
    sell_strategies: Iterable[str] | None,
    inputs: StrategyInputs | None = None,
    *,
    core_dip_timing_filter: str = "all",
) -> list[StrategyCombination]:
    inputs = inputs or StrategyInputs()
    combinations: list[StrategyCombination] = []
    for buy_variant in expand_buy_parameter_variants(
        buy_strategies,
        inputs,
        core_dip_timing_filter=core_dip_timing_filter,
    ):
        sell_variants = expand_sell_parameter_variants(
            sell_strategies,
            inputs,
            buy_variant=buy_variant,
        )
        for sell_variant in sell_variants:
            combination_key = f"{buy_variant.variant_key}__{sell_variant.variant_key}"
            combinations.append(
                StrategyCombination(
                    combination_key=combination_key,
                    buy_variant=buy_variant,
                    sell_variant=sell_variant,
                    label=f"{buy_variant.display_label} / {sell_variant.display_label}",
                )
            )
    return _dedupe_combinations(combinations)


def expand_strategy_candidate_payloads(
    buy_strategies: Iterable[str] | None,
    sell_strategies: Iterable[str] | None,
    inputs: StrategyInputs | None = None,
    *,
    core_dip_timing_filter: str = "all",
) -> list[dict[str, object]]:
    return [
        combination.to_candidate_payload()
        for combination in expand_strategy_combinations(
            buy_strategies,
            sell_strategies,
            inputs,
            core_dip_timing_filter=core_dip_timing_filter,
        )
    ]


def strategy_parameter_lab_manifest_payload(
    buy_strategies: Iterable[str] | None,
    sell_strategies: Iterable[str] | None,
    inputs: StrategyInputs | None = None,
    *,
    core_dip_timing_filter: str = "all",
) -> dict[str, object]:
    inputs = inputs or StrategyInputs()
    combinations = expand_strategy_combinations(
        buy_strategies,
        sell_strategies,
        inputs,
        core_dip_timing_filter=core_dip_timing_filter,
    )
    buy_variants: list[list[object]] = []
    sell_variants: list[list[object]] = []
    buy_variant_ids: dict[str, int] = {}
    sell_variant_ids: dict[str, int] = {}
    candidate_rows: list[list[int]] = []
    for candidate_id, combination in enumerate(combinations):
        buy_variant = combination.buy_variant
        sell_variant = combination.sell_variant
        buy_variant_id = buy_variant_ids.get(buy_variant.variant_key)
        if buy_variant_id is None:
            buy_variant_id = len(buy_variants)
            buy_variant_ids[buy_variant.variant_key] = buy_variant_id
            buy_variants.append(_parameter_lab_buy_variant_row(buy_variant_id, buy_variant))
        sell_variant_id = sell_variant_ids.get(sell_variant.variant_key)
        if sell_variant_id is None:
            sell_variant_id = len(sell_variants)
            sell_variant_ids[sell_variant.variant_key] = sell_variant_id
            sell_variants.append(_parameter_lab_sell_variant_row(sell_variant_id, sell_variant))
        candidate_rows.append([candidate_id, buy_variant_id, sell_variant_id])
    manifest = {
        "buy_variant_schema": list(PARAMETER_LAB_BUY_VARIANT_SCHEMA),
        "sell_variant_schema": list(PARAMETER_LAB_SELL_VARIANT_SCHEMA),
        "candidate_schema": list(PARAMETER_LAB_CANDIDATE_SCHEMA),
        "buy_variants": buy_variants,
        "sell_variants": sell_variants,
        "candidate_rows": candidate_rows,
    }
    manifest["candidate_manifest_hash"] = _parameter_lab_manifest_hash(manifest)
    manifest["candidate_counts"] = {
        "total": len(candidate_rows),
        "buy_variants": len(buy_variants),
        "sell_variants": len(sell_variants),
    }
    return manifest


def _parameter_lab_buy_variant_row(variant_id: int, variant: ParameterVariant) -> list[object]:
    row: list[object] = [variant_id, variant.variant_key, variant.strategy_key]
    row.extend(variant.parameters.get(field) for field in BUY_PARAMETER_FIELDS)
    return row


def _parameter_lab_sell_variant_row(variant_id: int, variant: ParameterVariant) -> list[object]:
    row: list[object] = [variant_id, variant.variant_key, variant.strategy_key]
    row.extend(variant.parameters.get(field) for field in SELL_PARAMETER_FIELDS)
    return row


def _parameter_lab_manifest_hash(manifest: Mapping[str, object]) -> str:
    digest_payload = {
        "strategy_definition_version": STRATEGY_DEFINITION_VERSION,
        "buy_variant_schema": list(manifest.get("buy_variant_schema") or []),
        "sell_variant_schema": list(manifest.get("sell_variant_schema") or []),
        "candidate_schema": list(manifest.get("candidate_schema") or []),
        "buy_variants": manifest.get("buy_variants") or [],
        "sell_variants": manifest.get("sell_variants") or [],
        "candidate_rows": manifest.get("candidate_rows") or [],
    }
    return hashlib.sha256(
        json.dumps(digest_payload, ensure_ascii=True, separators=(",", ":"), sort_keys=True).encode("utf-8")
    ).hexdigest()


def apply_candidate_to_inputs(inputs: StrategyInputs, candidate: Mapping[str, object]) -> StrategyInputs:
    """Return a StrategyInputs copy with a candidate's parameter snapshot applied."""
    from dataclasses import replace

    replacements: dict[str, object] = {}
    for field in BUY_PARAMETER_FIELDS + SELL_PARAMETER_FIELDS:
        value = candidate.get(field)
        if value is not None and hasattr(inputs, field):
            replacements[field] = value
    return replace(inputs, **replacements)


def _buy_definitions() -> dict[str, StrategyDefinition]:
    return {
        "pyramid_3": StrategyDefinition(
            "pyramid_3",
            STRATEGY_LABELS["pyramid_3"],
            "buy",
            {},
            {},
        ),
        "equal_slice": StrategyDefinition(
            "equal_slice",
            STRATEGY_LABELS["equal_slice"],
            "buy",
            {"step_pct": 5.0, "equal_slice_allocation_pct": 5.0},
            {
                "step_pct": list(ROBUST_BUY_STEP_VALUES),
                "equal_slice_allocation_pct": list(ROBUST_EQUAL_SLICE_ALLOCATION_VALUES),
            },
        ),
        "linear_weighted_slice": StrategyDefinition(
            "linear_weighted_slice",
            STRATEGY_LABELS["linear_weighted_slice"],
            "buy",
            {"step_pct": 5.0},
            {"step_pct": list(ROBUST_BUY_STEP_VALUES)},
        ),
        "weekly_dca": StrategyDefinition(
            "weekly_dca",
            STRATEGY_LABELS["weekly_dca"],
            "buy",
            {},
            {},
        ),
        "salary_flow_dca": StrategyDefinition(
            "salary_flow_dca",
            STRATEGY_LABELS["salary_flow_dca"],
            "buy",
            {},
            {},
        ),
        "core_dip_dca": StrategyDefinition(
            "core_dip_dca",
            STRATEGY_LABELS["core_dip_dca"],
            "buy",
            {
                "core_dip_initial_core_pct": 80.0,
                "core_dip_weekly_core_pct": 90.0,
                "core_dip_cash_reserve_pct": 8.0,
                "core_dip_start_drawdown_pct": 5.0,
                "core_dip_full_drawdown_pct": 25.0,
                "core_dip_timing_enabled": False,
            },
            {
                "core_dip_param_sets": [list(item) for item in ROBUST_CORE_DIP_PARAM_SETS],
                "core_dip_timing_enabled": [False, True],
                "core_dip_timing_max_delay_days": list(ROBUST_CORE_DIP_TIMING_MAX_DELAY_DAYS),
                "core_dip_timing_rise_threshold_pct": list(ROBUST_CORE_DIP_TIMING_RISE_THRESHOLDS),
                "core_dip_timing_near_low_pct": list(ROBUST_CORE_DIP_TIMING_NEAR_LOW_VALUES),
            },
        ),
    }


def _sell_definitions() -> dict[str, StrategyDefinition]:
    all_buys = tuple(STRATEGY_LABELS.keys())
    return {
        "none": StrategyDefinition(
            "none",
            SELL_STRATEGY_LABELS["none"],
            "sell",
            {},
            {},
            compatible_buy_strategies=all_buys,
        ),
        "repair_step": StrategyDefinition(
            "repair_step",
            SELL_STRATEGY_LABELS["repair_step"],
            "sell",
            {
                "sell_min_profit_pct": 10.0,
                "repair_sell_cooldown_days": 30,
                "repair_stage_sell_pct": 12.0,
                "sell_allow_same_day_sell": False,
            },
            {
                "sell_min_profit_pct": list(ROBUST_REPAIR_SELL_MIN_PROFITS),
                "repair_sell_cooldown_days": list(ROBUST_REPAIR_COOLDOWNS),
                "repair_stage_sell_pct": list(ROBUST_REPAIR_STAGE_SELLS),
                "sell_allow_same_day_sell": [False, True],
                "dca_rearm_drawdown_pct": list(ROBUST_DCA_REARM_DRAWDOWN_VALUES),
            },
            compatible_buy_strategies=tuple(REARM_BUY_STRATEGIES),
        ),
        "grid_rebound": StrategyDefinition(
            "grid_rebound",
            SELL_STRATEGY_LABELS["grid_rebound"],
            "sell",
            {
                "grid_rebound_step_pct": 5.0,
                "grid_first_sell_pct": 40.0,
                "grid_second_sell_pct": 40.0,
                "grid_min_sell_amount": 200.0,
                "sell_allow_same_day_sell": False,
            },
            {
                "grid_rebound_step_pct": list(ROBUST_GRID_REBOUND_STEPS),
                "grid_first_sell_pct": list(ROBUST_GRID_FIRST_SELLS),
                "grid_second_sell_pct": list(ROBUST_GRID_SECOND_SELLS),
                "sell_allow_same_day_sell": [False, True],
                "dca_rearm_drawdown_pct": list(ROBUST_DCA_REARM_DRAWDOWN_VALUES),
            },
            compatible_buy_strategies=all_buys,
        ),
        "cost_deleverage": StrategyDefinition(
            "cost_deleverage",
            SELL_STRATEGY_LABELS["cost_deleverage"],
            "sell",
            {
                "cost_first_profit_pct": 8.0,
                "cost_second_profit_pct": 15.0,
                "cost_third_profit_pct": 25.0,
                "cost_first_sell_pct": 30.0,
                "cost_second_sell_pct": 30.0,
                "cost_third_sell_pct": 30.0,
                "cost_deleverage_cooldown_days": 0,
                "sell_allow_same_day_sell": False,
            },
            {
                "cost_profit_sets": [list(item) for item in ROBUST_COST_PROFIT_SETS],
                "cost_sell_sets": [list(item) for item in ROBUST_COST_SELL_SETS],
                "cost_deleverage_cooldown_days": list(ROBUST_COST_COOLDOWNS),
                "sell_allow_same_day_sell": [False, True],
                "dca_rearm_drawdown_pct": list(ROBUST_DCA_REARM_DRAWDOWN_VALUES),
            },
            compatible_buy_strategies=all_buys,
        ),
    }


def _buy_param_variants(strategy_key: str, core_dip_timing_filter: str) -> list[dict[str, object]]:
    if strategy_key == "equal_slice":
        return [
            {"step_pct": float(step), "equal_slice_allocation_pct": float(allocation)}
            for step in ROBUST_BUY_STEP_VALUES
            for allocation in ROBUST_EQUAL_SLICE_ALLOCATION_VALUES
        ]
    if strategy_key == "linear_weighted_slice":
        return [{"step_pct": float(step)} for step in ROBUST_BUY_STEP_VALUES]
    if strategy_key == "core_dip_dca":
        variants = []
        timing_values = [False, True]
        if core_dip_timing_filter == "enabled":
            timing_values = [True]
        elif core_dip_timing_filter == "disabled":
            timing_values = [False]
        for initial_core, weekly_core, cash_reserve, start_drawdown, full_drawdown in ROBUST_CORE_DIP_PARAM_SETS:
            for timing_enabled in timing_values:
                params = {
                    "core_dip_initial_core_pct": float(initial_core),
                    "core_dip_weekly_core_pct": float(weekly_core),
                    "core_dip_cash_reserve_pct": float(cash_reserve),
                    "core_dip_start_drawdown_pct": float(start_drawdown),
                    "core_dip_full_drawdown_pct": float(full_drawdown),
                    "core_dip_timing_enabled": bool(timing_enabled),
                }
                if timing_enabled:
                    for max_delay_days in ROBUST_CORE_DIP_TIMING_MAX_DELAY_DAYS:
                        for rise_threshold in ROBUST_CORE_DIP_TIMING_RISE_THRESHOLDS:
                            for near_low in ROBUST_CORE_DIP_TIMING_NEAR_LOW_VALUES:
                                timing_params = dict(params)
                                timing_params.update(
                                    {
                                        "core_dip_timing_max_delay_days": int(max_delay_days),
                                        "core_dip_timing_rise_threshold_pct": float(rise_threshold),
                                        "core_dip_timing_near_low_pct": float(near_low),
                                    }
                                )
                                variants.append(timing_params)
                else:
                    variants.append(params)
        return variants
    return [{}]


def _sell_param_variants(strategy_key: str, buy_strategy: str, inputs: StrategyInputs) -> list[dict[str, object]]:
    if strategy_key == "none":
        return [{}]
    if strategy_key == "repair_step":
        if buy_strategy not in REARM_BUY_STRATEGIES:
            return []
        current = {
            "sell_min_profit_pct": float(inputs.sell_min_profit_pct),
            "repair_sell_cooldown_days": int(inputs.repair_sell_cooldown_days),
            "repair_stage_sell_pct": float(inputs.repair_stage_sell_pct),
        }
        variants = []
        if buy_strategy in LOT_SELL_BUY_STRATEGIES:
            for profit in ROBUST_REPAIR_SELL_MIN_PROFITS:
                for cooldown in ROBUST_REPAIR_COOLDOWNS:
                    for stage in ROBUST_REPAIR_STAGE_SELLS:
                        variants.append(
                            {
                                "sell_min_profit_pct": float(profit),
                                "repair_sell_cooldown_days": int(cooldown),
                                "repair_stage_sell_pct": float(stage),
                            }
                        )
        variants.append(current)
        return _with_rearm_variants(_with_same_day_sell_variants(variants), buy_strategy, strategy_key)
    if strategy_key == "grid_rebound":
        variants = [
            {
                "grid_rebound_step_pct": float(step),
                "grid_first_sell_pct": float(first_sell),
                "grid_second_sell_pct": float(second_sell),
                "grid_min_sell_amount": float(inputs.grid_min_sell_amount),
            }
            for step in ROBUST_GRID_REBOUND_STEPS
            for first_sell in ROBUST_GRID_FIRST_SELLS
            for second_sell in ROBUST_GRID_SECOND_SELLS
        ]
        return _with_rearm_variants(_with_same_day_sell_variants(variants), buy_strategy, strategy_key)
    if strategy_key == "cost_deleverage":
        variants = [
            {
                "cost_first_profit_pct": float(profit_set[0]),
                "cost_second_profit_pct": float(profit_set[1]),
                "cost_third_profit_pct": float(profit_set[2]),
                "cost_first_sell_pct": float(sell_set[0]),
                "cost_second_sell_pct": float(sell_set[1]),
                "cost_third_sell_pct": float(sell_set[2]),
                "cost_deleverage_cooldown_days": int(cooldown),
                "sell_allow_same_day_sell": bool(allow_same_day_sell),
                "cost_min_sell_amount": float(inputs.cost_min_sell_amount),
            }
            for profit_set in ROBUST_COST_PROFIT_SETS
            for sell_set in ROBUST_COST_SELL_SETS
            for cooldown in ROBUST_COST_COOLDOWNS
            for allow_same_day_sell in (False, True)
        ]
        return _with_rearm_variants(variants, buy_strategy, strategy_key)
    return []


def _with_same_day_sell_variants(base_variants: list[dict[str, object]]) -> list[dict[str, object]]:
    result: list[dict[str, object]] = []
    for params in base_variants:
        for allow_same_day_sell in (False, True):
            item = dict(params)
            item["sell_allow_same_day_sell"] = bool(allow_same_day_sell)
            result.append(item)
    return result


def _with_rearm_variants(
    base_variants: list[dict[str, object]],
    buy_strategy: str,
    sell_strategy: str,
) -> list[dict[str, object]]:
    rearm_values: list[float | None]
    if buy_strategy in REARM_BUY_STRATEGIES and sell_strategy in POSITION_SELL_REARM_STRATEGIES:
        rearm_values = list(ROBUST_DCA_REARM_DRAWDOWN_VALUES)
    else:
        rearm_values = [None]
    result: list[dict[str, object]] = []
    for params in base_variants:
        for rearm in rearm_values:
            item = dict(params)
            if rearm is not None:
                item["dca_rearm_drawdown_pct"] = float(rearm)
            result.append(item)
    return _dedupe_param_dicts(result)


def _candidate_key(
    buy_strategy: str,
    sell_strategy: str,
    buy_params: Mapping[str, object],
    sell_params: Mapping[str, object],
) -> str:
    parts = [buy_strategy]
    if buy_params.get("step_pct") is not None:
        parts.append(f"step{float(buy_params['step_pct']):g}")
    if buy_params.get("equal_slice_allocation_pct") is not None:
        parts.append(f"alloc{float(buy_params['equal_slice_allocation_pct']):g}")
    if buy_params.get("core_dip_initial_core_pct") is not None:
        parts.extend(
            [
                f"ci{float(buy_params['core_dip_initial_core_pct']):g}",
                f"cw{float(buy_params.get('core_dip_weekly_core_pct') or 0):g}",
                f"cr{float(buy_params.get('core_dip_cash_reserve_pct') or 0):g}",
                f"csd{float(buy_params.get('core_dip_start_drawdown_pct') or 0):g}",
                f"cfd{float(buy_params.get('core_dip_full_drawdown_pct') or 0):g}",
            ]
        )
        if buy_params.get("core_dip_timing_enabled"):
            parts.extend(
                [
                    f"ctd{int(buy_params.get('core_dip_timing_max_delay_days') or 0):g}",
                    f"ctr{float(buy_params.get('core_dip_timing_rise_threshold_pct') or 0):g}",
                    f"ctl{float(buy_params.get('core_dip_timing_near_low_pct') or 0):g}",
                ]
            )
    parts.append(sell_strategy)
    if sell_strategy == "repair_step":
        parts.extend(
            [
                f"p{float(sell_params.get('sell_min_profit_pct') or 0):g}",
                f"c{int(sell_params.get('repair_sell_cooldown_days') or 0):g}",
                f"s{float(sell_params.get('repair_stage_sell_pct') or 0):g}",
            ]
        )
    if sell_strategy == "grid_rebound":
        parts.extend(
            [
                f"g{float(sell_params.get('grid_rebound_step_pct') or 0):g}",
                f"g1{float(sell_params.get('grid_first_sell_pct') or 0):g}",
                f"g2{float(sell_params.get('grid_second_sell_pct') or 0):g}",
                f"gmin{float(sell_params.get('grid_min_sell_amount') or 0):g}",
            ]
        )
    if sell_strategy == "cost_deleverage":
        profits = (
            sell_params.get("cost_first_profit_pct") or 0,
            sell_params.get("cost_second_profit_pct") or 0,
            sell_params.get("cost_third_profit_pct") or 0,
        )
        sells = (
            sell_params.get("cost_first_sell_pct") or 0,
            sell_params.get("cost_second_sell_pct") or 0,
            sell_params.get("cost_third_sell_pct") or 0,
        )
        parts.extend(
            [
                "cp" + "-".join(f"{float(value):g}" for value in profits),
                "cs" + "-".join(f"{float(value):g}" for value in sells),
                f"cc{int(sell_params.get('cost_deleverage_cooldown_days') or 0):g}",
                f"cmin{float(sell_params.get('cost_min_sell_amount') or 0):g}",
            ]
        )
    if sell_strategy != "none" and sell_params.get("sell_allow_same_day_sell"):
        parts.append("same1")
    if sell_params.get("dca_rearm_drawdown_pct") is not None:
        parts.append(f"rearm{float(sell_params['dca_rearm_drawdown_pct']):g}")
    return "__".join(parts)


def _buy_label(strategy_key: str, params: Mapping[str, object]) -> str:
    label = STRATEGY_LABELS[strategy_key]
    bits: list[str] = []
    if params.get("step_pct") is not None:
        bits.append(f"步长 {float(params['step_pct']):g}%")
    if params.get("equal_slice_allocation_pct") is not None:
        bits.append(f"每步 {float(params['equal_slice_allocation_pct']):g}%")
    if params.get("core_dip_initial_core_pct") is not None:
        bits.extend(
            [
                f"初始 {float(params['core_dip_initial_core_pct']):g}%",
                f"周投 {float(params.get('core_dip_weekly_core_pct') or 0):g}%",
                f"现金垫 {float(params.get('core_dip_cash_reserve_pct') or 0):g}%",
                f"加仓 {float(params.get('core_dip_start_drawdown_pct') or 0):g}-{float(params.get('core_dip_full_drawdown_pct') or 0):g}%",
            ]
        )
        if params.get("core_dip_timing_enabled"):
            bits.append(
                f"买点优化 延迟{int(params.get('core_dip_timing_max_delay_days') or 0):g}日 "
                f"大涨{float(params.get('core_dip_timing_rise_threshold_pct') or 0):g}% "
                f"近低{float(params.get('core_dip_timing_near_low_pct') or 0):g}%"
            )
        else:
            bits.append("买点优化 关闭")
    return f"{label} ({' / '.join(bits)})" if bits else label


def _sell_label(strategy_key: str, params: Mapping[str, object]) -> str:
    if strategy_key == "repair_step":
        label = (
            f"阶梯修复 {float(params.get('sell_min_profit_pct') or 0):g}%盈利 "
            f"{int(params.get('repair_sell_cooldown_days') or 0):g}日冷却 "
            f"{float(params.get('repair_stage_sell_pct') or 0):g}%单档"
        )
    elif strategy_key == "grid_rebound":
        label = (
            f"网格回弹 {float(params.get('grid_rebound_step_pct') or 0):g}%步长 "
            f"{float(params.get('grid_first_sell_pct') or 0):g}%+"
            f"{float(params.get('grid_second_sell_pct') or 0):g}%卖出"
        )
    elif strategy_key == "cost_deleverage":
        profits = [
            params.get("cost_first_profit_pct") or 0,
            params.get("cost_second_profit_pct") or 0,
            params.get("cost_third_profit_pct") or 0,
        ]
        sells = [
            params.get("cost_first_sell_pct") or 0,
            params.get("cost_second_sell_pct") or 0,
            params.get("cost_third_sell_pct") or 0,
        ]
        label = (
            "成本去杠杆 "
            + "/".join(f"{float(value):g}%" for value in profits)
            + " 盈利 "
            + "+".join(f"{float(value):g}%" for value in sells)
            + f" 卖出 {int(params.get('cost_deleverage_cooldown_days') or 0):g}日冷却"
        )
    else:
        label = SELL_STRATEGY_LABELS[strategy_key]
    if strategy_key != "none" and params.get("sell_allow_same_day_sell"):
        label = f"{label} / 买入日可卖"
    if params.get("dca_rearm_drawdown_pct") is not None:
        label = f"{label} / 卖后重启 {float(params['dca_rearm_drawdown_pct']):g}%回撤"
    return label


def _variant_key(strategy_key: str, params: Mapping[str, object]) -> str:
    canonical = json.dumps(_normalized_params(params), sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    digest = hashlib.sha1(canonical.encode("utf-8")).hexdigest()[:12]
    return f"{strategy_key}:{digest}"


def _normalized_params(params: Mapping[str, object]) -> dict[str, object]:
    normalized: dict[str, object] = {}
    for key in sorted(params):
        value = params[key]
        if value is None:
            continue
        if isinstance(value, bool):
            normalized[key] = value
        elif isinstance(value, int):
            normalized[key] = value
        elif isinstance(value, float):
            normalized[key] = round(value, 10)
        else:
            normalized[key] = value
    return normalized


def _full_buy_params(params: Mapping[str, object]) -> dict[str, object | None]:
    return {field: params.get(field) for field in BUY_PARAMETER_FIELDS}


def _full_sell_params(params: Mapping[str, object]) -> dict[str, object | None]:
    return {field: params.get(field) for field in SELL_PARAMETER_FIELDS}


def _dedupe_param_dicts(items: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for item in items:
        result[_variant_key("params", item)] = item
    return list(result.values())


def _dedupe_variants(items: Iterable[ParameterVariant]) -> list[ParameterVariant]:
    result: dict[str, ParameterVariant] = {}
    for item in items:
        result[item.variant_key] = item
    return list(result.values())


def _dedupe_combinations(items: Iterable[StrategyCombination]) -> list[StrategyCombination]:
    result: dict[str, StrategyCombination] = {}
    for item in items:
        result[item.combination_key] = item
    return list(result.values())


def _validate_keys(raw: Iterable[str], labels: Mapping[str, str], item_label: str) -> list[str]:
    selected = [str(item) for item in raw]
    unknown = set(selected) - set(labels)
    if unknown:
        raise ValueError(f"未知{item_label}: " + ", ".join(sorted(unknown)))
    if not selected:
        raise ValueError(f"至少需要选择一个{item_label}。")
    return selected
