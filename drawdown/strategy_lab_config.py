"""Shared configuration model for the strategy lab."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Mapping

from drawdown.option_overlay import OptionOverlaySettings
from drawdown.position_strategy import (
    DEFAULT_PORTFOLIO,
    DEFAULT_INVESTMENT_UNIVERSE,
    SCORECARD_DRAWDOWN_WEIGHT,
    SCORECARD_PERIODS,
    SCORECARD_PORTFOLIOS,
    SCORECARD_RETURN_WEIGHT,
    SELL_STRATEGY_LABELS,
    STRATEGY_LABELS,
    StrategyInputs,
)


DEFAULT_SCORECARD_PORTFOLIO_KEYS = [
    "tsm_100",
    "googl_100",
    "tsla_100",
    "core_50_30_20",
]

DEFAULT_STRATEGY_LAB_DEFAULTS: dict[str, object] = {
    "default_initial_cash": 20000,
    "default_monthly_contribution": 1000,
    "default_max_drawdown_pct": 50,
    "default_trade_fee": 0.35,
    "default_slice_step_pct": 5,
    "default_equal_slice_allocation_pct": 10,
    "default_hkd_to_usd": 0.128,
    "default_reserve_position_pct": 40,
    "default_sell_min_profit_pct": 10,
    "default_repair_sell_cooldown_days": 30,
    "default_repair_stage_sell_pct": 12,
    "default_dca_rearm_drawdown_pct": 5,
    "default_grid_rebound_step_pct": 5,
    "default_grid_first_sell_pct": 40,
    "default_grid_second_sell_pct": 40,
    "default_grid_min_sell_amount": 200,
    "default_cost_first_profit_pct": 8,
    "default_cost_second_profit_pct": 15,
    "default_cost_third_profit_pct": 25,
    "default_cost_first_sell_pct": 30,
    "default_cost_second_sell_pct": 30,
    "default_cost_third_sell_pct": 30,
    "default_cost_deleverage_cooldown_days": 0,
    "default_cost_min_sell_amount": 0,
    "default_core_dip_initial_core_pct": 80,
    "default_core_dip_weekly_core_pct": 90,
    "default_core_dip_cash_reserve_pct": 8,
    "default_core_dip_start_drawdown_pct": 5,
    "default_core_dip_full_drawdown_pct": 25,
    "default_core_dip_timing_enabled": False,
    "default_core_dip_timing_max_delay_days": 3,
    "default_core_dip_timing_rise_threshold_pct": 1.5,
    "default_core_dip_timing_near_low_pct": 2,
    "default_drawdown_basis": "rolling_120",
    "default_buy_strategy": "all",
    "default_sell_strategy": "all",
    "default_score_sell_strategy": "all",
    "default_score_return_weight_pct": 90,
    "default_score_drawdown_weight_pct": 10,
    "default_scorecard_portfolio_keys": [],
    "default_scorecard_periods": [],
    "default_scan_buy_strategy": "pyramid_3",
    "default_scan_period_trading_days": 1260,
    "default_scan_sell_min_profit_values": "5,10,15,20,25",
    "default_scan_repair_cooldown_values": "0,15,30,45,60",
    "default_scan_repair_stage_sell_values": "8,12,16,20,25",
    "default_option_enabled": False,
    "default_option_wallet_pct": 20,
    "default_option_target_dte": 250,
    "default_option_min_dte": 200,
    "default_option_max_dte": 300,
    "default_option_moneyness": "otm_10",
    "default_option_profit_take_pct": 100,
    "default_option_profit_take_sell_pct": 50,
    "default_option_exit_dte": 60,
    "default_option_trade_fee": 0.35,
    "default_option_trade_allocation_pct": 30,
    "default_option_max_trades_per_strategy": 20,
    "default_portfolio": [],
    "default_investment_universe": [],
}


@dataclass(frozen=True)
class ScorecardPeriodConfig:
    key: str
    label: str = ""
    start: str = ""
    end: str = ""
    enabled: bool = True

    def to_payload(self) -> dict[str, object]:
        return {
            "key": self.key,
            "label": self.label,
            "start": self.start,
            "end": self.end,
            "enabled": self.enabled,
        }


@dataclass(frozen=True)
class StrategyLabConfig:
    initial_cash: float = 20000.0
    monthly_contribution: float = 1000.0
    max_drawdown_pct: float = 50.0
    trade_fee: float = 0.35
    slice_step_pct: float = 5.0
    equal_slice_allocation_pct: float = 10.0
    hkd_to_usd: float = 0.128
    reserve_position_pct: float = 40.0
    sell_min_profit_pct: float = 10.0
    repair_sell_cooldown_days: int = 30
    repair_stage_sell_pct: float = 12.0
    dca_rearm_drawdown_pct: float = 5.0
    grid_rebound_step_pct: float = 5.0
    grid_first_sell_pct: float = 40.0
    grid_second_sell_pct: float = 40.0
    grid_min_sell_amount: float = 200.0
    cost_first_profit_pct: float = 8.0
    cost_second_profit_pct: float = 15.0
    cost_third_profit_pct: float = 25.0
    cost_first_sell_pct: float = 30.0
    cost_second_sell_pct: float = 30.0
    cost_third_sell_pct: float = 30.0
    cost_deleverage_cooldown_days: int = 0
    cost_min_sell_amount: float = 0.0
    core_dip_initial_core_pct: float = 80.0
    core_dip_weekly_core_pct: float = 90.0
    core_dip_cash_reserve_pct: float = 8.0
    core_dip_start_drawdown_pct: float = 5.0
    core_dip_full_drawdown_pct: float = 25.0
    core_dip_timing_enabled: bool = False
    core_dip_timing_max_delay_days: int = 3
    core_dip_timing_rise_threshold_pct: float = 1.5
    core_dip_timing_near_low_pct: float = 2.0
    drawdown_basis: str = "rolling_120"
    buy_strategy: str = "all"
    sell_strategy: str = "all"
    score_sell_strategy: str = "all"
    score_return_weight_pct: float = 90.0
    score_drawdown_weight_pct: float = 10.0
    scorecard_portfolio_keys: list[str] = field(default_factory=list)
    scorecard_periods: list[ScorecardPeriodConfig] = field(default_factory=list)
    scan_buy_strategy: str = "pyramid_3"
    scan_period_trading_days: int = 1260
    scan_sell_min_profit_values: str = "5,10,15,20,25"
    scan_repair_cooldown_values: str = "0,15,30,45,60"
    scan_repair_stage_sell_values: str = "8,12,16,20,25"
    option_enabled: bool = False
    option_wallet_pct: float = 20.0
    option_trade_allocation_pct: float = 30.0
    option_target_dte: int = 250
    option_min_dte: int = 200
    option_max_dte: int = 300
    option_moneyness: str = "otm_10"
    option_profit_take_pct: float = 100.0
    option_profit_take_sell_pct: float = 50.0
    option_exit_dte: int = 60
    option_trade_fee: float = 0.35
    option_max_trades_per_strategy: int = 20
    portfolio: list[dict[str, object]] = field(default_factory=list)
    investment_universe: list[dict[str, object]] = field(default_factory=list)

    @classmethod
    def from_saved_defaults(cls, raw: Mapping[str, object] | None) -> "StrategyLabConfig":
        raw = raw or {}
        return cls(
            initial_cash=_read_float(raw, "default_initial_cash"),
            monthly_contribution=_read_float(raw, "default_monthly_contribution"),
            max_drawdown_pct=_read_float(raw, "default_max_drawdown_pct"),
            trade_fee=_read_float(raw, "default_trade_fee"),
            slice_step_pct=_read_float(raw, "default_slice_step_pct"),
            equal_slice_allocation_pct=_read_float(raw, "default_equal_slice_allocation_pct"),
            hkd_to_usd=_read_float(raw, "default_hkd_to_usd"),
            reserve_position_pct=_read_float(raw, "default_reserve_position_pct"),
            sell_min_profit_pct=_read_float(raw, "default_sell_min_profit_pct"),
            repair_sell_cooldown_days=_read_int(raw, "default_repair_sell_cooldown_days"),
            repair_stage_sell_pct=_read_float(raw, "default_repair_stage_sell_pct"),
            dca_rearm_drawdown_pct=_read_float(raw, "default_dca_rearm_drawdown_pct"),
            grid_rebound_step_pct=_read_float(raw, "default_grid_rebound_step_pct"),
            grid_first_sell_pct=_read_float(raw, "default_grid_first_sell_pct"),
            grid_second_sell_pct=_read_float(raw, "default_grid_second_sell_pct"),
            grid_min_sell_amount=_read_float(raw, "default_grid_min_sell_amount"),
            cost_first_profit_pct=_read_float(raw, "default_cost_first_profit_pct"),
            cost_second_profit_pct=_read_float(raw, "default_cost_second_profit_pct"),
            cost_third_profit_pct=_read_float(raw, "default_cost_third_profit_pct"),
            cost_first_sell_pct=_read_float(raw, "default_cost_first_sell_pct"),
            cost_second_sell_pct=_read_float(raw, "default_cost_second_sell_pct"),
            cost_third_sell_pct=_read_float(raw, "default_cost_third_sell_pct"),
            cost_deleverage_cooldown_days=_read_int(raw, "default_cost_deleverage_cooldown_days"),
            cost_min_sell_amount=_read_float(raw, "default_cost_min_sell_amount"),
            core_dip_initial_core_pct=_read_float(raw, "default_core_dip_initial_core_pct"),
            core_dip_weekly_core_pct=_read_float(raw, "default_core_dip_weekly_core_pct"),
            core_dip_cash_reserve_pct=_read_float(raw, "default_core_dip_cash_reserve_pct"),
            core_dip_start_drawdown_pct=_read_float(raw, "default_core_dip_start_drawdown_pct"),
            core_dip_full_drawdown_pct=_read_float(raw, "default_core_dip_full_drawdown_pct"),
            core_dip_timing_enabled=_read_bool(raw, "default_core_dip_timing_enabled"),
            core_dip_timing_max_delay_days=_read_int(raw, "default_core_dip_timing_max_delay_days"),
            core_dip_timing_rise_threshold_pct=_read_float(raw, "default_core_dip_timing_rise_threshold_pct"),
            core_dip_timing_near_low_pct=_read_float(raw, "default_core_dip_timing_near_low_pct"),
            drawdown_basis=str(raw.get("default_drawdown_basis") or _default("default_drawdown_basis")),
            buy_strategy=_saved_selector(raw, "default_buy_strategy", STRATEGY_LABELS),
            sell_strategy=_saved_selector(raw, "default_sell_strategy", SELL_STRATEGY_LABELS),
            score_sell_strategy=_saved_selector(raw, "default_score_sell_strategy", SELL_STRATEGY_LABELS),
            score_return_weight_pct=SCORECARD_RETURN_WEIGHT * 100,
            score_drawdown_weight_pct=SCORECARD_DRAWDOWN_WEIGHT * 100,
            scorecard_portfolio_keys=_valid_scorecard_keys(raw.get("default_scorecard_portfolio_keys")),
            scorecard_periods=_read_scorecard_periods(raw.get("default_scorecard_periods")),
            scan_buy_strategy=_saved_selector(raw, "default_scan_buy_strategy", STRATEGY_LABELS, allow_all=False),
            scan_period_trading_days=_read_int(raw, "default_scan_period_trading_days"),
            scan_sell_min_profit_values=_read_text(raw, "default_scan_sell_min_profit_values"),
            scan_repair_cooldown_values=_read_text(raw, "default_scan_repair_cooldown_values"),
            scan_repair_stage_sell_values=_read_text(raw, "default_scan_repair_stage_sell_values"),
            option_enabled=_read_bool(raw, "default_option_enabled"),
            option_wallet_pct=_read_float(
                raw,
                "default_option_wallet_pct",
                _read_float(raw, "default_option_allocation_pct", float(_default("default_option_wallet_pct"))),
            ),
            option_target_dte=_read_int(raw, "default_option_target_dte"),
            option_min_dte=_read_int(raw, "default_option_min_dte"),
            option_max_dte=_read_int(raw, "default_option_max_dte"),
            option_moneyness=str(raw.get("default_option_moneyness") or _default("default_option_moneyness")),
            option_profit_take_pct=_read_float(raw, "default_option_profit_take_pct"),
            option_profit_take_sell_pct=_read_float(raw, "default_option_profit_take_sell_pct"),
            option_exit_dte=_read_int(raw, "default_option_exit_dte"),
            option_trade_fee=_read_float(raw, "default_option_trade_fee"),
            option_trade_allocation_pct=_read_float(raw, "default_option_trade_allocation_pct"),
            option_max_trades_per_strategy=_read_int(raw, "default_option_max_trades_per_strategy"),
            portfolio=_read_portfolio(raw.get("default_portfolio")),
            investment_universe=_read_investment_universe(raw.get("default_investment_universe")),
        ).validated()

    @classmethod
    def from_defaults_payload(
        cls,
        payload: Mapping[str, object],
        base: Mapping[str, object] | "StrategyLabConfig" | None = None,
    ) -> "StrategyLabConfig":
        base_config = base if isinstance(base, StrategyLabConfig) else cls.from_saved_defaults(base)
        merged = base_config.to_legacy_defaults()
        merged.update(dict(payload))
        return cls.from_saved_defaults(merged)

    @classmethod
    def from_runtime_payload(
        cls,
        payload: Mapping[str, object],
        base: Mapping[str, object] | "StrategyLabConfig" | None = None,
    ) -> "StrategyLabConfig":
        base_config = base if isinstance(base, StrategyLabConfig) else cls.from_saved_defaults(base)
        option_payload = payload.get("option_overlay")
        option_payload = option_payload if isinstance(option_payload, Mapping) else {}
        return cls(
            initial_cash=_read_float(payload, "initial_cash", base_config.initial_cash),
            monthly_contribution=_read_float(payload, "monthly_contribution", base_config.monthly_contribution),
            max_drawdown_pct=_read_float(payload, "max_drawdown_pct", base_config.max_drawdown_pct),
            trade_fee=_read_float(payload, "trade_fee", base_config.trade_fee),
            slice_step_pct=_read_float(payload, "step_pct", base_config.slice_step_pct),
            equal_slice_allocation_pct=_read_float(
                payload,
                "equal_slice_allocation_pct",
                base_config.equal_slice_allocation_pct,
            ),
            hkd_to_usd=_read_float(payload, "hkd_to_usd", base_config.hkd_to_usd),
            reserve_position_pct=_read_float(payload, "reserve_position_pct", base_config.reserve_position_pct),
            sell_min_profit_pct=_read_float(payload, "sell_min_profit_pct", base_config.sell_min_profit_pct),
            repair_sell_cooldown_days=_read_int(
                payload,
                "repair_sell_cooldown_days",
                base_config.repair_sell_cooldown_days,
            ),
            repair_stage_sell_pct=_read_float(
                payload,
                "repair_stage_sell_pct",
                base_config.repair_stage_sell_pct,
            ),
            dca_rearm_drawdown_pct=_read_float(
                payload,
                "dca_rearm_drawdown_pct",
                base_config.dca_rearm_drawdown_pct,
            ),
            grid_rebound_step_pct=_read_float(
                payload,
                "grid_rebound_step_pct",
                base_config.grid_rebound_step_pct,
            ),
            grid_first_sell_pct=_read_float(
                payload,
                "grid_first_sell_pct",
                base_config.grid_first_sell_pct,
            ),
            grid_second_sell_pct=_read_float(
                payload,
                "grid_second_sell_pct",
                base_config.grid_second_sell_pct,
            ),
            grid_min_sell_amount=_read_float(
                payload,
                "grid_min_sell_amount",
                base_config.grid_min_sell_amount,
            ),
            cost_first_profit_pct=_read_float(
                payload,
                "cost_first_profit_pct",
                base_config.cost_first_profit_pct,
            ),
            cost_second_profit_pct=_read_float(
                payload,
                "cost_second_profit_pct",
                base_config.cost_second_profit_pct,
            ),
            cost_third_profit_pct=_read_float(
                payload,
                "cost_third_profit_pct",
                base_config.cost_third_profit_pct,
            ),
            cost_first_sell_pct=_read_float(
                payload,
                "cost_first_sell_pct",
                base_config.cost_first_sell_pct,
            ),
            cost_second_sell_pct=_read_float(
                payload,
                "cost_second_sell_pct",
                base_config.cost_second_sell_pct,
            ),
            cost_third_sell_pct=_read_float(
                payload,
                "cost_third_sell_pct",
                base_config.cost_third_sell_pct,
            ),
            cost_deleverage_cooldown_days=_read_int(
                payload,
                "cost_deleverage_cooldown_days",
                base_config.cost_deleverage_cooldown_days,
            ),
            cost_min_sell_amount=_read_float(
                payload,
                "cost_min_sell_amount",
                base_config.cost_min_sell_amount,
            ),
            core_dip_initial_core_pct=_read_float(
                payload,
                "core_dip_initial_core_pct",
                base_config.core_dip_initial_core_pct,
            ),
            core_dip_weekly_core_pct=_read_float(
                payload,
                "core_dip_weekly_core_pct",
                base_config.core_dip_weekly_core_pct,
            ),
            core_dip_cash_reserve_pct=_read_float(
                payload,
                "core_dip_cash_reserve_pct",
                base_config.core_dip_cash_reserve_pct,
            ),
            core_dip_start_drawdown_pct=_read_float(
                payload,
                "core_dip_start_drawdown_pct",
                base_config.core_dip_start_drawdown_pct,
            ),
            core_dip_full_drawdown_pct=_read_float(
                payload,
                "core_dip_full_drawdown_pct",
                base_config.core_dip_full_drawdown_pct,
            ),
            core_dip_timing_enabled=_read_bool(
                payload,
                "core_dip_timing_enabled",
                base_config.core_dip_timing_enabled,
            ),
            core_dip_timing_max_delay_days=_read_int(
                payload,
                "core_dip_timing_max_delay_days",
                base_config.core_dip_timing_max_delay_days,
            ),
            core_dip_timing_rise_threshold_pct=_read_float(
                payload,
                "core_dip_timing_rise_threshold_pct",
                base_config.core_dip_timing_rise_threshold_pct,
            ),
            core_dip_timing_near_low_pct=_read_float(
                payload,
                "core_dip_timing_near_low_pct",
                base_config.core_dip_timing_near_low_pct,
            ),
            drawdown_basis=str(payload.get("drawdown_basis") or base_config.drawdown_basis),
            buy_strategy=_selector_from_payload(payload.get("buy_strategies"), STRATEGY_LABELS, base_config.buy_strategy),
            sell_strategy=_selector_from_payload(
                payload.get("sell_strategies"),
                SELL_STRATEGY_LABELS,
                base_config.sell_strategy,
            ),
            score_sell_strategy=_selector_from_payload(
                payload.get("score_sell_strategies"),
                SELL_STRATEGY_LABELS,
                base_config.score_sell_strategy,
            ),
            score_return_weight_pct=_read_float(
                payload,
                "return_weight",
                base_config.score_return_weight_pct / 100.0,
            )
            * 100.0,
            score_drawdown_weight_pct=_read_float(
                payload,
                "drawdown_weight",
                base_config.score_drawdown_weight_pct / 100.0,
            )
            * 100.0,
            scorecard_portfolio_keys=_valid_scorecard_keys(
                payload.get("scorecard_portfolio_keys"),
                fallback=base_config.scorecard_portfolio_keys,
            ),
            scorecard_periods=_read_scorecard_periods(
                payload.get("scorecard_periods"),
                fallback=base_config.scorecard_periods,
            ),
            scan_buy_strategy=str(payload.get("buy_strategy") or base_config.scan_buy_strategy),
            scan_period_trading_days=_read_int(payload, "trading_days", base_config.scan_period_trading_days),
            scan_sell_min_profit_values=base_config.scan_sell_min_profit_values,
            scan_repair_cooldown_values=base_config.scan_repair_cooldown_values,
            scan_repair_stage_sell_values=base_config.scan_repair_stage_sell_values,
            option_enabled=_read_bool(option_payload, "enabled", base_config.option_enabled),
            option_wallet_pct=_read_float(
                option_payload,
                "wallet_pct",
                _read_float(option_payload, "allocation_pct", base_config.option_wallet_pct),
            ),
            option_target_dte=_read_int(option_payload, "target_dte", base_config.option_target_dte),
            option_min_dte=_read_int(option_payload, "min_dte", base_config.option_min_dte),
            option_max_dte=_read_int(option_payload, "max_dte", base_config.option_max_dte),
            option_moneyness=str(option_payload.get("moneyness") or base_config.option_moneyness),
            option_profit_take_pct=_read_float(option_payload, "profit_take_pct", base_config.option_profit_take_pct),
            option_profit_take_sell_pct=_read_float(
                option_payload,
                "profit_take_sell_pct",
                base_config.option_profit_take_sell_pct,
            ),
            option_exit_dte=_read_int(option_payload, "exit_dte", base_config.option_exit_dte),
            option_trade_fee=_read_float(option_payload, "trade_fee", base_config.option_trade_fee),
            option_trade_allocation_pct=_read_float(
                option_payload,
                "trade_allocation_pct",
                base_config.option_trade_allocation_pct,
            ),
            option_max_trades_per_strategy=_read_int(
                option_payload,
                "max_trades_per_strategy",
                base_config.option_max_trades_per_strategy,
            ),
            portfolio=_read_portfolio(payload.get("targets"), fallback=base_config.portfolio),
            investment_universe=_read_investment_universe(
                payload.get("investment_universe"),
                fallback=base_config.investment_universe,
            ),
        ).validated()

    def validated(self) -> "StrategyLabConfig":
        if self.drawdown_basis not in {"ath", "rolling_120"}:
            raise ValueError("回撤口径必须是 ath 或 rolling_120。")
        if self.buy_strategy != "all" and self.buy_strategy not in STRATEGY_LABELS:
            raise ValueError("买入策略无效。")
        if self.sell_strategy != "all" and self.sell_strategy not in SELL_STRATEGY_LABELS:
            raise ValueError("卖出策略无效。")
        if self.score_sell_strategy != "all" and self.score_sell_strategy not in SELL_STRATEGY_LABELS:
            raise ValueError("评分卖出策略无效。")
        if self.dca_rearm_drawdown_pct < 0:
            raise ValueError("DCA 卖出重启回撤不能小于 0。")
        if self.grid_rebound_step_pct <= 0:
            raise ValueError("网格回弹步长必须大于 0。")
        if not 0 <= self.grid_first_sell_pct <= 100:
            raise ValueError("网格第一档卖出比例必须在 0 到 100 之间。")
        if not 0 <= self.grid_second_sell_pct <= 100:
            raise ValueError("网格第二档卖出比例必须在 0 到 100 之间。")
        if self.grid_min_sell_amount < 0:
            raise ValueError("网格最小卖出金额不能小于 0。")
        cost_profits = [self.cost_first_profit_pct, self.cost_second_profit_pct, self.cost_third_profit_pct]
        if any(value < 0 or value > 100 for value in cost_profits):
            raise ValueError("成本区间盈利档位必须在 0 到 100 之间。")
        if cost_profits != sorted(cost_profits):
            raise ValueError("成本区间盈利档位必须从低到高。")
        for label, value in (
            ("第一档", self.cost_first_sell_pct),
            ("第二档", self.cost_second_sell_pct),
            ("第三档", self.cost_third_sell_pct),
        ):
            if not 0 <= value <= 100:
                raise ValueError(f"成本区间{label}卖出比例必须在 0 到 100 之间。")
        if self.cost_deleverage_cooldown_days < 0:
            raise ValueError("成本区间去杠杆冷却天数不能小于 0。")
        if self.cost_min_sell_amount < 0:
            raise ValueError("成本区间最小卖出金额不能小于 0。")
        for label, value in (
            ("初始核心仓", self.core_dip_initial_core_pct),
            ("每周核心投入", self.core_dip_weekly_core_pct),
            ("现金垫", self.core_dip_cash_reserve_pct),
            ("加仓起点回撤", self.core_dip_start_drawdown_pct),
            ("满档加仓回撤", self.core_dip_full_drawdown_pct),
        ):
            if not 0 <= value <= 100:
                raise ValueError(f"核心定投{label}必须在 0 到 100 之间。")
        if self.core_dip_start_drawdown_pct > self.core_dip_full_drawdown_pct:
            raise ValueError("核心定投加仓起点不能高于满档加仓回撤。")
        if self.core_dip_timing_max_delay_days < 0:
            raise ValueError("核心定投买点优化最多延迟天数不能小于 0。")
        if self.core_dip_timing_rise_threshold_pct < 0:
            raise ValueError("核心定投买点优化大涨阈值不能小于 0。")
        if self.core_dip_timing_near_low_pct < 0:
            raise ValueError("核心定投买点优化近低偏离不能小于 0。")
        if self.scan_buy_strategy not in STRATEGY_LABELS:
            raise ValueError("扫描买入策略无效。")
        if self.option_moneyness not in {"atm", "itm_10", "otm_10"}:
            raise ValueError("期权行权价规则无效。")
        if self.option_min_dte > self.option_max_dte:
            raise ValueError("期权最小 DTE 不能大于最大 DTE。")
        return self

    def to_strategy_inputs(self) -> StrategyInputs:
        return StrategyInputs(
            initial_cash=self.initial_cash,
            monthly_contribution=self.monthly_contribution,
            max_drawdown_pct=self.max_drawdown_pct,
            drawdown_basis=self.drawdown_basis,
            step_pct=self.slice_step_pct,
            equal_slice_allocation_pct=self.equal_slice_allocation_pct,
            trade_fee=self.trade_fee,
            hkd_to_usd=self.hkd_to_usd,
            reserve_position_pct=self.reserve_position_pct,
            sell_min_profit_pct=self.sell_min_profit_pct,
            repair_sell_cooldown_days=self.repair_sell_cooldown_days,
            repair_stage_sell_pct=self.repair_stage_sell_pct,
            dca_rearm_drawdown_pct=self.dca_rearm_drawdown_pct,
            grid_rebound_step_pct=self.grid_rebound_step_pct,
            grid_first_sell_pct=self.grid_first_sell_pct,
            grid_second_sell_pct=self.grid_second_sell_pct,
            grid_min_sell_amount=self.grid_min_sell_amount,
            cost_first_profit_pct=self.cost_first_profit_pct,
            cost_second_profit_pct=self.cost_second_profit_pct,
            cost_third_profit_pct=self.cost_third_profit_pct,
            cost_first_sell_pct=self.cost_first_sell_pct,
            cost_second_sell_pct=self.cost_second_sell_pct,
            cost_third_sell_pct=self.cost_third_sell_pct,
            cost_deleverage_cooldown_days=self.cost_deleverage_cooldown_days,
            cost_min_sell_amount=self.cost_min_sell_amount,
            core_dip_initial_core_pct=self.core_dip_initial_core_pct,
            core_dip_weekly_core_pct=self.core_dip_weekly_core_pct,
            core_dip_cash_reserve_pct=self.core_dip_cash_reserve_pct,
            core_dip_start_drawdown_pct=self.core_dip_start_drawdown_pct,
            core_dip_full_drawdown_pct=self.core_dip_full_drawdown_pct,
            core_dip_timing_enabled=self.core_dip_timing_enabled,
            core_dip_timing_max_delay_days=self.core_dip_timing_max_delay_days,
            core_dip_timing_rise_threshold_pct=self.core_dip_timing_rise_threshold_pct,
            core_dip_timing_near_low_pct=self.core_dip_timing_near_low_pct,
        )

    def score_weights(self) -> tuple[float, float]:
        return SCORECARD_RETURN_WEIGHT, SCORECARD_DRAWDOWN_WEIGHT

    def option_settings(self) -> OptionOverlaySettings:
        return OptionOverlaySettings(
            enabled=self.option_enabled,
            wallet_pct=self.option_wallet_pct,
            trade_allocation_pct=self.option_trade_allocation_pct,
            target_dte=self.option_target_dte,
            min_dte=self.option_min_dte,
            max_dte=self.option_max_dte,
            moneyness=self.option_moneyness,
            profit_take_pct=self.option_profit_take_pct,
            profit_take_sell_pct=self.option_profit_take_sell_pct,
            exit_dte=self.option_exit_dte,
            trade_fee=self.option_trade_fee,
            max_trades_per_strategy=self.option_max_trades_per_strategy,
        )

    def scorecard_period_payloads(self) -> list[dict[str, object]]:
        return [period.to_payload() for period in self.scorecard_periods]

    def selected_scorecard_keys(self) -> list[str]:
        return self.scorecard_portfolio_keys or list(DEFAULT_SCORECARD_PORTFOLIO_KEYS)

    def selected_buy_strategies(self) -> list[str]:
        return _strategies_from_selector(self.buy_strategy, STRATEGY_LABELS)

    def selected_sell_strategies(self) -> list[str]:
        return _strategies_from_selector(self.sell_strategy, SELL_STRATEGY_LABELS)

    def portfolio_or_default(self) -> list[dict[str, object]]:
        return self.portfolio or [dict(item) for item in DEFAULT_PORTFOLIO]

    def investment_universe_or_default(self) -> list[dict[str, object]]:
        return _merge_investment_universe(self.investment_universe)

    def to_legacy_defaults(self) -> dict[str, object]:
        return {
            "default_initial_cash": self.initial_cash,
            "default_monthly_contribution": self.monthly_contribution,
            "default_max_drawdown_pct": self.max_drawdown_pct,
            "default_trade_fee": self.trade_fee,
            "default_slice_step_pct": self.slice_step_pct,
            "default_equal_slice_allocation_pct": self.equal_slice_allocation_pct,
            "default_hkd_to_usd": self.hkd_to_usd,
            "default_reserve_position_pct": self.reserve_position_pct,
            "default_sell_min_profit_pct": self.sell_min_profit_pct,
            "default_repair_sell_cooldown_days": self.repair_sell_cooldown_days,
            "default_repair_stage_sell_pct": self.repair_stage_sell_pct,
            "default_dca_rearm_drawdown_pct": self.dca_rearm_drawdown_pct,
            "default_grid_rebound_step_pct": self.grid_rebound_step_pct,
            "default_grid_first_sell_pct": self.grid_first_sell_pct,
            "default_grid_second_sell_pct": self.grid_second_sell_pct,
            "default_grid_min_sell_amount": self.grid_min_sell_amount,
            "default_cost_first_profit_pct": self.cost_first_profit_pct,
            "default_cost_second_profit_pct": self.cost_second_profit_pct,
            "default_cost_third_profit_pct": self.cost_third_profit_pct,
            "default_cost_first_sell_pct": self.cost_first_sell_pct,
            "default_cost_second_sell_pct": self.cost_second_sell_pct,
            "default_cost_third_sell_pct": self.cost_third_sell_pct,
            "default_cost_deleverage_cooldown_days": self.cost_deleverage_cooldown_days,
            "default_cost_min_sell_amount": self.cost_min_sell_amount,
            "default_core_dip_initial_core_pct": self.core_dip_initial_core_pct,
            "default_core_dip_weekly_core_pct": self.core_dip_weekly_core_pct,
            "default_core_dip_cash_reserve_pct": self.core_dip_cash_reserve_pct,
            "default_core_dip_start_drawdown_pct": self.core_dip_start_drawdown_pct,
            "default_core_dip_full_drawdown_pct": self.core_dip_full_drawdown_pct,
            "default_core_dip_timing_enabled": self.core_dip_timing_enabled,
            "default_core_dip_timing_max_delay_days": self.core_dip_timing_max_delay_days,
            "default_core_dip_timing_rise_threshold_pct": self.core_dip_timing_rise_threshold_pct,
            "default_core_dip_timing_near_low_pct": self.core_dip_timing_near_low_pct,
            "default_drawdown_basis": self.drawdown_basis,
            "default_buy_strategy": self.buy_strategy,
            "default_sell_strategy": self.sell_strategy,
            "default_score_sell_strategy": self.score_sell_strategy,
            "default_score_return_weight_pct": SCORECARD_RETURN_WEIGHT * 100,
            "default_score_drawdown_weight_pct": SCORECARD_DRAWDOWN_WEIGHT * 100,
            "default_scorecard_portfolio_keys": list(self.scorecard_portfolio_keys),
            "default_scorecard_periods": self.scorecard_period_payloads(),
            "default_scan_buy_strategy": self.scan_buy_strategy,
            "default_scan_period_trading_days": self.scan_period_trading_days,
            "default_scan_sell_min_profit_values": self.scan_sell_min_profit_values,
            "default_scan_repair_cooldown_values": self.scan_repair_cooldown_values,
            "default_scan_repair_stage_sell_values": self.scan_repair_stage_sell_values,
            "default_option_enabled": self.option_enabled,
            "default_option_wallet_pct": self.option_wallet_pct,
            "default_option_target_dte": self.option_target_dte,
            "default_option_min_dte": self.option_min_dte,
            "default_option_max_dte": self.option_max_dte,
            "default_option_moneyness": self.option_moneyness,
            "default_option_profit_take_pct": self.option_profit_take_pct,
            "default_option_profit_take_sell_pct": self.option_profit_take_sell_pct,
            "default_option_exit_dte": self.option_exit_dte,
            "default_option_trade_fee": self.option_trade_fee,
            "default_option_trade_allocation_pct": self.option_trade_allocation_pct,
            "default_option_max_trades_per_strategy": self.option_max_trades_per_strategy,
            "default_portfolio": [dict(item) for item in self.portfolio],
            "default_investment_universe": [dict(item) for item in self.investment_universe],
        }


def strategy_lab_default_dict() -> dict[str, object]:
    return StrategyLabConfig.from_saved_defaults(DEFAULT_STRATEGY_LAB_DEFAULTS).to_legacy_defaults()


def _default(key: str) -> object:
    return DEFAULT_STRATEGY_LAB_DEFAULTS[key]


def _read_float(payload: Mapping[str, object], key: str, default: float | None = None) -> float:
    fallback = _default(key) if default is None else default
    value = payload.get(key, fallback)
    if value in (None, ""):
        return float(fallback)
    parsed = float(value)
    if not math.isfinite(parsed):
        return float(fallback)
    return parsed


def _read_int(payload: Mapping[str, object], key: str, default: int | None = None) -> int:
    fallback = _default(key) if default is None else default
    value = payload.get(key, fallback)
    if value in (None, ""):
        return int(fallback)
    return int(float(value))


def _read_bool(payload: Mapping[str, object], key: str, default: bool | None = None) -> bool:
    fallback = bool(_default(key)) if default is None else default
    value = payload.get(key, fallback)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    return bool(value)


def _read_text(payload: Mapping[str, object], key: str) -> str:
    value = payload.get(key, _default(key))
    return str(value if value not in (None, "") else _default(key))


def _saved_selector(
    payload: Mapping[str, object],
    key: str,
    labels: Mapping[str, str],
    *,
    allow_all: bool = True,
) -> str:
    fallback = str(_default(key))
    value = str(payload.get(key) or fallback)
    if allow_all and value == "all":
        return value
    if value in labels:
        return value
    return fallback if (allow_all and fallback == "all") or fallback in labels else next(iter(labels))


def _read_scorecard_periods(
    raw: object,
    *,
    fallback: list[ScorecardPeriodConfig] | None = None,
) -> list[ScorecardPeriodConfig]:
    if not isinstance(raw, list):
        return list(fallback or [])
    valid_periods = {str(item["key"]) for item in SCORECARD_PERIODS}
    periods: list[ScorecardPeriodConfig] = []
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        key = str(item.get("key", ""))
        if key not in valid_periods:
            continue
        periods.append(
            ScorecardPeriodConfig(
                key=key,
                label=str(item.get("label", "")),
                start=str(item.get("start", "")),
                end=str(item.get("end", "")),
                enabled=_read_bool(item, "enabled", True),
            )
        )
    return periods


def _valid_scorecard_keys(raw: object, fallback: list[str] | None = None) -> list[str]:
    if not isinstance(raw, list):
        return list(fallback or [])
    valid_keys = {str(item["key"]) for item in SCORECARD_PORTFOLIOS}
    result: list[str] = []
    for key in raw:
        key_value = str(key)
        if key_value in valid_keys or key_value.startswith("symbol_"):
            result.append(key_value)
    return result


def _read_portfolio(raw: object, fallback: list[dict[str, object]] | None = None) -> list[dict[str, object]]:
    if not isinstance(raw, list):
        return [dict(item) for item in (fallback or [])]
    portfolio: list[dict[str, object]] = []
    for item in raw:
        if isinstance(item, Mapping):
            portfolio.append(dict(item))
    return portfolio


def _read_investment_universe(
    raw: object,
    fallback: list[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    if not isinstance(raw, list):
        return [dict(item) for item in (fallback or [])]
    universe: list[dict[str, object]] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, Mapping):
            continue
        symbol = str(item.get("symbol", "")).strip().upper()
        if not symbol or symbol in seen:
            continue
        seen.add(symbol)
        row: dict[str, object] = {
            "symbol": symbol,
            "name": str(item.get("name") or symbol).strip(),
        }
        max_drawdown = _optional_float(item.get("max_drawdown_pct"))
        if max_drawdown is not None:
            row["max_drawdown_pct"] = max_drawdown
        universe.append(row)
    return universe


def _merge_investment_universe(custom: list[dict[str, object]]) -> list[dict[str, object]]:
    merged: dict[str, dict[str, object]] = {}
    for item in DEFAULT_INVESTMENT_UNIVERSE:
        merged[str(item["symbol"]).upper()] = dict(item)
    for item in _read_investment_universe(custom):
        merged[str(item["symbol"]).upper()] = dict(item)
    return list(merged.values())


def _optional_float(value: object) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    if not math.isfinite(parsed) or parsed <= 0:
        return None
    return parsed


def _selector_from_payload(raw: object, labels: Mapping[str, str], fallback: str) -> str:
    if not isinstance(raw, list):
        return fallback
    selected = [str(item) for item in raw if str(item) in labels]
    if not selected:
        return fallback
    if set(selected) == set(labels):
        return "all"
    if len(selected) == 1:
        return selected[0]
    return "all"


def _strategies_from_selector(selector: str, labels: Mapping[str, str]) -> list[str]:
    return list(labels) if selector == "all" else [selector]
