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
    inputs = config.to_strategy_inputs()
    longbridge_sym = normalize_longbridge_symbol(sym)

    # 2. Load trade history
    snapshot = load_symbol_snapshot(sym)
    if not snapshot:
        return {"symbol": sym, "preset_id": preset_id, "error": "无交易记录"}

    rows = snapshot.get("rows", [])
    if not rows:
        return {"symbol": sym, "preset_id": preset_id, "error": "交易记录为空"}

    # 3. Load signal targets for initial_cash
    initial_cash = _load_target_allocation(sym, inputs.initial_cash)

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

    # 7. Run simulation (use effective initial_cash from signal targets)
    effective_inputs = replace(inputs, initial_cash=initial_cash)

    target = PortfolioTarget(
        symbol=longbridge_sym,
        weight=100.0,
        name=sym,
        max_drawdown_pct=inputs.max_drawdown_pct if inputs.max_drawdown_pct < 100 else None,
    )

    price_points_by_symbol = {longbridge_sym: points}

    result = _simulate_strategy(
        price_points_by_symbol,
        [target],
        effective_inputs,
        strategy=config.buy_strategy,
        sell_strategy=config.sell_strategy,
        trade_overrides=dict(trade_overrides),
    )

    # 8. Extract signals for today
    signal_trades = [
        t for t in result.get("trades", [])
        if t.get("date") == today.isoformat() and not t.get("is_real")
    ]

    symbol_state = next(
        (s for s in result.get("symbols", []) if s["symbol"] == longbridge_sym),
        {},
    )

    return {
        "symbol": sym,
        "preset_id": preset_id,
        "signal_date": today.isoformat(),
        "buy_strategy": config.buy_strategy,
        "sell_strategy": config.sell_strategy,
        "current_state": {
            "shares": symbol_state.get("shares", 0),
            "cash": symbol_state.get("cash", 0),
            "invested": symbol_state.get("invested", 0),
            "market_value": symbol_state.get("market_value", 0),
            "last_price": symbol_state.get("last_price", 0),
            "avg_cost": symbol_state.get("avg_cost_usd", 0),
        },
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
    """Generate signals for all bound symbols."""
    bindings = load_signal_bindings()
    results: list[dict[str, object]] = []
    for symbol, preset_id in bindings.items():
        try:
            result = generate_signal(symbol, preset_id, signal_date=signal_date, dry_run=dry_run)
        except Exception as exc:
            result = {"symbol": symbol, "preset_id": preset_id, "error": str(exc)}
        results.append(result)
    return results


# ---------------------------------------------------------------------------
# internal helpers
# ---------------------------------------------------------------------------


def _load_target_allocation(symbol: str, fallback: float) -> float:
    """Parse signal_targets sheet for the symbol's 初始投入 (initial allocation)."""
    sym_base = symbol.upper()
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
        if name != sym_base:
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


def _signal_reason(trade: dict[str, object]) -> str:
    """Human-readable reason for a signal trade."""
    action = trade.get("action", "")
    threshold = trade.get("threshold_pct")

    if trade.get("sell_stage"):
        return f"阶梯修复卖出 第{trade['sell_stage']}档"
    if action == "sell" and threshold is not None:
        return f"卖出 {float(threshold):.0f}% 档位"

    drawdown = trade.get("drawdown_pct")
    if action == "buy":
        if drawdown is not None:
            return f"回撤 {float(drawdown)*100:.1f}% 触发买入"
        return "策略买入"

    strategy = trade.get("buy_strategy", "")
    return f"{strategy} 信号"
