"""Optional Polygon-backed call option overlay for strategy-lab results."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import requests


US_OPTION_UNDERLYINGS = {"TSM", "GOOGL", "TSLA"}


@dataclass(frozen=True)
class OptionOverlaySettings:
    enabled: bool = False
    allocation_pct: float = 20.0
    target_dte: int = 365
    min_dte: int = 300
    max_dte: int = 450
    moneyness: str = "atm"
    profit_take_pct: float = 100.0
    profit_take_sell_pct: float = 50.0
    exit_dte: int = 120
    trade_fee: float = 0.35
    max_trades_per_strategy: int = 20


class PolygonOptionClient:
    def __init__(self, api_key: str, timeout: int = 15):
        self.api_key = api_key
        self.timeout = timeout
        self.base_url = "https://api.polygon.io"
        self._contract_cache: dict[tuple[Any, ...], dict[str, Any] | None] = {}
        self._history_cache: dict[tuple[str, str, str], list[dict[str, Any]]] = {}

    def choose_call_contract(
        self,
        underlying: str,
        as_of: date,
        underlying_price: float,
        settings: OptionOverlaySettings,
    ) -> dict[str, Any] | None:
        target_expiration = as_of + timedelta(days=settings.target_dte)
        start_expiration = as_of + timedelta(days=settings.min_dte)
        end_expiration = as_of + timedelta(days=settings.max_dte)
        target_strike = _target_strike(underlying_price, settings.moneyness)
        cache_key = (
            underlying,
            as_of.isoformat(),
            round(underlying_price, 2),
            settings.min_dte,
            settings.max_dte,
            settings.target_dte,
            settings.moneyness,
        )
        if cache_key in self._contract_cache:
            return self._contract_cache[cache_key]

        response = requests.get(
            f"{self.base_url}/v3/reference/options/contracts",
            params={
                "underlying_ticker": underlying,
                "contract_type": "call",
                "expiration_date.gte": start_expiration.isoformat(),
                "expiration_date.lte": end_expiration.isoformat(),
                "as_of": as_of.isoformat(),
                "limit": 1000,
                "sort": "expiration_date",
                "order": "asc",
                "apiKey": self.api_key,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        contracts = payload.get("results") or []
        if not contracts:
            self._contract_cache[cache_key] = None
            return None

        def score(contract: dict[str, Any]) -> tuple[int, float]:
            expiration = date.fromisoformat(contract["expiration_date"])
            strike = float(contract["strike_price"])
            return (abs((expiration - target_expiration).days), abs(strike - target_strike))

        selected = min(contracts, key=score)
        self._contract_cache[cache_key] = selected
        return selected

    def option_history(self, ticker: str, start_date: date, end_date: date) -> list[dict[str, Any]]:
        cache_key = (ticker, start_date.isoformat(), end_date.isoformat())
        if cache_key in self._history_cache:
            return self._history_cache[cache_key]

        response = requests.get(
            f"{self.base_url}/v2/aggs/ticker/{ticker}/range/1/day/{start_date.isoformat()}/{end_date.isoformat()}",
            params={
                "adjusted": "true",
                "apiKey": self.api_key,
            },
            timeout=self.timeout,
        )
        response.raise_for_status()
        payload = response.json()
        bars = []
        for item in payload.get("results") or []:
            bars.append(
                {
                    "date": datetime.utcfromtimestamp(item["t"] / 1000).date(),
                    "open": float(item.get("o") or 0),
                    "high": float(item.get("h") or 0),
                    "low": float(item.get("l") or 0),
                    "close": float(item.get("c") or 0),
                    "volume": float(item.get("v") or 0),
                }
            )
        bars.sort(key=lambda item: item["date"])
        self._history_cache[cache_key] = bars
        return bars


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
    symbol = str(stock_buy["symbol"])
    underlying = _base_symbol(symbol)
    buy_date = date.fromisoformat(str(stock_buy["date"]))
    stock_price = float(stock_buy["price"])
    option_budget = float(stock_buy["gross_amount"]) * settings.allocation_pct / 100.0
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
    contracts = (option_budget - entry_fee) / (entry_price * 100.0)
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
            sell_contracts = remaining_contracts * settings.profit_take_sell_pct / 100.0
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
        "option_budget_pct": settings.allocation_pct,
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


def _validate_settings(settings: OptionOverlaySettings) -> None:
    if settings.allocation_pct < 0:
        raise ValueError("期权资金比例不能为负数。")
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


def _target_strike(price: float, moneyness: str) -> float:
    if moneyness == "itm_10":
        return price * 0.9
    if moneyness == "otm_10":
        return price * 1.1
    return price


def _base_symbol(symbol: str) -> str:
    return symbol.upper().split(".", 1)[0]


def _exit_value(contracts: float, price: float, fee: float) -> tuple[float, float]:
    gross = contracts * price * 100.0
    applied_fee = min(fee, gross)
    return gross - applied_fee, applied_fee


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
    contract: dict[str, Any] | None = None,
) -> dict[str, Any]:
    payload = {
        "status": "skipped",
        "reason": reason,
        "underlying": _base_symbol(str(stock_buy.get("symbol", ""))),
        "stock_symbol": stock_buy.get("symbol", ""),
        "stock_buy_date": stock_buy.get("date", ""),
        "stock_buy_amount": stock_buy.get("gross_amount", 0),
    }
    if contract:
        payload["option_ticker"] = contract.get("ticker", "")
        payload["expiration"] = contract.get("expiration_date", "")
        payload["strike"] = contract.get("strike_price", 0)
    return payload
