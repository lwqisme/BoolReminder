"""Fixed account-signal configuration and sheet snapshot parsing."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from config.config_manager import ConfigManager
from drawdown.position_strategy import StrategyInputs
from drawdown.strategy_rules import cost_deleverage_stages
from trade_sync.normalize import canonical_symbol, infer_longbridge_symbol
from trade_sync.store import load_account_snapshot, load_signal_targets_snapshot


TARGET_SYMBOLS = ("GOOGL.US", "TSLA.US")


@dataclass(frozen=True)
class AccountSignalRuntimeConfig:
    enabled: bool
    timezone: str
    schedule_hours: tuple[str, ...]
    sync_stale_minutes: int
    email_min_signals: int
    sell_allow_same_day_sell: bool


@dataclass(frozen=True)
class AccountSnapshot:
    as_of: str
    currency: str
    cash: float
    buying_power: float
    net_liquidation: float
    notes: str = ""


@dataclass(frozen=True)
class SignalTarget:
    symbol: str
    target_budget_usd: float
    monthly_contribution_usd: float
    min_buy_amount_usd: float
    enabled: bool


def get_runtime_config(config_manager: ConfigManager | None = None) -> AccountSignalRuntimeConfig:
    manager = config_manager or ConfigManager()
    raw = manager.get("account_signal", {})
    if not isinstance(raw, dict):
        raw = {}
    schedule_hours = raw.get("schedule_hours") or ("22:00", "22:30", "23:00", "23:30")
    if isinstance(schedule_hours, str):
        schedule_hours = tuple(item.strip() for item in schedule_hours.split(",") if item.strip())
    return AccountSignalRuntimeConfig(
        enabled=bool(raw.get("enabled", False)),
        timezone=str(raw.get("timezone", "Asia/Shanghai") or "Asia/Shanghai"),
        schedule_hours=tuple(str(item) for item in schedule_hours),
        sync_stale_minutes=int(raw.get("sync_stale_minutes", 60) or 60),
        email_min_signals=int(raw.get("email_min_signals", 1) or 1),
        sell_allow_same_day_sell=_bool(raw.get("sell_allow_same_day_sell"), False),
    )


def googl_inputs() -> StrategyInputs:
    return StrategyInputs(
        initial_cash=1.0,
        monthly_contribution=0.0,
        max_drawdown_pct=50.0,
        drawdown_basis="rolling_120",
        trade_fee=0.35,
        sell_min_profit_pct=8.0,
        dca_rearm_drawdown_pct=0.0,
        sell_stage_rearm_drawdown_pct=10.0,
        cost_first_profit_pct=8.0,
        cost_second_profit_pct=15.0,
        cost_third_profit_pct=25.0,
        cost_first_sell_pct=40.0,
        cost_second_sell_pct=30.0,
        cost_third_sell_pct=20.0,
        cost_deleverage_cooldown_days=15,
        sell_allow_same_day_sell=False,
        cost_min_sell_amount=0.0,
        core_dip_initial_core_pct=95.0,
        core_dip_weekly_core_pct=100.0,
        core_dip_cash_reserve_pct=3.0,
        core_dip_start_drawdown_pct=3.0,
        core_dip_full_drawdown_pct=15.0,
        core_dip_timing_enabled=True,
        core_dip_timing_max_delay_days=5,
        core_dip_timing_rise_threshold_pct=1.0,
        core_dip_timing_near_low_pct=1.0,
    )


def googl_cost_deleverage_stages(
    inputs: StrategyInputs | None = None,
) -> tuple[tuple[str, float, float], ...]:
    strategy_inputs = inputs or googl_inputs()
    return tuple((stage.mark, stage.profit_pct, stage.sell_pct) for stage in cost_deleverage_stages(strategy_inputs))


def account_strategy_summaries() -> dict[str, dict[str, Any]]:
    googl = googl_inputs()
    googl_stages = googl_cost_deleverage_stages(googl)
    return {
        "GOOGL.US": {
            "buy_strategy": "core_dip_dca",
            "sell_strategy": "cost_deleverage",
            "buy_summary": (
                "核心定投+回撤加仓: 初始95% / 周投核心100% / 现金垫3% / "
                "加仓3%-15% / 买点优化 延迟5日 大涨1% 近低1%"
            ),
            "sell_summary": "成本去杠杆: 盈利8/15/25%, 卖出40/30/20%, 冷却15日, 卖后重启0%回撤, 卖档重启10%回撤",
            "params": {
                "core_dip_initial_core_pct": googl.core_dip_initial_core_pct,
                "core_dip_weekly_core_pct": googl.core_dip_weekly_core_pct,
                "core_dip_cash_reserve_pct": googl.core_dip_cash_reserve_pct,
                "core_dip_start_drawdown_pct": googl.core_dip_start_drawdown_pct,
                "core_dip_full_drawdown_pct": googl.core_dip_full_drawdown_pct,
                "core_dip_timing_enabled": googl.core_dip_timing_enabled,
                "core_dip_timing_max_delay_days": googl.core_dip_timing_max_delay_days,
                "core_dip_timing_rise_threshold_pct": googl.core_dip_timing_rise_threshold_pct,
                "core_dip_timing_near_low_pct": googl.core_dip_timing_near_low_pct,
                "sell_min_profit_pct": googl.sell_min_profit_pct,
                "cost_profit_pcts": [threshold for _, threshold, _ in googl_stages],
                "cost_sell_pcts": [sell_pct for _, _, sell_pct in googl_stages],
                "cost_deleverage_cooldown_days": googl.cost_deleverage_cooldown_days,
                "dca_rearm_drawdown_pct": googl.dca_rearm_drawdown_pct,
                "sell_stage_rearm_drawdown_pct": googl.sell_stage_rearm_drawdown_pct,
            },
        },
        "TSLA.US": {
            "buy_strategy": "linear_weighted_slice",
            "sell_strategy": "grid_rebound",
            "buy_summary": "线性递增加权细切",
            "sell_summary": "网格回弹卖出",
            "params": {},
        },
    }


def tsla_inputs() -> StrategyInputs:
    return StrategyInputs(
        initial_cash=1.0,
        monthly_contribution=0.0,
        max_drawdown_pct=50.0,
        drawdown_basis="rolling_120",
        step_pct=5.0,
        trade_fee=0.35,
        sell_min_profit_pct=10.0,
        grid_rebound_step_pct=2.5,
        grid_first_sell_pct=10.0,
        grid_second_sell_pct=40.0,
        grid_min_sell_amount=0.0,
    )


def load_account_config() -> tuple[AccountSnapshot | None, dict[str, SignalTarget], list[str], dict[str, Any]]:
    errors: list[str] = []
    account_raw = load_account_snapshot()
    targets_raw = load_signal_targets_snapshot()
    account = _parse_account_snapshot(account_raw, errors)
    targets = _parse_signal_targets(targets_raw, errors)

    for symbol in TARGET_SYMBOLS:
        target = targets.get(symbol)
        if target is None:
            errors.append(f"signal_targets 缺少 {symbol.split('.')[0]} 行")
        elif not target.enabled:
            errors.append(f"signal_targets 中 {symbol.split('.')[0]} 未启用")
        elif target.target_budget_usd <= 0:
            errors.append(f"signal_targets 中 {symbol.split('.')[0]} initial_investment_usd 必须大于 0")

    meta = {
        "account_updated_at": (account_raw or {}).get("updated_at", ""),
        "account_saved_at": (account_raw or {}).get("saved_at", ""),
        "targets_updated_at": (targets_raw or {}).get("updated_at", ""),
        "targets_saved_at": (targets_raw or {}).get("saved_at", ""),
    }
    return account, targets, errors, meta


def _parse_account_snapshot(raw: dict[str, Any] | None, errors: list[str]) -> AccountSnapshot | None:
    rows = (raw or {}).get("rows")
    if not isinstance(rows, list) or not rows:
        errors.append("account sheet 缺少账户快照")
        return None
    row = rows[-1]
    if not isinstance(row, dict):
        errors.append("account sheet 最新行格式无效")
        return None
    cash = _float(_first_present(row, ("cash", "现金")))
    return AccountSnapshot(
        as_of=str(_first_present(row, ("as_of", "快照时间", "账户日期")) or ""),
        currency=str(_first_present(row, ("currency", "币种")) or "USD").upper(),
        cash=cash,
        buying_power=_float(_first_present(row, ("buying_power", "购买力")), cash),
        net_liquidation=_float(_first_present(row, ("net_liquidation", "净清算", "账户净值"))),
        notes=str(_first_present(row, ("notes", "备注")) or ""),
    )


def _parse_signal_targets(raw: dict[str, Any] | None, errors: list[str]) -> dict[str, SignalTarget]:
    rows = (raw or {}).get("rows")
    if not isinstance(rows, list) or not rows:
        errors.append("signal_targets sheet 缺少策略预算")
        return {}
    targets: dict[str, SignalTarget] = {}
    for row in rows:
        if not isinstance(row, dict):
            continue
        raw_symbol = _first_present(row, ("symbol", "标的", "股票代码", "股票"))
        if not raw_symbol:
            continue
        symbol = infer_longbridge_symbol(canonical_symbol(raw_symbol), "US")
        targets[symbol] = SignalTarget(
            symbol=symbol,
            target_budget_usd=_float(
                _first_present(
                    row,
                    (
                        "initial_investment_usd",
                        "初始投入",
                        "初始投入_usd",
                        "target_budget_usd",
                        "目标预算",
                    ),
                )
            ),
            monthly_contribution_usd=_float(_first_present(row, ("monthly_contribution_usd", "每月投入", "月投入"))),
            min_buy_amount_usd=_float(_first_present(row, ("min_buy_amount_usd", "最小买入金额", "最小提醒金额"))),
            enabled=_bool(_first_present(row, ("enabled", "启用")), True),
        )
    return targets


def _float(value: Any, default: float = 0.0) -> float:
    if value in ("", None):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value).strip().replace(",", ""))


def _first_present(row: dict[str, Any], keys: tuple[str, ...]) -> Any:
    for key in keys:
        if key in row and row[key] not in ("", None):
            return row[key]
    return None


def _bool(value: Any, default: bool = False) -> bool:
    if value in ("", None):
        return default
    if isinstance(value, bool):
        return value
    return str(value).strip().lower() in {"1", "true", "yes", "y", "on", "是", "启用"}
