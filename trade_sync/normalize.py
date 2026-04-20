"""Normalize Google Sheets trade rows into drawdown-ready trade events."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Any


BUY_DATE_KEYS = ("trade_date", "buy_date", "date", "买入日期", "日期")
BUY_PRICE_KEYS = ("price", "buy_price", "买入股价", "买入价格", "成交价")
BUY_SHARES_KEYS = ("shares", "quantity", "qty", "buy_shares", "买入股数", "股数")
BUY_AMOUNT_KEYS = ("amount", "total", "buy_amount", "总价", "买入金额")
SELL_DATE_KEYS = ("sell_date", "卖出日期")
SELL_PRICE_KEYS = ("sell_price", "real_sell_price", "真实卖出价格", "卖出价格")
SELL_SHARES_KEYS = ("sell_shares", "卖出股数")
SELL_AMOUNT_KEYS = ("sell_amount", "卖出金额")
SYMBOL_KEYS = ("symbol", "ticker", "stock_name", "股票名称", "股票代码")
MARKET_KEYS = ("market", "市场")
NOTE_KEYS = ("note", "reason", "买入理由", "备注")


def _first_value(row: dict[str, Any], candidates: tuple[str, ...]) -> Any:
    for key in candidates:
        if key in row and row[key] not in ("", None):
            return row[key]
    return None


def _parse_float(value: Any) -> float | None:
    if value in ("", None):
        return None
    if isinstance(value, (int, float)):
        return float(value)
    raw = str(value).strip().replace(",", "")
    if not raw or raw.lower() == "none":
        return None
    return float(raw)


def _parse_excelish_date(value: Any) -> str | None:
    if value in ("", None):
        return None
    if isinstance(value, datetime):
        return value.strftime("%Y-%m-%d")
    if isinstance(value, (int, float)):
        base = datetime(1899, 12, 30)
        return (base + timedelta(days=float(value))).strftime("%Y-%m-%d")

    raw = str(value).strip()
    if not raw or raw.lower() == "none":
        return None

    if re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
        base = datetime(1899, 12, 30)
        return (base + timedelta(days=float(raw))).strftime("%Y-%m-%d")

    for fmt in ("%Y-%m-%d", "%Y/%m/%d", "%Y.%m.%d", "%m/%d/%Y"):
        try:
            return datetime.strptime(raw, fmt).strftime("%Y-%m-%d")
        except ValueError:
            continue
    raise ValueError(f"Unsupported trade date format: {value}")


def canonical_symbol(value: Any) -> str:
    raw = str(value or "").strip()
    if not raw:
        raise ValueError("Missing symbol")
    if ":" in raw:
        raw = raw.split(":", 1)[1]
    raw = raw.replace(" ", "").upper()
    return raw


def infer_longbridge_symbol(symbol: str, market: str | None) -> str:
    normalized = canonical_symbol(symbol)
    if "." in normalized:
        return normalized

    market_raw = (market or "").strip().lower()
    if "港" in market_raw or market_raw == "hk":
        return f"{normalized}.HK"
    if "沪" in market_raw or market_raw == "sh":
        return f"{normalized}.SH"
    if "深" in market_raw or market_raw == "sz":
        return f"{normalized}.SZ"
    if "新加坡" in market_raw or market_raw == "sg":
        return f"{normalized}.SI"
    return f"{normalized}.US"


def _build_event(
    *,
    row: dict[str, Any],
    source_row_index: int | None,
    symbol: str,
    longbridge_symbol: str,
    market: str | None,
    note: str | None,
    trade_date: str | None,
    side: str,
    shares: float | None,
    price: float | None,
    amount: float | None,
) -> dict[str, Any] | None:
    if not trade_date:
        return None
    if shares is None and price is None and amount is None:
        return None
    if amount is None and shares is not None and price is not None:
        amount = shares * price

    return {
        "symbol": symbol,
        "longbridge_symbol": longbridge_symbol,
        "market": market or "",
        "trade_date": trade_date,
        "side": side,
        "shares": shares,
        "price": price,
        "amount": amount,
        "note": note or "",
        "source_row_index": source_row_index,
        "raw": row,
    }


def normalize_trade_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []

    for row in rows:
        symbol_value = _first_value(row, SYMBOL_KEYS)
        if symbol_value in ("", None):
            continue

        source_row_index = row.get("_sheet_row")
        symbol = canonical_symbol(symbol_value)
        market = _first_value(row, MARKET_KEYS)
        note = _first_value(row, NOTE_KEYS)
        longbridge_symbol = infer_longbridge_symbol(symbol, str(market or ""))

        # Already-standardized rows from a future script revision.
        explicit_side = row.get("side")
        explicit_trade_date = _parse_excelish_date(
            row.get("trade_date") or row.get("date")
        )
        if explicit_side and explicit_trade_date:
            shares = _parse_float(row.get("shares"))
            price = _parse_float(row.get("price"))
            amount = _parse_float(row.get("amount"))
            event = _build_event(
                row=row,
                source_row_index=source_row_index,
                symbol=symbol,
                longbridge_symbol=longbridge_symbol,
                market=str(market or ""),
                note=str(note or ""),
                trade_date=explicit_trade_date,
                side=str(explicit_side).strip().lower(),
                shares=shares,
                price=price,
                amount=amount,
            )
            if event:
                normalized.append(event)
            continue

        buy_date = _parse_excelish_date(_first_value(row, BUY_DATE_KEYS))
        buy_price = _parse_float(_first_value(row, BUY_PRICE_KEYS))
        buy_shares = _parse_float(_first_value(row, BUY_SHARES_KEYS))
        buy_amount = _parse_float(_first_value(row, BUY_AMOUNT_KEYS))
        buy_event = _build_event(
            row=row,
            source_row_index=source_row_index,
            symbol=symbol,
            longbridge_symbol=longbridge_symbol,
            market=str(market or ""),
            note=str(note or ""),
            trade_date=buy_date,
            side="buy",
            shares=buy_shares,
            price=buy_price,
            amount=buy_amount,
        )
        if buy_event:
            normalized.append(buy_event)

        sell_date = _parse_excelish_date(_first_value(row, SELL_DATE_KEYS))
        sell_price = _parse_float(_first_value(row, SELL_PRICE_KEYS))
        sell_shares = _parse_float(_first_value(row, SELL_SHARES_KEYS))
        sell_amount = _parse_float(_first_value(row, SELL_AMOUNT_KEYS))
        if sell_shares is None:
            sell_shares = buy_shares
        sell_event = _build_event(
            row=row,
            source_row_index=source_row_index,
            symbol=symbol,
            longbridge_symbol=longbridge_symbol,
            market=str(market or ""),
            note=str(note or ""),
            trade_date=sell_date,
            side="sell",
            shares=sell_shares,
            price=sell_price,
            amount=sell_amount,
        )
        if sell_event:
            normalized.append(sell_event)

    normalized.sort(key=lambda item: (item["symbol"], item["trade_date"], item["side"]))
    return normalized
