"""Shared strategy rule helpers for Python simulations and account signals."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import Protocol


class SellStrategyInputs(Protocol):
    sell_min_profit_pct: float
    cost_first_profit_pct: float
    cost_second_profit_pct: float
    cost_third_profit_pct: float
    cost_first_sell_pct: float
    cost_second_sell_pct: float
    cost_third_sell_pct: float
    cost_deleverage_cooldown_days: int
    sell_allow_same_day_sell: bool


@dataclass(frozen=True)
class CostDeleverageStage:
    mark: str
    profit_pct: float
    sell_pct: float


def cost_deleverage_stages(inputs: SellStrategyInputs) -> tuple[CostDeleverageStage, ...]:
    return (
        CostDeleverageStage("cost_1", inputs.cost_first_profit_pct, inputs.cost_first_sell_pct),
        CostDeleverageStage("cost_2", inputs.cost_second_profit_pct, inputs.cost_second_sell_pct),
        CostDeleverageStage("cost_3", inputs.cost_third_profit_pct, inputs.cost_third_sell_pct),
    )


def cost_deleverage_cooldown_elapsed(
    *,
    last_sell_trade_index: int | None,
    current_trade_index: int,
    cooldown_days: int,
) -> bool:
    return not (
        cooldown_days > 0
        and last_sell_trade_index is not None
        and current_trade_index - last_sell_trade_index < cooldown_days
    )


def cost_deleverage_date_cooldown_elapsed(
    last_sell_date: date | None,
    *,
    current_date: date,
    cooldown_days: int,
) -> bool:
    return not (
        cooldown_days > 0
        and last_sell_date is not None
        and (current_date - last_sell_date).days < cooldown_days
    )


def select_cost_deleverage_stage(
    *,
    inputs: SellStrategyInputs,
    active_marks: set[str],
    profit_pct: float,
) -> CostDeleverageStage | None:
    for stage in cost_deleverage_stages(inputs):
        trigger_pct = max(stage.profit_pct, inputs.sell_min_profit_pct)
        if stage.mark in active_marks or profit_pct + 1e-9 < trigger_pct:
            continue
        return stage
    return None


def should_check_sell_after_buy(
    *,
    sell_strategy: str,
    bought_today: bool,
    inputs: SellStrategyInputs,
) -> bool:
    return bool(
        bought_today
        and sell_strategy != "none"
        and inputs.sell_allow_same_day_sell
    )
