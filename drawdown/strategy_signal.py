"""Strategy signal generation – playback real trades, then compute today's signal."""

from __future__ import annotations

import json
from collections import defaultdict
from dataclasses import replace
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any

from drawdown.generate_drawdown_report import (
    PricePoint,
    build_longbridge_quote_context,
    build_price_points_from_series,
    candle_datetime,
    fetch_longbridge_daily_candles,
    normalize_longbridge_symbol,
)
from drawdown.position_strategy import (
    PortfolioTarget,
    StrategyInputs,
    _simulate_strategy,
    build_strategy_tranches,
    parse_portfolio_targets,
)
from drawdown.strategy_lab_config import StrategyLabConfig
from drawdown.strategy_lab_history import load_experiment_preset, presets_dir
from trade_sync.normalize import canonical_symbol
from trade_sync.store import (
    BY_SYMBOL_DIR,
    SIGNAL_TARGETS_LATEST_PATH,
    load_symbol_snapshot,
)

def _signal_bindings_path() -> Path:
    return presets_dir().parent / "signal_bindings.json"


def load_signal_bindings() -> dict[str, str]:
    """Load symbol -> preset_id mapping from JSON file."""
    path = _signal_bindings_path()
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    if not isinstance(raw, dict):
        return {}
    return {str(k): str(v) for k, v in raw.items() if v}


def save_signal_bindings(bindings: dict[str, str]) -> None:
    """Save symbol -> preset_id mapping to JSON file."""
    path = _signal_bindings_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    cleaned = {str(k).upper(): str(v) for k, v in bindings.items() if v}
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(cleaned, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


def _compute_position_context(
    *,
    shares: float,
    cash: float,
    price: float,
    buy_strategy: str,
    inputs: StrategyInputs,
) -> dict[str, object]:
    """Compute which tranches are already covered by the real position."""
    market_value = shares * price
    total = cash + market_value
    position_ratio = market_value / total if total > 0 else 0.0

    tranches = build_strategy_tranches(inputs, buy_strategy)
    consumed: list[dict[str, object]] = []
    remaining: list[dict[str, object]] = []
    for t in sorted(tranches, key=lambda x: x.threshold_pct):
        alloc_ratio = t.allocation_pct / 100.0
        info = {
            "threshold_pct": t.threshold_pct,
            "allocation_pct": t.allocation_pct,
            "description": t.label,
            "is_consumed": position_ratio >= alloc_ratio,
        }
        if position_ratio >= alloc_ratio:
            consumed.append(info)
        else:
            remaining.append(info)

    next_tranche = remaining[0] if remaining else None
    return {
        "position_ratio": round(position_ratio * 100, 1),
        "total_value": round(total, 2),
        "market_value": round(market_value, 2),
        "consumed_count": len(consumed),
        "remaining_count": len(remaining),
        "next_tranche": next_tranche,
        "summary": _format_position_summary(position_ratio, consumed, next_tranche),
    }


def _format_position_summary(
    position_ratio: float,
    consumed: list[dict[str, object]],
    next_tranche: dict[str, object] | None,
) -> str:
    parts = [f"已投入 {position_ratio * 100:.0f}%"]
    if consumed:
        consumed_allocs = [f"{c['allocation_pct']:.0f}%" for c in consumed]
        parts.append(f"已覆盖档位: {', '.join(consumed_allocs)}")
    if next_tranche:
        parts.append(
            f"下一档: 回撤 ≥ {next_tranche['threshold_pct']:.0f}% 时买入 "
            f"{next_tranche['allocation_pct']:.0f}% 仓位"
        )
    else:
        parts.append("所有档位已覆盖")
    return "、".join(parts)


def generate_signal(
    symbol: str,
    preset_id: str,
    *,
    signal_date: date | None = None,
    dry_run: bool = False,
) -> dict[str, object]:
    """Generate trading signal for one symbol using a preset strategy.

    Returns dict with keys: symbol, preset_id, signal_date, signals, current_state, dry_run.
    On error, includes 'error' key.
    """
    sym = canonical_symbol(symbol)
    today = signal_date or date.today()

    # 1. Load preset
    preset = load_experiment_preset(preset_id)
    if not preset:
        return {"symbol": sym, "preset_id": preset_id, "error": "预设不存在"}

    config = StrategyLabConfig.from_saved_defaults(preset.get("config_payload", {}))

    # "all" is a parameter-lab selector, not a concrete strategy — reject early
    if config.buy_strategy == "all" or config.sell_strategy == "all":
        return {
            "symbol": sym,
            "preset_id": preset_id,
            "error": "预设策略为“全部”，无法生成信号。请在参数实验室中为该预设选择具体的买卖策略后重新保存。",
        }

    inputs = config.to_strategy_inputs()
    longbridge_sym = normalize_longbridge_symbol(sym)

    # 2. Load trade history (try base symbol as fallback for .US/.HK suffix mismatch)
    snapshot = load_symbol_snapshot(sym)
    if not snapshot:
        base_sym = sym.split(".", 1)[0]
        snapshot = load_symbol_snapshot(base_sym)
    if not snapshot:
        return {"symbol": sym, "preset_id": preset_id, "error": "无交易记录"}

    rows = snapshot.get("rows", [])
    if not rows:
        return {"symbol": sym, "preset_id": preset_id, "error": "交易记录为空"}

    # 3. Load signal targets for initial_cash and monthly_contribution
    initial_cash = _load_target_allocation(sym, inputs.initial_cash)
    monthly_contribution = _load_target_monthly(sym, inputs.monthly_contribution)

    # 4. Determine date range
    trade_dates = sorted({
        str(r.get("trade_date", ""))[:10]
        for r in rows
        if r.get("trade_date")
    })
    if not trade_dates:
        return {"symbol": sym, "preset_id": preset_id, "error": "交易记录无有效日期"}

    first_trade_date = date.fromisoformat(trade_dates[0])
    end_date = today

    # Fetch prices from first trade to today (plus buffer)
    fetch_start = first_trade_date - timedelta(days=30)
    fetch_end = end_date + timedelta(days=1)

    # 5. Fetch prices
    quote_ctx = build_longbridge_quote_context()
    candles = fetch_longbridge_daily_candles(quote_ctx, longbridge_sym, fetch_start, fetch_end)
    if not candles:
        return {"symbol": sym, "preset_id": preset_id, "error": "无法获取行情数据"}
    series = [
        (candle_datetime(c).replace(tzinfo=None), float(c.close))
        for c in candles
    ]
    # Append real-time price if available (intraday quote replaces last close)
    from drawdown.leaps_signal import append_realtime_price
    series = append_realtime_price(series, quote_ctx, longbridge_sym, end_date)
    points = build_price_points_from_series(series)
    if not points:
        return {"symbol": sym, "preset_id": preset_id, "error": "无法构建价格序列"}

    # 6. Build trade overrides
    trade_overrides: dict[date, list[dict[str, object]]] = defaultdict(list)
    for row in rows:
        trade_date_str = str(row.get("trade_date", ""))[:10]
        try:
            td = date.fromisoformat(trade_date_str)
        except ValueError:
            continue
        trade_overrides[td].append(dict(row))

    # Determine effective signal date (weekend → use last trading day)
    # points[-1].date may be datetime (from candles) or date (after append_realtime_price normalization)
    _last_pt_date = points[-1].date
    last_price_date = (_last_pt_date.date() if hasattr(_last_pt_date, 'date') else _last_pt_date) if points else today
    is_weekend = today.weekday() >= 5
    effective_signal_date = last_price_date if (is_weekend and last_price_date < today) else today

    # 7. Run simulation: real trades for history, engine only on effective_signal_date
    effective_inputs = replace(inputs, initial_cash=initial_cash, monthly_contribution=monthly_contribution)

    target = PortfolioTarget(
        symbol=longbridge_sym,
        weight=100.0,
        name=sym,
        max_drawdown_pct=inputs.max_drawdown_pct if inputs.max_drawdown_pct < 100 else None,
    )

    price_points_by_symbol = {longbridge_sym: points}

    # Engine skips all days ≤ engine_start, runs only on effective_signal_date
    engine_start = effective_signal_date - timedelta(days=1)

    result = _simulate_strategy(
        price_points_by_symbol,
        [target],
        effective_inputs,
        strategy=config.buy_strategy,
        sell_strategy=config.sell_strategy,
        trade_overrides=dict(trade_overrides),
        last_trade_date=engine_start,
    )

    # 8. Extract signals for effective_signal_date

    signal_trades = [
        t for t in result.get("trades", [])
        if t.get("date") == effective_signal_date.isoformat() and not t.get("is_real")
    ]

    # Compute actual position from real trades only (before engine signals)
    real_shares = sum(
        float(e.get("shares", 0) or 0) * (1 if str(e.get("side", "")).lower() == "buy" else -1)
        for events in trade_overrides.values()
        for e in events
    )
    total_buy = sum(
        float(e.get("shares", 0) or 0) * float(e.get("price", 0) or 0)
        for events in trade_overrides.values()
        for e in events if str(e.get("side", "")).lower() == "buy"
    )
    total_sell = sum(
        float(e.get("shares", 0) or 0) * float(e.get("price", 0) or 0)
        for events in trade_overrides.values()
        for e in events if str(e.get("side", "")).lower() == "sell"
    )
    real_cash = initial_cash + total_sell - total_buy
    last_price_val = points[-1].close if points else 0

    # Compute position context: which tranches are already covered by real position
    position_context = _compute_position_context(
        shares=real_shares,
        cash=real_cash,
        price=last_price_val,
        buy_strategy=config.buy_strategy,
        inputs=inputs,
    )

    return {
        "symbol": sym,
        "preset_id": preset_id,
        "preset_name": preset.get("name", ""),
        "param_summary": _param_summary(config),
        "signal_date": today.isoformat(),
        "effective_date": effective_signal_date.isoformat(),
        "is_weekend": is_weekend,
        "buy_strategy": config.buy_strategy,
        "sell_strategy": config.sell_strategy,
        "current_state": {
            "shares": real_shares,
            "cash": real_cash,
            "invested": total_buy,
            "market_value": real_shares * last_price_val,
            "last_price": last_price_val,
            "avg_cost": total_buy / real_shares if real_shares > 0 else 0,
        },
        "position_context": position_context,
        "signals": [
            {
                "action": t.get("action"),
                "shares": t.get("shares"),
                "price": t.get("price"),
                "reason": _signal_reason(t),
            }
            for t in signal_trades
        ],
        "dry_run": dry_run,
    }


def generate_all_signals(
    *,
    signal_date: date | None = None,
    dry_run: bool = False,
) -> list[dict[str, object]]:
    """Generate signals for all bound symbols, routing by preset type."""
    from drawdown.strategy_lab_history import load_experiment_preset

    bindings = load_signal_bindings()
    results: list[dict[str, object]] = []
    for symbol, preset_id in bindings.items():
        try:
            preset = load_experiment_preset(preset_id)
            is_leaps = (
                preset is not None
                and preset.get("config_payload", {}).get("type") == "leaps"
            )
            if is_leaps:
                from drawdown.leaps_signal import generate_leaps_signals_for_symbol
                result_obj = generate_leaps_signals_for_symbol(
                    symbol, preset_id, signal_date=signal_date, dry_run=dry_run
                )
                result = _serialize_leaps_signal_result(result_obj)
            else:
                result = generate_signal(symbol, preset_id, signal_date=signal_date, dry_run=dry_run)
        except Exception as exc:
            result = {"symbol": symbol, "preset_id": preset_id, "error": str(exc)}
        results.append(result)
    return results


def _serialize_leaps_signal_result(result) -> dict[str, object]:
    """Serialize LeapsSignalResult to JSON-safe dict."""
    from dataclasses import asdict
    d = asdict(result)
    d["signal_date"] = result.signal_date.isoformat()
    for pos in d.get("open_positions", []):
        for date_key in ("entry_date", "expiration"):
            if date_key in pos and pos[date_key] is not None:
                if hasattr(pos[date_key], "isoformat"):
                    pos[date_key] = pos[date_key].isoformat()
                else:
                    pos[date_key] = str(pos[date_key])
    return d


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------


def _load_target_allocation(symbol: str, fallback: float) -> float:
    """Parse signal_targets sheet for the symbol's 初始投入 (initial allocation)."""
    sym_base = symbol.upper()
    sym_short = sym_base.split(".", 1)[0]
    match_keys = {sym_base, sym_short}
    if not SIGNAL_TARGETS_LATEST_PATH.exists():
        return fallback
    try:
        raw = json.loads(SIGNAL_TARGETS_LATEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    rows = raw.get("rows", [])
    if not isinstance(rows, list):
        return fallback
    name_keys = ("标的", "symbol", "ticker")
    allocation_keys = ("初始投入", "initial_cash", "target_value")
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = ""
        for k in name_keys:
            name = str(row.get(k, "")).strip().upper()
            if name:
                break
        if name not in match_keys:
            continue
        enabled = str(row.get("启用", "")).strip()
        if enabled and enabled not in ("是", "yes", "true", "1", "True"):
            continue
        for k in allocation_keys:
            try:
                val = float(row.get(k, 0) or 0)
                if val > 0:
                    return val
            except (TypeError, ValueError):
                continue
    return fallback


def _load_target_monthly(symbol: str, fallback: float) -> float:
    """Parse signal_targets sheet for the symbol's 每月投入 (monthly contribution)."""
    sym_base = symbol.upper()
    sym_short = sym_base.split(".", 1)[0]
    match_keys = {sym_base, sym_short}
    if not SIGNAL_TARGETS_LATEST_PATH.exists():
        return fallback
    try:
        raw = json.loads(SIGNAL_TARGETS_LATEST_PATH.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return fallback
    rows = raw.get("rows", [])
    if not isinstance(rows, list):
        return fallback
    name_keys = ("标的", "symbol", "ticker")
    monthly_keys = ("每月投入", "monthly_contribution", "monthly")
    for row in rows:
        if not isinstance(row, dict):
            continue
        name = ""
        for k in name_keys:
            name = str(row.get(k, "")).strip().upper()
            if name:
                break
        if name not in match_keys:
            continue
        for k in monthly_keys:
            try:
                val = float(row.get(k, 0) or 0)
                return val
            except (TypeError, ValueError):
                continue
    return fallback


def _param_summary(config) -> str:
    """Compact one-line parameter summary for display."""
    from drawdown.position_strategy import STRATEGY_LABELS, SELL_STRATEGY_LABELS

    buy_name = STRATEGY_LABELS.get(config.buy_strategy, config.buy_strategy)
    sell_name = SELL_STRATEGY_LABELS.get(config.sell_strategy, config.sell_strategy)

    parts = [buy_name]
    if config.buy_strategy in ("equal_slice", "linear_weighted_slice"):
        parts.append(f"(步长{config.slice_step_pct:.1f}%/每步{config.equal_slice_allocation_pct:.1f}%)")
    elif config.buy_strategy == "pyramid_3":
        parts.append(f"(步长{config.slice_step_pct:.1f}%)")
    elif config.buy_strategy == "core_dip_dca":
        parts.append(f"(核心{config.core_dip_initial_core_pct:.0f}%/周{config.core_dip_weekly_core_pct:.0f}%)")

    parts.append(" / ")
    parts.append(sell_name)

    if config.sell_strategy == "price_rise_grid":
        parts.append(f"(步长{config.grid_rebound_step_pct:.1f}% 卖{config.grid_sell_pct:.0f}% 盈≥{config.sell_min_profit_pct:.1f}%)")
    elif config.sell_strategy == "grid_rebound":
        parts.append(f"(步长{config.grid_rebound_step_pct:.1f}% 卖{config.grid_sell_pct:.0f}%)")
    elif config.sell_strategy == "repair_step":
        parts.append(f"(卖{config.repair_stage_sell_pct:.0f}% 盈≥{config.sell_min_profit_pct:.0f}% 冷却{config.repair_sell_cooldown_days}d)")
    elif config.sell_strategy == "cost_deleverage":
        parts.append(f"(盈{config.cost_first_profit_pct:.0f}/{config.cost_second_profit_pct:.0f}/{config.cost_third_profit_pct:.0f}%)")

    return "".join(parts)

def _signal_reason(trade: dict[str, object]) -> str:
    """Human-readable reason for a signal trade."""
    action = trade.get("action", "")
    threshold = trade.get("threshold_pct")

    if trade.get("sell_stage"):
        return f"卖出 第{trade['sell_stage']}档"
    if action == "sell" and threshold is not None:
        return f"卖出 {float(threshold):.0f}% 档位"

    drawdown = trade.get("drawdown_pct")
    base_threshold = trade.get("base_threshold_pct")
    if action == "buy":
        parts = []
        if drawdown is not None:
            parts.append(f"回撤{float(drawdown):.1f}%")
        if base_threshold is not None:
            parts.append(f"第{float(base_threshold):.1f}%档触发")
        if parts:
            return " ".join(parts) + " 买入"
        return "策略买入"

    strategy = trade.get("buy_strategy", "")
    return f"{strategy} 信号"
