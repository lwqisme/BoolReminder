"""Strategy signal generation – playback real trades, then compute today's signal."""

from __future__ import annotations

import json
import os
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
    _strategy_inputs_payload,
    build_strategy_tranches,
    parse_portfolio_targets,
)
from drawdown.strategy_lab_config import StrategyLabConfig
from drawdown.strategy_lab_history import load_experiment_preset, presets_dir
from drawdown.strategy_parameter_registry import (
    BUY_PARAMETER_FIELDS,
    SELL_PARAMETER_FIELDS,
    PARAMETER_LAB_BUY_VARIANT_SCHEMA,
    PARAMETER_LAB_SELL_VARIANT_SCHEMA,
    PARAMETER_LAB_CANDIDATE_SCHEMA,
    strategy_registry_payload,
)
from drawdown.worker_bridge import run_signal as worker_run_signal, WorkerBridgeError
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
    dca_strategies = {"weekly_dca", "salary_flow_dca", "core_dip_dca"}
    is_dca = buy_strategy in dca_strategies

    consumed: list[dict[str, object]] = []
    remaining: list[dict[str, object]] = []
    cumulative_alloc = 0.0
    for t in sorted(tranches, key=lambda x: x.threshold_pct):
        cumulative_alloc += t.allocation_pct / 100.0
        is_consumed = True if is_dca else position_ratio >= cumulative_alloc - 1e-9
        info = {
            "threshold_pct": t.threshold_pct,
            "allocation_pct": t.allocation_pct,
            "description": t.label,
            "is_consumed": is_consumed,
        }
        if is_consumed:
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
        "buy_strategy": buy_strategy,
        "summary": _format_position_summary(position_ratio, consumed, next_tranche, buy_strategy, cash, inputs),
    }


def _format_position_summary(
    position_ratio: float,
    consumed: list[dict[str, object]],
    next_tranche: dict[str, object] | None,
    buy_strategy: str,
    cash: float,
    inputs: StrategyInputs,
) -> str:
    parts = [f"已投入 {position_ratio * 100:.0f}%"]

    dca_labels = {
        "weekly_dca": "初始现金和工资到账后立即买入",
        "salary_flow_dca": "每周首个交易日按工资流动态定投",
        "core_dip_dca": "核心底仓 + 回撤扫入现金",
    }
    if buy_strategy in dca_labels:
        parts.append(dca_labels[buy_strategy])
        parts.append(f"剩余现金 ${cash:,.0f}")
        return "、".join(parts)

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


def _variant_row(variant_id: int, variant_key: str, strategy_key: str, inputs: StrategyInputs, fields) -> list:
    """Assemble one variant row aligned to PARAMETER_LAB_*_VARIANT_SCHEMA."""
    row = [variant_id, variant_key, strategy_key]
    for field in fields:
        row.append(getattr(inputs, field, None))
    return row


def _build_signal_worker_packet(
    longbridge_sym: str,
    sym: str,
    target: PortfolioTarget,
    points: list,
    inputs: StrategyInputs,
    buy_strategy: str,
    sell_strategy: str,
    first_trade_date: date,
    effective_signal_date: date,
) -> dict[str, Any]:
    """Build a single-candidate v3 worker packet for signal mode.

    market_data carries the full series (warmup + window); the worker's
    rebuildPricePoints(...,365) uses warmup for drawdown and only iterates
    [first_trade_date, signal_date] (real-trade replay + signal-day engine).
    """
    dates_full = [p.date.date().isoformat() for p in points]
    closes_full = [p.close for p in points]
    task = {
        "key": "signal_0",
        "portfolio_key": "signal",
        "period_key": "window",
        "portfolio_label": sym,
        "period_label": f"{first_trade_date.isoformat()}..{effective_signal_date.isoformat()}",
        "start": first_trade_date.isoformat(),
        "end": effective_signal_date.isoformat(),
        "symbols": [longbridge_sym],
        "targets": [{
            "symbol": target.symbol,
            "weight": float(target.weight),
            "name": target.name or target.symbol,
            "max_drawdown_pct": target.max_drawdown_pct,
        }],
    }
    return {
        "run_id": "signal_sim",
        "inputs": _strategy_inputs_payload(inputs),
        "market_data": {"symbols": {longbridge_sym: {"dates": dates_full, "closes": closes_full}}},
        "tasks": [task],
        "buy_variants": [_variant_row(0, "bv0", buy_strategy, inputs, BUY_PARAMETER_FIELDS)],
        "sell_variants": [_variant_row(0, "sv0", sell_strategy, inputs, SELL_PARAMETER_FIELDS)],
        "buy_variant_schema": list(PARAMETER_LAB_BUY_VARIANT_SCHEMA),
        "sell_variant_schema": list(PARAMETER_LAB_SELL_VARIANT_SCHEMA),
        "candidate_schema": list(PARAMETER_LAB_CANDIDATE_SCHEMA),
        "candidate_rows": [[0, 0, 0, "c0"]],
        "registry": strategy_registry_payload(),
        "include_trades": True,
    }


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
    #    When signal_targets has no entry, compute initial_cash from trade history
    #    instead of using the preset's default (which is often a backtest value like $50K).
    initial_cash = _load_target_allocation(sym)
    initial_cash_source = "signal_targets"
    if initial_cash is None:
        initial_cash = _compute_initial_cash_from_trades(rows, inputs.initial_cash)
        initial_cash_source = "trade_history" if initial_cash != inputs.initial_cash else "preset_default"
    monthly_contribution = _load_target_monthly(sym)
    monthly_contribution_source = "signal_targets"
    if monthly_contribution is None:
        # When signal_targets has no entry for this symbol, monthly contribution
        # cannot be reliably inferred from trade history (irregular deposits are
        # indistinguishable from discretionary buys).  Default to 0 instead of the
        # preset's backtest default (e.g. $1,000/mo) which would inject phantom cash.
        monthly_contribution = 0.0
        monthly_contribution_source = "zero_no_signal_targets"

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

    # Fetch prices from first trade to today (plus buffer).
    # 365-day warmup before first_trade_date so the worker computes drawdown with
    # the same warmup semantics as GA/preset backtest (engine一致性).
    fetch_start = first_trade_date - timedelta(days=365)
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

    # 7. Run simulation: real trades for history, engine only on effective_signal_date.
    #    默认走 JS worker（与 GA/预设回测同一引擎 + 365d warmup）；SIGNAL_SIM_ENGINE=python
    #    回退旧 Python _simulate_strategy 用于对照/回滚。
    effective_inputs = replace(inputs, initial_cash=initial_cash, monthly_contribution=monthly_contribution)

    target = PortfolioTarget(
        symbol=longbridge_sym,
        weight=100.0,
        name=sym,
        max_drawdown_pct=inputs.max_drawdown_pct if inputs.max_drawdown_pct < 100 else None,
    )

    price_points_by_symbol = {longbridge_sym: points}
    signal_date_iso = effective_signal_date.isoformat()
    engine = os.environ.get("SIGNAL_SIM_ENGINE", "worker").strip().lower()

    if engine == "python":
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
        signal_trades = [
            t for t in result.get("trades", [])
            if t.get("date") == signal_date_iso and not t.get("is_real")
        ]
    else:
        signal_packet = _build_signal_worker_packet(
            longbridge_sym, sym, target, points, effective_inputs,
            config.buy_strategy, config.sell_strategy, first_trade_date, effective_signal_date,
        )
        overrides_iso = {d.isoformat(): events for d, events in trade_overrides.items()}
        bridge_result = worker_run_signal(signal_packet, overrides_iso, signal_date_iso)
        signal_trades = bridge_result.get("signal_trades") or []

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

    # Build all-tranche signals: actionable buys + covered tranches marked as 已覆盖
    all_signals: list[dict[str, object]] = []
    # Today's actionable signals first
    for t in signal_trades:
        all_signals.append({
            "action": t.get("action"),
            "shares": t.get("shares"),
            "price": t.get("price"),
            "reason": _signal_reason(t),
            "status": "signal",
        })
    # Covered tranches from position_context
    for c in position_context.get("consumed", []):
        all_signals.append({
            "action": "buy",
            "status": "covered",
            "threshold_pct": c.get("threshold_pct"),
            "allocation_pct": c.get("allocation_pct"),
            "description": c.get("description", ""),
            "reason": f"已覆盖: 回撤 {c.get('threshold_pct', 0):.1f}% 档位 (仓位 {c.get('allocation_pct', 0):.1f}%)",
        })

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
        "initial_cash": initial_cash,
        "initial_cash_source": initial_cash_source,
        "monthly_contribution": monthly_contribution,
        "monthly_contribution_source": monthly_contribution_source,
        "position_context": position_context,
        "signals": all_signals,
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
                result["preset_name"] = (preset or {}).get("name", "")
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


def _compute_initial_cash_from_trades(rows: list[dict], fallback: float) -> float:
    """Estimate initial_cash from trade history as peak cumulative outflow.

    Walks trades in chronological order, tracking net cash flow.
    The peak cumulative outflow (= most cash ever committed at once)
    is a reasonable estimate of the "initial investment" when
    signal_targets has no entry for this symbol.
    """
    cash_flow = 0.0
    peak_outflow = 0.0
    for row in sorted(rows, key=lambda r: str(r.get("trade_date", ""))):
        side = str(row.get("side", "")).lower()
        shares = float(row.get("shares", 0) or 0)
        price = float(row.get("price", 0) or 0)
        amount = float(row.get("amount", 0) or shares * price)
        if side == "buy":
            cash_flow -= amount
        elif side == "sell":
            cash_flow += amount
        peak_outflow = max(peak_outflow, -cash_flow)
    return peak_outflow if peak_outflow > 0 else fallback


def _load_target_allocation(symbol: str, fallback: float | None = None) -> float | None:
    """Parse signal_targets sheet for the symbol's 初始投入 (initial allocation).

    Returns None when symbol not found and no fallback provided.
    """
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
    # Symbol not found in signal_targets — return None to trigger trade-based computation
    return fallback


def _load_target_monthly(symbol: str, fallback: float | None = None) -> float | None:
    """Parse signal_targets sheet for the symbol's 每月投入 (monthly contribution).

    Returns None when symbol not found and no fallback provided.
    """
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
    # Symbol not found in signal_targets
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


def _html_esc(s: str) -> str:
    return s.replace('&', '&amp;').replace('<', '&lt;').replace('>', '&gt;').replace('"', '&quot;')


def _fmt_num(v, decimals=1):
    """Format a number for display in email."""
    if v is None or v == '?':
        return '?'
    try:
        return f"{float(v):.{decimals}f}"
    except (ValueError, TypeError):
        return str(v)


def _fmt_price(v):
    if v is None or v == '?':
        return '?'
    try:
        return f"{float(v):.2f}"
    except (ValueError, TypeError):
        return str(v)


def _action_label(action):
    return {"buy": "买入", "sell": "卖出"}.get(str(action), str(action))


def build_signal_email_html(results: list[dict[str, object]]) -> tuple[str, str]:
    """Build subject + HTML body for signal email. Returns (subject, html).

    Public API used by both web/app.py and scheduler/signal_scheduler.py.
    """
    # Collect active signals
    active: list[dict[str, object]] = []
    for r in results:
        if r.get("error"):
            continue
        has_stock = any(s.get("status") == "signal" for s in r.get("signals", []))
        has_leaps = bool(r.get("entry_signals") or r.get("sell_signals"))
        if has_stock or has_leaps:
            active.append(r)

    subject_parts: list[str] = []
    rows_html = ""

    for r in active:
        symbol = str(r.get("symbol", "?"))
        preset_name = str(r.get("preset_name") or r.get("preset_id", ""))
        stock_sigs = r.get("signals", [])
        leaps_entry = r.get("entry_signals", [])
        leaps_sell = r.get("sell_signals", [])

        has_buy = any(s.get("action") == "buy" and s.get("status") != "covered" for s in stock_sigs) or bool(leaps_entry)
        has_sell = any(s.get("action") == "sell" for s in stock_sigs) or bool(leaps_sell)

        if has_buy:
            subject_parts.append(f"{symbol} 买入")
        if has_sell:
            subject_parts.append(f"{symbol} 卖出")

        border_color = "#00856f" if has_buy else "#d04437" if has_sell else "#d77b00"
        bg_tint = "#ecfdf5" if has_buy else "#fef2f2" if has_sell else "#fffbeb"

        signals_html = ""
        total_buy = 0.0
        total_buy_shares = 0.0

        for sig in stock_sigs:
            if sig.get("status") == "covered":
                signals_html += f'<tr><td style="color:#657184;font-size:13px;">✅ {_html_esc(str(sig.get("reason", "已覆盖")))}</td><td></td><td></td></tr>'
            else:
                action = sig.get("action", "?")
                is_buy = action == "buy"
                icon = "🟢" if is_buy else "🔴"
                color = "#00856f" if is_buy else "#d04437"
                shares = _fmt_num(sig.get('shares', '?'))
                price = _fmt_price(sig.get('price', '?'))
                reason = _html_esc(str(sig.get('reason', '')))
                action_label = _html_esc(_action_label(action))
                signals_html += f'<tr><td style="font-weight:700;color:{color};font-size:13px;white-space:nowrap;">{icon} {action_label}</td><td style="font-family:monospace;font-size:13px;white-space:nowrap;">{shares}股</td><td style="font-family:monospace;font-size:13px;white-space:nowrap;">@ ${price}</td><td style="color:#657184;font-size:12px;word-break:break-word;">{reason}</td></tr>'
                if is_buy:
                    try:
                        total_buy += float(sig.get('shares', 0)) * float(sig.get('price', 0))
                        total_buy_shares += float(sig.get('shares', 0))
                    except (ValueError, TypeError):
                        pass

        for es in leaps_entry:
            underlying = _html_esc(str(es.get('underlying', symbol)))
            stock_price = _fmt_price(es.get('stock_price', '?'))
            reason = _html_esc(str(es.get('reason', '')))
            signals_html += f'<tr><td style="font-weight:700;color:#00856f;font-size:13px;white-space:nowrap;">🟢 LEAPS买入</td><td style="font-family:monospace;font-size:13px;white-space:nowrap;">{underlying}</td><td style="font-family:monospace;font-size:13px;white-space:nowrap;">@ ${stock_price}</td><td style="color:#657184;font-size:12px;word-break:break-word;">{reason}</td></tr>'
            subject_parts.append(f"{symbol} LEAPS买入")

        for ss in leaps_sell:
            stage = ss.get('stage', '?')
            pct = _fmt_num(ss.get('pct_to_sell', '?'), 0)
            stock_price = _fmt_price(ss.get('stock_price', '?'))
            reason = _html_esc(str(ss.get('reason', '')))
            signals_html += f'<tr><td style="font-weight:700;color:#d04437;font-size:13px;white-space:nowrap;">🔴 LEAPS卖出 S{stage}</td><td style="font-family:monospace;font-size:13px;white-space:nowrap;">{pct}%</td><td style="font-family:monospace;font-size:13px;white-space:nowrap;">@ ${stock_price}</td><td style="color:#657184;font-size:12px;word-break:break-word;">{reason}</td></tr>'
            subject_parts.append(f"{symbol} LEAPS卖出")

        buy_summary = ""
        if total_buy > 0:
            cs = r.get("current_state", {})
            total_value = (cs.get("cash", 0) or 0) + (cs.get("market_value", 0) or 0)
            buy_pct = f"{total_buy / total_value * 100:.1f}" if total_value > 0 else "?"
            buy_summary = f'<tr><td colspan="4" style="padding:6px 10px;background:#ecfdf5;border:1px solid rgba(0,133,111,0.16);border-radius:6px;color:#00856f;font-weight:700;font-size:12px;">💰 买入合计：{total_buy_shares:.1f}股 / ${total_buy:.0f} · 占仓位 {buy_pct}%</td></tr>'

        state_html = ""
        cs = r.get("current_state")
        if cs:
            state_html = f'<tr><td colspan="4" style="color:#657184;font-size:11px;padding-top:6px;">持仓 {_fmt_num(cs.get("shares", 0), 1)}股 · 现金 ${_fmt_price(cs.get("cash", 0))} · 市值 ${_fmt_price(cs.get("market_value", 0))} · 均价 ${_fmt_price(cs.get("avg_cost", 0))}</td></tr>'
            ic = r.get("initial_cash")
            if ic is not None:
                ic_src = r.get("initial_cash_source", "")
                ic_icon = {"trade_history": "📈", "signal_targets": "📋"}.get(ic_src, "⚙️")
                ic_text = {"trade_history": "交易推算", "signal_targets": "信号目标表"}.get(ic_src, "预设默认")
                mc_src = r.get("monthly_contribution_source", "")
                mc_val = r.get("monthly_contribution", 0)
                mc_icon = {"signal_targets": "📋", "zero_no_signal_targets": "🚫"}.get(mc_src, "⚙️")
                mc_text = "无月定投" if mc_src == "zero_no_signal_targets" else f"${mc_val:.0f}/月"
                state_html += f'<tr><td colspan="4" style="color:#657184;font-size:11px;">{ic_icon} 初始 ${float(ic):.0f} ({ic_text}) · {mc_icon} {mc_text}</td></tr>'

        pc = r.get("position_context")
        pc_html = ""
        if pc and pc.get("summary"):
            pc_html = f'<tr><td colspan="4" style="color:#1167d8;font-size:11px;">📊 {_html_esc(pc["summary"])}</td></tr>'

        rows_html += f'''
        <div style="border-left:3px solid {border_color};background:{bg_tint}0a;border-radius:10px;margin-bottom:12px;overflow:hidden;">
          <div style="padding:14px 16px;">
            <div style="display:flex;align-items:center;gap:8px;margin-bottom:8px;">
              <span style="font-size:16px;font-weight:900;letter-spacing:0.02em;">{symbol}</span>
              <span style="font-size:11px;color:#657184;padding:2px 8px;border-radius:999px;background:#f4f8fc;border:1px solid #d9e0ea;">{preset_name}</span>
            </div>
            <table style="width:100%;border-collapse:collapse;">{signals_html}{buy_summary}{state_html}{pc_html}</table>
          </div>
        </div>'''

    if not active:
        rows_html = '<p style="color:#657184;font-size:13px;text-align:center;padding:20px;">— 今日无信号 —</p>'

    subject = "策略信号: " + (", ".join(subject_parts) if subject_parts else "无")
    html = f'''<!DOCTYPE html>
<html><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"></head>
<body style="margin:0;padding:20px;background:#f6f8fb;font-family:'Aptos','Noto Sans SC','Microsoft YaHei',sans-serif;color:#111827;">
<div style="max-width:560px;margin:0 auto;">
  <div style="margin-bottom:20px;padding-bottom:12px;border-bottom:1px solid #d9e0ea;">
    <h1 style="margin:0;font-size:18px;font-weight:900;">🔔 策略信号</h1>
    <p style="margin:4px 0 0;color:#657184;font-size:12px;">自动生成 · 仅供参考</p>
  </div>
  {rows_html}
  <div style="margin-top:16px;padding-top:10px;border-top:1px solid #d9e0ea;color:#657184;font-size:11px;text-align:center;">
    BoolReminder · 策略信号系统
  </div>
</div>
</body></html>'''
    return subject, html
