"""Shared strategy rule helpers for Python simulations."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from typing import TYPE_CHECKING, Protocol

if TYPE_CHECKING:
    from drawdown.generate_drawdown_report import PricePoint
    from drawdown.position_strategy import StrategyInputs


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


def clamp(value: float, minimum: float, maximum: float) -> float:
    return max(minimum, min(maximum, value))


def point_drawdown_pct(point: PricePoint, inputs: StrategyInputs) -> float:
    if inputs.drawdown_basis == "rolling_120":
        return abs(float(point.drawdown_120) * 100.0)
    return abs(float(point.drawdown_ath) * 100.0)


def core_dip_boost_ratio(drawdown_pct: float, inputs: StrategyInputs) -> float:
    start = max(0.0, float(inputs.core_dip_start_drawdown_pct))
    full = max(start, float(inputs.core_dip_full_drawdown_pct))
    if drawdown_pct <= start:
        return 0.0
    if drawdown_pct >= full:
        return 1.0
    if full <= start:
        return 1.0
    return (drawdown_pct - start) / (full - start)


def core_dip_cash_reserve_ratio(drawdown_pct: float, inputs: StrategyInputs) -> float:
    base = clamp(inputs.core_dip_cash_reserve_pct / 100.0, 0.0, 1.0)
    boost = core_dip_boost_ratio(drawdown_pct, inputs)
    return max(0.01, base * (1.0 - boost * 0.85))


def core_dip_timing_allows_buy(
    point: PricePoint,
    recent_points: list[PricePoint],
    drawdown_pct: float,
    pending_days: int,
    is_initial_buy: bool,
    inputs: StrategyInputs,
) -> tuple[bool, str]:
    if not inputs.core_dip_timing_enabled:
        return True, "disabled"
    if is_initial_buy:
        return True, "initial_core"
    if drawdown_pct >= inputs.core_dip_start_drawdown_pct:
        return True, "drawdown_reached"
    max_delay_days = int(inputs.core_dip_timing_max_delay_days)
    if max_delay_days <= 0 or pending_days >= max_delay_days:
        return True, "delay_expired"
    closes = [float(item.close) for item in recent_points if item.close > 0]
    if len(closes) < 2:
        return True, "insufficient_history"
    previous_close = closes[-2]
    day_change_pct = (float(point.close) / previous_close - 1.0) * 100.0 if previous_close > 0 else 0.0
    recent_low = min(closes)
    distance_from_low_pct = (float(point.close) / recent_low - 1.0) * 100.0 if recent_low > 0 else 0.0
    if day_change_pct <= 0:
        return True, "down_day"
    if distance_from_low_pct <= inputs.core_dip_timing_near_low_pct:
        return True, "near_recent_low"
    if day_change_pct >= inputs.core_dip_timing_rise_threshold_pct:
        return False, "defer_after_rise"
    return True, "normal"


def grid_rebound_stages(
    anchor_drawdown_pct: float,
    inputs: StrategyInputs,
) -> list[tuple[str, float, float]]:
    if anchor_drawdown_pct <= 0:
        return []
    step = max(float(inputs.grid_rebound_step_pct), 1e-9)
    stages: list[tuple[str, float, float]] = []
    stage_index = 1
    while True:
        threshold = max(0.0, anchor_drawdown_pct - step * stage_index)
        stages.append((f"grid_{stage_index}", threshold, float(inputs.grid_sell_pct or 0)))
        if threshold <= 0:
            return stages
        stage_index += 1


def sell_stage_rearm_drawdown_pct(inputs: StrategyInputs) -> float:
    raw_threshold = (
        inputs.sell_stage_rearm_drawdown_pct
        if inputs.sell_stage_rearm_drawdown_pct is not None
        else inputs.dca_rearm_drawdown_pct
    )
    threshold = max(0.0, float(raw_threshold))
    return min(threshold, float(inputs.max_drawdown_pct))
