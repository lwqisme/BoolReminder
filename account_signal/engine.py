"""Generate GOOGL/TSLA real-account reminder signals."""

from __future__ import annotations

import uuid
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from typing import Any

from config.config_manager import ConfigManager
from drawdown.generate_drawdown_report import (
    PricePoint,
    build_longbridge_quote_context,
    build_price_points_from_series,
    candle_datetime,
    fetch_longbridge_daily_candles,
)
from drawdown.position_strategy import StrategyInputs, build_strategy_tranches
from drawdown.strategy_rules import (
    cost_deleverage_date_cooldown_elapsed,
    select_cost_deleverage_stage,
)

from account_signal.config import (
    TARGET_SYMBOLS,
    AccountSnapshot,
    SignalTarget,
    account_strategy_summaries,
    get_runtime_config,
    googl_inputs,
    load_account_config,
    tsla_inputs,
)
from account_signal.email import send_account_signal_email
from account_signal.state import AccountPosition, load_account_positions, parse_iso_date
from account_signal.state import AccountLot
from account_signal.store import (
    append_sent_signals,
    load_latest_run,
    load_run_history,
    load_sent_signal_ids,
    save_latest_run,
    signal_id,
    utc_now_iso,
)


def account_signal_status(config_manager: ConfigManager | None = None) -> dict[str, Any]:
    account, targets, errors, meta = load_account_config()
    latest = load_latest_run()
    positions = load_account_positions(list(TARGET_SYMBOLS))
    runtime_config = get_runtime_config(config_manager)
    stale = _sync_is_stale(meta, runtime_config.sync_stale_minutes)
    return {
        "success": True,
        "enabled": runtime_config.enabled,
        "status": "config_error" if errors else ("sync_stale" if stale else "ready"),
        "errors": errors,
        "warnings": ["Google Sheets 同步快照可能已过期"] if stale else [],
        "account": account.__dict__ if account else None,
        "targets": {symbol: target.__dict__ for symbol, target in targets.items()},
        "positions": {symbol: position.to_dict() for symbol, position in positions.items()},
        "strategies": account_strategy_summaries(),
        "sync": meta,
        "latest_run": latest,
        "run_history": load_run_history(limit=10),
    }


def run_account_signal(
    *,
    config_manager: ConfigManager | None = None,
    dry_run: bool = True,
    send_email: bool = False,
    symbols: list[str] | None = None,
    include_debug: bool = False,
    price_points_by_symbol: dict[str, list[PricePoint]] | None = None,
    now: datetime | None = None,
) -> dict[str, Any]:
    manager = config_manager or ConfigManager()
    requested_symbols = _normalize_symbols(symbols or list(TARGET_SYMBOLS))
    run_id = uuid.uuid4().hex
    generated_at = (now or datetime.now(timezone.utc)).replace(microsecond=0).isoformat()

    account, targets, errors, meta = load_account_config()
    runtime_config = get_runtime_config(manager)
    sync_stale = _sync_is_stale(meta, runtime_config.sync_stale_minutes)
    positions = load_account_positions(requested_symbols)
    market: dict[str, dict[str, Any]] = {}
    candidates: list[dict[str, Any]] = []
    debug: dict[str, Any] = {}

    if not errors:
        points_by_symbol = price_points_by_symbol or _fetch_market_points(requested_symbols)
        for symbol in requested_symbols:
            points = points_by_symbol.get(symbol) or []
            if not points:
                errors.append(f"{symbol} 没有可用行情")
                continue
            target = targets.get(symbol)
            position = positions.get(symbol) or AccountPosition(symbol=symbol)
            market[symbol] = _market_payload(points)
            symbol_debug: list[dict[str, Any]] = []
            if symbol == "GOOGL.US" and target and account:
                candidates.extend(
                    _googl_signals(
                        position,
                        target,
                        account,
                        points,
                        symbol_debug,
                        allow_same_day_sell=runtime_config.sell_allow_same_day_sell,
                    )
                )
            elif symbol == "TSLA.US" and target and account:
                candidates.extend(
                    _tsla_signals(
                        position,
                        target,
                        account,
                        points,
                        symbol_debug,
                        allow_same_day_sell=runtime_config.sell_allow_same_day_sell,
                    )
                )
            if include_debug:
                debug[symbol] = symbol_debug

    sent_ids = load_sent_signal_ids()
    for signal in candidates:
        signal["signal_id"] = signal_id(signal)
        signal["duplicate"] = signal["signal_id"] in sent_ids
    new_signals = [signal for signal in candidates if not signal.get("duplicate")]

    status = "config_error" if errors else ("signals" if new_signals else ("sync_stale" if sync_stale else "no_signal"))
    run_payload: dict[str, Any] = {
        "success": not errors,
        "run_id": run_id,
        "generated_at": generated_at,
        "dry_run": dry_run,
        "send_email_requested": send_email,
        "status": status,
        "errors": errors,
        "warnings": ["Google Sheets 同步快照可能已过期"] if sync_stale else [],
        "account": account.__dict__ if account else None,
        "targets": {symbol: target.__dict__ for symbol, target in targets.items()},
        "positions": {symbol: position.to_dict() for symbol, position in positions.items()},
        "strategies": account_strategy_summaries(),
        "market": market,
        "signals": candidates,
        "new_signals": new_signals,
        "sync": meta,
    }
    if include_debug:
        run_payload["debug"] = debug

    email_sent = False
    email_message = ""
    if send_email and not dry_run and new_signals and not errors:
        email_sent, email_message = send_account_signal_email(run_payload, new_signals, manager)
    elif send_email and dry_run:
        email_message = "dry_run 不发送邮件"
    elif send_email and not new_signals:
        email_message = "没有新信号，跳过邮件"

    run_payload["email"] = {"sent": email_sent, "message": email_message}

    if not dry_run and new_signals and (not send_email or email_sent):
        append_sent_signals(new_signals, run_id)
        run_payload["ledger_written"] = True
    else:
        run_payload["ledger_written"] = False

    save_latest_run(run_payload)
    return run_payload


def _googl_signals(
    position: AccountPosition,
    target: SignalTarget,
    account: AccountSnapshot,
    points: list[PricePoint],
    debug: list[dict[str, Any]],
    *,
    allow_same_day_sell: bool = False,
) -> list[dict[str, Any]]:
    inputs = replace(googl_inputs(), sell_allow_same_day_sell=allow_same_day_sell)
    point = points[-1]
    drawdown_pct = _point_drawdown_pct(point, inputs)
    trade_date = point.date.date().isoformat()
    signals: list[dict[str, Any]] = []
    available_cash = max(0.0, min(account.cash, account.buying_power))
    reserve_cash = target.target_budget_usd * _core_cash_reserve_ratio(drawdown_pct, inputs)
    spendable_cash = max(0.0, available_cash - reserve_cash)

    if position.shares <= 1e-9:
        amount = min(target.target_budget_usd * 0.95, spendable_cash)
        if amount >= target.min_buy_amount_usd > 0:
            signals.append(
                _buy_signal(
                    symbol="GOOGL.US",
                    strategy="core_dip_dca",
                    stage="initial_core",
                    trade_date=trade_date,
                    point=point,
                    amount=amount,
                    rationale=[
                        "真实账户当前没有 GOOGL 持仓",
                        "初始核心仓按初始投入 95% 计算",
                        f"保留现金垫约 ${reserve_cash:,.2f}",
                    ],
                    confidence="high",
                )
            )
        return signals

    pre_buy_sell_signal = None
    if not allow_same_day_sell:
        pre_buy_sell_signal = _googl_sell_signal(position, points, point, inputs, trade_date, debug)

    weekly_core = max(0.0, target.monthly_contribution_usd / 4.0)
    boost = _core_boost_ratio(drawdown_pct, inputs)
    idle_sweep = max(0.0, spendable_cash - weekly_core) * (0.25 + 0.65 * boost) if drawdown_pct >= 3.0 else 0.0
    amount = min(spendable_cash, weekly_core + idle_sweep)
    timing_allowed, timing_reason = _core_timing_allows_buy(points, inputs, pending_days=1)
    debug.append({"event": "googl_buy_check", "amount": amount, "timing_allowed": timing_allowed, "timing_reason": timing_reason})
    buy_signal = None
    if amount >= target.min_buy_amount_usd > 0 and timing_allowed and not _bought_this_week(position, point.date.date()):
        buy_signal = _buy_signal(
            symbol="GOOGL.US",
            strategy="core_dip_dca",
            stage="weekly_core" if drawdown_pct < 3.0 else "weekly_core_dip",
            trade_date=trade_date,
            point=point,
            amount=amount,
            rationale=[
                f"周投核心金额 ${weekly_core:,.2f}",
                f"rolling_120 回撤 {drawdown_pct:.2f}%",
                f"买点优化: {timing_reason}",
            ],
            confidence="high" if drawdown_pct >= 3.0 or timing_reason in {"down_day", "near_recent_low"} else "medium",
        )

    if allow_same_day_sell and buy_signal:
        signals.append(buy_signal)
        estimated = _position_after_estimated_buy(position, point, float(buy_signal["amount_usd"]), trade_date, inputs)
        sell_signal = _googl_sell_signal(
            estimated,
            points,
            point,
            inputs,
            trade_date,
            debug,
            estimated_same_day_buy=True,
        )
        if sell_signal:
            signals.append(sell_signal)
    else:
        sell_signal = (
            _googl_sell_signal(position, points, point, inputs, trade_date, debug)
            if allow_same_day_sell and buy_signal is None
            else pre_buy_sell_signal
        )
        if sell_signal:
            signals.append(sell_signal)
        if buy_signal:
            signals.append(buy_signal)
    return signals


def _position_after_estimated_buy(
    position: AccountPosition,
    point: PricePoint,
    amount: float,
    trade_date: str,
    inputs: StrategyInputs,
) -> AccountPosition:
    price = float(point.close)
    if amount <= 0 or price <= 0:
        return position
    shares = amount / price
    drawdown_pct = _point_drawdown_pct(point, inputs)
    lots = [
        AccountLot(
            buy_date=lot.buy_date,
            buy_price=lot.buy_price,
            initial_shares=lot.initial_shares,
            remaining_shares=lot.remaining_shares,
            amount=lot.amount,
            buy_drawdown_pct=lot.buy_drawdown_pct,
        )
        for lot in position.lots
    ]
    lots.append(
        AccountLot(
            buy_date=trade_date,
            buy_price=price,
            initial_shares=shares,
            remaining_shares=shares,
            amount=amount,
            buy_drawdown_pct=drawdown_pct,
        )
    )
    marks = set(position.cost_deleverage_marks)
    if marks and drawdown_pct + 1e-9 >= min(inputs.max_drawdown_pct, max(0.0, inputs.dca_rearm_drawdown_pct)):
        marks.clear()
    return AccountPosition(
        symbol=position.symbol,
        shares=position.shares + shares,
        lots=lots,
        buy_events=[*position.buy_events, {"trade_date": trade_date, "side": "buy", "shares": shares, "price": price, "amount": amount}],
        sell_events=list(position.sell_events),
        cost_deleverage_marks=marks,
        grid_rebound_marks=set(position.grid_rebound_marks),
        last_cost_deleverage_sell_date=position.last_cost_deleverage_sell_date,
        last_sell_date=position.last_sell_date,
    )


def _googl_sell_signal(
    position: AccountPosition,
    points: list[PricePoint],
    point: PricePoint,
    inputs: StrategyInputs,
    trade_date: str,
    debug: list[dict[str, Any]],
    *,
    estimated_same_day_buy: bool = False,
) -> dict[str, Any] | None:
    if position.shares <= 0 or position.avg_cost <= 0:
        return None
    if not cost_deleverage_date_cooldown_elapsed(
        parse_iso_date(position.last_cost_deleverage_sell_date),
        current_date=point.date.date(),
        cooldown_days=inputs.cost_deleverage_cooldown_days,
    ):
        debug.append({"event": "googl_sell_cooldown", "last_sell": position.last_cost_deleverage_sell_date})
        return None
    current_price = float(point.close)
    profit_pct = current_price / position.avg_cost * 100.0 - 100.0
    marks = _googl_active_cost_marks(position, points, point, inputs, debug)
    stage = select_cost_deleverage_stage(inputs=inputs, active_marks=marks, profit_pct=profit_pct)
    if stage is None:
        return None
    shares = position.shares * stage.sell_pct / 100.0
    basis_text = "基于同日买入后估算" if estimated_same_day_buy else "相对真实剩余均价盈利"
    rationale = [
        f"{basis_text} {profit_pct:.2f}%",
        f"触发 {stage.profit_pct:g}% 盈利档，建议卖出当前剩余持仓 {stage.sell_pct:g}%",
        "成本去杠杆冷却已满足",
    ]
    if estimated_same_day_buy:
        rationale.append("基于同日买入后估算，需以真实成交后的持仓为准")
    return _sell_signal(
        symbol="GOOGL.US",
        strategy="cost_deleverage",
        stage=stage.mark,
        trade_date=trade_date,
        point=point,
        shares=shares,
        rationale=rationale,
    )
    return None


def _googl_active_cost_marks(
    position: AccountPosition,
    points: list[PricePoint],
    point: PricePoint,
    inputs: StrategyInputs,
    debug: list[dict[str, Any]],
) -> set[str]:
    marks = set(position.cost_deleverage_marks)
    if not marks:
        return marks
    current_drawdown = _point_drawdown_pct(point, inputs)
    sell_context = _latest_googl_cost_sell_context(position, points, inputs)
    if sell_context is not None:
        rearm_drawdown = min(inputs.max_drawdown_pct, sell_context["drawdown_pct"] + inputs.dca_rearm_drawdown_pct)
        if current_drawdown + 1e-9 >= rearm_drawdown:
            debug.append(
                {
                    "event": "googl_cost_marks_rearmed",
                    "basis": "last_real_sell_drawdown",
                    "last_sell_date": sell_context["trade_date"],
                    "last_sell_drawdown_pct": sell_context["drawdown_pct"],
                    "rearm_drawdown_pct": rearm_drawdown,
                    "current_drawdown_pct": current_drawdown,
                }
            )
            return set()
        debug.append(
            {
                "event": "googl_cost_marks_retained",
                "basis": "last_real_sell_drawdown",
                "last_sell_date": sell_context["trade_date"],
                "last_sell_drawdown_pct": sell_context["drawdown_pct"],
                "rearm_drawdown_pct": rearm_drawdown,
                "current_drawdown_pct": current_drawdown,
            }
        )
        return marks

    if current_drawdown + 1e-9 >= inputs.dca_rearm_drawdown_pct:
        debug.append(
            {
                "event": "googl_cost_marks_rearmed",
                "basis": "current_drawdown_fallback",
                "current_drawdown_pct": current_drawdown,
            }
        )
        return set()
    return marks


def _latest_googl_cost_sell_context(
    position: AccountPosition,
    points: list[PricePoint],
    inputs: StrategyInputs,
) -> dict[str, Any] | None:
    drawdown_by_day = {item.date.date().isoformat(): _point_drawdown_pct(item, inputs) for item in points}
    for event in reversed(position.sell_events):
        if float(event.get("profit_pct") or 0.0) + 1e-9 < inputs.cost_first_profit_pct:
            continue
        trade_date = str(event.get("trade_date", "") or "")
        if not trade_date or trade_date not in drawdown_by_day:
            continue
        return {"trade_date": trade_date, "drawdown_pct": drawdown_by_day[trade_date]}
    return None


def _tsla_signals(
    position: AccountPosition,
    target: SignalTarget,
    account: AccountSnapshot,
    points: list[PricePoint],
    debug: list[dict[str, Any]],
    *,
    allow_same_day_sell: bool = False,
) -> list[dict[str, Any]]:
    inputs = replace(tsla_inputs(), sell_allow_same_day_sell=allow_same_day_sell)
    point = points[-1]
    trade_date = point.date.date().isoformat()
    drawdown_pct = _point_drawdown_pct(point, inputs)
    signals: list[dict[str, Any]] = []
    _attach_buy_drawdowns(position, points, inputs)

    sell_signal = _tsla_sell_signal(position, point, inputs, trade_date) if not allow_same_day_sell else None

    completed = _tsla_completed_buy_thresholds(position, points, inputs)
    tranches = build_strategy_tranches(inputs, "linear_weighted_slice")
    available_cash = max(0.0, min(account.cash, account.buying_power))
    crossed_thresholds: list[float] = []
    crossed_allocations: list[float] = []
    trigger_events: list[dict[str, Any]] = []
    total_amount = 0.0
    for tranche in tranches:
        threshold = round(tranche.threshold_pct, 8)
        if threshold in completed or drawdown_pct + 1e-9 < tranche.threshold_pct:
            continue
        amount = min(available_cash, target.target_budget_usd * tranche.allocation_pct / 100.0)
        debug.append({"event": "tsla_buy_cross", "threshold": tranche.threshold_pct, "amount": amount})
        if amount <= 0:
            continue
        crossed_thresholds.append(tranche.threshold_pct)
        crossed_allocations.append(tranche.allocation_pct)
        trigger_events.append(
            {
                "stage": f"dd_{tranche.threshold_pct:g}",
                "threshold_pct": tranche.threshold_pct,
                "allocation_pct": tranche.allocation_pct,
                "amount_usd": round(float(amount), 2),
                "trade_date": trade_date,
                "price": float(point.close),
                "drawdown_pct": drawdown_pct,
            }
        )
        total_amount += amount
        available_cash = max(0.0, available_cash - amount)

    if total_amount > 0 and total_amount < target.min_buy_amount_usd:
        debug.append(
            {
                "event": "tsla_buy_filtered_min_amount",
                "amount": total_amount,
                "min_buy_amount_usd": target.min_buy_amount_usd,
                "thresholds": crossed_thresholds,
                "suppressed_trigger_events": trigger_events,
            }
        )
    elif total_amount >= target.min_buy_amount_usd and crossed_thresholds:
        stage = (
            f"dd_{crossed_thresholds[0]:g}"
            if len(crossed_thresholds) == 1
            else f"dd_{crossed_thresholds[0]:g}_{crossed_thresholds[-1]:g}"
        )
        threshold_text = ", ".join(f"{item:g}%" for item in crossed_thresholds)
        buy_signal = _buy_signal(
            symbol="TSLA.US",
            strategy="linear_weighted_slice",
            stage=stage,
            trade_date=trade_date,
            point=point,
            amount=total_amount,
            rationale=[
                f"当前 rolling_120 回撤 {drawdown_pct:.2f}% 穿越 {threshold_text} 档",
                f"同日未完成档位已聚合，合计权重 {sum(crossed_allocations):.2f}%",
                f"聚合金额 ${total_amount:,.2f} 已达到最小提醒额 ${target.min_buy_amount_usd:,.2f}",
            ],
            confidence="high" if drawdown_pct >= 15.0 else "medium",
            trigger_events=trigger_events,
        )
        if allow_same_day_sell:
            signals.append(buy_signal)
            estimated = _position_after_estimated_buy(position, point, float(buy_signal["amount_usd"]), trade_date, inputs)
            estimated_sell = _tsla_sell_signal(
                estimated,
                point,
                inputs,
                trade_date,
                estimated_same_day_buy=True,
            )
            if estimated_sell:
                signals.append(estimated_sell)
        else:
            if sell_signal:
                signals.append(sell_signal)
            signals.append(buy_signal)
    elif sell_signal:
        signals.append(sell_signal)
    elif allow_same_day_sell:
        sell_signal = _tsla_sell_signal(position, point, inputs, trade_date)
        if sell_signal:
            signals.append(sell_signal)
    return signals


def _tsla_sell_signal(
    position: AccountPosition,
    point: PricePoint,
    inputs: StrategyInputs,
    trade_date: str,
    *,
    estimated_same_day_buy: bool = False,
) -> dict[str, Any] | None:
    if position.shares <= 0 or position.avg_cost <= 0:
        return None
    current_price = float(point.close)
    profit_pct = current_price / position.avg_cost * 100.0 - 100.0
    if profit_pct + 1e-9 < 10.0:
        return None
    avg_buy_drawdown = _avg_lot_buy_drawdown(position)
    if avg_buy_drawdown <= 0:
        return None
    current_drawdown = _point_drawdown_pct(point, inputs)
    stages = [
        ("grid_1", max(0.0, avg_buy_drawdown - 2.5), 10.0),
        ("grid_2", max(0.0, avg_buy_drawdown - 5.0), 40.0),
    ]
    for stage, threshold, sell_pct in stages:
        if stage in position.grid_rebound_marks or current_drawdown > threshold + 1e-9:
            continue
        shares = position.shares * sell_pct / 100.0
        return _sell_signal(
            symbol="TSLA.US",
            strategy="grid_rebound",
            stage=stage,
            trade_date=trade_date,
            point=point,
            shares=shares,
            rationale=[
                (
                    f"基于同日买入后估算，lot 加权买入回撤 {avg_buy_drawdown:.2f}%"
                    if estimated_same_day_buy
                    else f"真实 lot 加权买入回撤 {avg_buy_drawdown:.2f}%"
                ),
                f"当前回撤修复到 {current_drawdown:.2f}%，触发阈值 {threshold:.2f}%",
                (
                    f"基于同日买入后估算 {profit_pct:.2f}%，满足 10% 门槛"
                    if estimated_same_day_buy
                    else f"相对真实剩余均价盈利 {profit_pct:.2f}%，满足 10% 门槛"
                ),
            ],
        )
    return None


def _fetch_market_points(symbols: list[str]) -> dict[str, list[PricePoint]]:
    quote_ctx = build_longbridge_quote_context()
    today = datetime.now().date()
    start = today - timedelta(days=365 * 3)
    result: dict[str, list[PricePoint]] = {}
    for symbol in symbols:
        candles = fetch_longbridge_daily_candles(quote_ctx, symbol, start, today)
        series = [(candle_datetime(candle).replace(tzinfo=None), float(candle.close)) for candle in candles]
        current = _fetch_current_quote(quote_ctx, symbol)
        if current and current > 0:
            today_dt = datetime.combine(today, datetime.min.time())
            series = [item for item in series if item[0].date() != today]
            series.append((today_dt, current))
        result[symbol] = build_price_points_from_series(series)
    return result


def _fetch_current_quote(quote_ctx: object, symbol: str) -> float | None:
    try:
        quotes = quote_ctx.quote([symbol])
        if quotes:
            value = getattr(quotes[0], "last_done", None)
            return float(value) if value not in ("", None) else None
    except Exception:
        return None
    return None


def _buy_signal(
    *,
    symbol: str,
    strategy: str,
    stage: str,
    trade_date: str,
    point: PricePoint,
    amount: float,
    rationale: list[str],
    confidence: str,
    trigger_events: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    signal = {
        "symbol": symbol,
        "action": "buy",
        "strategy": strategy,
        "stage": stage,
        "trade_date": trade_date,
        "price": float(point.close),
        "drawdown_pct": _point_drawdown_pct(point, googl_inputs() if symbol == "GOOGL.US" else tsla_inputs()),
        "amount_usd": round(float(amount), 2),
        "confidence": confidence,
        "rationale": rationale,
    }
    if trigger_events:
        signal["trigger_events"] = trigger_events
    if confidence == "high":
        leaps_triggers = trigger_events or [
            {
                "stage": stage,
                "threshold_pct": signal["drawdown_pct"],
                "allocation_pct": None,
                "amount_usd": signal["amount_usd"],
                "trade_date": trade_date,
                "price": float(point.close),
                "drawdown_pct": signal["drawdown_pct"],
            }
        ]
        signal["leaps"] = {
            "enabled": True,
            "target_dte": "180-540",
            "stock_entry": f"${float(point.close):.2f}",
            "drawdown_pct": signal["drawdown_pct"],
            "trigger_count": len(leaps_triggers),
            "triggers": leaps_triggers,
        }
    return signal


def _sell_signal(
    *,
    symbol: str,
    strategy: str,
    stage: str,
    trade_date: str,
    point: PricePoint,
    shares: float,
    rationale: list[str],
) -> dict[str, Any]:
    return {
        "symbol": symbol,
        "action": "sell",
        "strategy": strategy,
        "stage": stage,
        "trade_date": trade_date,
        "price": float(point.close),
        "drawdown_pct": _point_drawdown_pct(point, googl_inputs() if symbol == "GOOGL.US" else tsla_inputs()),
        "shares": round(float(shares), 6),
        "confidence": "high",
        "rationale": rationale,
    }


def _normalize_symbols(symbols: list[str]) -> list[str]:
    normalized = []
    for symbol in symbols:
        raw = str(symbol or "").strip().upper()
        if not raw:
            continue
        if "." not in raw:
            raw = f"{raw}.US"
        if raw in TARGET_SYMBOLS and raw not in normalized:
            normalized.append(raw)
    return normalized or list(TARGET_SYMBOLS)


def _sync_is_stale(meta: dict[str, Any], stale_minutes: int) -> bool:
    if stale_minutes <= 0:
        return False
    saved_values = [meta.get("account_saved_at"), meta.get("targets_saved_at")]
    if any(not value for value in saved_values):
        return False
    cutoff = datetime.now(timezone.utc) - timedelta(minutes=stale_minutes)
    for value in saved_values:
        try:
            saved_at = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
        except ValueError:
            return True
        if saved_at.tzinfo is None:
            saved_at = saved_at.replace(tzinfo=timezone.utc)
        if saved_at < cutoff:
            return True
    return False


def _market_payload(points: list[PricePoint]) -> dict[str, Any]:
    point = points[-1]
    return {
        "trade_date": point.date.date().isoformat(),
        "price": float(point.close),
        "drawdown_ath_pct": abs(float(point.drawdown_ath) * 100.0),
        "drawdown_120_pct": abs(float(point.drawdown_120) * 100.0),
        "rolling_120_peak": float(point.rolling_120_peak),
    }


def _point_drawdown_pct(point: PricePoint, inputs: StrategyInputs) -> float:
    if inputs.drawdown_basis == "rolling_120":
        return abs(float(point.drawdown_120) * 100.0)
    return abs(float(point.drawdown_ath) * 100.0)


def _core_boost_ratio(drawdown_pct: float, inputs: StrategyInputs) -> float:
    start = inputs.core_dip_start_drawdown_pct
    full = inputs.core_dip_full_drawdown_pct
    if drawdown_pct <= start:
        return 0.0
    if drawdown_pct >= full:
        return 1.0
    return (drawdown_pct - start) / (full - start)


def _core_cash_reserve_ratio(drawdown_pct: float, inputs: StrategyInputs) -> float:
    base = max(0.0, min(1.0, inputs.core_dip_cash_reserve_pct / 100.0))
    return max(0.01, base * (1.0 - _core_boost_ratio(drawdown_pct, inputs) * 0.85))


def _core_timing_allows_buy(points: list[PricePoint], inputs: StrategyInputs, pending_days: int) -> tuple[bool, str]:
    if not inputs.core_dip_timing_enabled:
        return True, "disabled"
    if len(points) < 2:
        return True, "insufficient_history"
    point = points[-1]
    drawdown_pct = _point_drawdown_pct(point, inputs)
    if drawdown_pct >= inputs.core_dip_start_drawdown_pct:
        return True, "drawdown_reached"
    if pending_days >= inputs.core_dip_timing_max_delay_days:
        return True, "delay_expired"
    recent = [float(item.close) for item in points[-6:] if item.close > 0]
    previous_close = recent[-2]
    day_change_pct = (float(point.close) / previous_close - 1.0) * 100.0 if previous_close > 0 else 0.0
    recent_low = min(recent)
    distance_from_low_pct = (float(point.close) / recent_low - 1.0) * 100.0 if recent_low > 0 else 0.0
    if day_change_pct <= 0:
        return True, "down_day"
    if distance_from_low_pct <= inputs.core_dip_timing_near_low_pct:
        return True, "near_recent_low"
    if day_change_pct >= inputs.core_dip_timing_rise_threshold_pct:
        return False, "defer_after_rise"
    return True, "normal"


def _bought_this_week(position: AccountPosition, current_day: date) -> bool:
    current_year_week = current_day.isocalendar()[:2]
    for event in position.buy_events:
        event_day = parse_iso_date(str(event.get("trade_date", "") or ""))
        if event_day and event_day.isocalendar()[:2] == current_year_week:
            return True
    return False


def _cooldown_elapsed(last_sell_date: str | None, *, points_date: date, cooldown_days: int) -> bool:
    last = parse_iso_date(last_sell_date)
    if not last:
        return True
    return (points_date - last).days >= cooldown_days


def _tsla_completed_buy_thresholds(
    position: AccountPosition,
    points: list[PricePoint],
    inputs: StrategyInputs,
) -> set[float]:
    drawdown_by_day = {point.date.date().isoformat(): _point_drawdown_pct(point, inputs) for point in points}
    thresholds = [round(item.threshold_pct, 8) for item in build_strategy_tranches(inputs, "linear_weighted_slice")]
    completed: set[float] = set()
    for event in position.buy_events:
        buy_day = str(event.get("trade_date", "") or "")
        buy_drawdown = drawdown_by_day.get(buy_day)
        if buy_drawdown is None:
            continue
        for threshold in thresholds:
            if buy_drawdown + 1e-9 >= threshold:
                completed.add(threshold)
    return completed


def _attach_buy_drawdowns(position: AccountPosition, points: list[PricePoint], inputs: StrategyInputs) -> None:
    drawdown_by_day = {point.date.date().isoformat(): _point_drawdown_pct(point, inputs) for point in points}
    for lot in position.lots:
        if lot.buy_drawdown_pct is None and lot.buy_date in drawdown_by_day:
            lot.buy_drawdown_pct = drawdown_by_day[lot.buy_date]
    for event in position.buy_events:
        buy_day = str(event.get("trade_date", "") or "")
        if "buy_drawdown_pct" not in event and buy_day in drawdown_by_day:
            event["buy_drawdown_pct"] = drawdown_by_day[buy_day]


def _avg_lot_buy_drawdown(position: AccountPosition) -> float:
    total_shares = 0.0
    weighted = 0.0
    for lot in position.lots:
        if lot.remaining_shares <= 0 or lot.buy_drawdown_pct is None:
            continue
        total_shares += lot.remaining_shares
        weighted += lot.remaining_shares * lot.buy_drawdown_pct
    if total_shares <= 0:
        buy_drawdowns = [
            float(event.get("buy_drawdown_pct", 0.0) or 0.0)
            for event in position.buy_events
            if event.get("buy_drawdown_pct") is not None
        ]
        return sum(buy_drawdowns) / len(buy_drawdowns) if buy_drawdowns else 0.0
    return weighted / total_shares
