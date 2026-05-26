"""Portfolio-level drawdown position strategy simulation."""

from __future__ import annotations

import math
import re
import time
from dataclasses import dataclass, replace
from datetime import date, datetime, timedelta
from typing import Callable, Iterable

from drawdown.generate_drawdown_report import (
    PricePoint,
    build_longbridge_quote_context,
    build_price_points_from_series,
    candle_datetime,
    fetch_longbridge_daily_candles,
    normalize_longbridge_symbol,
)
from drawdown.strategy_rules import (
    cost_deleverage_cooldown_elapsed,
    select_cost_deleverage_stage,
    should_check_sell_after_buy,
)


DEFAULT_PORTFOLIO = [
    {"symbol": "TSM.US", "weight": 40.0, "name": "TSM", "max_drawdown_pct": 40.0},
    {"symbol": "GOOGL.US", "weight": 30.0, "name": "GOOGL", "max_drawdown_pct": 40.0},
    {"symbol": "TSLA.US", "weight": 20.0, "name": "TSLA", "max_drawdown_pct": 50.0},
    {"symbol": "0700.HK", "weight": 10.0, "name": "Tencent", "max_drawdown_pct": 50.0},
]

STRATEGY_LABELS = {
    "pyramid_3": "三档金字塔",
    "equal_slice": "等距细切",
    "linear_weighted_slice": "线性递增加权细切",
    "weekly_dca": "每周定投",
    "salary_flow_dca": "工资流定投",
    "core_dip_dca": "核心定投+回撤加仓",
}

SELL_STRATEGY_LABELS = {
    "none": "不卖出",
    "repair_step": "阶梯修复卖出",
    "grid_rebound": "网格回弹卖出",
    "cost_deleverage": "成本区间去杠杆",
}

SCORECARD_PORTFOLIOS = [
    {
        "key": "tsm_100",
        "label": "全仓 TSM",
        "targets": [{"symbol": "TSM.US", "weight": 100.0, "name": "TSM"}],
    },
    {
        "key": "googl_100",
        "label": "全仓 GOOGL",
        "targets": [{"symbol": "GOOGL.US", "weight": 100.0, "name": "GOOGL"}],
    },
    {
        "key": "tsla_100",
        "label": "全仓 TSLA",
        "targets": [{"symbol": "TSLA.US", "weight": 100.0, "name": "TSLA"}],
    },
    {
        "key": "tencent_100",
        "label": "全仓 腾讯",
        "targets": [{"symbol": "0700.HK", "weight": 100.0, "name": "Tencent", "max_drawdown_pct": 50.0}],
    },
    {
        "key": "qqq_100",
        "label": "全仓 QQQ",
        "targets": [{"symbol": "QQQ.US", "weight": 100.0, "name": "QQQ"}],
    },
    {
        "key": "core_50_30_20",
        "label": "50% TSM + 30% GOOGL + 20% TSLA",
        "targets": [
            {"symbol": "TSM.US", "weight": 50.0, "name": "TSM"},
            {"symbol": "GOOGL.US", "weight": 30.0, "name": "GOOGL"},
            {"symbol": "TSLA.US", "weight": 20.0, "name": "TSLA"},
        ],
    },
]
DEFAULT_INVESTMENT_UNIVERSE = [
    {"symbol": "TSM.US", "name": "TSM", "max_drawdown_pct": 40.0},
    {"symbol": "GOOGL.US", "name": "GOOGL", "max_drawdown_pct": 40.0},
    {"symbol": "TSLA.US", "name": "TSLA", "max_drawdown_pct": 50.0},
    {"symbol": "0700.HK", "name": "腾讯", "max_drawdown_pct": 50.0},
    {"symbol": "QQQ.US", "name": "QQQ", "max_drawdown_pct": 40.0},
]

SCORECARD_PERIODS = [
    {"key": "1y", "label": "近 1 年", "trading_days": 252, "fetch_days": 365},
    {"key": "3y", "label": "近 3 年", "trading_days": 252 * 3, "fetch_days": 365 * 3},
    {"key": "5y", "label": "近 5 年", "trading_days": 252 * 5, "fetch_days": 365 * 5},
]

SCORECARD_RETURN_WEIGHT = 0.90
SCORECARD_DRAWDOWN_WEIGHT = 0.10
ROBUST_REPAIR_SELL_MIN_PROFITS = [5, 10, 20]
ROBUST_REPAIR_COOLDOWNS = [0, 30, 60]
ROBUST_REPAIR_STAGE_SELLS = [8, 15, 25]
ROBUST_GRID_REBOUND_STEPS = [2.5, 5.0, 7.5, 10.0, 15.0]
ROBUST_GRID_SELLS = [15.0, 25.0, 40.0, 50.0]
ROBUST_GRID_FIRST_SELLS = [10.0, 15.0, 25.0, 40.0]
ROBUST_GRID_SECOND_SELLS = ROBUST_GRID_SELLS
ROBUST_COST_PROFIT_SETS = [(8.0, 15.0, 25.0), (10.0, 20.0, 30.0), (15.0, 25.0, 40.0)]
ROBUST_COST_SELL_SETS = [(20.0, 20.0, 20.0), (30.0, 30.0, 30.0), (40.0, 30.0, 20.0)]
ROBUST_COST_COOLDOWNS = [0, 15, 30]
ROBUST_NON_REPAIR_SELL_STRATEGIES = ("none",)
LOT_SELL_BUY_STRATEGIES = {"pyramid_3"}
REARM_BUY_STRATEGIES = {
    "pyramid_3",
    "equal_slice",
    "linear_weighted_slice",
    "weekly_dca",
    "salary_flow_dca",
    "core_dip_dca",
}
POSITION_SELL_REARM_STRATEGIES = {"repair_step", "grid_rebound", "cost_deleverage"}
ROBUST_DCA_REARM_DRAWDOWN_VALUES = [0.0, 5.0, 10.0, 15.0, 20.0]
ROBUST_SELL_STAGE_REARM_DRAWDOWN_VALUES = [10.0, 15.0]
BUY_REARM_MODE_CUMULATIVE = "cumulative"
BUY_REARM_MODE_RESTART_FROM_REARM = "restart_from_rearm"
BUY_REARM_MODES = (BUY_REARM_MODE_CUMULATIVE, BUY_REARM_MODE_RESTART_FROM_REARM)
ROBUST_BUY_STEP_VALUES = [2.5, 5.0, 10.0]
ROBUST_EQUAL_SLICE_ALLOCATION_VALUES = [2.5, 5.0, 7.5, 10.0]
ROBUST_CORE_DIP_PARAM_SETS = [
    (70.0, 85.0, 12.0, 10.0, 30.0),
    (80.0, 90.0, 8.0, 5.0, 25.0),
    (85.0, 95.0, 6.0, 8.0, 25.0),
    (90.0, 95.0, 5.0, 5.0, 20.0),
    (90.0, 100.0, 5.0, 5.0, 15.0),
    (95.0, 100.0, 3.0, 3.0, 15.0),
]
ROBUST_CORE_DIP_TIMING_MAX_DELAY_DAYS = [1, 3, 5]
ROBUST_CORE_DIP_TIMING_RISE_THRESHOLDS = [1.0, 1.5, 2.5]
ROBUST_CORE_DIP_TIMING_NEAR_LOW_VALUES = [1.0, 2.0, 3.0]

class StrategyLabComputationCancelled(RuntimeError):
    """Raised when a long-running Strategy Lab computation is cancelled."""


@dataclass(frozen=True)
class StrategyInputs:
    initial_cash: float = 10000.0
    monthly_contribution: float = 0.0
    max_drawdown_pct: float = 50.0
    drawdown_basis: str = "ath"
    step_pct: float = 5.0
    equal_slice_allocation_pct: float = 5.0
    trade_fee: float = 0.35
    hkd_to_usd: float = 0.128
    reserve_position_pct: float = 25.0
    sell_min_profit_pct: float = 10.0
    repair_sell_cooldown_days: int = 30
    repair_stage_sell_pct: float = 12.0
    dca_rearm_drawdown_pct: float = 5.0
    sell_stage_rearm_drawdown_pct: float | None = None
    grid_rebound_step_pct: float = 5.0
    grid_sell_pct: float | None = None
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
    sell_allow_same_day_sell: bool = False
    cost_min_sell_amount: float = 0.0
    buy_rearm_mode: str = BUY_REARM_MODE_CUMULATIVE
    core_dip_initial_core_pct: float = 80.0
    core_dip_weekly_core_pct: float = 90.0
    core_dip_cash_reserve_pct: float = 8.0
    core_dip_start_drawdown_pct: float = 5.0
    core_dip_full_drawdown_pct: float = 25.0
    core_dip_timing_enabled: bool = False
    core_dip_timing_max_delay_days: int = 3
    core_dip_timing_rise_threshold_pct: float = 1.5
    core_dip_timing_near_low_pct: float = 2.0

    def __post_init__(self) -> None:
        if self.grid_sell_pct is None:
            object.__setattr__(self, "grid_sell_pct", float(self.grid_second_sell_pct))


@dataclass(frozen=True)
class PortfolioTarget:
    symbol: str
    weight: float
    name: str = ""
    max_drawdown_pct: float | None = None


@dataclass(frozen=True)
class StrategyTranche:
    strategy: str
    threshold_pct: float
    allocation_pct: float
    label: str


@dataclass
class PositionLot:
    threshold_pct: float
    buy_drawdown_pct: float
    buy_price_usd: float
    initial_shares: float
    remaining_shares: float
    first_grid_sell_done: bool = False
    second_grid_sell_done: bool = False
    repair_sell_marks: set[str] | None = None
    grid_sell_marks: set[str] | None = None


@dataclass
class SymbolState:
    symbol: str
    name: str
    weight: float
    budget: float
    cash: float
    shares: float = 0.0
    invested: float = 0.0
    fees: float = 0.0
    trades: int = 0
    buy_trades: int = 0
    sell_trades: int = 0
    sold_gross: float = 0.0
    max_shares: float = 0.0
    last_price: float | None = None
    last_value: float = 0.0
    lots: list[PositionLot] | None = None
    sell_marks: set[str] | None = None
    last_repair_sell_date: date | None = None
    last_repair_sell_trade_index: int | None = None
    last_cost_deleverage_sell_trade_index: int | None = None
    dca_pending_cash: float = 0.0
    core_dip_pending_cash: float = 0.0
    core_dip_pending_days: int = 0
    buy_rearm_drawdown_pct: float | None = None
    buy_rearm_anchor_drawdown_pct: float | None = None
    grid_rebound_cycle_anchor_drawdown_pct: float | None = None


def parse_date_range(start_raw: str | None, end_raw: str | None) -> tuple[date, date]:
    end_date = date.fromisoformat(end_raw) if end_raw else datetime.now().date()
    start_date = date.fromisoformat(start_raw) if start_raw else end_date - timedelta(days=365 * 3)
    if start_date > end_date:
        raise ValueError("开始日期不能晚于结束日期。")
    return start_date, end_date


def parse_portfolio_targets(raw_targets: Iterable[dict[str, object]]) -> list[PortfolioTarget]:
    targets: list[PortfolioTarget] = []
    for raw in raw_targets:
        symbol = str(raw.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        weight = float(raw.get("weight", 0) or 0)
        if weight <= 0:
            continue
        max_drawdown_pct = _optional_positive_pct(raw.get("max_drawdown_pct"))
        targets.append(
            PortfolioTarget(
                symbol=normalize_longbridge_symbol(symbol),
                weight=weight,
                name=str(raw.get("name", "") or symbol).strip(),
                max_drawdown_pct=max_drawdown_pct,
            )
        )
    if not targets:
        raise ValueError("至少需要一个有效标的。")

    total_weight = sum(target.weight for target in targets)
    if total_weight <= 0:
        raise ValueError("组合权重总和必须大于 0。")

    return [
        PortfolioTarget(
            target.symbol,
            target.weight / total_weight * 100.0,
            target.name,
            target.max_drawdown_pct,
        )
        for target in targets
    ]


def build_strategy_tranches(inputs: StrategyInputs, strategy: str) -> list[StrategyTranche]:
    if strategy == "weekly_dca":
        return [StrategyTranche(strategy, 0.0, 0.0, "初始现金和工资到账后立即投入")]
    if strategy == "salary_flow_dca":
        return [StrategyTranche(strategy, 0.0, 0.0, "每周首个交易日按工资流动态定投")]
    if strategy == "core_dip_dca":
        return [StrategyTranche(strategy, 0.0, 0.0, "先建核心底仓，再用工资流定投和回撤扫入现金")]

    max_dd = _positive_pct(inputs.max_drawdown_pct, "最大可接受回撤")
    step = _positive_pct(inputs.step_pct, "细切步长")

    if strategy == "pyramid_3":
        return [
            StrategyTranche(strategy, max_dd * 0.2, 20.0, "20% 回撤锚点"),
            StrategyTranche(strategy, max_dd * 0.5, 30.0, "50% 回撤锚点"),
            StrategyTranche(strategy, max_dd, 50.0, "100% 回撤锚点"),
        ]

    thresholds = _slice_thresholds(max_dd, step)
    if strategy == "equal_slice":
        allocation = _positive_pct(inputs.equal_slice_allocation_pct, "等距细切每档仓位")
        return [
            StrategyTranche(strategy, threshold, allocation, f"每 {step:g}% 等距")
            for threshold in thresholds
        ]

    if strategy == "linear_weighted_slice":
        weight_sum = sum(index + 1 for index in range(len(thresholds)))
        return [
            StrategyTranche(
                strategy,
                threshold,
                (index + 1) / weight_sum * 100.0,
                "线性递增加权",
            )
            for index, threshold in enumerate(thresholds)
        ]

    raise ValueError(f"未知策略: {strategy}")


def simulate_portfolio(
    price_points_by_symbol: dict[str, list[PricePoint]],
    targets: list[PortfolioTarget],
    inputs: StrategyInputs,
    strategies: Iterable[str] = (
        "pyramid_3",
        "equal_slice",
        "linear_weighted_slice",
        "weekly_dca",
        "salary_flow_dca",
        "core_dip_dca",
    ),
    sell_strategies: Iterable[str] = ("none",),
) -> dict[str, object]:
    if inputs.initial_cash <= 0:
        raise ValueError("初始资金必须大于 0。")
    if inputs.monthly_contribution < 0:
        raise ValueError("每月注入资金不能为负数。")
    if inputs.trade_fee < 0:
        raise ValueError("手续费不能为负数。")
    if inputs.hkd_to_usd <= 0:
        raise ValueError("HKD/USD 汇率必须大于 0。")
    if inputs.drawdown_basis not in {"ath", "rolling_120"}:
        raise ValueError("回撤口径必须是 ath 或 rolling_120。")
    if inputs.reserve_position_pct < 0 or inputs.reserve_position_pct > 100:
        raise ValueError("底仓比例必须在 0 到 100 之间。")
    if inputs.sell_min_profit_pct < 0 or inputs.sell_min_profit_pct > 100:
        raise ValueError("最小卖出盈利必须在 0 到 100 之间。")
    if inputs.repair_sell_cooldown_days < 0:
        raise ValueError("阶梯修复卖出冷却天数不能为负数。")
    if inputs.repair_stage_sell_pct < 0 or inputs.repair_stage_sell_pct > 100:
        raise ValueError("阶梯修复单档卖出比例必须在 0 到 100 之间。")
    if inputs.sell_stage_rearm_drawdown_pct is not None and inputs.sell_stage_rearm_drawdown_pct < 0:
        raise ValueError("卖出档位重启回撤不能为负数。")
    if inputs.grid_rebound_step_pct <= 0 or inputs.grid_rebound_step_pct > 100:
        raise ValueError("网格回弹步长必须在 0 到 100 之间。")
    if float(inputs.grid_sell_pct or 0) < 0 or float(inputs.grid_sell_pct or 0) > 100:
        raise ValueError("网格每档卖出比例必须在 0 到 100 之间。")
    if inputs.grid_min_sell_amount < 0:
        raise ValueError("网格最小卖出金额不能为负数。")
    cost_profits = [
        inputs.cost_first_profit_pct,
        inputs.cost_second_profit_pct,
        inputs.cost_third_profit_pct,
    ]
    if any(value < 0 or value > 100 for value in cost_profits):
        raise ValueError("成本区间盈利档位必须在 0 到 100 之间。")
    if cost_profits != sorted(cost_profits):
        raise ValueError("成本区间盈利档位必须从低到高。")
    if inputs.cost_first_sell_pct < 0 or inputs.cost_first_sell_pct > 100:
        raise ValueError("成本区间第一档卖出比例必须在 0 到 100 之间。")
    if inputs.cost_second_sell_pct < 0 or inputs.cost_second_sell_pct > 100:
        raise ValueError("成本区间第二档卖出比例必须在 0 到 100 之间。")
    if inputs.cost_third_sell_pct < 0 or inputs.cost_third_sell_pct > 100:
        raise ValueError("成本区间第三档卖出比例必须在 0 到 100 之间。")
    if inputs.cost_deleverage_cooldown_days < 0:
        raise ValueError("成本区间去杠杆冷却天数不能为负数。")
    if inputs.cost_min_sell_amount < 0:
        raise ValueError("成本区间最小卖出金额不能为负数。")
    if inputs.buy_rearm_mode not in BUY_REARM_MODES:
        raise ValueError("买入重启模式必须是 cumulative 或 restart_from_rearm。")
    if not 0 <= inputs.core_dip_initial_core_pct <= 100:
        raise ValueError("核心定投初始核心仓比例必须在 0 到 100 之间。")
    if not 0 <= inputs.core_dip_weekly_core_pct <= 100:
        raise ValueError("核心定投每周核心投入比例必须在 0 到 100 之间。")
    if not 0 <= inputs.core_dip_cash_reserve_pct <= 100:
        raise ValueError("核心定投现金垫比例必须在 0 到 100 之间。")
    if not 0 <= inputs.core_dip_start_drawdown_pct <= 100:
        raise ValueError("核心定投加仓起点回撤必须在 0 到 100 之间。")
    if not 0 <= inputs.core_dip_full_drawdown_pct <= 100:
        raise ValueError("核心定投满档回撤必须在 0 到 100 之间。")
    if inputs.core_dip_start_drawdown_pct > inputs.core_dip_full_drawdown_pct:
        raise ValueError("核心定投加仓起点不能高于满档回撤。")
    if inputs.core_dip_timing_max_delay_days < 0:
        raise ValueError("核心定投买点优化最多延迟天数不能为负数。")
    if inputs.core_dip_timing_rise_threshold_pct < 0:
        raise ValueError("核心定投买点优化大涨阈值不能为负数。")
    if inputs.core_dip_timing_near_low_pct < 0:
        raise ValueError("核心定投买点优化近低偏离不能为负数。")

    target_by_symbol = {target.symbol: target for target in targets}
    missing_symbols = [symbol for symbol in target_by_symbol if symbol not in price_points_by_symbol]
    if missing_symbols:
        raise ValueError("缺少价格数据: " + ", ".join(missing_symbols))

    strategy_results = []
    for strategy in strategies:
        for sell_strategy in sell_strategies:
            strategy_results.append(
                _simulate_strategy(price_points_by_symbol, targets, inputs, strategy, sell_strategy)
            )

    return {
        "inputs": {
            "initial_cash": inputs.initial_cash,
            "monthly_contribution": inputs.monthly_contribution,
            "max_drawdown_pct": inputs.max_drawdown_pct,
            "drawdown_basis": inputs.drawdown_basis,
            "step_pct": inputs.step_pct,
            "equal_slice_allocation_pct": inputs.equal_slice_allocation_pct,
            "trade_fee": inputs.trade_fee,
            "hkd_to_usd": inputs.hkd_to_usd,
            "reserve_position_pct": inputs.reserve_position_pct,
            "sell_min_profit_pct": inputs.sell_min_profit_pct,
            "repair_sell_cooldown_days": inputs.repair_sell_cooldown_days,
            "repair_stage_sell_pct": inputs.repair_stage_sell_pct,
            "dca_rearm_drawdown_pct": inputs.dca_rearm_drawdown_pct,
            "sell_stage_rearm_drawdown_pct": inputs.sell_stage_rearm_drawdown_pct,
            "grid_rebound_step_pct": inputs.grid_rebound_step_pct,
            "grid_sell_pct": inputs.grid_sell_pct,
            "grid_first_sell_pct": inputs.grid_first_sell_pct,
            "grid_second_sell_pct": inputs.grid_second_sell_pct,
            "grid_min_sell_amount": inputs.grid_min_sell_amount,
            "cost_first_profit_pct": inputs.cost_first_profit_pct,
            "cost_second_profit_pct": inputs.cost_second_profit_pct,
            "cost_third_profit_pct": inputs.cost_third_profit_pct,
            "cost_first_sell_pct": inputs.cost_first_sell_pct,
            "cost_second_sell_pct": inputs.cost_second_sell_pct,
            "cost_third_sell_pct": inputs.cost_third_sell_pct,
            "cost_deleverage_cooldown_days": inputs.cost_deleverage_cooldown_days,
            "sell_allow_same_day_sell": inputs.sell_allow_same_day_sell,
            "cost_min_sell_amount": inputs.cost_min_sell_amount,
            "buy_rearm_mode": inputs.buy_rearm_mode,
            "core_dip_initial_core_pct": inputs.core_dip_initial_core_pct,
            "core_dip_weekly_core_pct": inputs.core_dip_weekly_core_pct,
            "core_dip_cash_reserve_pct": inputs.core_dip_cash_reserve_pct,
            "core_dip_start_drawdown_pct": inputs.core_dip_start_drawdown_pct,
            "core_dip_full_drawdown_pct": inputs.core_dip_full_drawdown_pct,
            "core_dip_timing_enabled": inputs.core_dip_timing_enabled,
            "core_dip_timing_max_delay_days": inputs.core_dip_timing_max_delay_days,
            "core_dip_timing_rise_threshold_pct": inputs.core_dip_timing_rise_threshold_pct,
            "core_dip_timing_near_low_pct": inputs.core_dip_timing_near_low_pct,
        },
        "targets": [
            {
                "symbol": target.symbol,
                "name": target.name,
                "weight": target.weight,
                "max_drawdown_pct": target.max_drawdown_pct,
            }
            for target in targets
        ],
        "price_series": _build_price_series_payload(price_points_by_symbol),
        "strategies": strategy_results,
    }


def run_longbridge_strategy_lab(
    raw_targets: Iterable[dict[str, object]],
    inputs: StrategyInputs,
    start_date: date,
    end_date: date,
    buy_strategies: Iterable[str] = (
        "pyramid_3",
        "equal_slice",
        "linear_weighted_slice",
        "weekly_dca",
        "salary_flow_dca",
        "core_dip_dca",
    ),
    sell_strategies: Iterable[str] = ("none", "repair_step", "cost_deleverage"),
    trading_days: int | None = None,
) -> dict[str, object]:
    targets = parse_portfolio_targets(raw_targets)
    quote_ctx = build_longbridge_quote_context()
    price_points_by_symbol: dict[str, list[PricePoint]] = {}
    warnings: list[str] = []

    for target in targets:
        candles = fetch_longbridge_daily_candles(quote_ctx, target.symbol, start_date, end_date)
        if not candles:
            raise RuntimeError(f"Longbridge 没有返回 {target.symbol} 的历史日线。")
        series = [
            (candle_datetime(candle).replace(tzinfo=None), float(candle.close))
            for candle in candles
        ]
        points = build_price_points_from_series(series)
        if not points:
            raise RuntimeError(f"无法从 Longbridge 构建 {target.symbol} 的价格序列。")
        if points[0].date.date() > start_date:
            warnings.append(f"{target.symbol} 首个可用交易日为 {points[0].date.date().isoformat()}")
        if trading_days is not None:
            points = _last_trading_points(points, int(trading_days))
        price_points_by_symbol[target.symbol] = points

    result = simulate_portfolio(
        price_points_by_symbol,
        targets,
        inputs,
        strategies=buy_strategies,
        sell_strategies=sell_strategies,
    )
    result["range"] = {
        "start": start_date.isoformat(),
        "end": end_date.isoformat(),
    }
    result["warnings"] = warnings
    return result


def run_longbridge_strategy_scorecard(
    inputs: StrategyInputs,
    end_date: date,
    buy_strategies: Iterable[str] = (
        "pyramid_3",
        "equal_slice",
        "linear_weighted_slice",
        "weekly_dca",
        "salary_flow_dca",
        "core_dip_dca",
    ),
    sell_strategies: Iterable[str] = ("none", "repair_step", "cost_deleverage"),
    core_targets: Iterable[dict[str, object]] | None = None,
    portfolio_keys: Iterable[str] | None = None,
    scorecard_periods: Iterable[dict[str, object]] | None = None,
    investment_universe: Iterable[dict[str, object]] | None = None,
    return_weight: float = SCORECARD_RETURN_WEIGHT,
    drawdown_weight: float = SCORECARD_DRAWDOWN_WEIGHT,
) -> dict[str, object]:
    return_weight, drawdown_weight = _normalize_score_weights(return_weight, drawdown_weight)
    scorecard_portfolios = _resolve_scorecard_portfolios(core_targets, portfolio_keys, investment_universe)
    resolved_periods = _resolve_scorecard_periods(end_date, scorecard_periods)
    start_date = min(period["fetch_start"] for period in resolved_periods)
    fetch_end_date = max(period["end"] for period in resolved_periods)
    full_points_by_symbol, warnings = _fetch_scorecard_points(scorecard_portfolios, start_date, fetch_end_date)

    questions: list[dict[str, object]] = []
    summary_by_key: dict[str, dict[str, object]] = {}

    for portfolio in scorecard_portfolios:
        targets = parse_portfolio_targets(portfolio["targets"])
        for period in resolved_periods:
            if period["mode"] == "exact":
                scoped_points = {
                    target.symbol: _rebuild_points_for_range(
                        full_points_by_symbol[target.symbol],
                        period["start"],
                        period["end"],
                    )
                    for target in targets
                }
            else:
                scoped_points = {
                    target.symbol: _last_trading_points(
                        _rebuild_points_for_range(
                            full_points_by_symbol[target.symbol],
                            period["fetch_start"],
                            period["end"],
                        ),
                        int(period["trading_days"]),
                    )
                    for target in targets
                }
            question_start = min(points[0].date.date() for points in scoped_points.values())
            question_result = simulate_portfolio(
                scoped_points,
                targets,
                inputs,
                strategies=buy_strategies,
                sell_strategies=sell_strategies,
            )
            scored = _score_question_strategies(
                question_result["strategies"],
                return_weight=return_weight,
                drawdown_weight=drawdown_weight,
            )
            question_key = f"{portfolio['key']}__{period['key']}"
            questions.append(
                {
                    "key": question_key,
                    "portfolio_key": portfolio["key"],
                    "portfolio_label": portfolio["label"],
                    "period_key": period["key"],
                    "period_label": period["label"],
                    "start": question_start.isoformat(),
                    "end": period["end"].isoformat(),
                    "strategies": scored,
                }
            )
            for item in scored:
                summary = summary_by_key.setdefault(
                    item["key"],
                    {
                        "key": item["key"],
                        "label": item["label"],
                        "buy_strategy": item["buy_strategy"],
                        "sell_strategy": item["sell_strategy"],
                        "return_sum": 0.0,
                        "drawdown_sum": 0.0,
                        "rank_sum": 0.0,
                        "sell_quality_sum": 0.0,
                        "sell_profit_pct_sum": 0.0,
                        "sell_drawdown_sum": 0.0,
                        "cash_reuse_sum": 0.0,
                        "buy_drawdown_sum": 0.0,
                        "avg_cash_sum": 0.0,
                        "best_rank": math.inf,
                        "worst_rank": 0,
                        "question_count": 0,
                    },
                )
                summary["return_sum"] += item["return_pct"]
                summary["drawdown_sum"] += item["max_drawdown_pct"]
                summary["rank_sum"] += item["rank"]
                summary["sell_quality_sum"] += item["sell_quality_score"]
                summary["sell_profit_pct_sum"] += item["avg_sell_profit_pct"]
                summary["sell_drawdown_sum"] += item["avg_sell_drawdown_pct"]
                summary["cash_reuse_sum"] += item["cash_reuse_pct"]
                summary["buy_drawdown_sum"] += item["avg_buy_drawdown_pct"]
                summary["avg_cash_sum"] += item["avg_cash_pct"]
                summary["best_rank"] = min(summary["best_rank"], item["rank"])
                summary["worst_rank"] = max(summary["worst_rank"], item["rank"])
                summary["question_count"] += 1

    summary_rows = []
    for raw in summary_by_key.values():
        count = int(raw["question_count"])
        summary_rows.append(
            {
                "key": raw["key"],
                "label": raw["label"],
                "buy_strategy": raw["buy_strategy"],
                "sell_strategy": raw["sell_strategy"],
                "score": 0.0,
                "avg_return_pct": raw["return_sum"] / count if count else 0.0,
                "avg_drawdown_pct": raw["drawdown_sum"] / count if count else 0.0,
                "avg_rank": raw["rank_sum"] / count if count else 0.0,
                "avg_sell_quality_score": raw["sell_quality_sum"] / count if count else 0.0,
                "avg_sell_profit_pct": raw["sell_profit_pct_sum"] / count if count else 0.0,
                "avg_sell_drawdown_pct": raw["sell_drawdown_sum"] / count if count else 0.0,
                "avg_cash_reuse_pct": raw["cash_reuse_sum"] / count if count else 0.0,
                "avg_buy_drawdown_pct": raw["buy_drawdown_sum"] / count if count else 0.0,
                "avg_cash_pct": raw["avg_cash_sum"] / count if count else 0.0,
                "best_rank": int(raw["best_rank"]) if count else 0,
                "worst_rank": int(raw["worst_rank"]) if count else 0,
                "question_count": count,
            }
        )
    _score_summary_rows(summary_rows, return_weight, drawdown_weight)
    summary_rows.sort(key=lambda item: item["score"], reverse=True)

    return {
        "weights": {
            "return": return_weight,
            "drawdown": drawdown_weight,
        },
        "range": {
            "start": start_date.isoformat(),
            "end": fetch_end_date.isoformat(),
        },
        "portfolios": [portfolio["key"] for portfolio in scorecard_portfolios],
        "periods": [
            {
                "key": period["key"],
                "label": period["label"],
                "start": period["start"].isoformat() if period.get("start") else None,
                "end": period["end"].isoformat(),
                "mode": period["mode"],
                "trading_days": period.get("trading_days"),
            }
            for period in resolved_periods
        ],
        "questions": questions,
        "summary": summary_rows,
        "warnings": warnings,
    }


def run_longbridge_sell_parameter_scan(
    raw_targets: Iterable[dict[str, object]],
    inputs: StrategyInputs,
    start_date: date,
    end_date: date,
    buy_strategy: str,
    sell_min_profit_values: Iterable[float],
    repair_cooldown_values: Iterable[int],
    repair_stage_sell_values: Iterable[float],
    trading_days: int | None = None,
) -> dict[str, object]:
    """Scan repair-step sell parameters for one portfolio and one buy strategy."""
    if buy_strategy not in STRATEGY_LABELS:
        raise ValueError(f"未知买入策略: {buy_strategy}")

    min_profit_values = _scan_float_values(sell_min_profit_values, "最小卖出盈利", maximum=100.0)
    cooldown_values = _scan_int_values(repair_cooldown_values, "修复冷却天数")
    stage_sell_values = _scan_float_values(repair_stage_sell_values, "修复单档卖出", maximum=100.0)
    combination_count = len(min_profit_values) * len(cooldown_values) * len(stage_sell_values)
    if combination_count > 200:
        raise ValueError("参数扫描组合过多，请控制在 200 组以内。")

    targets = parse_portfolio_targets(raw_targets)
    quote_ctx = build_longbridge_quote_context()
    price_points_by_symbol: dict[str, list[PricePoint]] = {}
    warnings: list[str] = []

    for target in targets:
        candles = fetch_longbridge_daily_candles(quote_ctx, target.symbol, start_date, end_date)
        if not candles:
            raise RuntimeError(f"Longbridge 没有返回 {target.symbol} 的历史日线。")
        series = [
            (candle_datetime(candle).replace(tzinfo=None), float(candle.close))
            for candle in candles
        ]
        points = build_price_points_from_series(series)
        if not points:
            raise RuntimeError(f"无法从 Longbridge 构建 {target.symbol} 的价格序列。")
        if points[0].date.date() > start_date:
            warnings.append(f"{target.symbol} 首个可用交易日为 {points[0].date.date().isoformat()}")
        if trading_days is not None:
            points = _last_trading_points(points, int(trading_days))
        price_points_by_symbol[target.symbol] = points

    cells: list[dict[str, object]] = []
    best_cell: dict[str, object] | None = None
    baseline_cell: dict[str, object] | None = None
    for stage_sell_pct in stage_sell_values:
        for cooldown_days in cooldown_values:
            for min_profit_pct in min_profit_values:
                scan_inputs = replace(
                    inputs,
                    sell_min_profit_pct=min_profit_pct,
                    repair_sell_cooldown_days=cooldown_days,
                    repair_stage_sell_pct=stage_sell_pct,
                )
                result = simulate_portfolio(
                    price_points_by_symbol,
                    targets,
                    scan_inputs,
                    strategies=[buy_strategy],
                    sell_strategies=["repair_step"],
                )
                strategy = result["strategies"][0]
                metrics = strategy["metrics"]
                cell = {
                    "buy_strategy": buy_strategy,
                    "sell_strategy": "repair_step",
                    "sell_min_profit_pct": min_profit_pct,
                    "repair_sell_cooldown_days": cooldown_days,
                    "repair_stage_sell_pct": stage_sell_pct,
                    "return_pct": float(metrics["return_pct"]),
                    "max_drawdown_pct": float(metrics["max_drawdown_pct"]),
                    "sell_quality_score": float(metrics.get("sell_quality_score", 0.0)),
                    "avg_sell_profit_pct": float(metrics.get("avg_sell_profit_pct", 0.0)),
                    "avg_sell_drawdown_pct": float(metrics.get("avg_sell_drawdown_pct", 0.0)),
                    "cash_reuse_pct": float(metrics.get("cash_reuse_pct", 0.0)),
                    "sell_trade_count": int(metrics.get("sell_trade_count", 0)),
                    "buy_trade_count": int(metrics.get("buy_trade_count", 0)),
                    "trade_count": int(metrics.get("trade_count", 0)),
                    "final_value": float(metrics.get("final_value", 0.0)),
                }
                cells.append(cell)
                if best_cell is None or _scan_cell_sort_key(cell) > _scan_cell_sort_key(best_cell):
                    best_cell = cell
                if (
                    math.isclose(min_profit_pct, inputs.sell_min_profit_pct)
                    and cooldown_days == inputs.repair_sell_cooldown_days
                    and math.isclose(stage_sell_pct, inputs.repair_stage_sell_pct)
                ):
                    baseline_cell = cell

    return {
        "range": {
            "start": start_date.isoformat(),
            "end": end_date.isoformat(),
        },
        "buy_strategy": buy_strategy,
        "buy_strategy_label": STRATEGY_LABELS[buy_strategy],
        "sell_strategy": "repair_step",
        "sell_strategy_label": SELL_STRATEGY_LABELS["repair_step"],
        "axes": {
            "sell_min_profit_pct": min_profit_values,
            "repair_sell_cooldown_days": cooldown_values,
            "repair_stage_sell_pct": stage_sell_values,
        },
        "baseline_params": {
            "sell_min_profit_pct": inputs.sell_min_profit_pct,
            "repair_sell_cooldown_days": inputs.repair_sell_cooldown_days,
            "repair_stage_sell_pct": inputs.repair_stage_sell_pct,
        },
        "baseline": baseline_cell,
        "best": best_cell,
        "cells": cells,
        "warnings": warnings,
    }


def run_longbridge_robust_leaderboard(
    inputs: StrategyInputs,
    end_date: date,
    core_targets: Iterable[dict[str, object]] | None = None,
    portfolio_keys: Iterable[str] | None = None,
    scorecard_periods: Iterable[dict[str, object]] | None = None,
    investment_universe: Iterable[dict[str, object]] | None = None,
    buy_strategies: Iterable[str] | None = None,
    sell_strategies: Iterable[str] | None = None,
    top_n: int = 10,
    return_weight: float = SCORECARD_RETURN_WEIGHT,
    drawdown_weight: float = SCORECARD_DRAWDOWN_WEIGHT,
    core_dip_timing_filter: str = "all",
    control_checker: Callable[[], None] | None = None,
) -> dict[str, object]:
    """Find robust top strategy/parameter candidates on all selected tasks."""
    _check_robust_control(control_checker)
    return_weight, drawdown_weight = _normalize_score_weights(return_weight, drawdown_weight)
    selected_buy_strategies, selected_sell_strategies = _validate_robust_strategy_selection(
        buy_strategies,
        sell_strategies,
    )

    scorecard_portfolios = _resolve_scorecard_portfolios(core_targets, portfolio_keys, investment_universe)
    resolved_periods = _resolve_scorecard_periods(end_date, scorecard_periods)
    start_date = min(period["fetch_start"] for period in resolved_periods)
    fetch_end_date = max(period["end"] for period in resolved_periods)
    full_points_by_symbol, warnings = _fetch_scorecard_points(scorecard_portfolios, start_date, fetch_end_date)
    tasks = _build_robust_tasks(scorecard_portfolios, resolved_periods, full_points_by_symbol)
    if not tasks:
        raise ValueError("没有可用于稳健榜的题目。")
    _check_robust_control(control_checker)

    candidates = _robust_candidate_pool(
        selected_buy_strategies,
        selected_sell_strategies,
        inputs,
        core_dip_timing_filter,
    )
    rows = _score_robust_candidates(
        tasks,
        inputs,
        candidates,
        return_weight=return_weight,
        drawdown_weight=drawdown_weight,
        control_checker=control_checker,
    )
    _score_robust_summary_like_scorecard(rows, return_weight, drawdown_weight)
    leaderboard = sorted(rows, key=lambda item: item["robust_score"], reverse=True)[: max(1, int(top_n))]
    simulation_counts = {
        "total": len(tasks) * len(candidates),
    }

    return {
        "range": {
            "start": start_date.isoformat(),
            "end": fetch_end_date.isoformat(),
        },
        "method": {
            "name": "full_candidate_return_90_drawdown_10_leaderboard",
            "ranking_formula": "return_90_drawdown_10",
            "weights": {
                "return": return_weight,
                "drawdown": drawdown_weight,
            },
            "parameter_grid": _robust_parameter_grid_payload(inputs),
            "score_formula": "90% return + 10% drawdown",
        },
        "candidate_counts": {
            "total": len(candidates),
        },
        "simulation_counts": simulation_counts,
        "sell_strategies": selected_sell_strategies,
        "tasks": [
            {
                "key": task["key"],
                "portfolio_key": task["portfolio_key"],
                "portfolio_label": task["portfolio_label"],
                "period_key": task["period_key"],
                "period_label": task["period_label"],
                "start": task["start"].isoformat(),
                "end": task["end"].isoformat(),
            }
            for task in tasks
        ],
        "leaderboard": leaderboard,
        "warnings": warnings,
    }


def prepare_robust_leaderboard_packet(
    inputs: StrategyInputs,
    end_date: date,
    core_targets: Iterable[dict[str, object]] | None = None,
    portfolio_keys: Iterable[str] | None = None,
    scorecard_periods: Iterable[dict[str, object]] | None = None,
    investment_universe: Iterable[dict[str, object]] | None = None,
    buy_strategies: Iterable[str] | None = None,
    sell_strategies: Iterable[str] | None = None,
    top_n: int = 10,
    return_weight: float = SCORECARD_RETURN_WEIGHT,
    drawdown_weight: float = SCORECARD_DRAWDOWN_WEIGHT,
    core_dip_timing_filter: str = "all",
) -> dict[str, object]:
    """Prepare all market data and candidates for browser-side robust scoring."""
    return_weight, drawdown_weight = _normalize_score_weights(return_weight, drawdown_weight)
    selected_buy_strategies, selected_sell_strategies = _validate_robust_strategy_selection(
        buy_strategies,
        sell_strategies,
    )

    scorecard_portfolios = _resolve_scorecard_portfolios(core_targets, portfolio_keys, investment_universe)
    resolved_periods = _resolve_scorecard_periods(end_date, scorecard_periods)
    start_date = min(period["fetch_start"] for period in resolved_periods)
    fetch_end_date = max(period["end"] for period in resolved_periods)
    full_points_by_symbol, warnings = _fetch_scorecard_points(scorecard_portfolios, start_date, fetch_end_date)
    tasks = _build_robust_tasks(scorecard_portfolios, resolved_periods, full_points_by_symbol)
    if not tasks:
        raise ValueError("没有可用于稳健榜的题目。")

    candidates = _robust_candidate_pool(
        selected_buy_strategies,
        selected_sell_strategies,
        inputs,
        core_dip_timing_filter,
    )
    simulation_counts = {
        "total": len(tasks) * len(candidates),
    }
    return {
        "range": {
            "start": start_date.isoformat(),
            "end": fetch_end_date.isoformat(),
        },
        "inputs": _strategy_inputs_payload(inputs),
        "method": {
            "name": "client_full_candidate_return_90_drawdown_10_packet",
            "ranking_formula": "return_90_drawdown_10",
            "weights": {
                "return": return_weight,
                "drawdown": drawdown_weight,
            },
            "parameter_grid": _robust_parameter_grid_payload(inputs),
            "top_n": int(top_n),
            "score_formula": "90% return + 10% drawdown",
        },
        "candidate_counts": {
            "total": len(candidates),
        },
        "simulation_counts": simulation_counts,
        "buy_strategies": selected_buy_strategies,
        "sell_strategies": selected_sell_strategies,
        "tasks": [_robust_task_packet(task) for task in tasks],
        "candidate_pool": candidates,
        "warnings": warnings,
    }


def _strategy_inputs_payload(inputs: StrategyInputs) -> dict[str, object]:
    return {
        "initial_cash": inputs.initial_cash,
        "monthly_contribution": inputs.monthly_contribution,
        "max_drawdown_pct": inputs.max_drawdown_pct,
        "drawdown_basis": inputs.drawdown_basis,
        "step_pct": inputs.step_pct,
        "equal_slice_allocation_pct": inputs.equal_slice_allocation_pct,
        "trade_fee": inputs.trade_fee,
        "hkd_to_usd": inputs.hkd_to_usd,
        "reserve_position_pct": inputs.reserve_position_pct,
        "sell_min_profit_pct": inputs.sell_min_profit_pct,
        "repair_sell_cooldown_days": inputs.repair_sell_cooldown_days,
        "repair_stage_sell_pct": inputs.repair_stage_sell_pct,
        "dca_rearm_drawdown_pct": inputs.dca_rearm_drawdown_pct,
        "sell_stage_rearm_drawdown_pct": inputs.sell_stage_rearm_drawdown_pct,
        "grid_rebound_step_pct": inputs.grid_rebound_step_pct,
        "grid_sell_pct": inputs.grid_sell_pct,
        "grid_first_sell_pct": inputs.grid_first_sell_pct,
        "grid_second_sell_pct": inputs.grid_second_sell_pct,
        "grid_min_sell_amount": inputs.grid_min_sell_amount,
        "cost_first_profit_pct": inputs.cost_first_profit_pct,
        "cost_second_profit_pct": inputs.cost_second_profit_pct,
        "cost_third_profit_pct": inputs.cost_third_profit_pct,
        "cost_first_sell_pct": inputs.cost_first_sell_pct,
        "cost_second_sell_pct": inputs.cost_second_sell_pct,
        "cost_third_sell_pct": inputs.cost_third_sell_pct,
        "cost_deleverage_cooldown_days": inputs.cost_deleverage_cooldown_days,
        "sell_allow_same_day_sell": inputs.sell_allow_same_day_sell,
        "cost_min_sell_amount": inputs.cost_min_sell_amount,
        "buy_rearm_mode": inputs.buy_rearm_mode,
        "core_dip_initial_core_pct": inputs.core_dip_initial_core_pct,
        "core_dip_weekly_core_pct": inputs.core_dip_weekly_core_pct,
        "core_dip_cash_reserve_pct": inputs.core_dip_cash_reserve_pct,
        "core_dip_start_drawdown_pct": inputs.core_dip_start_drawdown_pct,
        "core_dip_full_drawdown_pct": inputs.core_dip_full_drawdown_pct,
        "core_dip_timing_enabled": inputs.core_dip_timing_enabled,
        "core_dip_timing_max_delay_days": inputs.core_dip_timing_max_delay_days,
        "core_dip_timing_rise_threshold_pct": inputs.core_dip_timing_rise_threshold_pct,
        "core_dip_timing_near_low_pct": inputs.core_dip_timing_near_low_pct,
    }


def _robust_parameter_grid_payload(inputs: StrategyInputs) -> dict[str, object]:
    return {
        "sell_min_profit_pct": ROBUST_REPAIR_SELL_MIN_PROFITS,
        "repair_sell_cooldown_days": ROBUST_REPAIR_COOLDOWNS,
        "repair_stage_sell_pct": ROBUST_REPAIR_STAGE_SELLS,
        "dca_rearm_drawdown_pct": ROBUST_DCA_REARM_DRAWDOWN_VALUES,
        "sell_stage_rearm_drawdown_pct": ROBUST_SELL_STAGE_REARM_DRAWDOWN_VALUES,
        "buy_rearm_mode": list(BUY_REARM_MODES),
        "grid_rebound_step_pct": ROBUST_GRID_REBOUND_STEPS,
        "grid_sell_pct": ROBUST_GRID_SELLS,
        "grid_min_sell_amount": [inputs.grid_min_sell_amount],
        "sell_allow_same_day_sell": [False, True],
        "cost_profit_sets": ROBUST_COST_PROFIT_SETS,
        "cost_sell_sets": ROBUST_COST_SELL_SETS,
        "cost_deleverage_cooldown_days": ROBUST_COST_COOLDOWNS,
        "cost_min_sell_amount": [inputs.cost_min_sell_amount],
        "core_dip_param_sets": ROBUST_CORE_DIP_PARAM_SETS,
        "core_dip_timing_enabled": [False, True],
        "core_dip_timing_max_delay_days": ROBUST_CORE_DIP_TIMING_MAX_DELAY_DAYS,
        "core_dip_timing_rise_threshold_pct": ROBUST_CORE_DIP_TIMING_RISE_THRESHOLDS,
        "core_dip_timing_near_low_pct": ROBUST_CORE_DIP_TIMING_NEAR_LOW_VALUES,
    }


def _robust_task_packet(task: dict[str, object]) -> dict[str, object]:
    price_points = task["price_points"]
    return {
        "key": task["key"],
        "portfolio_key": task["portfolio_key"],
        "portfolio_label": task["portfolio_label"],
        "period_key": task["period_key"],
        "period_label": task["period_label"],
        "start": task["start"].isoformat(),
        "end": task["end"].isoformat(),
        "targets": [
            {
                "symbol": target.symbol,
                "weight": target.weight,
                "name": target.name,
                "max_drawdown_pct": target.max_drawdown_pct,
            }
            for target in task["targets"]
        ],
        "price_points": {
            symbol: [
                {
                    "date": point.date.date().isoformat(),
                    "close": point.close,
                    "rolling_peak": point.rolling_peak,
                    "drawdown_ath": point.drawdown_ath,
                    "rolling_120_peak": point.rolling_120_peak,
                    "drawdown_120": point.drawdown_120,
                }
                for point in points
            ]
            for symbol, points in price_points.items()
        },
    }


def _resolve_scorecard_portfolios(
    core_targets: Iterable[dict[str, object]] | None = None,
    portfolio_keys: Iterable[str] | None = None,
    investment_universe: Iterable[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    scorecard_portfolios = [
        {**portfolio, "targets": [dict(target) for target in portfolio["targets"]]}
        for portfolio in SCORECARD_PORTFOLIOS
    ]
    scorecard_portfolios = _merge_universe_scorecard_portfolios(scorecard_portfolios, investment_universe)
    if portfolio_keys is not None:
        selected_keys = {str(key) for key in portfolio_keys}
        if not selected_keys:
            raise ValueError("至少需要选择一个评分题目。")
        known_keys = {str(portfolio["key"]) for portfolio in scorecard_portfolios}
        unknown_keys = selected_keys - known_keys
        if unknown_keys:
            raise ValueError("未知评分题目: " + ", ".join(sorted(unknown_keys)))
        scorecard_portfolios = [
            portfolio for portfolio in scorecard_portfolios
            if str(portfolio["key"]) in selected_keys
        ]
    symbol_max_drawdowns = _target_max_drawdown_by_symbol(core_targets or [])
    if core_targets is not None:
        parsed_core_targets = parse_portfolio_targets(core_targets)
        for portfolio in scorecard_portfolios:
            if portfolio["key"] == "core_50_30_20":
                portfolio["label"] = "当前组合"
                portfolio["targets"] = [
                    {
                        "symbol": target.symbol,
                        "weight": target.weight,
                        "name": target.name,
                        "max_drawdown_pct": target.max_drawdown_pct,
                    }
                    for target in parsed_core_targets
                ]
                break
    if symbol_max_drawdowns:
        for portfolio in scorecard_portfolios:
            for target in portfolio["targets"]:
                symbol = normalize_longbridge_symbol(str(target.get("symbol", "")))
                if symbol in symbol_max_drawdowns:
                    target["max_drawdown_pct"] = symbol_max_drawdowns[symbol]
    return scorecard_portfolios


def _merge_universe_scorecard_portfolios(
    scorecard_portfolios: list[dict[str, object]],
    investment_universe: Iterable[dict[str, object]] | None,
) -> list[dict[str, object]]:
    if investment_universe is None:
        return scorecard_portfolios
    by_symbol: dict[str, dict[str, object]] = {}
    for portfolio in scorecard_portfolios:
        targets = portfolio.get("targets")
        if isinstance(targets, list) and len(targets) == 1:
            parsed = parse_portfolio_targets(targets)
            if parsed:
                by_symbol[parsed[0].symbol] = portfolio
    for item in investment_universe:
        if not isinstance(item, dict):
            continue
        symbol = normalize_longbridge_symbol(str(item.get("symbol", "")).strip())
        if not symbol:
            continue
        name = str(item.get("name") or symbol).strip()
        max_drawdown_pct = _optional_positive_pct(item.get("max_drawdown_pct"))
        target = {"symbol": symbol, "weight": 100.0, "name": name}
        if max_drawdown_pct is not None:
            target["max_drawdown_pct"] = max_drawdown_pct
        portfolio = {
            "key": _scorecard_symbol_key(symbol),
            "label": f"全仓 {name}",
            "targets": [target],
        }
        existing = by_symbol.get(symbol)
        if existing:
            existing["label"] = portfolio["label"]
            existing["targets"] = [target]
        else:
            scorecard_portfolios.append(portfolio)
            by_symbol[symbol] = portfolio
    return scorecard_portfolios


def _scorecard_symbol_key(symbol: str) -> str:
    return "symbol_" + re.sub(r"[^a-z0-9]+", "_", symbol.lower()).strip("_")


def _fetch_scorecard_points(
    scorecard_portfolios: list[dict[str, object]],
    start_date: date,
    fetch_end_date: date,
) -> tuple[dict[str, list[PricePoint]], list[str]]:
    symbols = sorted(
        {
            target.symbol
            for portfolio in scorecard_portfolios
            for target in parse_portfolio_targets(portfolio["targets"])
        }
    )
    quote_ctx = build_longbridge_quote_context()
    full_points_by_symbol: dict[str, list[PricePoint]] = {}
    warnings: list[str] = []
    for symbol in symbols:
        candles = fetch_longbridge_daily_candles(quote_ctx, symbol, start_date, fetch_end_date)
        if not candles:
            raise RuntimeError(f"Longbridge 没有返回 {symbol} 的历史日线。")
        series = [
            (candle_datetime(candle).replace(tzinfo=None), float(candle.close))
            for candle in candles
        ]
        points = build_price_points_from_series(series)
        if not points:
            raise RuntimeError(f"无法从 Longbridge 构建 {symbol} 的价格序列。")
        full_points_by_symbol[symbol] = points
        if points[0].date.date() > start_date:
            warnings.append(f"{symbol} 首个可用交易日为 {points[0].date.date().isoformat()}")
    return full_points_by_symbol, warnings


def _build_robust_tasks(
    scorecard_portfolios: list[dict[str, object]],
    resolved_periods: list[dict[str, object]],
    full_points_by_symbol: dict[str, list[PricePoint]],
) -> list[dict[str, object]]:
    tasks: list[dict[str, object]] = []
    for portfolio in scorecard_portfolios:
        targets = parse_portfolio_targets(portfolio["targets"])
        for period in resolved_periods:
            if period["mode"] == "exact":
                scoped_points = {
                    target.symbol: _rebuild_points_for_range(
                        full_points_by_symbol[target.symbol],
                        period["start"],
                        period["end"],
                    )
                    for target in targets
                }
            else:
                scoped_points = {
                    target.symbol: _last_trading_points(
                        _rebuild_points_for_range(
                            full_points_by_symbol[target.symbol],
                            period["fetch_start"],
                            period["end"],
                        ),
                        int(period["trading_days"]),
                    )
                    for target in targets
                }
            question_start = min(points[0].date.date() for points in scoped_points.values())
            tasks.append(
                {
                    "key": f"{portfolio['key']}__{period['key']}",
                    "portfolio_key": portfolio["key"],
                    "portfolio_label": portfolio["label"],
                    "period_key": period["key"],
                    "period_label": period["label"],
                    "start": question_start,
                    "end": period["end"],
                    "targets": targets,
                    "price_points": scoped_points,
                }
            )
    return tasks


def _validate_robust_strategy_selection(
    buy_strategies: Iterable[str] | None,
    sell_strategies: Iterable[str] | None,
) -> tuple[list[str], list[str]]:
    selected_buy_strategies = list(buy_strategies or STRATEGY_LABELS.keys())
    unknown_buy_strategies = set(selected_buy_strategies) - set(STRATEGY_LABELS)
    if unknown_buy_strategies:
        raise ValueError("未知买入策略: " + ", ".join(sorted(unknown_buy_strategies)))

    selected_sell_strategies = list(sell_strategies or SELL_STRATEGY_LABELS.keys())
    unknown_sell_strategies = set(selected_sell_strategies) - set(SELL_STRATEGY_LABELS)
    if unknown_sell_strategies:
        raise ValueError("未知卖出策略: " + ", ".join(sorted(unknown_sell_strategies)))
    if not selected_sell_strategies:
        raise ValueError("至少需要选择一个卖出策略。")
    return selected_buy_strategies, selected_sell_strategies


def _robust_candidate_pool(
    selected_buy_strategies: Iterable[str],
    selected_sell_strategies: Iterable[str],
    inputs: StrategyInputs,
    core_dip_timing_filter: str,
) -> list[dict[str, object]]:
    from drawdown.strategy_parameter_registry import expand_strategy_candidate_payloads

    return expand_strategy_candidate_payloads(
        selected_buy_strategies,
        selected_sell_strategies,
        inputs,
        core_dip_timing_filter=core_dip_timing_filter,
    )


def _non_repair_candidates(buy_strategies: Iterable[str]) -> list[dict[str, object]]:
    return [
        {
            "key": _candidate_key(
                buy_strategy,
                sell_strategy,
                buy_params,
                dca_rearm_drawdown_pct=rearm,
                sell_allow_same_day_sell=allow_same_day_sell,
            ),
            "label": _candidate_label(
                buy_strategy,
                sell_strategy,
                buy_params,
                dca_rearm_drawdown_pct=rearm,
                sell_allow_same_day_sell=allow_same_day_sell,
            ),
            "buy_strategy": buy_strategy,
            "sell_strategy": sell_strategy,
            **buy_params,
            "sell_min_profit_pct": None,
            "repair_sell_cooldown_days": None,
            "repair_stage_sell_pct": None,
            "grid_rebound_step_pct": None,
            "grid_sell_pct": None,
            "grid_first_sell_pct": None,
            "grid_second_sell_pct": None,
            "grid_min_sell_amount": None,
            "cost_first_profit_pct": None,
            "cost_second_profit_pct": None,
            "cost_third_profit_pct": None,
            "cost_first_sell_pct": None,
            "cost_second_sell_pct": None,
            "cost_third_sell_pct": None,
            "cost_deleverage_cooldown_days": None,
            "sell_allow_same_day_sell": bool(allow_same_day_sell) if sell_strategy != "none" else None,
            "cost_min_sell_amount": None,
            "dca_rearm_drawdown_pct": rearm,
        }
        for buy_strategy in buy_strategies
        for buy_params in _buy_param_variants(buy_strategy)
        for sell_strategy in ROBUST_NON_REPAIR_SELL_STRATEGIES
        for allow_same_day_sell in ((False, True) if sell_strategy != "none" else (None,))
        for rearm in _dca_rearm_variants(buy_strategy, sell_strategy)
    ]


def _repair_candidates(
    buy_strategies: Iterable[str],
    params: Iterable[tuple[float, int, float]],
) -> list[dict[str, object]]:
    return [
        {
            "key": _candidate_key(
                buy_strategy,
                "repair_step",
                buy_params,
                profit,
                cooldown,
                stage,
                dca_rearm_drawdown_pct=rearm,
                sell_allow_same_day_sell=allow_same_day_sell,
            ),
            "label": _candidate_label(
                buy_strategy,
                "repair_step",
                buy_params,
                profit,
                cooldown,
                stage,
                dca_rearm_drawdown_pct=rearm,
                sell_allow_same_day_sell=allow_same_day_sell,
            ),
            "buy_strategy": buy_strategy,
            "sell_strategy": "repair_step",
            **buy_params,
            "sell_min_profit_pct": float(profit),
            "repair_sell_cooldown_days": int(cooldown),
            "repair_stage_sell_pct": float(stage),
            "grid_rebound_step_pct": None,
            "grid_sell_pct": None,
            "grid_first_sell_pct": None,
            "grid_second_sell_pct": None,
            "grid_min_sell_amount": None,
            "cost_first_profit_pct": None,
            "cost_second_profit_pct": None,
            "cost_third_profit_pct": None,
            "cost_first_sell_pct": None,
            "cost_second_sell_pct": None,
            "cost_third_sell_pct": None,
            "cost_deleverage_cooldown_days": None,
            "sell_allow_same_day_sell": bool(allow_same_day_sell),
            "cost_min_sell_amount": None,
            "dca_rearm_drawdown_pct": rearm,
        }
        for buy_strategy in buy_strategies
        if buy_strategy in LOT_SELL_BUY_STRATEGIES
        for buy_params in _buy_param_variants(buy_strategy)
        for profit, cooldown, stage in params
        for allow_same_day_sell in (False, True)
        for rearm in _dca_rearm_variants(buy_strategy, "repair_step")
    ]


def _dca_repair_candidates(
    buy_strategies: Iterable[str],
    inputs: StrategyInputs,
) -> list[dict[str, object]]:
    return [
        {
            "key": _candidate_key(
                buy_strategy,
                "repair_step",
                buy_params,
                inputs.sell_min_profit_pct,
                inputs.repair_sell_cooldown_days,
                inputs.repair_stage_sell_pct,
                dca_rearm_drawdown_pct=rearm,
                sell_allow_same_day_sell=allow_same_day_sell,
            ),
            "label": _candidate_label(
                buy_strategy,
                "repair_step",
                buy_params,
                inputs.sell_min_profit_pct,
                inputs.repair_sell_cooldown_days,
                inputs.repair_stage_sell_pct,
                dca_rearm_drawdown_pct=rearm,
                sell_allow_same_day_sell=allow_same_day_sell,
            ),
            "buy_strategy": buy_strategy,
            "sell_strategy": "repair_step",
            **buy_params,
            "sell_min_profit_pct": float(inputs.sell_min_profit_pct),
            "repair_sell_cooldown_days": int(inputs.repair_sell_cooldown_days),
            "repair_stage_sell_pct": float(inputs.repair_stage_sell_pct),
            "grid_rebound_step_pct": None,
            "grid_sell_pct": None,
            "grid_first_sell_pct": None,
            "grid_second_sell_pct": None,
            "grid_min_sell_amount": None,
            "cost_first_profit_pct": None,
            "cost_second_profit_pct": None,
            "cost_third_profit_pct": None,
            "cost_first_sell_pct": None,
            "cost_second_sell_pct": None,
            "cost_third_sell_pct": None,
            "cost_deleverage_cooldown_days": None,
            "sell_allow_same_day_sell": bool(allow_same_day_sell),
            "cost_min_sell_amount": None,
            "dca_rearm_drawdown_pct": rearm,
        }
        for buy_strategy in buy_strategies
        if buy_strategy in REARM_BUY_STRATEGIES
        for buy_params in _buy_param_variants(buy_strategy)
        for allow_same_day_sell in (False, True)
        for rearm in ROBUST_DCA_REARM_DRAWDOWN_VALUES
    ]


def _grid_rebound_candidates(
    buy_strategies: Iterable[str],
    inputs: StrategyInputs,
) -> list[dict[str, object]]:
    return [
        {
            "key": _candidate_key(
                buy_strategy,
                "grid_rebound",
                buy_params,
                grid_step=step,
                grid_sell=sell_pct,
                grid_min=inputs.grid_min_sell_amount,
                dca_rearm_drawdown_pct=rearm,
                sell_allow_same_day_sell=allow_same_day_sell,
            ),
            "label": _candidate_label(
                buy_strategy,
                "grid_rebound",
                buy_params,
                grid_step=step,
                grid_sell=sell_pct,
                grid_min=inputs.grid_min_sell_amount,
                sell_min_profit_pct=inputs.sell_min_profit_pct,
                dca_rearm_drawdown_pct=rearm,
                sell_allow_same_day_sell=allow_same_day_sell,
            ),
            "buy_strategy": buy_strategy,
            "sell_strategy": "grid_rebound",
            **buy_params,
            "sell_min_profit_pct": float(inputs.sell_min_profit_pct),
            "repair_sell_cooldown_days": None,
            "repair_stage_sell_pct": None,
            "grid_rebound_step_pct": float(step),
            "grid_sell_pct": float(sell_pct),
            "grid_first_sell_pct": None,
            "grid_second_sell_pct": None,
            "grid_min_sell_amount": float(inputs.grid_min_sell_amount),
            "cost_first_profit_pct": None,
            "cost_second_profit_pct": None,
            "cost_third_profit_pct": None,
            "cost_first_sell_pct": None,
            "cost_second_sell_pct": None,
            "cost_third_sell_pct": None,
            "cost_deleverage_cooldown_days": None,
            "sell_allow_same_day_sell": bool(allow_same_day_sell),
            "cost_min_sell_amount": None,
            "dca_rearm_drawdown_pct": rearm,
        }
        for buy_strategy in buy_strategies
        for buy_params in _buy_param_variants(buy_strategy)
        for step in ROBUST_GRID_REBOUND_STEPS
        for sell_pct in ROBUST_GRID_SELLS
        for allow_same_day_sell in (False, True)
        for rearm in _dca_rearm_variants(buy_strategy, "grid_rebound")
    ]


def _cost_deleverage_candidates(
    buy_strategies: Iterable[str],
    inputs: StrategyInputs,
) -> list[dict[str, object]]:
    return [
        {
            "key": _candidate_key(
                buy_strategy,
                "cost_deleverage",
                buy_params,
                cost_profits=profit_set,
                cost_sells=sell_set,
                cost_cooldown=cooldown,
                cost_allow_same_day_sell=allow_same_day_sell,
                cost_min=inputs.cost_min_sell_amount,
                dca_rearm_drawdown_pct=rearm,
                sell_stage_rearm_drawdown_pct=sell_stage_rearm,
            ),
            "label": _candidate_label(
                buy_strategy,
                "cost_deleverage",
                buy_params,
                cost_profits=profit_set,
                cost_sells=sell_set,
                cost_cooldown=cooldown,
                cost_allow_same_day_sell=allow_same_day_sell,
                cost_min=inputs.cost_min_sell_amount,
                dca_rearm_drawdown_pct=rearm,
                sell_stage_rearm_drawdown_pct=sell_stage_rearm,
            ),
            "buy_strategy": buy_strategy,
            "sell_strategy": "cost_deleverage",
            **buy_params,
            "sell_min_profit_pct": None,
            "repair_sell_cooldown_days": None,
            "repair_stage_sell_pct": None,
            "grid_rebound_step_pct": None,
            "grid_sell_pct": None,
            "grid_first_sell_pct": None,
            "grid_second_sell_pct": None,
            "grid_min_sell_amount": None,
            "cost_first_profit_pct": float(profit_set[0]),
            "cost_second_profit_pct": float(profit_set[1]),
            "cost_third_profit_pct": float(profit_set[2]),
            "cost_first_sell_pct": float(sell_set[0]),
            "cost_second_sell_pct": float(sell_set[1]),
            "cost_third_sell_pct": float(sell_set[2]),
            "cost_deleverage_cooldown_days": int(cooldown),
            "sell_allow_same_day_sell": bool(allow_same_day_sell),
            "cost_min_sell_amount": float(inputs.cost_min_sell_amount),
            "dca_rearm_drawdown_pct": rearm,
            "sell_stage_rearm_drawdown_pct": sell_stage_rearm,
        }
        for buy_strategy in buy_strategies
        for buy_params in _buy_param_variants(buy_strategy)
        for profit_set in ROBUST_COST_PROFIT_SETS
        for sell_set in ROBUST_COST_SELL_SETS
        for cooldown in ROBUST_COST_COOLDOWNS
        for allow_same_day_sell in (False, True)
        for rearm in _dca_rearm_variants(buy_strategy, "cost_deleverage")
        for sell_stage_rearm in _sell_stage_rearm_variants(rearm)
    ]


def _buy_param_variants(buy_strategy: str) -> list[dict[str, float | None]]:
    if buy_strategy in {"equal_slice", "linear_weighted_slice"}:
        return [
            {
                "step_pct": step,
                "equal_slice_allocation_pct": allocation if buy_strategy == "equal_slice" else None,
                **_empty_core_dip_params(),
            }
            for step in ROBUST_BUY_STEP_VALUES
            for allocation in (ROBUST_EQUAL_SLICE_ALLOCATION_VALUES if buy_strategy == "equal_slice" else [None])
        ]
    if buy_strategy == "core_dip_dca":
        disabled_variants = [
            {
                "step_pct": None,
                "equal_slice_allocation_pct": None,
                "core_dip_initial_core_pct": initial_core,
                "core_dip_weekly_core_pct": weekly_core,
                "core_dip_cash_reserve_pct": cash_reserve,
                "core_dip_start_drawdown_pct": start_drawdown,
                "core_dip_full_drawdown_pct": full_drawdown,
                "core_dip_timing_enabled": None,
                "core_dip_timing_max_delay_days": None,
                "core_dip_timing_rise_threshold_pct": None,
                "core_dip_timing_near_low_pct": None,
            }
            for initial_core, weekly_core, cash_reserve, start_drawdown, full_drawdown in ROBUST_CORE_DIP_PARAM_SETS
        ]
        enabled_variants = [
            {
                "step_pct": None,
                "equal_slice_allocation_pct": None,
                "core_dip_initial_core_pct": initial_core,
                "core_dip_weekly_core_pct": weekly_core,
                "core_dip_cash_reserve_pct": cash_reserve,
                "core_dip_start_drawdown_pct": start_drawdown,
                "core_dip_full_drawdown_pct": full_drawdown,
                "core_dip_timing_enabled": True,
                "core_dip_timing_max_delay_days": max_delay_days,
                "core_dip_timing_rise_threshold_pct": rise_threshold,
                "core_dip_timing_near_low_pct": near_low,
            }
            for initial_core, weekly_core, cash_reserve, start_drawdown, full_drawdown in ROBUST_CORE_DIP_PARAM_SETS
            for max_delay_days in ROBUST_CORE_DIP_TIMING_MAX_DELAY_DAYS
            for rise_threshold in ROBUST_CORE_DIP_TIMING_RISE_THRESHOLDS
            for near_low in ROBUST_CORE_DIP_TIMING_NEAR_LOW_VALUES
        ]
        return disabled_variants + enabled_variants
    return [{
        "step_pct": None,
        "equal_slice_allocation_pct": None,
        **_empty_core_dip_params(),
    }]


def _empty_core_dip_params() -> dict[str, object | None]:
    return {
        "core_dip_initial_core_pct": None,
        "core_dip_weekly_core_pct": None,
        "core_dip_cash_reserve_pct": None,
        "core_dip_start_drawdown_pct": None,
        "core_dip_full_drawdown_pct": None,
        "core_dip_timing_enabled": None,
        "core_dip_timing_max_delay_days": None,
        "core_dip_timing_rise_threshold_pct": None,
        "core_dip_timing_near_low_pct": None,
    }


def _dca_rearm_variants(buy_strategy: str, sell_strategy: str) -> list[float | None]:
    if buy_strategy in REARM_BUY_STRATEGIES and sell_strategy in POSITION_SELL_REARM_STRATEGIES:
        return list(ROBUST_DCA_REARM_DRAWDOWN_VALUES)
    return [None]


def _sell_stage_rearm_variants(dca_rearm_drawdown_pct: float | None) -> list[float | None]:
    if dca_rearm_drawdown_pct is None:
        return [None]
    return [
        None,
        *[
            float(value)
            for value in ROBUST_SELL_STAGE_REARM_DRAWDOWN_VALUES
            if float(value) > float(dca_rearm_drawdown_pct)
        ],
    ]


def _candidate_key(
    buy_strategy: str,
    sell_strategy: str,
    buy_params: dict[str, float | None],
    profit: float | None = None,
    cooldown: int | None = None,
    stage: float | None = None,
    *,
    dca_rearm_drawdown_pct: float | None = None,
    sell_stage_rearm_drawdown_pct: float | None = None,
    grid_step: float | None = None,
    grid_sell: float | None = None,
    grid_min: float | None = None,
    cost_profits: tuple[float, float, float] | None = None,
    cost_sells: tuple[float, float, float] | None = None,
    cost_cooldown: int | None = None,
    cost_allow_same_day_sell: bool = False,
    cost_min: float | None = None,
    sell_allow_same_day_sell: bool | None = None,
) -> str:
    parts = [buy_strategy]
    if buy_params.get("step_pct") is not None:
        parts.append(f"step{float(buy_params['step_pct']):g}")
    if buy_params.get("equal_slice_allocation_pct") is not None:
        parts.append(f"alloc{float(buy_params['equal_slice_allocation_pct']):g}")
    if buy_params.get("core_dip_initial_core_pct") is not None:
        parts.extend([
            f"ci{float(buy_params['core_dip_initial_core_pct']):g}",
            f"cw{float(buy_params['core_dip_weekly_core_pct'] or 0):g}",
            f"cr{float(buy_params['core_dip_cash_reserve_pct'] or 0):g}",
            f"csd{float(buy_params['core_dip_start_drawdown_pct'] or 0):g}",
            f"cfd{float(buy_params['core_dip_full_drawdown_pct'] or 0):g}",
        ])
        if buy_params.get("core_dip_timing_enabled"):
            parts.extend([
                f"ctd{int(buy_params['core_dip_timing_max_delay_days'] or 0):g}",
                f"ctr{float(buy_params['core_dip_timing_rise_threshold_pct'] or 0):g}",
                f"ctl{float(buy_params['core_dip_timing_near_low_pct'] or 0):g}",
            ])
    parts.append(sell_strategy)
    if sell_strategy == "repair_step":
        parts.extend([f"p{float(profit or 0):g}", f"c{int(cooldown or 0):g}", f"s{float(stage or 0):g}"])
    if sell_strategy == "grid_rebound":
        parts.extend([
            f"g{float(grid_step or 0):g}",
            f"gsell{float(grid_sell or 0):g}",
            f"gmin{float(grid_min or 0):g}",
        ])
    if sell_strategy == "cost_deleverage":
        cost_profits = cost_profits or (0.0, 0.0, 0.0)
        cost_sells = cost_sells or (0.0, 0.0, 0.0)
        parts.extend([
            "cp" + "-".join(f"{float(value):g}" for value in cost_profits),
            "cs" + "-".join(f"{float(value):g}" for value in cost_sells),
            f"cc{int(cost_cooldown or 0):g}",
            f"cmin{float(cost_min or 0):g}",
        ])
    same_day_sell = cost_allow_same_day_sell if sell_allow_same_day_sell is None else sell_allow_same_day_sell
    if sell_strategy != "none" and same_day_sell:
        parts.append("same1")
    if dca_rearm_drawdown_pct is not None:
        parts.append(f"rearm{float(dca_rearm_drawdown_pct):g}")
    if sell_stage_rearm_drawdown_pct is not None:
        parts.append(f"sellrearm{float(sell_stage_rearm_drawdown_pct):g}")
    return "__".join(parts)


def _candidate_label(
    buy_strategy: str,
    sell_strategy: str,
    buy_params: dict[str, float | None],
    profit: float | None = None,
    cooldown: int | None = None,
    stage: float | None = None,
    *,
    dca_rearm_drawdown_pct: float | None = None,
    sell_stage_rearm_drawdown_pct: float | None = None,
    grid_step: float | None = None,
    grid_sell: float | None = None,
    grid_min: float | None = None,
    sell_min_profit_pct: float | None = None,
    cost_profits: tuple[float, float, float] | None = None,
    cost_sells: tuple[float, float, float] | None = None,
    cost_cooldown: int | None = None,
    cost_allow_same_day_sell: bool = False,
    cost_min: float | None = None,
    sell_allow_same_day_sell: bool | None = None,
) -> str:
    buy_label = STRATEGY_LABELS[buy_strategy]
    buy_bits = []
    if buy_params.get("step_pct") is not None:
        buy_bits.append(f"步长 {float(buy_params['step_pct']):g}%")
    if buy_params.get("equal_slice_allocation_pct") is not None:
        buy_bits.append(f"每步 {float(buy_params['equal_slice_allocation_pct']):g}%")
    if buy_params.get("core_dip_initial_core_pct") is not None:
        buy_bits.extend([
            f"初始 {float(buy_params['core_dip_initial_core_pct']):g}%",
            f"周投 {float(buy_params['core_dip_weekly_core_pct'] or 0):g}%",
            f"现金垫 {float(buy_params['core_dip_cash_reserve_pct'] or 0):g}%",
            f"加仓 {float(buy_params['core_dip_start_drawdown_pct'] or 0):g}-{float(buy_params['core_dip_full_drawdown_pct'] or 0):g}%",
        ])
        if buy_params.get("core_dip_timing_enabled"):
            buy_bits.append(
                f"买点优化 延迟{int(buy_params['core_dip_timing_max_delay_days'] or 0):g}日 "
                f"大涨{float(buy_params['core_dip_timing_rise_threshold_pct'] or 0):g}% "
                f"近低{float(buy_params['core_dip_timing_near_low_pct'] or 0):g}%"
            )
        else:
            buy_bits.append("买点优化 关闭")
    if buy_bits:
        buy_label = f"{buy_label} ({' / '.join(buy_bits)})"
    if sell_strategy == "repair_step":
        sell_label = f"阶梯修复 {float(profit or 0):g}%盈利 {int(cooldown or 0):g}日冷却 {float(stage or 0):g}%单档"
    elif sell_strategy == "grid_rebound":
        sell_label = (
            f"网格回弹 {float(grid_step or 0):g}%步长 "
            f"每档{float(grid_sell or 0):g}%卖出"
            + (f" {float(sell_min_profit_pct):g}%最小盈利" if sell_min_profit_pct is not None else "")
        )
    elif sell_strategy == "cost_deleverage":
        cost_profits = cost_profits or (0.0, 0.0, 0.0)
        cost_sells = cost_sells or (0.0, 0.0, 0.0)
        sell_label = (
            "成本去杠杆 "
            + "/".join(f"{float(profit_value):g}%" for profit_value in cost_profits)
            + " 盈利 "
            + "+".join(f"{float(sell_value):g}%" for sell_value in cost_sells)
            + f" 卖出 {int(cost_cooldown or 0):g}日冷却"
        )
    else:
        sell_label = SELL_STRATEGY_LABELS[sell_strategy]
    same_day_sell = cost_allow_same_day_sell if sell_allow_same_day_sell is None else sell_allow_same_day_sell
    if sell_strategy != "none" and same_day_sell:
        sell_label = f"{sell_label} / 买入日可卖"
    if dca_rearm_drawdown_pct is not None:
        sell_label = f"{sell_label} / 卖后重启 {float(dca_rearm_drawdown_pct):g}%回撤"
    if sell_stage_rearm_drawdown_pct is not None:
        sell_label = f"{sell_label} / 卖档重启 {float(sell_stage_rearm_drawdown_pct):g}%回撤"
    return f"{buy_label} / {sell_label}"


def _dedupe_candidates(candidates: Iterable[dict[str, object]]) -> list[dict[str, object]]:
    result: dict[str, dict[str, object]] = {}
    for candidate in candidates:
        result[str(candidate["key"])] = candidate
    return list(result.values())


def _filter_core_dip_timing_candidates(
    candidates: list[dict[str, object]],
    timing_filter: str,
) -> list[dict[str, object]]:
    if timing_filter == "all":
        return candidates
    enabled = timing_filter == "enabled"
    return [
        candidate for candidate in candidates
        if candidate.get("buy_strategy") != "core_dip_dca"
        or bool(candidate.get("core_dip_timing_enabled")) is enabled
    ]


def _filter_candidates_by_sell_strategy(
    candidates: Iterable[dict[str, object]],
    sell_strategies: Iterable[str],
) -> list[dict[str, object]]:
    selected = set(sell_strategies)
    return [
        candidate for candidate in candidates
        if str(candidate.get("sell_strategy")) in selected
    ]


def _score_robust_candidates(
    tasks: list[dict[str, object]],
    inputs: StrategyInputs,
    candidates: list[dict[str, object]],
    *,
    return_weight: float,
    drawdown_weight: float,
    control_checker: Callable[[], None] | None = None,
) -> list[dict[str, object]]:
    observations_by_key: dict[str, list[dict[str, object]]] = {
        str(candidate["key"]): []
        for candidate in candidates
    }
    for task in tasks:
        _check_robust_control(control_checker)
        observations = []
        for candidate in candidates:
            _check_robust_control(control_checker)
            candidate_inputs = inputs
            if candidate.get("step_pct") is not None or candidate.get("equal_slice_allocation_pct") is not None:
                candidate_inputs = replace(
                    candidate_inputs,
                    step_pct=float(candidate.get("step_pct") or candidate_inputs.step_pct),
                    equal_slice_allocation_pct=float(
                        candidate.get("equal_slice_allocation_pct")
                        or candidate_inputs.equal_slice_allocation_pct
                    ),
                )
            if candidate.get("core_dip_initial_core_pct") is not None:
                candidate_inputs = replace(
                    candidate_inputs,
                    core_dip_initial_core_pct=float(candidate["core_dip_initial_core_pct"]),
                    core_dip_weekly_core_pct=float(candidate["core_dip_weekly_core_pct"]),
                    core_dip_cash_reserve_pct=float(candidate["core_dip_cash_reserve_pct"]),
                    core_dip_start_drawdown_pct=float(candidate["core_dip_start_drawdown_pct"]),
                    core_dip_full_drawdown_pct=float(candidate["core_dip_full_drawdown_pct"]),
                    core_dip_timing_enabled=bool(candidate.get("core_dip_timing_enabled")),
                    core_dip_timing_max_delay_days=int(candidate.get("core_dip_timing_max_delay_days") or 0),
                    core_dip_timing_rise_threshold_pct=float(candidate.get("core_dip_timing_rise_threshold_pct") or 0),
                    core_dip_timing_near_low_pct=float(candidate.get("core_dip_timing_near_low_pct") or 0),
                )
            if candidate.get("sell_strategy") == "repair_step":
                candidate_inputs = replace(
                    candidate_inputs,
                    sell_min_profit_pct=float(candidate["sell_min_profit_pct"]),
                    repair_sell_cooldown_days=int(candidate["repair_sell_cooldown_days"]),
                    repair_stage_sell_pct=float(candidate["repair_stage_sell_pct"]),
                    sell_allow_same_day_sell=bool(candidate.get("sell_allow_same_day_sell")),
                )
            if candidate.get("sell_strategy") == "grid_rebound":
                candidate_inputs = replace(
                    candidate_inputs,
                    grid_rebound_step_pct=float(candidate.get("grid_rebound_step_pct") or inputs.grid_rebound_step_pct),
                    grid_sell_pct=float(
                        candidate.get("grid_sell_pct")
                        or candidate.get("grid_second_sell_pct")
                        or inputs.grid_sell_pct
                        or inputs.grid_second_sell_pct
                    ),
                    grid_min_sell_amount=float(candidate.get("grid_min_sell_amount") or inputs.grid_min_sell_amount),
                    sell_allow_same_day_sell=bool(candidate.get("sell_allow_same_day_sell")),
                )
            if candidate.get("sell_strategy") == "cost_deleverage":
                candidate_inputs = replace(
                    candidate_inputs,
                    cost_first_profit_pct=float(candidate.get("cost_first_profit_pct") or inputs.cost_first_profit_pct),
                    cost_second_profit_pct=float(candidate.get("cost_second_profit_pct") or inputs.cost_second_profit_pct),
                    cost_third_profit_pct=float(candidate.get("cost_third_profit_pct") or inputs.cost_third_profit_pct),
                    cost_first_sell_pct=float(candidate.get("cost_first_sell_pct") or inputs.cost_first_sell_pct),
                    cost_second_sell_pct=float(candidate.get("cost_second_sell_pct") or inputs.cost_second_sell_pct),
                    cost_third_sell_pct=float(candidate.get("cost_third_sell_pct") or inputs.cost_third_sell_pct),
                    cost_deleverage_cooldown_days=int(
                        candidate.get("cost_deleverage_cooldown_days")
                        if candidate.get("cost_deleverage_cooldown_days") is not None
                        else inputs.cost_deleverage_cooldown_days
                    ),
                    sell_allow_same_day_sell=bool(
                        candidate.get("sell_allow_same_day_sell")
                    ),
                    cost_min_sell_amount=float(candidate.get("cost_min_sell_amount") or inputs.cost_min_sell_amount),
                )
            if candidate.get("dca_rearm_drawdown_pct") is not None:
                candidate_inputs = replace(
                    candidate_inputs,
                    dca_rearm_drawdown_pct=float(candidate["dca_rearm_drawdown_pct"]),
                )
            if candidate.get("buy_rearm_mode") is not None:
                candidate_inputs = replace(
                    candidate_inputs,
                    buy_rearm_mode=str(candidate["buy_rearm_mode"]),
                )
            if "sell_stage_rearm_drawdown_pct" in candidate:
                sell_stage_rearm = candidate["sell_stage_rearm_drawdown_pct"]
                candidate_inputs = replace(
                    candidate_inputs,
                    sell_stage_rearm_drawdown_pct=(
                        None if sell_stage_rearm is None else float(sell_stage_rearm)
                    ),
                )
            result = simulate_portfolio(
                task["price_points"],
                task["targets"],
                candidate_inputs,
                strategies=[str(candidate["buy_strategy"])],
                sell_strategies=[str(candidate["sell_strategy"])],
            )
            strategy = result["strategies"][0]
            metrics = strategy["metrics"]
            observations.append(
                {
                    "candidate": candidate,
                    "task": task,
                    "return_pct": float(metrics.get("return_pct", 0.0)),
                    "max_drawdown_pct": float(metrics.get("max_drawdown_pct", 0.0)),
                    "sell_quality_score": float(metrics.get("sell_quality_score", 0.0)),
                    "trade_count": int(metrics.get("trade_count", 0)),
                }
            )
        _score_robust_task_observations(
            observations,
            return_weight=return_weight,
            drawdown_weight=drawdown_weight,
        )
        for observation in observations:
            observations_by_key[str(observation["candidate"]["key"])].append(observation)
    _check_robust_control(control_checker)
    rows = [
        _aggregate_robust_candidate(candidate, observations_by_key[str(candidate["key"])])
        for candidate in candidates
    ]
    _score_robust_summary_like_scorecard(rows, return_weight, drawdown_weight)
    return rows


def _check_robust_control(control_checker: Callable[[], None] | None) -> None:
    if control_checker is None:
        return
    while True:
        control_checker()
        try:
            is_paused = bool(getattr(control_checker, "is_paused")())
        except AttributeError:
            return
        if not is_paused:
            return
        time.sleep(0.25)


def _score_robust_task_observations(
    observations: list[dict[str, object]],
    *,
    return_weight: float,
    drawdown_weight: float,
) -> None:
    returns = [float(item["return_pct"]) for item in observations]
    inverse_drawdowns = [-float(item["max_drawdown_pct"]) for item in observations]
    for item in observations:
        return_score = _normalize_bigger_better(float(item["return_pct"]), returns) * 100.0
        drawdown_score = _normalize_bigger_better(-float(item["max_drawdown_pct"]), inverse_drawdowns) * 100.0
        item["task_score"] = return_score * return_weight + drawdown_score * drawdown_weight
    ranked = sorted(observations, key=lambda item: item["task_score"], reverse=True)
    for index, item in enumerate(ranked, start=1):
        item["task_rank"] = index


def _aggregate_robust_candidate(
    candidate: dict[str, object],
    observations: list[dict[str, object]],
) -> dict[str, object]:
    scores = [float(item["task_score"]) for item in observations]
    returns = [float(item["return_pct"]) for item in observations]
    drawdowns = [float(item["max_drawdown_pct"]) for item in observations]
    sell_qualities = [float(item["sell_quality_score"]) for item in observations]
    mean_score = _avg_float(scores)
    score = mean_score
    sorted_observations = sorted(observations, key=lambda item: item["task_score"])
    weakest = sorted_observations[0] if sorted_observations else None
    strongest = sorted_observations[-1] if sorted_observations else None
    return {
        "candidate": candidate,
        "score": score,
        "robust_score": score,
        "mean_score": mean_score,
        "avg_return_pct": _avg_float(returns),
        "avg_drawdown_pct": _avg_float(drawdowns),
        "avg_sell_quality_score": _avg_float(sell_qualities),
        "task_count": len(observations),
        "strongest_task": _robust_task_summary(strongest),
        "weakest_task": _robust_task_summary(weakest),
    }


def _score_robust_summary_like_scorecard(
    rows: list[dict[str, object]],
    return_weight: float,
    drawdown_weight: float,
) -> None:
    returns = [float(row["avg_return_pct"]) for row in rows]
    drawdowns = [float(row["avg_drawdown_pct"]) for row in rows]
    for row in rows:
        return_score = _normalize_bigger_better(float(row["avg_return_pct"]), returns) * 100.0
        drawdown_score = _normalize_bigger_better(float(row["avg_drawdown_pct"]), drawdowns) * 100.0
        score = return_score * return_weight + drawdown_score * drawdown_weight
        row["return_score"] = return_score
        row["drawdown_score"] = drawdown_score
        row["mean_score"] = score
        row["score"] = score
        row["robust_score"] = score


def _robust_task_summary(observation: dict[str, object] | None) -> dict[str, object] | None:
    if not observation:
        return None
    task = observation["task"]
    return {
        "key": task["key"],
        "label": f"{task['portfolio_label']} / {task['period_label']}",
        "score": float(observation["task_score"]),
        "return_pct": float(observation["return_pct"]),
        "max_drawdown_pct": float(observation["max_drawdown_pct"]),
    }


def _percentile(values: list[float], percentile: float) -> float:
    if not values:
        return 0.0
    sorted_values = sorted(values)
    index = (len(sorted_values) - 1) * min(1.0, max(0.0, percentile))
    lower = math.floor(index)
    upper = math.ceil(index)
    if lower == upper:
        return sorted_values[int(index)]
    ratio = index - lower
    return sorted_values[lower] * (1 - ratio) + sorted_values[upper] * ratio


def _simulate_strategy(
    price_points_by_symbol: dict[str, list[PricePoint]],
    targets: list[PortfolioTarget],
    inputs: StrategyInputs,
    strategy: str,
    sell_strategy: str,
) -> dict[str, object]:
    if sell_strategy not in SELL_STRATEGY_LABELS:
        raise ValueError(f"未知卖出策略: {sell_strategy}")

    default_tranches = build_strategy_tranches(inputs, strategy)
    tranches_by_symbol = {
        target.symbol: build_strategy_tranches(_inputs_for_target(inputs, target), strategy)
        for target in targets
    } if strategy not in {"weekly_dca", "salary_flow_dca", "core_dip_dca"} else {}
    states = {
        target.symbol: SymbolState(
            symbol=target.symbol,
            name=target.name or target.symbol,
            weight=target.weight,
            budget=inputs.initial_cash * target.weight / 100.0,
            cash=inputs.initial_cash * target.weight / 100.0,
            lots=[],
            sell_marks=set(),
            dca_pending_cash=inputs.initial_cash * target.weight / 100.0 if strategy == "weekly_dca" else 0.0,
        )
        for target in targets
    }
    executed = {target.symbol: {} for target in targets}
    point_by_day = {
        symbol: {point.date.date(): point for point in points}
        for symbol, points in price_points_by_symbol.items()
    }
    trading_index_by_symbol = {
        symbol: {point.date.date(): index for index, point in enumerate(points)}
        for symbol, points in price_points_by_symbol.items()
    }
    dca_days = {
        symbol: _weekly_dca_days(points)
        for symbol, points in price_points_by_symbol.items()
    } if strategy in {"salary_flow_dca", "core_dip_dca"} else {}
    all_days = sorted(
        {day for points in point_by_day.values() for day in points}
    )
    contribution_days = _monthly_contribution_days(all_days)
    contribution_count = 0
    total_monthly_contributions = 0.0
    portfolio_values: list[float] = []
    cash_values: list[float] = []
    invested_values: list[float] = []
    contribution_values: list[float] = []
    trade_log: list[dict[str, object]] = []

    for current_day in all_days:
        if inputs.monthly_contribution > 0 and current_day in contribution_days:
            contribution_count += 1
            for target in targets:
                contribution = inputs.monthly_contribution * target.weight / 100.0
                if contribution <= 0:
                    continue
                state = states[target.symbol]
                state.cash += contribution
                state.budget += contribution
                if strategy == "weekly_dca":
                    state.dca_pending_cash += contribution
                total_monthly_contributions += contribution

        for symbol, day_points in point_by_day.items():
            point = day_points.get(current_day)
            if point is None:
                continue
            state = states[symbol]
            state.last_price = point.close
            state.last_value = _position_value_usd(symbol, state.shares, point.close, inputs)
            trade_index = trading_index_by_symbol[symbol][current_day]
            if strategy == "weekly_dca":
                bought_today = _execute_weekly_dca(
                    state,
                    point,
                    inputs,
                    trade_log,
                    sell_strategy,
                )
            elif strategy == "salary_flow_dca":
                bought_today = _execute_salary_flow_dca(
                    state,
                    point,
                    dca_days.get(symbol, set()),
                    inputs,
                    trade_log,
                    sell_strategy,
                )
            elif strategy == "core_dip_dca":
                bought_today = _execute_core_dip_dca(
                    state,
                    point,
                    dca_days.get(symbol, set()),
                    price_points_by_symbol[symbol][max(0, trade_index - 4):trade_index + 1],
                    inputs,
                    trade_log,
                    sell_strategy,
                )
            else:
                _rearm_buy_tranches_after_repair(state, point, executed[symbol], inputs)
                _rearm_buy_tranches_after_position_sell(state, point, executed[symbol], inputs)
                bought_today = _execute_crossed_tranches(
                    state,
                    point,
                    tranches_by_symbol.get(symbol, default_tranches),
                    executed[symbol],
                    inputs,
                    trade_log,
                    strategy,
                    sell_strategy,
                )
            if not bought_today or should_check_sell_after_buy(
                sell_strategy=sell_strategy,
                bought_today=bought_today,
                inputs=inputs,
            ):
                _execute_sell_strategy(state, point, inputs, strategy, sell_strategy, trade_log, trade_index)

        total_value = sum(state.cash + state.last_value for state in states.values())
        total_cash = sum(state.cash for state in states.values())
        total_invested = sum(state.invested for state in states.values())
        portfolio_values.append(total_value)
        cash_values.append(total_cash)
        invested_values.append(total_invested)
        contribution_values.append(inputs.initial_cash + total_monthly_contributions)

    if not portfolio_values:
        raise ValueError("所选时间范围内没有可用价格数据。")

    final_value = portfolio_values[-1]
    final_cash = sum(state.cash for state in states.values())
    final_market_value = sum(state.last_value for state in states.values())
    total_contributed = inputs.initial_cash + total_monthly_contributions
    profit = final_value - total_contributed
    total_fees = sum(state.fees for state in states.values())
    total_invested = sum(state.invested for state in states.values())
    trades = sum(state.trades for state in states.values())
    buy_trades = sum(state.buy_trades for state in states.values())
    sell_trades = sum(state.sell_trades for state in states.values())
    sell_observation = _sell_observation_metrics(trade_log, portfolio_values, cash_values)

    return {
        "key": f"{strategy}__{sell_strategy}",
        "buy_strategy": strategy,
        "sell_strategy": sell_strategy,
        "label": f"{STRATEGY_LABELS[strategy]} / {SELL_STRATEGY_LABELS[sell_strategy]}",
        "metrics": {
            "final_value": final_value,
            "total_contributed": total_contributed,
            "profit": profit,
            "monthly_contribution": inputs.monthly_contribution,
            "contribution_count": contribution_count,
            "contributed_cash": total_monthly_contributions,
            "return_pct": _pct(final_value / total_contributed - 1.0) if total_contributed > 0 else 0.0,
            "max_drawdown_pct": _max_drawdown_pct(portfolio_values),
            "cash_remaining": final_cash,
            "cash_usage_pct": _pct(final_market_value / final_value) if final_value > 0 else 0.0,
            "total_invested": total_invested,
            "trade_count": trades,
            "buy_trade_count": buy_trades,
            "sell_trade_count": sell_trades,
            "total_fees": total_fees,
            "fee_ratio_pct": _pct(total_fees / total_invested) if total_invested > 0 else 0.0,
            **sell_observation,
        },
        "series": {
            "dates": [day.isoformat() for day in all_days],
            "portfolio_values": portfolio_values,
            "cash_values": cash_values,
            "invested_values": invested_values,
            "contribution_values": contribution_values,
        },
        "symbols": [_symbol_summary(state, inputs) for state in states.values()],
        "trades": trade_log,
        "tranches": [
            {
                "threshold_pct": tranche.threshold_pct,
                "allocation_pct": tranche.allocation_pct,
                "label": tranche.label,
            }
            for tranche in default_tranches
        ],
        "tranches_by_symbol": {
            symbol: [
                {
                    "threshold_pct": tranche.threshold_pct,
                    "allocation_pct": tranche.allocation_pct,
                    "label": tranche.label,
                }
                for tranche in tranches
            ]
            for symbol, tranches in tranches_by_symbol.items()
        },
    }


def _monthly_contribution_days(all_days: list[date]) -> set[date]:
    first_by_month: dict[tuple[int, int], date] = {}
    for day in all_days:
        first_by_month.setdefault((day.year, day.month), day)
    month_starts = sorted(first_by_month.values())
    if len(month_starts) <= 1:
        return set()
    return set(month_starts[1:])


def _weekly_dca_days(points: list[PricePoint]) -> set[date]:
    first_by_week: dict[tuple[int, int], date] = {}
    for point in points:
        day = point.date.date()
        iso_year, iso_week, _ = day.isocalendar()
        first_by_week.setdefault((iso_year, iso_week), day)
    return set(first_by_week.values())


def _rebuild_points_for_range(
    points: list[PricePoint],
    start_date: date,
    end_date: date,
) -> list[PricePoint]:
    series = [
        (point.date, point.close)
        for point in points
        if start_date <= point.date.date() <= end_date
    ]
    if not series:
        raise ValueError("所选时间范围内没有可用价格数据。")
    return build_price_points_from_series(series)


def _last_trading_points(points: list[PricePoint], trading_days: int) -> list[PricePoint]:
    if trading_days <= 0:
        raise ValueError("交易日数量必须大于 0。")
    scoped = points[-trading_days:]
    if not scoped:
        raise ValueError("所选时间范围内没有可用价格数据。")
    return build_price_points_from_series((point.date, point.close) for point in scoped)


def _normalize_score_weights(return_weight: float, drawdown_weight: float) -> tuple[float, float]:
    return SCORECARD_RETURN_WEIGHT, SCORECARD_DRAWDOWN_WEIGHT


def _score_summary_rows(
    rows: list[dict[str, object]],
    return_weight: float,
    drawdown_weight: float,
) -> None:
    return_weight, drawdown_weight = _normalize_score_weights(return_weight, drawdown_weight)
    returns = [float(row["avg_return_pct"]) for row in rows]
    drawdowns = [float(row["avg_drawdown_pct"]) for row in rows]
    for row in rows:
        return_score = _normalize_bigger_better(float(row["avg_return_pct"]), returns)
        drawdown_score = _normalize_bigger_better(float(row["avg_drawdown_pct"]), drawdowns)
        row["return_score"] = return_score * 100.0
        row["drawdown_score"] = drawdown_score * 100.0
        row["score"] = (return_score * return_weight + drawdown_score * drawdown_weight) * 100.0


def _score_question_strategies(
    strategies: list[dict[str, object]],
    return_weight: float = SCORECARD_RETURN_WEIGHT,
    drawdown_weight: float = SCORECARD_DRAWDOWN_WEIGHT,
) -> list[dict[str, object]]:
    return_weight, drawdown_weight = _normalize_score_weights(return_weight, drawdown_weight)
    returns = [float(strategy["metrics"]["return_pct"]) for strategy in strategies]
    drawdowns = [float(strategy["metrics"]["max_drawdown_pct"]) for strategy in strategies]
    scored: list[dict[str, object]] = []
    for strategy in strategies:
        metrics = strategy["metrics"]
        return_pct = float(metrics["return_pct"])
        drawdown_pct = float(metrics["max_drawdown_pct"])
        return_score = _normalize_bigger_better(return_pct, returns)
        drawdown_score = _normalize_bigger_better(drawdown_pct, drawdowns)
        score = (return_score * return_weight + drawdown_score * drawdown_weight) * 100.0
        scored.append(
            {
                "key": strategy["key"],
                "label": strategy["label"],
                "buy_strategy": strategy["buy_strategy"],
                "sell_strategy": strategy["sell_strategy"],
                "score": score,
                "return_score": return_score * 100.0,
                "drawdown_score": drawdown_score * 100.0,
                "return_pct": return_pct,
                "max_drawdown_pct": drawdown_pct,
                "final_value": float(metrics["final_value"]),
                "trade_count": int(metrics["trade_count"]),
                "buy_trade_count": int(metrics["buy_trade_count"]),
                "sell_trade_count": int(metrics["sell_trade_count"]),
                "avg_buy_drawdown_pct": float(metrics.get("avg_buy_drawdown_pct", 0.0)),
                "avg_sell_drawdown_pct": float(metrics.get("avg_sell_drawdown_pct", 0.0)),
                "avg_sell_profit_pct": float(metrics.get("avg_sell_profit_pct", 0.0)),
                "sell_profit": float(metrics.get("sell_profit", 0.0)),
                "cash_reuse_pct": float(metrics.get("cash_reuse_pct", 0.0)),
                "avg_cash_pct": float(metrics.get("avg_cash_pct", 0.0)),
                "sell_quality_score": float(metrics.get("sell_quality_score", 0.0)),
            }
        )
    scored.sort(key=lambda item: item["score"], reverse=True)
    for index, item in enumerate(scored, start=1):
        item["rank"] = index
    return scored


def _resolve_scorecard_periods(
    default_end_date: date,
    raw_periods: Iterable[dict[str, object]] | None = None,
) -> list[dict[str, object]]:
    has_custom_periods = raw_periods is not None
    raw_by_key = {
        str(period.get("key", "")): period
        for period in (raw_periods or [])
        if isinstance(period, dict)
    }
    resolved: list[dict[str, object]] = []
    for default_period in SCORECARD_PERIODS:
        key = str(default_period["key"])
        raw = raw_by_key.get(key)
        if has_custom_periods:
            if raw is None:
                continue
            if not _scorecard_period_enabled(raw):
                continue
        raw = raw or {}
        label = str(raw.get("label") or default_period["label"])
        start_raw = raw.get("start") or raw.get("start_date")
        end_raw = raw.get("end") or raw.get("end_date")
        if start_raw or end_raw:
            end = date.fromisoformat(str(end_raw)) if end_raw else default_end_date
            start = date.fromisoformat(str(start_raw)) if start_raw else end - timedelta(days=int(default_period["fetch_days"]))
            if start > end:
                raise ValueError(f"{label} 的开始日期不能晚于结束日期。")
            resolved.append(
                {
                    "key": key,
                    "label": label,
                    "mode": "exact",
                    "start": start,
                    "end": end,
                    "fetch_start": start,
                    "trading_days": None,
                }
            )
        else:
            end = default_end_date
            fetch_start = end - timedelta(days=int(default_period["fetch_days"]))
            resolved.append(
                {
                    "key": key,
                    "label": label,
                    "mode": "trading_days",
                    "start": None,
                    "end": end,
                    "fetch_start": fetch_start,
                    "trading_days": int(default_period["trading_days"]),
                }
            )
    if not resolved:
        raise ValueError("至少需要选择一个评分时间阶段。")
    return resolved


def _scorecard_period_enabled(raw: dict[str, object]) -> bool:
    value = raw.get("enabled", True)
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() not in {"0", "false", "no", "off"}
    return bool(value)


def _scan_float_values(values: Iterable[object], label: str, maximum: float | None = None) -> list[float]:
    parsed: list[float] = []
    for raw in values:
        try:
            value = float(raw)
        except (TypeError, ValueError):
            raise ValueError(f"{label}包含无效数值。") from None
        if not math.isfinite(value) or value < 0:
            raise ValueError(f"{label}必须是非负数。")
        if maximum is not None and value > maximum:
            raise ValueError(f"{label}必须在 0 到 {maximum:g} 之间。")
        parsed.append(round(value, 4))
    result = sorted(set(parsed))
    if not result:
        raise ValueError(f"{label}至少需要一个扫描值。")
    return result


def _scan_int_values(values: Iterable[object], label: str) -> list[int]:
    parsed: list[int] = []
    for raw in values:
        try:
            value = int(float(raw))
        except (TypeError, ValueError):
            raise ValueError(f"{label}包含无效数值。") from None
        if value < 0:
            raise ValueError(f"{label}必须是非负整数。")
        parsed.append(value)
    result = sorted(set(parsed))
    if not result:
        raise ValueError(f"{label}至少需要一个扫描值。")
    return result


def _scan_cell_sort_key(cell: dict[str, object]) -> tuple[float, float, float, float]:
    return (
        float(cell.get("sell_quality_score", 0.0)),
        float(cell.get("return_pct", 0.0)),
        float(cell.get("cash_reuse_pct", 0.0)),
        float(cell.get("max_drawdown_pct", 0.0)),
    )


def _normalize_bigger_better(value: float, values: list[float]) -> float:
    minimum = min(values)
    maximum = max(values)
    if math.isclose(minimum, maximum):
        return 1.0
    return (value - minimum) / (maximum - minimum)


def _sell_observation_metrics(
    trade_log: list[dict[str, object]],
    portfolio_values: list[float],
    cash_values: list[float],
) -> dict[str, float]:
    buy_trades = [trade for trade in trade_log if trade.get("action") == "buy"]
    sell_trades = [trade for trade in trade_log if trade.get("action") == "sell"]
    avg_buy_drawdown = _avg_float([trade.get("drawdown_pct", 0.0) for trade in buy_trades])
    avg_sell_drawdown = _avg_float([trade.get("drawdown_pct", 0.0) for trade in sell_trades])
    avg_sell_profit = _avg_float([trade.get("estimated_profit_pct", 0.0) for trade in sell_trades])
    total_sell_profit = sum(float(trade.get("estimated_profit", 0.0) or 0.0) for trade in sell_trades)
    total_sell_cash = sum(float(trade.get("net_amount", 0.0) or 0.0) for trade in sell_trades)
    reused_cash = _cash_reuse_from_sells(trade_log)
    cash_reuse_pct = _pct(reused_cash / total_sell_cash) if total_sell_cash > 0 else 0.0
    avg_cash_pct = _avg_float(
        [
            _pct(cash / total_value)
            for cash, total_value in zip(cash_values, portfolio_values)
            if total_value > 0
        ]
    )
    if not sell_trades:
        sell_quality_score = 0.0
    else:
        profit_component = _clamp(avg_sell_profit / 35.0, 0.0, 1.0)
        position_component = _clamp((30.0 - avg_sell_drawdown) / 30.0, 0.0, 1.0)
        reuse_component = _clamp(cash_reuse_pct / 100.0, 0.0, 1.0)
        idle_component = _clamp((65.0 - avg_cash_pct) / 65.0, 0.0, 1.0)
        sell_quality_score = (
            profit_component * 0.35
            + position_component * 0.30
            + reuse_component * 0.20
            + idle_component * 0.15
        ) * 100.0

    return {
        "avg_buy_drawdown_pct": avg_buy_drawdown,
        "avg_sell_drawdown_pct": avg_sell_drawdown,
        "avg_sell_profit_pct": avg_sell_profit,
        "sell_profit": total_sell_profit,
        "cash_reuse_pct": cash_reuse_pct,
        "avg_cash_pct": avg_cash_pct,
        "sell_quality_score": sell_quality_score,
    }


def _cash_reuse_from_sells(trade_log: list[dict[str, object]]) -> float:
    sell_cash_pool = 0.0
    reused_cash = 0.0
    for trade in sorted(trade_log, key=lambda item: str(item.get("date", ""))):
        if trade.get("action") == "sell":
            sell_cash_pool += float(trade.get("net_amount", 0.0) or 0.0)
        elif trade.get("action") == "buy" and sell_cash_pool > 0:
            gross_amount = float(trade.get("gross_amount", 0.0) or 0.0)
            reused = min(sell_cash_pool, gross_amount)
            reused_cash += reused
            sell_cash_pool -= reused
    return reused_cash


def _avg_float(values: Iterable[object]) -> float:
    parsed = [float(value or 0.0) for value in values]
    return sum(parsed) / len(parsed) if parsed else 0.0


def _clamp(value: float, minimum: float, maximum: float) -> float:
    return min(maximum, max(minimum, value))


def _execute_weekly_dca(
    state: SymbolState,
    point: PricePoint,
    inputs: StrategyInputs,
    trade_log: list[dict[str, object]],
    sell_strategy: str,
) -> bool:
    if state.dca_pending_cash <= 0:
        return False
    gross_amount = min(state.dca_pending_cash, state.cash)
    if gross_amount <= 0:
        return False
    fee = min(inputs.trade_fee, gross_amount)
    net_amount = gross_amount - fee
    price_usd = _price_usd(state.symbol, point.close, inputs)
    shares = net_amount / price_usd if net_amount > 0 and price_usd > 0 else 0.0
    drawdown_pct = _point_drawdown_pct(point, inputs)

    state.cash -= gross_amount
    state.dca_pending_cash = max(0.0, state.dca_pending_cash - gross_amount)
    state.shares += shares
    state.invested += gross_amount
    state.fees += fee
    state.trades += 1
    state.buy_trades += 1
    state.max_shares = max(state.max_shares, state.shares)
    state.last_value = _position_value_usd(state.symbol, state.shares, point.close, inputs)
    if state.lots is None:
        state.lots = []
    state.lots.append(
        PositionLot(
            threshold_pct=0.0,
            buy_drawdown_pct=drawdown_pct,
            buy_price_usd=price_usd,
            initial_shares=shares,
            remaining_shares=shares,
        )
    )
    sell_cycle_rearmed = _rearm_position_sell_cycle_after_dca_buy(state, drawdown_pct, inputs, sell_strategy)
    trade_log.append(
        {
            "action": "buy",
            "date": point.date.date().isoformat(),
            "symbol": state.symbol,
            "buy_strategy": "weekly_dca",
            "sell_strategy": sell_strategy,
            "threshold_pct": 0.0,
            "drawdown_pct": drawdown_pct,
            "price": point.close,
            "price_usd": price_usd,
            "display_price": point.close,
            "display_price_usd": price_usd,
            "gross_amount": gross_amount,
            "fee": fee,
            "net_amount": net_amount,
            "shares": shares,
            "allocation_pct": 0.0,
            "sell_cycle_rearmed": sell_cycle_rearmed,
        }
    )
    return True


def _execute_salary_flow_dca(
    state: SymbolState,
    point: PricePoint,
    dca_days: set[date],
    inputs: StrategyInputs,
    trade_log: list[dict[str, object]],
    sell_strategy: str,
) -> bool:
    if point.date.date() not in dca_days or inputs.monthly_contribution <= 0:
        return False
    drawdown_pct = _point_drawdown_pct(point, inputs)
    multiplier = _salary_flow_dca_multiplier(drawdown_pct)
    monthly_amount = inputs.monthly_contribution * state.weight / 100.0
    base_scheduled_amount = monthly_amount / 4.0 * multiplier
    reserve_cash = state.budget * _salary_flow_cash_reserve_ratio(drawdown_pct)
    available_cash = max(0.0, state.cash - reserve_cash)
    extra_cash = max(0.0, available_cash - base_scheduled_amount)
    scheduled_amount = base_scheduled_amount + extra_cash * _salary_flow_idle_cash_sweep_ratio(drawdown_pct)
    gross_amount = min(scheduled_amount, available_cash)
    if gross_amount <= 0:
        return False

    fee = min(inputs.trade_fee, gross_amount)
    net_amount = gross_amount - fee
    price_usd = _price_usd(state.symbol, point.close, inputs)
    shares = net_amount / price_usd if net_amount > 0 and price_usd > 0 else 0.0

    state.cash -= gross_amount
    state.shares += shares
    state.invested += gross_amount
    state.fees += fee
    state.trades += 1
    state.buy_trades += 1
    state.max_shares = max(state.max_shares, state.shares)
    state.last_value = _position_value_usd(state.symbol, state.shares, point.close, inputs)
    if state.lots is None:
        state.lots = []
    state.lots.append(
        PositionLot(
            threshold_pct=0.0,
            buy_drawdown_pct=drawdown_pct,
            buy_price_usd=price_usd,
            initial_shares=shares,
            remaining_shares=shares,
        )
    )
    sell_cycle_rearmed = _rearm_position_sell_cycle_after_dca_buy(state, drawdown_pct, inputs, sell_strategy)
    trade_log.append(
        {
            "action": "buy",
            "date": point.date.date().isoformat(),
            "symbol": state.symbol,
            "buy_strategy": "salary_flow_dca",
            "sell_strategy": sell_strategy,
            "threshold_pct": 0.0,
            "drawdown_pct": drawdown_pct,
            "price": point.close,
            "price_usd": price_usd,
            "display_price": point.close,
            "display_price_usd": price_usd,
            "gross_amount": gross_amount,
            "fee": fee,
            "net_amount": net_amount,
            "shares": shares,
            "allocation_pct": 0.0,
            "scheduled_amount": scheduled_amount,
            "base_scheduled_amount": base_scheduled_amount,
            "cash_reserve": reserve_cash,
            "idle_cash_sweep": max(0.0, gross_amount - base_scheduled_amount),
            "drawdown_boost": multiplier,
            "sell_cycle_rearmed": sell_cycle_rearmed,
        }
    )
    return True


def _execute_core_dip_dca(
    state: SymbolState,
    point: PricePoint,
    dca_days: set[date],
    recent_points: list[PricePoint],
    inputs: StrategyInputs,
    trade_log: list[dict[str, object]],
    sell_strategy: str,
) -> bool:
    is_dca_day = point.date.date() in dca_days
    if not is_dca_day and state.core_dip_pending_cash <= 0:
        return False
    monthly_amount = inputs.monthly_contribution * state.weight / 100.0
    scheduled_amount = monthly_amount / 4.0
    drawdown_pct = _point_drawdown_pct(point, inputs)
    boost_ratio = _core_dip_boost_ratio(drawdown_pct, inputs)
    core_ratio = _clamp(inputs.core_dip_weekly_core_pct / 100.0, 0.0, 1.0)
    new_core_amount = scheduled_amount * core_ratio if is_dca_day and inputs.monthly_contribution > 0 else 0.0
    dip_amount = (
        scheduled_amount * max(0.0, 1.0 - core_ratio) * boost_ratio
        if is_dca_day and inputs.monthly_contribution > 0
        else 0.0
    )
    if inputs.core_dip_timing_enabled:
        state.core_dip_pending_cash += new_core_amount
        if state.core_dip_pending_cash > 0:
            state.core_dip_pending_days = state.core_dip_pending_days + 1 if state.core_dip_pending_days else 1
        core_buy_allowed, timing_reason = _core_dip_timing_allows_buy(
            point,
            recent_points,
            drawdown_pct,
            state.core_dip_pending_days,
            state.buy_trades == 0,
            inputs,
        )
        core_amount = state.core_dip_pending_cash if core_buy_allowed else 0.0
    else:
        core_buy_allowed = True
        timing_reason = "disabled"
        core_amount = new_core_amount
    reserve_cash = state.budget * _core_dip_cash_reserve_ratio(drawdown_pct, inputs)
    available_cash = max(0.0, state.cash - reserve_cash)
    extra_cash = max(0.0, available_cash - core_amount - dip_amount)
    initial_core_amount = 0.0
    if not state.buy_trades:
        initial_core_amount = min(
            extra_cash,
            inputs.initial_cash * state.weight / 100.0 * _clamp(inputs.core_dip_initial_core_pct / 100.0, 0.0, 1.0),
        )
        extra_cash = max(0.0, extra_cash - initial_core_amount)
    idle_cash_sweep = (
        extra_cash * _core_dip_idle_cash_sweep_ratio(drawdown_pct, inputs)
        if core_buy_allowed or boost_ratio > 0 or initial_core_amount > 0
        else 0.0
    )
    target_amount = core_amount + dip_amount + initial_core_amount + idle_cash_sweep
    gross_amount = min(target_amount, available_cash)
    if gross_amount <= 0:
        return False
    core_bought = min(core_amount, gross_amount)
    if inputs.core_dip_timing_enabled and core_bought > 0:
        state.core_dip_pending_cash = max(0.0, state.core_dip_pending_cash - core_bought)
        if state.core_dip_pending_cash <= 1e-9:
            state.core_dip_pending_cash = 0.0
            state.core_dip_pending_days = 0

    fee = min(inputs.trade_fee, gross_amount)
    net_amount = gross_amount - fee
    price_usd = _price_usd(state.symbol, point.close, inputs)
    shares = net_amount / price_usd if net_amount > 0 and price_usd > 0 else 0.0

    state.cash -= gross_amount
    state.shares += shares
    state.invested += gross_amount
    state.fees += fee
    state.trades += 1
    state.buy_trades += 1
    state.max_shares = max(state.max_shares, state.shares)
    state.last_value = _position_value_usd(state.symbol, state.shares, point.close, inputs)
    if state.lots is None:
        state.lots = []
    state.lots.append(
        PositionLot(
            threshold_pct=0.0,
            buy_drawdown_pct=drawdown_pct,
            buy_price_usd=price_usd,
            initial_shares=shares,
            remaining_shares=shares,
        )
    )
    sell_cycle_rearmed = _rearm_position_sell_cycle_after_dca_buy(state, drawdown_pct, inputs, sell_strategy)
    trade_log.append(
        {
            "action": "buy",
            "date": point.date.date().isoformat(),
            "symbol": state.symbol,
            "buy_strategy": "core_dip_dca",
            "sell_strategy": sell_strategy,
            "threshold_pct": 0.0,
            "drawdown_pct": drawdown_pct,
            "price": point.close,
            "price_usd": price_usd,
            "display_price": point.close,
            "display_price_usd": price_usd,
            "gross_amount": gross_amount,
            "fee": fee,
            "net_amount": net_amount,
            "shares": shares,
            "allocation_pct": 0.0,
            "scheduled_amount": scheduled_amount,
            "core_amount": core_amount,
            "new_core_amount": new_core_amount,
            "pending_core_amount": state.core_dip_pending_cash,
            "timing_reason": timing_reason,
            "dip_amount": dip_amount,
            "initial_core_amount": initial_core_amount,
            "idle_cash_sweep": max(0.0, gross_amount - core_amount - dip_amount - initial_core_amount),
            "dip_boost_ratio": boost_ratio,
            "cash_reserve": reserve_cash,
            "sell_cycle_rearmed": sell_cycle_rearmed,
        }
    )
    return True


def _core_dip_timing_allows_buy(
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


def _rearm_position_sell_cycle_after_dca_buy(
    state: SymbolState,
    drawdown_pct: float,
    inputs: StrategyInputs,
    sell_strategy: str,
) -> bool:
    if sell_strategy not in POSITION_SELL_REARM_STRATEGIES:
        return False
    if not state.sell_marks:
        return False
    if drawdown_pct + 1e-9 < _sell_stage_rearm_drawdown_pct(inputs):
        return False
    state.sell_marks.clear()
    if sell_strategy == "grid_rebound":
        state.grid_rebound_cycle_anchor_drawdown_pct = None
    return True


def _position_sell_rearm_drawdown_pct(inputs: StrategyInputs) -> float:
    threshold = max(0.0, float(inputs.dca_rearm_drawdown_pct))
    return min(threshold, float(inputs.max_drawdown_pct))


def _sell_stage_rearm_drawdown_pct(inputs: StrategyInputs) -> float:
    raw_threshold = (
        inputs.sell_stage_rearm_drawdown_pct
        if inputs.sell_stage_rearm_drawdown_pct is not None
        else inputs.dca_rearm_drawdown_pct
    )
    threshold = max(0.0, float(raw_threshold))
    return min(threshold, float(inputs.max_drawdown_pct))


def _salary_flow_dca_multiplier(drawdown_pct: float) -> float:
    if drawdown_pct >= 30.0:
        return 4.0
    if drawdown_pct >= 20.0:
        return 3.0
    if drawdown_pct >= 10.0:
        return 2.0
    if drawdown_pct >= 5.0:
        return 1.4
    return 1.0


def _core_dip_boost_ratio(drawdown_pct: float, inputs: StrategyInputs) -> float:
    start = max(0.0, float(inputs.core_dip_start_drawdown_pct))
    full = max(start, float(inputs.core_dip_full_drawdown_pct))
    if drawdown_pct <= start:
        return 0.0
    if drawdown_pct >= full:
        return 1.0
    if full <= start:
        return 1.0
    return (drawdown_pct - start) / (full - start)


def _core_dip_cash_reserve_ratio(drawdown_pct: float, inputs: StrategyInputs) -> float:
    base = _clamp(inputs.core_dip_cash_reserve_pct / 100.0, 0.0, 1.0)
    boost = _core_dip_boost_ratio(drawdown_pct, inputs)
    return max(0.01, base * (1.0 - boost * 0.85))


def _core_dip_idle_cash_sweep_ratio(drawdown_pct: float, inputs: StrategyInputs) -> float:
    boost = _core_dip_boost_ratio(drawdown_pct, inputs)
    if boost <= 0:
        return 0.12
    return min(0.90, 0.25 + boost * 0.65)


def _salary_flow_cash_reserve_ratio(drawdown_pct: float) -> float:
    if drawdown_pct >= 30.0:
        return 0.0
    if drawdown_pct >= 20.0:
        return 0.03
    if drawdown_pct >= 10.0:
        return 0.05
    return 0.08


def _salary_flow_idle_cash_sweep_ratio(drawdown_pct: float) -> float:
    if drawdown_pct >= 30.0:
        return 0.90
    if drawdown_pct >= 20.0:
        return 0.70
    if drawdown_pct >= 10.0:
        return 0.50
    if drawdown_pct >= 5.0:
        return 0.35
    return 0.20


def _execute_crossed_tranches(
    state: SymbolState,
    point: PricePoint,
    tranches: list[StrategyTranche],
    executed_thresholds: dict[float, float],
    inputs: StrategyInputs,
    trade_log: list[dict[str, object]],
    buy_strategy: str,
    sell_strategy: str,
) -> bool:
    drawdown_pct = _point_drawdown_pct(point, inputs)
    anchor_drawdown_pct = _buy_rearm_anchor_drawdown_pct(state)
    bought = False
    for tranche in tranches:
        effective_threshold_pct = anchor_drawdown_pct + tranche.threshold_pct
        threshold_key = round(tranche.threshold_pct, 8)
        if drawdown_pct + 1e-9 < effective_threshold_pct:
            continue
        target_amount = state.budget * tranche.allocation_pct / 100.0
        already_executed = executed_thresholds.get(threshold_key, 0.0)
        if buy_strategy == "pyramid_3":
            if already_executed > 0:
                continue
            gross_amount = min(target_amount, state.cash)
        else:
            gross_amount = min(max(0.0, target_amount - already_executed), state.cash)
        if gross_amount <= 0:
            if buy_strategy == "pyramid_3":
                executed_thresholds[threshold_key] = 1.0
            else:
                executed_thresholds[threshold_key] = max(already_executed, target_amount)
            continue
        fee = min(inputs.trade_fee, gross_amount)
        net_amount = gross_amount - fee
        shares = net_amount / _price_usd(state.symbol, point.close, inputs) if net_amount > 0 else 0.0
        state.cash -= gross_amount
        state.shares += shares
        state.invested += gross_amount
        state.fees += fee
        state.trades += 1
        state.buy_trades += 1
        bought = True
        state.max_shares = max(state.max_shares, state.shares)
        state.last_value = _position_value_usd(state.symbol, state.shares, point.close, inputs)
        if state.lots is None:
            state.lots = []
        state.lots.append(
            PositionLot(
                threshold_pct=tranche.threshold_pct,
                buy_drawdown_pct=drawdown_pct,
                buy_price_usd=_price_usd(state.symbol, point.close, inputs),
                initial_shares=shares,
                remaining_shares=shares,
            )
        )
        sell_cycle_rearmed = _rearm_position_sell_cycle_after_dca_buy(
            state,
            drawdown_pct,
            inputs,
            sell_strategy,
        )
        executed_thresholds[threshold_key] = 1.0 if buy_strategy == "pyramid_3" else already_executed + gross_amount
        trade_log.append(
            {
                "action": "buy",
                "date": point.date.date().isoformat(),
                "symbol": state.symbol,
                "buy_strategy": buy_strategy,
                "sell_strategy": sell_strategy,
                "threshold_pct": effective_threshold_pct,
                "base_threshold_pct": tranche.threshold_pct,
                "buy_rearm_anchor_drawdown_pct": anchor_drawdown_pct or None,
                "drawdown_pct": drawdown_pct,
                "price": point.close,
                "price_usd": _price_usd(state.symbol, point.close, inputs),
                "display_price": _point_peak(point, inputs) * (1.0 - effective_threshold_pct / 100.0),
                "display_price_usd": _price_usd(
                    state.symbol,
                    _point_peak(point, inputs) * (1.0 - effective_threshold_pct / 100.0),
                    inputs,
                ),
                "gross_amount": gross_amount,
                "fee": fee,
                "net_amount": net_amount,
                "shares": shares,
                "allocation_pct": tranche.allocation_pct,
                "sell_cycle_rearmed": sell_cycle_rearmed,
            }
        )
    return bought


def _buy_rearm_anchor_drawdown_pct(state: SymbolState) -> float:
    return max(0.0, float(state.buy_rearm_anchor_drawdown_pct or 0.0))


def _rearm_buy_tranches_after_repair(
    state: SymbolState,
    point: PricePoint,
    executed_thresholds: dict[float, float],
    inputs: StrategyInputs,
) -> None:
    drawdown_pct = _point_drawdown_pct(point, inputs)
    if drawdown_pct <= 0.50:
        executed_thresholds.clear()
        state.buy_rearm_anchor_drawdown_pct = None


def _rearm_buy_tranches_after_position_sell(
    state: SymbolState,
    point: PricePoint,
    executed_thresholds: dict[float, float],
    inputs: StrategyInputs,
) -> None:
    if not executed_thresholds or state.buy_rearm_drawdown_pct is None:
        return
    drawdown_pct = _point_drawdown_pct(point, inputs)
    if drawdown_pct + 1e-9 >= state.buy_rearm_drawdown_pct:
        executed_thresholds.clear()
        if inputs.buy_rearm_mode == BUY_REARM_MODE_RESTART_FROM_REARM:
            state.buy_rearm_anchor_drawdown_pct = drawdown_pct
        else:
            state.buy_rearm_anchor_drawdown_pct = None
        state.buy_rearm_drawdown_pct = None


def _execute_sell_strategy(
    state: SymbolState,
    point: PricePoint,
    inputs: StrategyInputs,
    buy_strategy: str,
    sell_strategy: str,
    trade_log: list[dict[str, object]],
    trade_index: int,
) -> None:
    if sell_strategy == "none" or state.shares <= 0:
        return

    if sell_strategy == "repair_step":
        if _uses_lot_level_sells(buy_strategy):
            _execute_repair_step_sells(state, point, inputs, trade_log, trade_index)
        else:
            _execute_position_repair_step_sells(state, point, inputs, trade_log, trade_index)
    elif sell_strategy == "grid_rebound":
        if _uses_lot_level_sells(buy_strategy):
            _execute_grid_rebound_sells(state, point, inputs, trade_log)
        else:
            _execute_position_grid_rebound_sells(state, point, inputs, trade_log)
    elif sell_strategy == "cost_deleverage":
        _execute_cost_deleverage_sells(state, point, inputs, trade_log, trade_index)


def _uses_lot_level_sells(buy_strategy: str) -> bool:
    return buy_strategy in LOT_SELL_BUY_STRATEGIES


def _execute_repair_step_sells(
    state: SymbolState,
    point: PricePoint,
    inputs: StrategyInputs,
    trade_log: list[dict[str, object]],
    trade_index: int,
) -> None:
    if not state.lots:
        return
    if (
        inputs.repair_sell_cooldown_days > 0
        and state.last_repair_sell_trade_index is not None
        and trade_index - state.last_repair_sell_trade_index < inputs.repair_sell_cooldown_days
    ):
        return
    drawdown_pct = _point_drawdown_pct(point, inputs)
    current_price_usd = _price_usd(state.symbol, point.close, inputs)
    min_profit_multiplier = 1 + inputs.sell_min_profit_pct / 100.0

    for lot in list(state.lots):
        if lot.remaining_shares <= 0:
            continue
        if current_price_usd < lot.buy_price_usd * min_profit_multiplier:
            continue
        if lot.repair_sell_marks is None:
            lot.repair_sell_marks = set()
        for mark, threshold, sell_pct in _repair_stages_for_lot(lot):
            if mark in lot.repair_sell_marks or drawdown_pct > threshold + 1e-9:
                continue
            stage_sell_pct = inputs.repair_stage_sell_pct
            shares = min(lot.initial_shares * stage_sell_pct / 100.0, lot.remaining_shares)
            if _sell_lot_shares(state, lot, point, shares, inputs, trade_log, "repair_step", threshold, sell_stage=mark):
                lot.repair_sell_marks.add(mark)
                state.last_repair_sell_date = point.date.date()
                state.last_repair_sell_trade_index = trade_index
                return


def _repair_stages_for_lot(lot: PositionLot) -> list[tuple[str, float, float]]:
    return [
        ("repair_50", lot.buy_drawdown_pct * 0.50, 25.0),
        ("repair_20", lot.buy_drawdown_pct * 0.20, 25.0),
        ("repair_ath", 0.50, 25.0),
    ]


def _execute_position_repair_step_sells(
    state: SymbolState,
    point: PricePoint,
    inputs: StrategyInputs,
    trade_log: list[dict[str, object]],
    trade_index: int,
) -> None:
    if state.shares <= 0:
        return
    if state.sell_marks is None:
        state.sell_marks = set()
    if (
        inputs.repair_sell_cooldown_days > 0
        and state.last_repair_sell_trade_index is not None
        and trade_index - state.last_repair_sell_trade_index < inputs.repair_sell_cooldown_days
    ):
        return
    avg_cost = _avg_cost_usd(state)
    if avg_cost <= 0:
        return
    current_price_usd = _price_usd(state.symbol, point.close, inputs)
    if current_price_usd < avg_cost * (1 + inputs.sell_min_profit_pct / 100.0):
        return
    drawdown_pct = _point_drawdown_pct(point, inputs)
    avg_buy_drawdown = _avg_buy_drawdown_pct(state)
    stages = [
        ("repair_50", avg_buy_drawdown * 0.50),
        ("repair_20", avg_buy_drawdown * 0.20),
        ("repair_ath", 0.50),
    ]
    for mark, threshold in stages:
        if mark in state.sell_marks or drawdown_pct > threshold + 1e-9:
            continue
        shares = state.shares * inputs.repair_stage_sell_pct / 100.0
        if _sell_shares(state, point, shares, inputs, trade_log, "repair_step", threshold, sell_stage=mark):
            state.sell_marks.add(mark)
            state.last_repair_sell_date = point.date.date()
            state.last_repair_sell_trade_index = trade_index
            return


def _execute_grid_rebound_sells(
    state: SymbolState,
    point: PricePoint,
    inputs: StrategyInputs,
    trade_log: list[dict[str, object]],
) -> None:
    if not state.lots:
        return
    drawdown_pct = _point_drawdown_pct(point, inputs)
    current_price_usd = _price_usd(state.symbol, point.close, inputs)
    min_profit_multiplier = 1 + inputs.sell_min_profit_pct / 100.0

    for lot in list(state.lots):
        if lot.remaining_shares <= 0:
            continue
        if current_price_usd < lot.buy_price_usd * min_profit_multiplier:
            continue
        if lot.grid_sell_marks is None:
            lot.grid_sell_marks = set()
        for mark, threshold, sell_pct in _grid_rebound_stages(lot.buy_drawdown_pct, inputs):
            if mark in lot.grid_sell_marks or drawdown_pct > threshold + 1e-9:
                continue
            shares = lot.remaining_shares * sell_pct / 100.0
            if _sell_lot_shares(
                state,
                lot,
                point,
                shares,
                inputs,
                trade_log,
                "grid_rebound",
                threshold,
                min_gross_amount=inputs.grid_min_sell_amount,
                sell_stage=mark,
            ):
                lot.grid_sell_marks.add(mark)
                return


def _execute_position_grid_rebound_sells(
    state: SymbolState,
    point: PricePoint,
    inputs: StrategyInputs,
    trade_log: list[dict[str, object]],
) -> None:
    if state.shares <= 0:
        return
    if state.sell_marks is None:
        state.sell_marks = set()
    avg_cost = _avg_cost_usd(state)
    if avg_cost <= 0:
        return
    current_price_usd = _price_usd(state.symbol, point.close, inputs)
    if current_price_usd < avg_cost * (1 + inputs.sell_min_profit_pct / 100.0):
        return
    drawdown_pct = _point_drawdown_pct(point, inputs)
    cycle_anchor_drawdown = _grid_rebound_cycle_anchor_drawdown_pct(state)
    for mark, threshold, sell_pct in _grid_rebound_stages(cycle_anchor_drawdown, inputs):
        if mark in state.sell_marks or drawdown_pct > threshold + 1e-9:
            continue
        shares = state.shares * sell_pct / 100.0
        if _sell_shares(
            state,
            point,
            shares,
            inputs,
            trade_log,
            "grid_rebound",
            threshold,
            min_gross_amount=inputs.grid_min_sell_amount,
            sell_stage=mark,
        ):
            state.sell_marks.add(mark)
            # Continue grid rebound cycle toward ATH:
            # when the last stage fires and shares remain, reset for the next cycle.
            if threshold <= 1e-9 and state.shares > 0:
                state.sell_marks.clear()
                state.grid_rebound_cycle_anchor_drawdown_pct = None
            return


def _grid_rebound_stages(
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


def _grid_rebound_cycle_anchor_drawdown_pct(state: SymbolState) -> float:
    if state.grid_rebound_cycle_anchor_drawdown_pct is not None:
        return state.grid_rebound_cycle_anchor_drawdown_pct
    anchor = _avg_buy_drawdown_pct(state)
    state.grid_rebound_cycle_anchor_drawdown_pct = anchor
    return anchor


def _execute_cost_deleverage_sells(
    state: SymbolState,
    point: PricePoint,
    inputs: StrategyInputs,
    trade_log: list[dict[str, object]],
    trade_index: int,
) -> None:
    if state.shares <= 0:
        return
    if state.sell_marks is None:
        state.sell_marks = set()
    if not cost_deleverage_cooldown_elapsed(
        last_sell_trade_index=state.last_cost_deleverage_sell_trade_index,
        current_trade_index=trade_index,
        cooldown_days=inputs.cost_deleverage_cooldown_days,
    ):
        return
    avg_cost = _avg_cost_usd(state)
    current_price_usd = _price_usd(state.symbol, point.close, inputs)
    if avg_cost <= 0:
        return
    profit_pct = current_price_usd / avg_cost * 100.0 - 100.0
    stage = select_cost_deleverage_stage(inputs=inputs, active_marks=state.sell_marks, profit_pct=profit_pct)
    if stage is None:
        return
    shares = state.shares * stage.sell_pct / 100.0
    if _sell_shares(
        state,
        point,
        shares,
        inputs,
        trade_log,
        "cost_deleverage",
        stage.profit_pct,
        min_gross_amount=inputs.cost_min_sell_amount,
        sell_stage=stage.mark,
    ):
        state.sell_marks.add(stage.mark)
        state.last_cost_deleverage_sell_trade_index = trade_index


def _sell_lot_shares(
    state: SymbolState,
    lot: PositionLot,
    point: PricePoint,
    requested_shares: float,
    inputs: StrategyInputs,
    trade_log: list[dict[str, object]],
    sell_strategy: str,
    trigger_value: float,
    min_gross_amount: float = 0.0,
    sell_stage: str | None = None,
) -> bool:
    shares = _sellable_shares(state, requested_shares, inputs)
    shares = min(shares, lot.remaining_shares)
    if shares <= 0 or not _meets_min_sell_amount(state, point, shares, inputs, min_gross_amount):
        return False
    lot.remaining_shares -= shares
    sold = _record_sell(state, point, shares, inputs, trade_log, sell_strategy, trigger_value, lot, sell_stage=sell_stage)
    if sold and sell_strategy != "repair_step":
        _mark_buy_rearm_after_position_sell(state, point, inputs)
    return sold


def _sell_shares(
    state: SymbolState,
    point: PricePoint,
    requested_shares: float,
    inputs: StrategyInputs,
    trade_log: list[dict[str, object]],
    sell_strategy: str,
    trigger_value: float,
    min_gross_amount: float = 0.0,
    sell_stage: str | None = None,
) -> bool:
    shares = _sellable_shares(state, requested_shares, inputs)
    if shares <= 0 or not _meets_min_sell_amount(state, point, shares, inputs, min_gross_amount):
        return False
    cost_basis = _avg_cost_usd(state) * shares
    _reduce_lots_fifo(state, shares)
    sold = _record_sell(
        state,
        point,
        shares,
        inputs,
        trade_log,
        sell_strategy,
        trigger_value,
        cost_basis=cost_basis,
        sell_stage=sell_stage,
    )
    if sold:
        _mark_buy_rearm_after_position_sell(state, point, inputs)
    return sold


def _mark_buy_rearm_after_position_sell(state: SymbolState, point: PricePoint, inputs: StrategyInputs) -> None:
    drawdown_pct = _point_drawdown_pct(point, inputs)
    threshold = min(float(inputs.max_drawdown_pct), drawdown_pct + _position_sell_rearm_drawdown_pct(inputs))
    state.buy_rearm_drawdown_pct = threshold


def _meets_min_sell_amount(
    state: SymbolState,
    point: PricePoint,
    shares: float,
    inputs: StrategyInputs,
    min_gross_amount: float,
) -> bool:
    if min_gross_amount <= 0:
        return True
    return shares * _price_usd(state.symbol, point.close, inputs) + 1e-9 >= min_gross_amount


def _record_sell(
    state: SymbolState,
    point: PricePoint,
    shares: float,
    inputs: StrategyInputs,
    trade_log: list[dict[str, object]],
    sell_strategy: str,
    trigger_value: float,
    lot: PositionLot | None = None,
    cost_basis: float | None = None,
    sell_stage: str | None = None,
) -> bool:
    price_usd = _price_usd(state.symbol, point.close, inputs)
    gross_amount = shares * price_usd
    if gross_amount <= 0:
        return False
    if cost_basis is None:
        avg_cost_before_sell = _avg_cost_usd(state)
        cost_basis = lot.buy_price_usd * shares if lot else avg_cost_before_sell * shares
    estimated_profit = gross_amount - cost_basis if cost_basis > 0 else 0.0
    estimated_profit_pct = _pct(gross_amount / cost_basis - 1.0) if cost_basis > 0 else 0.0
    fee = min(inputs.trade_fee, gross_amount)
    net_amount = gross_amount - fee
    state.cash += net_amount
    state.shares -= shares
    if state.shares < 1e-10:
        state.shares = 0.0
    state.fees += fee
    state.trades += 1
    state.sell_trades += 1
    state.sold_gross += gross_amount
    state.last_value = _position_value_usd(state.symbol, state.shares, point.close, inputs)
    event = {
        "action": "sell",
        "date": point.date.date().isoformat(),
        "symbol": state.symbol,
        "sell_strategy": sell_strategy,
        "trigger_value": trigger_value,
        "drawdown_pct": _point_drawdown_pct(point, inputs),
        "price": point.close,
        "price_usd": price_usd,
        "gross_amount": gross_amount,
        "fee": fee,
        "net_amount": net_amount,
        "shares": shares,
        "allocation_pct": 0.0,
        "estimated_profit": estimated_profit,
        "estimated_profit_pct": estimated_profit_pct,
        "lot_threshold_pct": lot.threshold_pct if lot else None,
        "lot_buy_drawdown_pct": lot.buy_drawdown_pct if lot else None,
        "lot_buy_price_usd": lot.buy_price_usd if lot else None,
    }
    if sell_stage:
        event["stage"] = sell_stage
    trade_log.append(event)
    return True


def _symbol_summary(state: SymbolState, inputs: StrategyInputs) -> dict[str, object]:
    market_value = state.last_value
    avg_cost = _avg_cost_usd(state)
    total_value = state.cash + market_value
    profit = total_value - state.budget
    return {
        "symbol": state.symbol,
        "name": state.name,
        "weight": state.weight,
        "budget": state.budget,
        "cash": state.cash,
        "shares": state.shares,
        "market_value": market_value,
        "total_value": total_value,
        "profit": profit,
        "return_pct": _pct(profit / state.budget) if state.budget > 0 else 0.0,
        "invested": state.invested,
        "sold_gross": state.sold_gross,
        "fees": state.fees,
        "trades": state.trades,
        "buy_trades": state.buy_trades,
        "sell_trades": state.sell_trades,
        "avg_cost_usd": avg_cost,
        "last_price": state.last_price or 0.0,
        "last_price_usd": _price_usd(state.symbol, state.last_price or 0.0, inputs),
    }


def _build_price_series_payload(
    price_points_by_symbol: dict[str, list[PricePoint]],
) -> dict[str, dict[str, list[float] | list[str]]]:
    payload: dict[str, dict[str, list[float] | list[str]]] = {}
    for symbol, points in price_points_by_symbol.items():
        payload[symbol] = {
            "dates": [point.date.date().isoformat() for point in points],
            "closes": [point.close for point in points],
            "drawdowns": [point.drawdown_ath * 100.0 for point in points],
            "drawdowns_120": [point.drawdown_120 * 100.0 for point in points],
        }
    return payload


def _avg_cost_usd(state: SymbolState) -> float:
    if not state.lots or state.shares <= 0:
        return 0.0
    total_cost = sum(lot.remaining_shares * lot.buy_price_usd for lot in state.lots)
    total_shares = sum(lot.remaining_shares for lot in state.lots)
    return total_cost / total_shares if total_shares > 0 else 0.0


def _avg_buy_drawdown_pct(state: SymbolState) -> float:
    if not state.lots or state.shares <= 0:
        return 0.0
    weighted_drawdown = sum(lot.remaining_shares * lot.buy_drawdown_pct for lot in state.lots)
    total_shares = sum(lot.remaining_shares for lot in state.lots)
    return weighted_drawdown / total_shares if total_shares > 0 else 0.0


def _sellable_shares(state: SymbolState, requested_shares: float, inputs: StrategyInputs) -> float:
    reserve_shares = state.max_shares * inputs.reserve_position_pct / 100.0
    available = max(0.0, state.shares - reserve_shares)
    return min(max(0.0, requested_shares), available)


def _reduce_lots_fifo(state: SymbolState, shares: float) -> None:
    if not state.lots:
        return
    remaining = shares
    for lot in state.lots:
        if remaining <= 0:
            break
        sold = min(lot.remaining_shares, remaining)
        lot.remaining_shares -= sold
        remaining -= sold


def _slice_thresholds(max_dd: float, step: float) -> list[float]:
    count = int(math.floor(max_dd / step))
    thresholds = [step * index for index in range(1, count + 1)]
    if not thresholds or not math.isclose(thresholds[-1], max_dd):
        thresholds.append(max_dd)
    return thresholds


def _price_usd(symbol: str, price: float, inputs: StrategyInputs) -> float:
    if symbol.endswith(".HK"):
        return price * inputs.hkd_to_usd
    return price


def _position_value_usd(symbol: str, shares: float, price: float, inputs: StrategyInputs) -> float:
    return shares * _price_usd(symbol, price, inputs)


def _point_drawdown_pct(point: PricePoint, inputs: StrategyInputs) -> float:
    if inputs.drawdown_basis == "rolling_120":
        return abs(point.drawdown_120 * 100.0)
    return abs(point.drawdown_ath * 100.0)


def _point_peak(point: PricePoint, inputs: StrategyInputs) -> float:
    if inputs.drawdown_basis == "rolling_120":
        return point.rolling_120_peak or point.rolling_peak
    return point.rolling_peak


def _max_drawdown_pct(values: list[float]) -> float:
    peak = -math.inf
    max_drawdown = 0.0
    for value in values:
        peak = max(peak, value)
        if peak > 0:
            max_drawdown = min(max_drawdown, value / peak - 1.0)
    return _pct(max_drawdown)


def _pct(value: float) -> float:
    return value * 100.0


def _positive_pct(value: float, field_name: str) -> float:
    parsed = float(value)
    if parsed <= 0:
        raise ValueError(f"{field_name} 必须大于 0。")
    return parsed


def _optional_positive_pct(value: object) -> float | None:
    if value in (None, ""):
        return None
    parsed = float(value)
    if not math.isfinite(parsed) or parsed <= 0 or parsed > 100:
        raise ValueError("单标的最大可接受回撤必须在 0 到 100 之间。")
    return parsed


def _inputs_for_target(inputs: StrategyInputs, target: PortfolioTarget) -> StrategyInputs:
    if target.max_drawdown_pct is None:
        return inputs
    return replace(inputs, max_drawdown_pct=target.max_drawdown_pct)


def _target_max_drawdown_by_symbol(raw_targets: Iterable[dict[str, object]]) -> dict[str, float]:
    result: dict[str, float] = {}
    for raw in raw_targets:
        if not isinstance(raw, dict):
            continue
        symbol = str(raw.get("symbol", "")).strip().upper()
        if not symbol:
            continue
        max_drawdown_pct = _optional_positive_pct(raw.get("max_drawdown_pct"))
        if max_drawdown_pct is not None:
            result[normalize_longbridge_symbol(symbol)] = max_drawdown_pct
    return result
