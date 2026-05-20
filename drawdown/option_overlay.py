"""Optional Polygon/Longbridge-backed call option overlay & parameter scan (wallet model)."""

from __future__ import annotations

from collections import defaultdict, namedtuple
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Optional

from drawdown.option_provider import (
    OptionBar,
    OptionContractInfo,
    OptionDataProvider,
    PolygonOptionProvider,
    create_option_provider,
)


US_OPTION_UNDERLYINGS = {"TSM", "GOOGL", "TSLA"}


@dataclass(frozen=True)
class OptionOverlaySettings:
    enabled: bool = False
    wallet_pct: float = 20.0            # % of stock account capital allocated to option wallet
    trade_allocation_pct: float = 30.0   # % of current wallet cash used per buy signal
    target_dte: int = 250
    min_dte: int = 200
    max_dte: int = 300
    moneyness: str = "otm_10"
    profit_take_pct: float = 100.0
    profit_take_sell_pct: float = 50.0
    exit_dte: int = 60
    trade_fee: float = 0.35
    max_trades_per_strategy: int = 20


# ── OptionWallet ──────────────────────────────────────────────────────────


@dataclass
class OptionWallet:
    """Sub-wallet for option overlay trading.

    Receives ``wallet_pct`` × (initial_cash + cumulative monthly contributions)
    as its capital pool.  Buy/sell signals follow the stock strategy.
    """
    cash: float = 0.0
    positions: list[dict] = field(default_factory=list)
    realized_pnl: float = 0.0
    total_premium_paid: float = 0.0
    total_fees_paid: float = 0.0
    monthly_injected: float = 0.0       # cumulative injected contributions
    symbol_positions: dict = field(default_factory=dict)   # symbol → list of position indices

    def inject_monthly(self, contribution: float) -> None:
        """Add one month's wallet share to cash."""
        self.cash += contribution
        self.monthly_injected += contribution

    def buy_budget(self, trade_allocation_pct: float, fee: float) -> float:
        """Return available budget for one buy signal. Returns 0 if insufficient."""
        budget = self.cash * trade_allocation_pct / 100.0
        if budget <= fee:
            return 0.0
        return budget

    def buy(
        self,
        symbol: str,
        trade_allocation_pct: float,
        premium: float,
        entry_fee: float,
        position_payload: dict,
    ) -> bool:
        """Execute a buy: deduct cash, record position. Returns True on success."""
        budget = self.buy_budget(trade_allocation_pct, entry_fee)
        if budget <= 0 or premium > budget:
            return False
        total_cost = premium + entry_fee
        if total_cost > self.cash:
            return False
        self.cash -= total_cost
        self.total_premium_paid += premium
        self.total_fees_paid += entry_fee
        idx = len(self.positions)
        self.positions.append(position_payload)
        self.symbol_positions.setdefault(symbol, []).append(idx)
        return True

    def close_position(self, idx: int, exit_value: float, exit_fee: float) -> float:
        """Close one position by index.  Returns net cash received."""
        pos = self.positions[idx]
        net = exit_value - exit_fee
        self.cash += net
        self.realized_pnl += (exit_value - pos.get("premium", 0) - exit_fee)
        self.total_fees_paid += exit_fee
        pos["status"] = "closed"
        pos["exit_value"] = exit_value
        pos["exit_fee"] = exit_fee
        return net

    def close_all_for_symbol(self, symbol: str, close_value_fn, exit_fee: float) -> float:
        """Close all open positions for *symbol*.  Returns total net cash."""
        indices = list(self.symbol_positions.get(symbol, []))
        total_net = 0.0
        for idx in indices:
            pos = self.positions[idx]
            if pos.get("status") == "closed":
                continue
            val = close_value_fn(pos)
            net = self.close_position(idx, val, exit_fee)
            total_net += net
        return total_net

    def open_positions_value(self, mark_price_fn) -> float:
        """Sum mark-to-market value of all open positions."""
        total = 0.0
        for pos in self.positions:
            if pos.get("status") == "closed":
                continue
            total += mark_price_fn(pos)
        return total

    def open_positions(self) -> list[dict]:
        return [p for p in self.positions if p.get("status") != "closed"]

    def metrics(self, open_value: float = 0.0) -> dict:
        option_total_value = sum(float(pos.get("total_value", 0.0) or 0.0) for pos in self.positions)
        if option_total_value <= 0 and self.total_premium_paid > 0:
            option_total_value = self.total_premium_paid + self.realized_pnl + open_value
        wallet_total_value = self.cash + open_value
        return {
            "wallet_cash": round(self.cash, 2),
            "open_positions_value": round(open_value, 2),
            "realized_pnl": round(self.realized_pnl, 2),
            "total_premium_paid": round(self.total_premium_paid, 2),
            "total_fees_paid": round(self.total_fees_paid, 2),
            "total_value": round(option_total_value, 2),
            "wallet_total_value": round(wallet_total_value, 2),
            "return_pct": (
                round((option_total_value / self.total_premium_paid - 1.0) * 100.0, 2)
                if self.total_premium_paid > 0
                else 0.0
            ),
            "monthly_injected": round(self.monthly_injected, 2),
            "position_count": len(self.positions),
            "open_count": len(self.open_positions()),
        }


# ── Wallet-level replay ──────────────────────────────────────────────────


def replay_option_wallet(
    stock_trades: list[dict],
    option_data_lookup: dict,
    settings: OptionOverlaySettings,
    stock_inputs: Any,      # StrategyInputs or dict with initial_cash, monthly_contribution
    end_date: date,
    warnings: list[str] | None = None,
) -> dict[str, Any]:
    """Simulate one option wallet through a single strategy's trade timeline.

    Wallet principal = wallet_pct × (initial_cash + n × monthly_contributions)
    where n = number of elapsed full months since simulation start.

    Returns a dict with ``wallet`` (OptionWallet), ``positions`` list,
    ``skipped`` list, and ``metrics``.
    """
    if warnings is None:
        warnings = []

    # Extract stock inputs
    if isinstance(stock_inputs, dict):
        initial_cash = float(stock_inputs.get("initial_cash", 10000))
        monthly_contribution = float(stock_inputs.get("monthly_contribution", 0))
    else:
        initial_cash = float(getattr(stock_inputs, "initial_cash", 10000))
        monthly_contribution = float(getattr(stock_inputs, "monthly_contribution", 0))

    # Determine simulation start from first trade
    all_dates = []
    for t in stock_trades:
        d = str(t.get("date", "")).strip()
        if d:
            all_dates.append(date.fromisoformat(d))
    if not all_dates:
        return _empty_wallet_result(settings, initial_cash, monthly_contribution)
    sim_start = min(all_dates)

    # Initialize wallet
    wallet_cash = settings.wallet_pct / 100.0 * initial_cash
    wallet = OptionWallet(cash=wallet_cash)

    # Build sorted timeline: (event_date, event_type, trade)
    Event = namedtuple("Event", ["date", "type", "trade"])
    events: list[Event] = []
    stock_sell_dates = _stock_sell_dates_by_symbol(stock_trades)
    for trade in stock_trades:
        d = date.fromisoformat(str(trade["date"]))
        action = str(trade.get("action", ""))
        if action in ("buy", "sell"):
            events.append(Event(d, action, trade))
    events.sort(key=lambda e: e.date)

    # Monthly injection schedule
    monthly_dates: list[date] = []
    d = date(sim_start.year, sim_start.month, 1)
    while d <= end_date:
        if d > sim_start:
            monthly_dates.append(d)
        # next month
        if d.month == 12:
            d = date(d.year + 1, 1, 1)
        else:
            d = date(d.year, d.month + 1, 1)

    monthly_injection = settings.wallet_pct / 100.0 * monthly_contribution
    monthly_idx = 0

    # Track positions that use pre-fetched contract+bars
    open_pos_bars: dict[int, tuple[OptionContractInfo, list[OptionBar], date]] = {}
    # pos_idx → (contract, bars, buy_date)

    skipped: list[dict] = []
    positions_out: list[dict] = []
    last_bar_process_date: date | None = None

    def _close_position_contracts(
        pos_idx: int,
        contracts: int,
        price: float,
        exit_date: date,
        reason: str,
        dte: int,
    ) -> None:
        pos = wallet.positions[pos_idx]
        value, fee = _exit_value(contracts, price, settings.trade_fee)
        pos["remaining_contracts"] = pos.get("remaining_contracts", 0) - contracts
        pos["realized_value"] = pos.get("realized_value", 0.0) + value
        pos["fees"] = pos.get("fees", 0.0) + fee
        pos.setdefault("exits", []).append({
            "reason": reason,
            "date": exit_date.isoformat(),
            "price": price,
            "contracts": contracts,
            "value": value,
            "fee": fee,
            "dte": dte,
        })
        wallet.cash += value
        wallet.realized_pnl += value - (pos.get("premium", 0) * contracts / max(pos.get("contracts", 1), 1))
        wallet.total_fees_paid += fee
        if pos.get("remaining_contracts", 0) <= 0:
            pos["remaining_contracts"] = 0
            pos["status"] = "closed"

    def _process_option_bars_through(limit_date: date) -> None:
        nonlocal last_bar_process_date
        if not open_pos_bars:
            last_bar_process_date = limit_date
            return
        bar_dates: set[date] = set()
        for _, bars, buy_date in open_pos_bars.values():
            for bar in bars:
                if bar.date < buy_date or bar.date > limit_date:
                    continue
                if last_bar_process_date is not None and bar.date <= last_bar_process_date:
                    continue
                bar_dates.add(bar.date)

        for day in sorted(bar_dates):
            for pos_idx, (contract, bars, buy_date) in list(open_pos_bars.items()):
                pos = wallet.positions[pos_idx]
                if pos.get("status") == "closed":
                    continue
                remaining = int(pos.get("remaining_contracts", 0))
                if remaining <= 0:
                    pos["status"] = "closed"
                    continue
                if day < buy_date:
                    continue

                bar = next((b for b in bars if b.date == day), None)
                if not bar:
                    continue

                pos["last_mark_price"] = bar.close
                pos["last_price"] = bar.close
                pos["last_date"] = bar.date.isoformat()

                dte = (contract.expiration - day).days
                entry_price = pos.get("entry_price", 0)

                profit_taken = any(ex.get("reason") == "profit_take" for ex in pos.get("exits", []))
                if not profit_taken and bar.close >= entry_price * (1.0 + settings.profit_take_pct / 100.0):
                    sell_ctr = int(remaining * settings.profit_take_sell_pct / 100.0)
                    if sell_ctr > 0:
                        _close_position_contracts(pos_idx, sell_ctr, bar.close, day, "profit_take", dte)
                        remaining = int(pos.get("remaining_contracts", 0))

                if remaining <= 0:
                    continue

                if dte <= settings.exit_dte:
                    _close_position_contracts(pos_idx, remaining, bar.close, day, "dte_exit", dte)

        last_bar_process_date = limit_date

    # Process events in order
    for evt in events:
        # Inject monthly contributions up to event date
        while monthly_idx < len(monthly_dates) and monthly_dates[monthly_idx] <= evt.date:
            wallet.inject_monthly(monthly_injection)
            monthly_idx += 1

        _process_option_bars_through(evt.date)

        if evt.type == "sell":
            # Stock sell → close all option positions for that symbol
            underlying = _base_symbol(str(evt.trade.get("symbol", "")))
            if underlying in US_OPTION_UNDERLYINGS:
                sell_date_str = evt.date.isoformat() if isinstance(evt.date, date) else str(evt.date)
                # Iterate positions directly so we can look up bar close on sell date
                for pos_idx in sorted(wallet.symbol_positions.get(underlying, []), reverse=True):
                    pos = wallet.positions[pos_idx]
                    if pos.get("status") == "closed":
                        continue
                    rem = pos.get("remaining_contracts", 0)
                    if rem <= 0:
                        continue
                    # Try bar close on sell date; fall back to last_mark_price
                    mark = pos.get("last_mark_price", 0.0)
                    ctr_bars = open_pos_bars.get(pos_idx)
                    if ctr_bars is not None:
                        _, bars, _ = ctr_bars
                        for bar in bars:
                            bar_date = bar.date.isoformat() if isinstance(bar.date, date) else str(bar.date)
                            if bar_date == sell_date_str:
                                mark = bar.close
                                break
                    contract = ctr_bars[0] if ctr_bars is not None else None
                    dte = (contract.expiration - evt.date).days if contract is not None else 0
                    _close_position_contracts(pos_idx, int(rem), mark, evt.date, "stock_sell", dte)
            continue

        # evt.type == "buy"
        underlying = _base_symbol(str(evt.trade.get("symbol", "")))
        if underlying not in US_OPTION_UNDERLYINGS:
            continue

        # Check budget
        budget = wallet.buy_budget(settings.trade_allocation_pct, settings.trade_fee)
        if budget <= 0:
            skipped.append(_skipped(evt.trade, "wallet_insufficient"))
            continue

        # Look up contract
        buy_date_iso = str(evt.trade.get("date", ""))
        lookup_key = (underlying, buy_date_iso, settings.moneyness, settings.target_dte)
        entry = option_data_lookup.get(lookup_key)
        if not entry:
            skipped.append(_skipped(evt.trade, "contract_not_found"))
            continue

        contract: OptionContractInfo = entry["contract"]
        bars: list[OptionBar] = entry["bars"]
        buy_date = evt.date

        entry_bar = next((b for b in bars if b.date >= buy_date and b.close > 0), None)
        if not entry_bar:
            skipped.append(_skipped(evt.trade, "entry_price_not_found"))
            continue

        entry_price = entry_bar.close
        entry_fee = min(settings.trade_fee, budget)
        contracts_count = int((budget - entry_fee) / (entry_price * 100.0))
        if contracts_count <= 0:
            skipped.append(_skipped(evt.trade, "contracts_too_small"))
            continue

        premium = contracts_count * entry_price * 100.0
        actual_entry_fee = min(settings.trade_fee, premium)

        # Position payload (open at entry)
        pos_payload = {
            "status": "open",
            "underlying": underlying,
            "stock_symbol": str(evt.trade.get("symbol", "")),
            "stock_buy_date": buy_date_iso,
            "stock_buy_price": float(evt.trade.get("price", 0)),
            "stock_buy_amount": float(evt.trade.get("gross_amount", 0)),
            "wallet_pct": settings.wallet_pct,
            "trade_allocation_pct": settings.trade_allocation_pct,
            "option_ticker": contract.ticker,
            "expiration": contract.expiration.isoformat(),
            "dte_at_entry": (contract.expiration - entry_bar.date).days,
            "strike": contract.strike,
            "moneyness": settings.moneyness,
            "entry_date": entry_bar.date.isoformat(),
            "entry_price": entry_price,
            "premium": premium,
            "contracts": contracts_count,
            "remaining_contracts": contracts_count,
            "realized_value": 0.0,
            "open_value": 0.0,
            "total_value": 0.0,
            "fees": actual_entry_fee,
            "return_pct": 0.0,
            "exits": [],
            "last_price": entry_price,
            "last_date": entry_bar.date.isoformat(),
            "last_mark_price": entry_price,
        }

        if not wallet.buy(underlying, settings.trade_allocation_pct, premium, actual_entry_fee, pos_payload):
            skipped.append(_skipped(evt.trade, "wallet_insufficient"))
            continue

        positions_out.append(pos_payload)
        pos_idx = len(wallet.positions) - 1
        open_pos_bars[pos_idx] = (contract, bars, buy_date)

    # Inject remaining monthly contributions
    _process_option_bars_through(end_date)
    while monthly_idx < len(monthly_dates) and monthly_dates[monthly_idx] <= end_date:
        wallet.inject_monthly(monthly_injection)
        monthly_idx += 1

    # Backtest end exits any remaining option contracts.
    for pos_idx, (contract, _, _) in list(open_pos_bars.items()):
        pos = wallet.positions[pos_idx]
        if pos.get("status") == "closed":
            continue
        remaining = int(pos.get("remaining_contracts", 0))
        if remaining <= 0:
            pos["status"] = "closed"
            continue
        mark = float(pos.get("last_mark_price", pos.get("entry_price", 0.0)) or 0.0)
        dte = (contract.expiration - end_date).days
        _close_position_contracts(pos_idx, remaining, mark, end_date, "backtest_end", dte)

    # ── Final mark-to-market ──────────────────────────────────────────────
    open_value = 0.0
    for pos in wallet.positions:
        remaining = pos.get("remaining_contracts", 0)
        mark = pos.get("last_mark_price", 0)
        pos["open_value"] = remaining * mark * 100.0
        open_value += pos["open_value"]
        pos["total_value"] = pos.get("realized_value", 0.0) + pos["open_value"]
        premium = pos.get("premium", 0)
        pos["return_pct"] = (pos["total_value"] / premium - 1.0) * 100.0 if premium > 0 else 0.0

    return {
        "wallet": wallet,
        "positions": positions_out,
        "skipped": skipped,
        "metrics": wallet.metrics(open_value),
    }


def _empty_wallet_result(
    settings: OptionOverlaySettings,
    initial_cash: float,
    monthly_contribution: float,
) -> dict:
    wallet_cash = settings.wallet_pct / 100.0 * initial_cash
    w = OptionWallet(cash=wallet_cash)
    return {
        "wallet": w,
        "positions": [],
        "skipped": [{"status": "skipped", "reason": "no_trades_in_timeline"}],
        "metrics": w.metrics(0.0),
    }


# ── existing overlay pipeline (unchanged signatures) ──────────────────────


class PolygonOptionClient:
    """Legacy wrapper — delegates to PolygonOptionProvider for backward compat."""

    def __init__(self, api_key: str, timeout: int = 15):
        self._provider = PolygonOptionProvider(api_key, timeout)
        self.api_key = api_key
        self.timeout = timeout

    def choose_call_contract(
        self,
        underlying: str,
        as_of: date,
        underlying_price: float,
        settings: OptionOverlaySettings,
    ) -> dict[str, Any] | None:
        return self._provider.choose_call_contract(
            underlying, as_of, underlying_price,
            settings.min_dte, settings.max_dte, settings.moneyness,
        )

    def option_history(
        self, ticker: str, start_date: date, end_date: date
    ) -> list[dict[str, Any]]:
        return self._provider.option_history_raw(ticker, start_date, end_date)


def apply_option_overlay(
    strategy_lab_result: dict[str, Any],
    *,
    api_key: str,
    settings: OptionOverlaySettings,
) -> dict[str, Any]:
    if not settings.enabled:
        strategy_lab_result["option_overlay"] = {"enabled": False}
        return strategy_lab_result
    if not api_key:
        raise ValueError("Polygon.io API Key 未配置，无法启用期权叠加。")

    _validate_settings(settings)
    client = PolygonOptionClient(api_key)
    end_date = date.fromisoformat(strategy_lab_result["range"]["end"])
    warnings: list[str] = []
    strategy_lab_result["option_overlay"] = {
        "enabled": True,
        "settings": settings.__dict__,
        "not_included_in_main_return": True,
    }

    for strategy in strategy_lab_result.get("strategies", []):
        overlay = _build_strategy_option_overlay(strategy, client, settings, end_date, warnings)
        strategy["option_overlay"] = overlay

    existing_warnings = strategy_lab_result.get("warnings") or []
    strategy_lab_result["warnings"] = list(existing_warnings) + warnings[:30]
    return strategy_lab_result


def _build_strategy_option_overlay(
    strategy: dict[str, Any],
    client: PolygonOptionClient,
    settings: OptionOverlaySettings,
    end_date: date,
    warnings: list[str],
) -> dict[str, Any]:
    trades = strategy.get("trades") or []
    stock_sell_dates = _stock_sell_dates_by_symbol(trades)
    positions = []
    skipped = []
    buy_trades = [
        trade
        for trade in trades
        if trade.get("action") == "buy" and _base_symbol(str(trade.get("symbol", ""))) in US_OPTION_UNDERLYINGS
    ]

    for trade in buy_trades[: settings.max_trades_per_strategy]:
        position = _simulate_option_position(
            trade,
            stock_sell_dates.get(str(trade["symbol"]), []),
            client,
            settings,
            end_date,
            warnings,
        )
        if position.get("status") == "skipped":
            skipped.append(position)
        else:
            positions.append(position)

    if len(buy_trades) > settings.max_trades_per_strategy:
        skipped.append(
            {
                "status": "skipped",
                "reason": "max_trades_limit",
                "count": len(buy_trades) - settings.max_trades_per_strategy,
            }
        )

    total_premium = sum(float(item.get("premium", 0)) for item in positions)
    total_fees = sum(float(item.get("fees", 0)) for item in positions)
    realized_value = sum(float(item.get("realized_value", 0)) for item in positions)
    open_value = sum(float(item.get("open_value", 0)) for item in positions)
    total_value = realized_value + open_value
    return {
        "enabled": True,
        "positions": positions,
        "skipped": skipped,
        "metrics": {
            "position_count": len(positions),
            "skipped_count": len(skipped),
            "total_premium": total_premium,
            "realized_value": realized_value,
            "open_value": open_value,
            "total_value": total_value,
            "total_fees": total_fees,
            "return_pct": (total_value / total_premium - 1.0) * 100.0 if total_premium > 0 else 0.0,
        },
    }


def _simulate_option_position(
    stock_buy: dict[str, Any],
    stock_sell_dates: list[date],
    client: PolygonOptionClient,
    settings: OptionOverlaySettings,
    end_date: date,
    warnings: list[str],
) -> dict[str, Any]:
    """Legacy per-trade simulation — kept for apply_option_overlay compat."""
    symbol = str(stock_buy["symbol"])
    underlying = _base_symbol(symbol)
    buy_date = date.fromisoformat(str(stock_buy["date"]))
    stock_price = float(stock_buy["price"])
    # Legacy: used allocation_pct; now map wallet_pct
    option_budget = float(stock_buy["gross_amount"]) * settings.wallet_pct / 100.0
    if option_budget <= settings.trade_fee:
        return _skipped(stock_buy, "budget_too_small")

    try:
        contract = client.choose_call_contract(underlying, buy_date, stock_price, settings)
    except Exception as exc:
        warnings.append(f"{underlying} {buy_date.isoformat()} 期权链查询失败: {exc}")
        return _skipped(stock_buy, "contract_query_failed")
    if not contract:
        return _skipped(stock_buy, "contract_not_found")

    expiration = date.fromisoformat(contract["expiration_date"])
    try:
        bars = client.option_history(contract["ticker"], buy_date, min(end_date, expiration))
    except Exception as exc:
        warnings.append(f"{contract['ticker']} 历史价格查询失败: {exc}")
        return _skipped(stock_buy, "history_query_failed", contract)
    entry_bar = next((bar for bar in bars if bar["date"] >= buy_date and bar["close"] > 0), None)
    if not entry_bar:
        return _skipped(stock_buy, "entry_price_not_found", contract)

    entry_price = entry_bar["close"]
    entry_fee = min(settings.trade_fee, option_budget)
    premium = option_budget
    contracts = int((option_budget - entry_fee) / (entry_price * 100.0))
    if contracts <= 0:
        return _skipped(stock_buy, "contracts_too_small", contract)

    remaining_contracts = contracts
    exits = []
    realized_value = 0.0
    fees = entry_fee
    profit_taken = False
    stock_exit_date = next((item for item in stock_sell_dates if item >= buy_date), None)

    for bar in bars:
        if bar["date"] < entry_bar["date"] or remaining_contracts <= 0:
            continue
        dte = (expiration - bar["date"]).days
        if not profit_taken and bar["close"] >= entry_price * (1.0 + settings.profit_take_pct / 100.0):
            sell_contracts = int(remaining_contracts * settings.profit_take_sell_pct / 100.0)
            if sell_contracts > 0:
                value, fee = _exit_value(sell_contracts, bar["close"], settings.trade_fee)
                remaining_contracts -= sell_contracts
                realized_value += value
                fees += fee
                profit_taken = True
                exits.append(_exit_payload("profit_take", bar, sell_contracts, value, fee, dte))
        if remaining_contracts <= 0:
            break
        if (stock_exit_date and bar["date"] >= stock_exit_date) or dte <= settings.exit_dte:
            reason = "stock_sell" if stock_exit_date and bar["date"] >= stock_exit_date else "dte_exit"
            value, fee = _exit_value(remaining_contracts, bar["close"], settings.trade_fee)
            exits.append(_exit_payload(reason, bar, remaining_contracts, value, fee, dte))
            realized_value += value
            fees += fee
            remaining_contracts = 0.0
            break

    last_bar = bars[-1] if bars else entry_bar
    open_value = remaining_contracts * last_bar["close"] * 100.0 if remaining_contracts > 0 else 0.0
    total_value = realized_value + open_value
    return {
        "status": "open" if remaining_contracts > 0 else "closed",
        "underlying": underlying,
        "stock_symbol": symbol,
        "stock_buy_date": buy_date.isoformat(),
        "stock_buy_price": stock_price,
        "stock_buy_amount": float(stock_buy["gross_amount"]),
        "wallet_pct": settings.wallet_pct,
        "option_ticker": contract["ticker"],
        "expiration": expiration.isoformat(),
        "dte_at_entry": (expiration - entry_bar["date"]).days,
        "strike": float(contract["strike_price"]),
        "moneyness": settings.moneyness,
        "entry_date": entry_bar["date"].isoformat(),
        "entry_price": entry_price,
        "premium": premium,
        "contracts": contracts,
        "remaining_contracts": remaining_contracts,
        "realized_value": realized_value,
        "open_value": open_value,
        "total_value": total_value,
        "fees": fees,
        "return_pct": (total_value / premium - 1.0) * 100.0 if premium > 0 else 0.0,
        "exits": exits,
        "last_price": last_bar["close"],
        "last_date": last_bar["date"].isoformat(),
    }


# ── Option data pre-fetch (unchanged) ─────────────────────────────────────


def batch_fetch_option_data(
    trades: list[dict],
    settings_template: OptionOverlaySettings,
    provider: OptionDataProvider,
    moneyness_values: list[str],
    dte_targets: list[int],
    end_date: date,
    _progress_cb: object = None,
    dte_windows: list[tuple[int, int, int]] | None = None,
) -> tuple[dict, list[str]]:
    """Pre-fetch all option contracts and price histories needed for scanning.

    Returns:
        lookup: dict[(underlying, buy_date_iso, moneyness, dte_target), {
            "contract": OptionContractInfo,
            "bars": list[OptionBar],
        }]
        warnings: list[str]
    """
    import time as _time
    warnings: list[str] = []

    # 1. Extract unique (underlying, buy_date, stock_price) from buy trades
    buy_points: dict[tuple[str, str, float], dict] = {}
    for trade in trades:
        if trade.get("action") != "buy":
            continue
        underlying = _base_symbol(str(trade.get("symbol", "")))
        if underlying not in US_OPTION_UNDERLYINGS:
            continue
        buy_date = str(trade.get("date", ""))
        stock_price = float(trade.get("price", 0))
        if stock_price <= 0:
            continue
        key = (underlying, buy_date, stock_price)
        if key not in buy_points:
            buy_points[key] = {"trade": trade, "underlying": underlying, "buy_date_iso": buy_date, "stock_price": stock_price}

    if not buy_points:
        return {}, ["买入记录中没有期权支持的美股标的。"]

    # 2. Pre-fetch all contracts once per (underlying, moneyness) → match locally
    #    This reduces Polygon API calls from O(months×moneyness×DTE) to O(underlyings×moneyness).
    from drawdown.option_provider import _contract_score as _opt_contract_score, _target_strike as _opt_target_strike

    _all_buy_dates = sorted(set(date.fromisoformat(bd) for (_, bd, _) in buy_points))
    _min_buy_date = _all_buy_dates[0]
    _max_buy_date = _all_buy_dates[-1]
    dte_windows = dte_windows or [
        (settings_template.min_dte, dte, settings_template.max_dte)
        if dte == settings_template.target_dte
        else (max(1, dte - 60), dte, dte + 60)
        for dte in dte_targets
    ]
    _min_dte = min(item[0] for item in dte_windows)
    _max_dte = max(item[2] for item in dte_windows)
    _start_exp = _min_buy_date + timedelta(days=_min_dte)
    _end_exp = _max_buy_date + timedelta(days=_max_dte)

    _prefetched: dict[tuple[str, str], list[dict]] = {}
    _unique_und = sorted(set(und for (und, _, _) in buy_points))
    chain_done = 0
    chain_start = _time.perf_counter()
    for und in _unique_und:
        for mn in moneyness_values:
            _prefetched[(und, mn)] = provider.fetch_option_contracts(
                und, _max_buy_date, _start_exp, _end_exp,
            )
            chain_done += 1
            _time.sleep(0.5)

    contract_map: dict[tuple[str, str, str, int], Optional[OptionContractInfo]] = {}
    contract_by_ticker: dict[str, OptionContractInfo] = {}
    total_chain_calls = chain_done

    for (underlying, buy_date_iso, stock_price), bp in buy_points.items():
        buy_date = date.fromisoformat(buy_date_iso)
        for mn in moneyness_values:
            contracts = _prefetched.get((underlying, mn), [])
            _tgt_strike = _opt_target_strike(stock_price, mn)
            for min_dte, dte_tgt, max_dte in dte_windows:
                lookup_key = (underlying, buy_date_iso, mn, dte_tgt)
                _tgt_exp = buy_date + timedelta(days=dte_tgt)
                _min_exp = buy_date + timedelta(days=min_dte)
                _max_exp = buy_date + timedelta(days=max_dte)
                candidates = [
                    c for c in contracts
                    if _min_exp <= date.fromisoformat(c["expiration_date"]) <= _max_exp
                ]
                if not candidates:
                    contract_map[lookup_key] = None
                    continue
                best = min(candidates, key=lambda c: _opt_contract_score(c, _tgt_exp, _tgt_strike))
                info = OptionContractInfo(
                    ticker=str(best["ticker"]),
                    underlying=underlying,
                    expiration=date.fromisoformat(best["expiration_date"]),
                    strike=float(best["strike_price"]),
                    contract_type="call",
                )
                contract_map[lookup_key] = info
                contract_by_ticker[info.ticker] = info

    _parameter_lab_log = None
    try:
        from web.app import _parameter_lab_log as _plog
        _parameter_lab_log = _plog
    except Exception:
        pass
    if _parameter_lab_log:
        _parameter_lab_log("option_batch_chains_done",
            calls=chain_done, unique_tickers=len(contract_by_ticker),
            elapsed_ms=round((_time.perf_counter() - chain_start) * 1000, 1),
        )

    if not contract_by_ticker:
        return {}, warnings + ["没有找到任何匹配的期权合约。"]

    # 3. For each unique contract ticker → fetch history
    history_by_ticker: dict[str, list[OptionBar]] = {}
    history_done = 0
    history_start = _time.perf_counter()
    for ticker, contract in contract_by_ticker.items():
        try:
            history_end = min(end_date, contract.expiration)
            bars = provider.get_option_history(ticker, contract.expiration - timedelta(days=730), history_end)
        except Exception as exc:
            warnings.append(f"期权历史查询失败 {ticker}: {exc}")
            bars = []
        history_by_ticker[ticker] = bars
        history_done += 1
        if history_done % 5 == 0:
            _time.sleep(0.3)

    if _parameter_lab_log:
        _parameter_lab_log("option_batch_history_done",
            calls=history_done,
            elapsed_ms=round((_time.perf_counter() - history_start) * 1000, 1),
        )

    # 4. Build lookup dict
    lookup: dict = {}
    for (underlying, buy_date_iso, mn, dte_tgt), info in contract_map.items():
        if info is None:
            lookup[(underlying, buy_date_iso, mn, dte_tgt)] = None
            continue
        bars = history_by_ticker.get(info.ticker, [])
        lookup[(underlying, buy_date_iso, mn, dte_tgt)] = {
            "contract": info,
            "bars": bars,
        }

    return lookup, warnings


# ── option parameter scan (wallet mode) ───────────────────────────────────


def scan_option_variants(
    stock_result: dict[str, Any],
    option_variants: list[dict],
    provider: OptionDataProvider,
    moneyness_values: list[str],
    dte_targets: list[int],
    end_date: date,
    stock_inputs: Any = None,
) -> dict[str, Any]:
    """Option parameter scan using wallet model.

    Each strategy gets its own OptionWallet.  Scans all option_variants
    across all strategies and returns aggregate results.
    """
    warnings: list[str] = []

    # Collect all buy trades from stock strategies
    all_buy_trades: list[dict] = []
    for strategy in stock_result.get("strategies", []):
        for trade in strategy.get("trades", []):
            if trade.get("action") == "buy":
                underlying = _base_symbol(str(trade.get("symbol", "")))
                if underlying in US_OPTION_UNDERLYINGS:
                    all_buy_trades.append(trade)

    if not all_buy_trades:
        return {
            "variants": option_variants,
            "results": [],
            "warnings": ["股票策略结果中没有可以叠加期权的买入交易。"],
        }

    # Build settings template from first variant
    first = option_variants[0] if option_variants else {}
    base_settings = OptionOverlaySettings(
        enabled=True,
        wallet_pct=float(first.get("wallet_pct", first.get("allocation_pct", 20))),
        trade_allocation_pct=float(first.get("trade_allocation_pct", 30)),
        target_dte=int(first.get("target_dte", 250)),
        min_dte=int(first.get("min_dte", max(1, int(first.get("target_dte", 250)) - 60))),
        max_dte=int(first.get("max_dte", int(first.get("target_dte", 250)) + 60)),
        moneyness=str(first.get("moneyness", "otm_10")),
        profit_take_pct=float(first.get("profit_take_pct", 100)),
        profit_take_sell_pct=float(first.get("profit_take_sell_pct", 50)),
        exit_dte=int(first.get("exit_dte", 60)),
    )

    # Pre-fetch all option data
    dte_windows = [
        (
            int(variant.get("min_dte", max(1, int(variant.get("target_dte", 250)) - 60))),
            int(variant.get("target_dte", 250)),
            int(variant.get("max_dte", int(variant.get("target_dte", 250)) + 60)),
        )
        for variant in option_variants
    ]
    dte_windows = sorted(set(dte_windows), key=lambda item: (item[1], item[0], item[2]))
    lookup, fetch_warnings = batch_fetch_option_data(
        all_buy_trades, base_settings, provider, moneyness_values, dte_targets, end_date,
        dte_windows=dte_windows,
    )
    warnings.extend(fetch_warnings)

    # Build stock_inputs from stock_result if not provided
    if stock_inputs is None:
        # Create minimal stock_inputs from stock_result range
        from drawdown.position_strategy import StrategyInputs
        rng = stock_result.get("range", {})
        start_str = str(rng.get("start", "2020-01-01"))
        stock_inputs = StrategyInputs(initial_cash=20000.0, monthly_contribution=1000.0)

    # For each option variant, replay wallet for each strategy
    results: list[dict] = []
    for variant in option_variants:
        variant_settings = OptionOverlaySettings(
            enabled=True,
            wallet_pct=float(variant.get("wallet_pct", variant.get("allocation_pct", 20))),
            trade_allocation_pct=float(variant.get("trade_allocation_pct", 30)),
            target_dte=int(variant.get("target_dte", 250)),
            min_dte=int(variant.get("min_dte", max(1, int(variant.get("target_dte", 250)) - 60))),
            max_dte=int(variant.get("max_dte", int(variant.get("target_dte", 250)) + 60)),
            moneyness=str(variant.get("moneyness", "otm_10")),
            profit_take_pct=float(variant.get("profit_take_pct", 100)),
            profit_take_sell_pct=float(variant.get("profit_take_sell_pct", 50)),
            exit_dte=int(variant.get("exit_dte", 60)),
        )
        per_strategy: list[dict] = []
        for strategy in stock_result.get("strategies", []):
            trades = strategy.get("trades", [])
            wallet_result = replay_option_wallet(
                trades, lookup, variant_settings, stock_inputs, end_date, warnings,
            )
            positions = wallet_result.get("positions", [])
            skipped_items = wallet_result.get("skipped", [])
            metrics = wallet_result.get("metrics", {})

            per_strategy.append({
                "strategy_key": strategy.get("strategy_key", "unknown"),
                "strategy_label": strategy.get("strategy_label", "unknown"),
                "option_positions": positions,
                "option_skipped": skipped_items,
                "option_metrics": {
                    "position_count": metrics.get("position_count", 0),
                    "skipped_count": len(skipped_items),
                    "total_premium": metrics.get("total_premium_paid", 0),
                    "total_value": metrics.get("total_value", 0),
                    "return_pct": metrics.get("return_pct", 0),
                    "open_value": metrics.get("open_positions_value", 0),
                    "wallet_cash": metrics.get("wallet_cash", 0),
                    "realized_pnl": metrics.get("realized_pnl", 0),
                },
            })

        # Aggregate metrics
        returns = [s["option_metrics"]["return_pct"] for s in per_strategy]
        premiums = [s["option_metrics"]["total_premium"] for s in per_strategy]
        total_values = [s["option_metrics"]["total_value"] for s in per_strategy]

        results.append({
            "variant_index": variant.get("variant_index", 0),
            "variant": variant,
            "per_strategy": per_strategy,
            "aggregate_metrics": {
                "avg_return_pct": sum(returns) / len(returns) if returns else 0.0,
                "max_return_pct": max(returns) if returns else 0.0,
                "min_return_pct": min(returns) if returns else 0.0,
                "total_premium": sum(premiums),
                "total_value": sum(total_values),
                "combined_return_pct": (sum(total_values) / sum(premiums) - 1.0) * 100.0 if sum(premiums) > 0 else 0.0,
            },
        })

    return {
        "variants": option_variants,
        "results": results,
        "warnings": warnings,
    }


# ── internal helpers ──────────────────────────────────────────────────────


def _validate_settings(settings: OptionOverlaySettings) -> None:
    if settings.wallet_pct < 0:
        raise ValueError("期权钱包比例不能为负数。")
    if settings.trade_allocation_pct < 0 or settings.trade_allocation_pct > 100:
        raise ValueError("每笔交易分配比例必须在 0 到 100 之间。")
    if settings.min_dte <= 0 or settings.max_dte <= 0 or settings.min_dte > settings.max_dte:
        raise ValueError("期权 DTE 范围无效。")
    if settings.target_dte < settings.min_dte or settings.target_dte > settings.max_dte:
        raise ValueError("目标 DTE 必须在最小/最大 DTE 范围内。")
    if settings.moneyness not in {"atm", "itm_10", "otm_10"}:
        raise ValueError("未知期权行权价规则。")
    if settings.profit_take_sell_pct < 0 or settings.profit_take_sell_pct > 100:
        raise ValueError("期权止盈卖出比例必须在 0 到 100 之间。")


def _stock_sell_dates_by_symbol(trades: list[dict[str, Any]]) -> dict[str, list[date]]:
    by_symbol: dict[str, list[date]] = {}
    for trade in trades:
        if trade.get("action") != "sell":
            continue
        by_symbol.setdefault(str(trade["symbol"]), []).append(date.fromisoformat(str(trade["date"])))
    for dates in by_symbol.values():
        dates.sort()
    return by_symbol


def _base_symbol(symbol: str) -> str:
    return symbol.upper().split(".", 1)[0]


def _exit_value(contracts: float, price: float, fee: float) -> tuple[float, float]:
    gross = contracts * price * 100.0
    applied_fee = min(fee, gross)
    return gross - applied_fee, applied_fee


def _exit_value_typed(contracts: float, price: float, fee: float) -> tuple[float, float]:
    return _exit_value(contracts, price, fee)


def _exit_payload(
    reason: str,
    bar: dict[str, Any],
    contracts: float,
    value: float,
    fee: float,
    dte: int,
) -> dict[str, Any]:
    return {
        "reason": reason,
        "date": bar["date"].isoformat(),
        "price": bar["close"],
        "contracts": contracts,
        "value": value,
        "fee": fee,
        "dte": dte,
    }


def _skipped(
    stock_buy: dict[str, Any],
    reason: str,
    contract: dict[str, Any] | OptionContractInfo | None = None,
) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "status": "skipped",
        "reason": reason,
        "underlying": _base_symbol(str(stock_buy.get("symbol", ""))),
        "stock_symbol": stock_buy.get("symbol", ""),
        "stock_buy_date": stock_buy.get("date", ""),
        "stock_buy_amount": stock_buy.get("gross_amount", 0),
    }
    if contract is not None:
        if isinstance(contract, OptionContractInfo):
            payload["option_ticker"] = contract.ticker
            payload["expiration"] = contract.expiration.isoformat()
            payload["strike"] = contract.strike
        else:
            payload["option_ticker"] = contract.get("ticker", "")
            payload["expiration"] = contract.get("expiration_date", "")
            payload["strike"] = contract.get("strike_price", 0)
    return payload
