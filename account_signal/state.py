"""Recover real account positions from synced trade rows."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from typing import Any

from account_signal.config import googl_cost_deleverage_stages, googl_inputs
from trade_sync.normalize import canonical_symbol, infer_longbridge_symbol
from trade_sync.store import load_symbol_snapshot


@dataclass
class AccountLot:
    buy_date: str
    buy_price: float
    initial_shares: float
    remaining_shares: float
    amount: float
    buy_drawdown_pct: float | None = None


@dataclass
class AccountPosition:
    symbol: str
    shares: float = 0.0
    lots: list[AccountLot] = field(default_factory=list)
    buy_events: list[dict[str, Any]] = field(default_factory=list)
    sell_events: list[dict[str, Any]] = field(default_factory=list)
    cost_deleverage_marks: set[str] = field(default_factory=set)
    grid_rebound_marks: set[str] = field(default_factory=set)
    last_cost_deleverage_sell_date: str | None = None
    last_sell_date: str | None = None

    @property
    def avg_cost(self) -> float:
        if self.shares <= 0:
            return 0.0
        total_cost = sum(lot.remaining_shares * lot.buy_price for lot in self.lots)
        return total_cost / self.shares if total_cost > 0 else 0.0

    @property
    def invested_remaining(self) -> float:
        return sum(lot.remaining_shares * lot.buy_price for lot in self.lots)

    def to_dict(self) -> dict[str, Any]:
        return {
            "symbol": self.symbol,
            "shares": self.shares,
            "avg_cost": self.avg_cost,
            "invested_remaining": self.invested_remaining,
            "lots": [lot.__dict__ for lot in self.lots],
            "buy_count": len(self.buy_events),
            "sell_count": len(self.sell_events),
            "cost_deleverage_marks": sorted(self.cost_deleverage_marks),
            "grid_rebound_marks": sorted(self.grid_rebound_marks),
            "last_sell_date": self.last_sell_date,
        }


def load_account_positions(symbols: list[str] | tuple[str, ...]) -> dict[str, AccountPosition]:
    positions: dict[str, AccountPosition] = {}
    for symbol in symbols:
        longbridge_symbol = infer_longbridge_symbol(canonical_symbol(symbol), "US")
        base = longbridge_symbol.split(".", 1)[0]
        snapshot = load_symbol_snapshot(base) or load_symbol_snapshot(longbridge_symbol)
        rows = (snapshot or {}).get("rows") if isinstance(snapshot, dict) else []
        positions[longbridge_symbol] = recover_position(longbridge_symbol, rows if isinstance(rows, list) else [])
    return positions


def recover_position(symbol: str, rows: list[dict[str, Any]]) -> AccountPosition:
    position = AccountPosition(symbol=symbol)
    ordered = sorted(rows, key=lambda item: (str(item.get("trade_date", "")), 0 if item.get("side") == "buy" else 1))
    for row in ordered:
        side = str(row.get("side", "") or "").lower()
        if side == "buy":
            _apply_buy(position, row)
        elif side == "sell":
            _apply_sell(position, row)
    position.shares = sum(lot.remaining_shares for lot in position.lots)
    return position


def _apply_buy(position: AccountPosition, row: dict[str, Any]) -> None:
    shares = _float(row.get("shares"))
    if shares <= 0:
        return
    price = _float(row.get("price"))
    amount = _float(row.get("amount"), shares * price)
    if price <= 0 and amount > 0:
        price = amount / shares
    if price <= 0:
        return
    lot = AccountLot(
        buy_date=str(row.get("trade_date", "") or ""),
        buy_price=price,
        initial_shares=shares,
        remaining_shares=shares,
        amount=amount or shares * price,
    )
    position.lots.append(lot)
    position.shares += shares
    position.buy_events.append({**row, "shares": shares, "price": price, "amount": amount})


def _apply_sell(position: AccountPosition, row: dict[str, Any]) -> None:
    shares_to_sell = _float(row.get("shares"))
    if shares_to_sell <= 0:
        return
    sell_price = _float(row.get("price"))
    pre_avg_cost = position.avg_cost
    remaining = shares_to_sell
    for lot in position.lots:
        if remaining <= 1e-9:
            break
        sold = min(lot.remaining_shares, remaining)
        lot.remaining_shares -= sold
        remaining -= sold
    position.lots = [lot for lot in position.lots if lot.remaining_shares > 1e-9]
    position.shares = sum(lot.remaining_shares for lot in position.lots)
    profit_pct = sell_price / pre_avg_cost * 100.0 - 100.0 if sell_price > 0 and pre_avg_cost > 0 else 0.0
    event = {**row, "shares": shares_to_sell, "price": sell_price, "pre_avg_cost": pre_avg_cost, "profit_pct": profit_pct}
    position.sell_events.append(event)
    position.last_sell_date = str(row.get("trade_date", "") or "") or position.last_sell_date
    if position.symbol == "GOOGL.US":
        _mark_next_cost_stage(position, profit_pct)
        if profit_pct + 1e-9 >= googl_inputs().cost_first_profit_pct:
            position.last_cost_deleverage_sell_date = position.last_sell_date
    if position.symbol == "TSLA.US" and profit_pct >= 10.0:
        if "grid_1" not in position.grid_rebound_marks:
            position.grid_rebound_marks.add("grid_1")
        elif "grid_2" not in position.grid_rebound_marks:
            position.grid_rebound_marks.add("grid_2")


def _mark_next_cost_stage(position: AccountPosition, profit_pct: float) -> None:
    for mark, threshold, _sell_pct in googl_cost_deleverage_stages():
        if mark not in position.cost_deleverage_marks and profit_pct + 1e-9 >= threshold:
            position.cost_deleverage_marks.add(mark)
            return


def _float(value: Any, default: float = 0.0) -> float:
    if value in ("", None):
        return default
    if isinstance(value, (int, float)):
        return float(value)
    return float(str(value).strip().replace(",", ""))


def parse_iso_date(value: str | None) -> date | None:
    if not value:
        return None
    try:
        return date.fromisoformat(value[:10])
    except ValueError:
        return None
