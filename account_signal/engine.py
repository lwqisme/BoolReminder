"""Generate real-account reminder signals."""

from __future__ import annotations

import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

from config.config_manager import ConfigManager
from drawdown.generate_drawdown_report import (
    PricePoint,
    build_longbridge_quote_context,
    build_price_points_from_series,
    candle_datetime,
    fetch_longbridge_daily_candles,
)
from trade_sync.normalize import canonical_symbol, infer_longbridge_symbol

from account_signal.config import (
    TARGET_SYMBOLS,
    SignalTarget,
    account_strategy_summaries,
    get_runtime_config,
    load_account_config,
)
from account_signal.email import send_account_signal_email
from account_signal.profiles import active_profiles_for_symbols, profiles_status_payload
from account_signal.state import AccountPosition, load_account_positions
from account_signal.strategy_engine import generate_profile_signals
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
    profile_payload = profiles_status_payload(targets)
    status_symbols = sorted(set(profile_payload["enabled_targets"]) | set(profile_payload["active"].keys()))
    positions = load_account_positions(status_symbols)
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
        "profiles": profile_payload,
        "password_required": bool((config_manager or ConfigManager()).get_web_config().get("update_password", "")),
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
    run_id = uuid.uuid4().hex
    generated_at = (now or datetime.now(timezone.utc)).replace(microsecond=0).isoformat()

    account, targets, errors, meta = load_account_config()
    requested_symbols = _normalize_symbols(symbols) if symbols else _default_run_symbols(targets)
    runtime_config = get_runtime_config(manager)
    sync_stale = _sync_is_stale(meta, runtime_config.sync_stale_minutes)
    profiles = active_profiles_for_symbols(requested_symbols)
    positions = load_account_positions(requested_symbols)
    market: dict[str, dict[str, Any]] = {}
    candidates: list[dict[str, Any]] = []
    debug: dict[str, Any] = {}

    global_errors = list(errors)
    errors = []
    if not global_errors:
        points_by_symbol = price_points_by_symbol or _fetch_market_points(requested_symbols)
        for symbol in requested_symbols:
            points = points_by_symbol.get(symbol) or []
            if not points:
                errors.append(f"{symbol} 没有可用行情")
                continue
            target = targets.get(symbol)
            if target is None:
                errors.append(f"signal_targets 缺少 {symbol} 行")
                continue
            if not target.enabled:
                errors.append(f"signal_targets 中 {symbol} 未启用")
                continue
            if target.target_budget_usd <= 0:
                errors.append(f"signal_targets 中 {symbol} initial_investment_usd 必须大于 0")
                continue
            profile = profiles.get(symbol)
            if profile is None:
                errors.append(f"{symbol} 缺少 live profile")
                continue
            position = positions.get(symbol) or AccountPosition(symbol=symbol)
            market[symbol] = _market_payload(points)
            symbol_debug: list[dict[str, Any]] = []
            if target and account:
                candidates.extend(
                    generate_profile_signals(
                        profile=profile,
                        position=position,
                        target=target,
                        account=account,
                        points=points,
                        debug=symbol_debug,
                        fallback_same_day_sell=runtime_config.sell_allow_same_day_sell,
                    )
                )
            if include_debug:
                debug[symbol] = symbol_debug
    else:
        errors = global_errors

    sent_ids = load_sent_signal_ids()
    for signal in candidates:
        signal["signal_id"] = signal_id(signal)
        signal["duplicate"] = signal["signal_id"] in sent_ids
    new_signals = [signal for signal in candidates if not signal.get("duplicate")]

    fatal_errors = bool(global_errors)
    status = (
        "config_error"
        if fatal_errors or (errors and not new_signals)
        else ("partial_signals" if errors and new_signals else ("signals" if new_signals else ("sync_stale" if sync_stale else "no_signal")))
    )
    run_payload: dict[str, Any] = {
        "success": not fatal_errors and (not errors or bool(new_signals)),
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
        "profiles": profiles_status_payload(targets),
        "market": market,
        "signals": candidates,
        "new_signals": new_signals,
        "sync": meta,
    }
    if include_debug:
        run_payload["debug"] = debug

    email_sent = False
    email_message = ""
    if send_email and not dry_run and new_signals and not fatal_errors:
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

def _normalize_symbols(symbols: list[str]) -> list[str]:
    normalized = []
    for symbol in symbols:
        raw = str(symbol or "").strip()
        if not raw:
            continue
        longbridge_symbol = infer_longbridge_symbol(canonical_symbol(raw), "US")
        if longbridge_symbol not in normalized:
            normalized.append(longbridge_symbol)
    return normalized


def _default_run_symbols(targets: dict[str, SignalTarget]) -> list[str]:
    enabled = [symbol for symbol, target in targets.items() if target.enabled]
    return enabled or list(TARGET_SYMBOLS)


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
