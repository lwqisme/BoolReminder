"""LEAPS option signal generation for real account positions.

Parses option contract codes, loads real option trades from synced data,
and generates entry/sell signals using GA-optimized LEAPS parameters.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Any


# ── Contract code parsing ────────────────────────────────────────────────

# Pattern: TICKER + YYMMDD + (C|P) + STRIKE*1000 (zero-padded to 8 digits) + .US
_CONTRACT_RE = re.compile(
    r"^([A-Z]+)(\d{2})(\d{2})(\d{2})([CP])(\d{8})\.US$"
)


@dataclass
class OptionContract:
    """Parsed option contract."""
    contract_code: str
    underlying: str
    expiration: date
    option_type: str  # "call" or "put"
    strike: float


def parse_option_contract(code: str) -> OptionContract | None:
    """Parse an OCC-style option contract code.

    Examples:
        AMZN260618C00240000.US → AMZN.US call 2026-06-18 strike 240.00
        NVDA251219P00150000.US → NVDA.US put 2025-12-19 strike 150.00
    """
    if not code:
        return None
    code = code.strip().upper()
    m = _CONTRACT_RE.match(code)
    if not m:
        return None

    ticker = m.group(1)
    yy = int(m.group(2))
    mm = int(m.group(3))
    dd = int(m.group(4))
    opt_type = "call" if m.group(5) == "C" else "put"
    strike_raw = m.group(6)

    # Strike is stored as strike * 1000, zero-padded to 8 digits
    strike = int(strike_raw) / 1000.0

    # Year: 00-99 → 2000-2099
    full_year = 2000 + yy

    try:
        expiration = date(full_year, mm, dd)
    except ValueError:
        return None

    return OptionContract(
        contract_code=code,
        underlying=f"{ticker}.US",
        expiration=expiration,
        option_type=opt_type,
        strike=strike,
    )


# ── Option trade data ────────────────────────────────────────────────────

@dataclass
class OptionTrade:
    """A single option trade event."""
    contract_code: str
    underlying: str
    trade_date: date
    side: str  # "buy" or "sell"
    quantity: float
    option_price: float
    amount: float
    expiration: date
    strike: float
    option_type: str


@dataclass
class OpenPosition:
    """A currently open LEAPS position."""
    contract_code: str
    underlying: str
    entry_date: date
    entry_stock_price: float | None  # underlying stock price at entry
    option_buy_price: float
    total_quantity: float  # remaining (net buy - sell)
    expiration: date
    strike: float
    option_type: str
    total_sold: float = 0.0  # cumulative quantity sold


def load_option_trades(
    rows: list[dict[str, Any]],
    underlying_symbol: str,
) -> list[OptionTrade]:
    """Extract option trades for a given underlying from synced trade rows.

    Args:
        rows: Normalized trade rows (from trade_sync by_symbol).
        underlying_symbol: e.g. "AMZN.US".

    Returns:
        List of OptionTrade sorted by trade_date.
    """
    trades: list[OptionTrade] = []
    sym_upper = underlying_symbol.upper()

    for row in rows:
        symbol = str(row.get("symbol", "")).strip().upper()
        contract = parse_option_contract(symbol)
        if not contract:
            continue
        if contract.underlying != sym_upper:
            continue

        side = str(row.get("side", "")).strip().lower()
        if side not in ("buy", "sell"):
            continue

        trade_date_str = str(row.get("trade_date", ""))[:10]
        try:
            trade_date = date.fromisoformat(trade_date_str)
        except (ValueError, TypeError):
            continue

        quantity = float(row.get("shares", 0) or 0)
        if quantity <= 0:
            continue

        option_price = float(row.get("price", 0) or 0)
        amount = float(row.get("amount", 0) or 0)
        if amount <= 0:
            amount = quantity * option_price

        trades.append(OptionTrade(
            contract_code=contract.contract_code,
            underlying=contract.underlying,
            trade_date=trade_date,
            side=side,
            quantity=quantity,
            option_price=option_price,
            amount=amount,
            expiration=contract.expiration,
            strike=contract.strike,
            option_type=contract.option_type,
        ))

    trades.sort(key=lambda t: (t.trade_date, t.side == "sell"))
    return trades


def compute_open_positions(
    trades: list[OptionTrade],
    as_of: date | None = None,
) -> list[OpenPosition]:
    """Compute currently open LEAPS positions from trade history.

    Net buys per contract code. Only contracts with remaining quantity > 0
    and expiration > as_of.
    """
    today = as_of or date.today()
    by_contract: dict[str, dict[str, Any]] = {}

    for t in trades:
        key = t.contract_code
        if key not in by_contract:
            by_contract[key] = {
                "entry_date": t.trade_date,
                "entry_stock_price": None,
                "option_buy_price": t.option_price if t.side == "buy" else 0,
                "total_quantity": 0,
                "total_sold": 0,
                "expiration": t.expiration,
                "strike": t.strike,
                "option_type": t.option_type,
                "contract_code": t.contract_code,
                "underlying": t.underlying,
            }

        pos = by_contract[key]
        if t.side == "buy":
            pos["total_quantity"] += t.quantity
            pos["entry_date"] = min(pos["entry_date"], t.trade_date)
            pos["option_buy_price"] = (
                (pos["option_buy_price"] * (pos["total_quantity"] - t.quantity) + t.option_price * t.quantity)
                / pos["total_quantity"]
                if pos["total_quantity"] > 0 else t.option_price
            )
        else:
            pos["total_sold"] += t.quantity
            pos["total_quantity"] -= t.quantity

    positions: list[OpenPosition] = []
    for key, pos in by_contract.items():
        remaining = pos["total_quantity"]
        if remaining <= 0.001:
            continue
        # Check not expired
        if today > pos["expiration"]:
            continue
        positions.append(OpenPosition(
            contract_code=pos["contract_code"],
            underlying=pos["underlying"],
            entry_date=pos["entry_date"],
            entry_stock_price=pos["entry_stock_price"],
            option_buy_price=pos["option_buy_price"],
            total_quantity=remaining,
            expiration=pos["expiration"],
            strike=pos["strike"],
            option_type=pos["option_type"],
            total_sold=pos["total_sold"],
        ))

    return positions


# ── Signal result ─────────────────────────────────────────────────────────

@dataclass
class EntrySignal:
    """LEAPS call entry signal."""
    underlying: str
    date: str
    stock_price: float
    drawdown_pct: float
    bollinger_score: float
    composite_score: float
    reason: str


@dataclass
class SellSignal:
    """LEAPS sell signal for an open position."""
    contract_code: str
    date: str
    stock_price: float
    option_roi_pct: float
    pct_to_sell: float
    stage: int
    reason: str


@dataclass
class LeapsSignalResult:
    """Aggregated LEAPS signals for one symbol."""
    symbol: str
    preset_id: str
    signal_date: date
    entry_signals: list[EntrySignal]
    sell_signals: list[SellSignal]
    open_positions: list[OpenPosition]
    errors: list[str] = field(default_factory=list)


# ── Signal generation ─────────────────────────────────────────────────────

def detect_entry_signals(
    prices: list[tuple[date, float]],
    option_trades: list[OptionTrade],
    *,
    drawdown_threshold_pct: float = 20.0,
    entry_mode: str = "both",
    cooldown_days: int = 5,
) -> list[EntrySignal]:
    """Detect LEAPS call entry signals from price data, respecting cooldown.

    Args:
        prices: (date, price) sorted ascending, at least 122 data points.
        option_trades: Real LEAPS option trades for cooldown calculation.
        drawdown_threshold_pct: Minimum drawdown from 120d high.
        entry_mode: "touch", "bounce", or "both".
        cooldown_days: Days after last buy to block new entries.

    Returns:
        EntrySignal list for dates where conditions are met.
    """
    from drawdown.leaps_option_ga import detect_leaps_entries

    if len(prices) < 122:
        return []

    # Detect raw entry signals
    entries = detect_leaps_entries(prices, drawdown_threshold_pct, entry_mode)
    if not entries:
        return []

    # Find last buy date for cooldown
    last_buy_date: date | None = None
    buys = [t for t in option_trades if t.side == "buy"]
    if buys:
        last_buy_date = max(t.trade_date for t in buys)

    # Only the most recent price date matters for today's signal
    today = prices[-1][0]

    # Check cooldown
    if last_buy_date and (today - last_buy_date).days < cooldown_days:
        return []

    # Return only the latest entry (today's signal)
    latest = [e for e in entries if e.date == today]
    if not latest:
        return []

    signals: list[EntrySignal] = []
    for e in latest:
        signals.append(EntrySignal(
            underlying="",  # filled by caller
            date=e.date.isoformat(),
            stock_price=e.price,
            drawdown_pct=e.drawdown_pct,
            bollinger_score=e.bollinger_score,
            composite_score=e.composite_score,
            reason=f"回撤{e.drawdown_pct:.1f}% 布林{e.bollinger_score:.2f}",
        ))
    return signals


def detect_sell_signals(
    positions: list[OpenPosition],
    *,
    current_stock_price: float,
    current_date: date,
    stages: list[tuple[int, float, float]],
) -> list[SellSignal]:
    """Detect sell signals for open LEAPS positions (delegates to unified engine).

    Legacy API kept for test backward compatibility.
    """
    from drawdown.leaps_option_ga import proxy_option_roi

    signals: list[SellSignal] = []
    for pos in positions:
        if pos.option_type != "call":
            continue
        hold_days = (current_date - pos.entry_date).days
        roi = proxy_option_roi(
            entry_price=pos.entry_stock_price or current_stock_price,
            exit_price=current_stock_price,
            entry_date=pos.entry_date,
            exit_date=current_date,
            expiration=pos.expiration,
            strike_price=pos.strike,
        )
        for stage_idx, (min_hold, profit_threshold, sell_fraction) in enumerate(stages):
            if hold_days < min_hold:
                continue
            if roi < profit_threshold:
                continue
            signals.append(SellSignal(
                contract_code=pos.contract_code,
                date=current_date.isoformat(),
                stock_price=current_stock_price,
                option_roi_pct=round(roi, 2),
                pct_to_sell=sell_fraction,
                stage=stage_idx + 1,
                reason=f"S{stage_idx+1} 持有{hold_days}天 ROA{roi:.0f}%≥{profit_threshold:.0f}% 建议卖{sell_fraction:.0f}%",
            ))
            break
    return signals


def compute_leaps_sell_signals(
    position: OpenPosition,
    prices: list[tuple[date, float]],
    option_trades: list[OptionTrade],
    stages: list[tuple[int, float, float]],
    current_date: date,
) -> list[SellSignal]:
    """Compute sell signals for a LEAPS position, aligned with GA compute_sell_ladder.

    Delegates to compute_sell_ladder with real trade overrides for partial
    execution tracking (方案 B), then filters results to current_date.
    """
    from drawdown.leaps_option_ga import compute_sell_ladder, LeapsEntrySignal

    if not prices:
        return []

    entry_date = position.entry_date
    entry_price = position.entry_stock_price or prices[0][1]

    # Build trade overrides: aggregate sell quantity per day as pct of position
    initial_qty = position.total_quantity + (position.total_sold or 0.0)
    if initial_qty <= 0:
        return []
    trade_overrides: dict[date, float] = {}
    for t in option_trades:
        if t.side == "sell" and t.contract_code == position.contract_code:
            trade_overrides[t.trade_date] = trade_overrides.get(t.trade_date, 0.0) + t.quantity / initial_qty * 100.0

    entry = LeapsEntrySignal(
        date=entry_date,
        price=entry_price,
        drawdown_pct=0.0,
        bollinger_score=0.0,
        composite_score=0.0,
    )

    trade = compute_sell_ladder(
        entry, prices, stages,
        expiration_days=(position.expiration - entry_date).days,
        strike_price=position.strike,
        trade_overrides=trade_overrides if trade_overrides else None,
        allow_open=True,
    )

    # Filter sell events to current_date and map to SellSignal
    effective_stages = list(stages)
    while len(effective_stages) < 3:
        effective_stages.append((9999, 0.0, 100.0))

    # Determine stage for each sell event by cumulative threshold matching
    cum = sum(trade_overrides.values()) if trade_overrides else 0.0
    signals: list[SellSignal] = []
    for se in trade.sell_events:
        if se.date != current_date:
            cum += se.pct_sold
            continue

        before = cum
        after = cum + se.pct_sold
        stage_num = len(effective_stages)  # default: last stage (force-sell)
        cum_stages = 0.0
        for s_idx, (_, _, s_pct) in enumerate(effective_stages):
            cum_stages += s_pct
            if after <= cum_stages + 0.01:
                stage_num = s_idx + 1
                break
        cum += se.pct_sold

        hold_days = (current_date - entry_date).days
        if stage_num <= len(effective_stages):
            _, profit_threshold, _ = effective_stages[stage_num - 1]
            reason = f"S{stage_num} 持有{hold_days}天 ROA{se.roi_pct:.0f}%≥{profit_threshold:.0f}% 建议卖{se.pct_sold:.0f}%"
        else:
            reason = f"到期强平 ROA{se.roi_pct:.0f}% 卖剩余{se.pct_sold:.0f}%"

        signals.append(SellSignal(
            contract_code=position.contract_code,
            date=se.date.isoformat(),
            stock_price=se.price,
            option_roi_pct=se.roi_pct,
            pct_to_sell=se.pct_sold,
            stage=stage_num,
            reason=reason,
        ))

    return signals


def generate_leaps_signals_for_symbol(
    symbol: str,
    preset_id: str,
    *,
    signal_date: date | None = None,
    dry_run: bool = False,
) -> LeapsSignalResult:
    """Generate LEAPS entry and sell signals for a symbol using a preset.

    Args:
        symbol: Underlying symbol e.g. "AMZN.US".
        preset_id: LEAPS preset ID from strategy_lab_presets.
        signal_date: Date to generate signals for (default: today).
        dry_run: If True, don't send email.

    Returns:
        LeapsSignalResult with entry/sell signals and open positions.
    """
    from drawdown.strategy_lab_history import load_experiment_preset
    from trade_sync.store import load_symbol_snapshot
    from trade_sync.normalize import canonical_symbol, infer_longbridge_symbol

    errors: list[str] = []
    today = signal_date or date.today()
    sym = canonical_symbol(symbol)

    # 1. Load preset
    preset = load_experiment_preset(preset_id)
    if not preset:
        return LeapsSignalResult(sym, preset_id, today, [], [], [], ["预设不存在"])
    cfg = preset.get("config_payload", {})
    if cfg.get("type") != "leaps":
        return LeapsSignalResult(sym, preset_id, today, [], [], [], ["预设不是LEAPS类型"])

    drawdown_threshold_pct = float(cfg.get("drawdown_threshold_pct", 20))
    entry_mode = str(cfg.get("entry_mode", "both"))
    stage1_days = int(cfg.get("stage1_days", 15))
    stage1_profit = float(cfg.get("stage1_profit", 80))
    stage1_sell = float(cfg.get("stage1_sell", 50))
    stage2_days = int(cfg.get("stage2_days", 60))
    stage2_profit = float(cfg.get("stage2_profit", 60))
    stage2_sell = float(cfg.get("stage2_sell", 50))
    cooldown_days = int(cfg.get("cooldown_days", 5))

    stages = [
        (stage1_days, stage1_profit, stage1_sell),
        (stage2_days, stage2_profit, stage2_sell),
    ]

    # 2. Load trade snapshot
    snapshot = load_symbol_snapshot(sym)
    if not snapshot:
        return LeapsSignalResult(sym, preset_id, today, [], [], [], ["无交易记录"])
    rows = snapshot.get("rows", [])
    if not rows:
        return LeapsSignalResult(sym, preset_id, today, [], [], [], ["交易记录为空"])

    # 3. Parse option trades
    option_trades = load_option_trades(rows, sym)
    open_positions = compute_open_positions(option_trades, as_of=today)

    # 4. Fetch prices
    try:
        prices = _fetch_prices_for_leaps_signal(sym, option_trades, today)
    except Exception as e:
        errors.append(f"获取行情失败: {e}")
        return LeapsSignalResult(sym, preset_id, today, [], [], open_positions, errors)

    if not prices or len(prices) < 122:
        errors.append("价格数据不足（需要至少122个交易日）")
        return LeapsSignalResult(sym, preset_id, today, [], [], open_positions, errors)

    # 5. Detect entry signals
    entry_signals = detect_entry_signals(
        prices, option_trades,
        drawdown_threshold_pct=drawdown_threshold_pct,
        entry_mode=entry_mode,
        cooldown_days=cooldown_days,
    )
    for es in entry_signals:
        es.underlying = sym

    # 6. Detect sell signals for open positions using unified engine
    current_price = prices[-1][1]
    # Set entry_stock_price for positions that don't have it
    for pos in open_positions:
        if pos.entry_stock_price is None:
            pos.entry_stock_price = _find_price_on_date(prices, pos.entry_date) or current_price

    # Filter option_trades to only sells for open positions
    open_contracts = {p.contract_code for p in open_positions}
    relevant_trades = [t for t in option_trades if t.contract_code in open_contracts]

    sell_signals: list[SellSignal] = []
    for pos in open_positions:
        pos_signals = compute_leaps_sell_signals(
            pos, prices, relevant_trades, stages, today,
        )
        sell_signals.extend(pos_signals)

    return LeapsSignalResult(
        symbol=sym,
        preset_id=preset_id,
        signal_date=today,
        entry_signals=entry_signals,
        sell_signals=sell_signals,
        open_positions=open_positions,
        errors=errors,
    )


# ── Internal helpers ──────────────────────────────────────────────────────

def _fetch_prices_for_leaps_signal(
    symbol: str,
    option_trades: list[OptionTrade],
    end_date: date,
) -> list[tuple[date, float]]:
    """Fetch daily close prices from Longbridge."""
    from drawdown.position_strategy import (
        build_longbridge_quote_context,
        fetch_longbridge_daily_candles,
    )
    from trade_sync.normalize import infer_longbridge_symbol

    # Determine earliest date: first option trade minus 180 days (for 120-day high + bollinger)
    trade_dates = [t.trade_date for t in option_trades]
    first_date = min(trade_dates) if trade_dates else end_date - timedelta(days=180)
    fetch_start = first_date - timedelta(days=180)
    fetch_end = end_date + timedelta(days=1)

    longbridge_sym = infer_longbridge_symbol(symbol, None)
    ctx = build_longbridge_quote_context()
    candles = fetch_longbridge_daily_candles(ctx, longbridge_sym, fetch_start, fetch_end)

    from drawdown.strategy_signal import candle_datetime  # type: ignore[import-untyped]
    daily = [
        (candle_datetime(c).replace(tzinfo=None), float(c.close))
        for c in candles
    ]

    return append_realtime_price(daily, ctx, longbridge_sym, end_date)


def append_realtime_price(
    daily_prices: list[tuple[date, float]],
    quote_ctx: object,
    longbridge_symbol: str,
    today: date,
) -> list[tuple[date, float]]:
    """Append or replace real-time price if market is open.

    Args:
        daily_prices: (date, price) sorted ascending from daily candles.
        quote_ctx: Longbridge QuoteContext.
        longbridge_symbol: Longbridge-format symbol.
        today: Current date to check against.

    Returns:
        Potentially modified price list.
    """
    try:
        sessions = quote_ctx.trading_session()
        if not sessions:
            return daily_prices
        market_state = getattr(sessions[0], "market_state", "closed")
        if market_state == "closed":
            return daily_prices
    except Exception:
        return daily_prices

    try:
        quotes = quote_ctx.quote([longbridge_symbol])
        if not quotes:
            return daily_prices
        realtime_price = float(quotes[0].last_done)
    except Exception:
        return daily_prices

    result = list(daily_prices)
    if result and result[-1][0] == today:
        # Replace last entry with real-time price
        result[-1] = (today, realtime_price)
    elif not result or result[-1][0] < today:
        # Append new entry for today
        result.append((today, realtime_price))
    return result


def _find_price_on_date(
    prices: list[tuple[date, float]],
    target_date: date,
) -> float | None:
    """Find stock price on or nearest before a date."""
    best: float | None = None
    for d, p in prices:
        if d <= target_date:
            best = p
        else:
            break
    return best
