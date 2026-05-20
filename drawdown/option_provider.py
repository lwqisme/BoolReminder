"""Unified option data provider abstraction (Polygon / Longbridge)."""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Optional

import requests


# ── data types ──────────────────────────────────────────────────────────


@dataclass(frozen=True)
class OptionContractInfo:
    ticker: str          # OCC symbol e.g. "AAPL270115C200000"
    underlying: str      # "AAPL"
    expiration: date
    strike: float
    contract_type: str   # "call"


@dataclass(frozen=True)
class OptionBar:
    date: date
    open: float
    high: float
    low: float
    close: float
    volume: float = 0.0


def _polygon_retry(func, *args, max_retries=3, **kwargs):
    """Call func(*args, **kwargs) + raise_for_status with exponential backoff on 429 / 5xx."""
    import time as _time
    last_exc = None
    for attempt in range(max_retries):
        try:
            resp = func(*args, **kwargs)
            resp.raise_for_status()
            return resp
        except Exception as exc:
            last_exc = exc
            status = getattr(getattr(exc, 'response', None), 'status_code', None)
            if status in (429, 502, 503, 504) and attempt < max_retries - 1:
                wait = 2.0 * (2 ** attempt)
                _time.sleep(wait)
                continue
            raise
    raise last_exc


# ── abstract interface ──────────────────────────────────────────────────


class OptionDataProvider(ABC):
    """Abstract provider so callers don't care about Polygon vs Longbridge."""

    @abstractmethod
    def get_option_chain(
        self,
        underlying: str,
        as_of: date,
        underlying_price: float,
        min_dte: int,
        max_dte: int,
        moneyness: str,  # "atm" | "itm_10" | "otm_10"
    ) -> Optional[OptionContractInfo]:
        """Return the best-matching LEAPS call contract for given criteria."""
        ...

    @abstractmethod
    def get_option_history(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> list[OptionBar]:
        """Return daily OHLC bars for an option contract."""
        ...

    def fetch_option_contracts(
        self,
        underlying: str,
        as_of: date,
        start_expiration: date,
        end_expiration: date,
    ) -> list[dict[str, object]]:
        """Return ALL matching option contract dicts for the given range.

        Default returns empty list; Polygon provider overrides.
        """
        del underlying, as_of, start_expiration, end_expiration
        return []


# ── helpers ──────────────────────────────────────────────────────────────


def _target_strike(price: float, moneyness: str) -> float:
    if moneyness == "itm_10":
        return price * 0.9
    if moneyness == "otm_10":
        return price * 1.1
    return price  # "atm"


def _contract_score(
    contract: dict[str, Any],
    target_expiration: date,
    target_strike: float,
) -> tuple[float, float]:
    """Lower score = better match. Returns (dte_diff, strike_diff)."""
    expiration = date.fromisoformat(contract["expiration_date"])
    strike = float(contract["strike_price"])
    return (abs((expiration - target_expiration).days), abs(strike - target_strike))


# ── Polygon provider ────────────────────────────────────────────────────


class PolygonOptionProvider(OptionDataProvider):
    """Migrated & refactored from original PolygonOptionClient in option_overlay.py."""

    def __init__(self, api_key: str, timeout: int = 15):
        if not api_key:
            raise ValueError("Polygon.io API Key 未配置，无法使用 Polygon 期权数据。")
        self.api_key = api_key
        self.timeout = timeout
        self.base_url = "https://api.polygon.io"
        self._contract_cache: dict[tuple[Any, ...], Optional[OptionContractInfo]] = {}
        self._contracts_cache: dict[tuple[Any, ...], list[dict[str, object]]] = {}
        self._history_cache: dict[tuple[str, str, str], list[OptionBar]] = {}

    # ── OptionDataProvider interface ────────────────────────────────

    def get_option_chain(
        self,
        underlying: str,
        as_of: date,
        underlying_price: float,
        min_dte: int,
        max_dte: int,
        moneyness: str,
    ) -> Optional[OptionContractInfo]:
        target_expiration = as_of + timedelta(days=(min_dte + max_dte) // 2)
        start_expiration = as_of + timedelta(days=min_dte)
        end_expiration = as_of + timedelta(days=max_dte)
        target_strike = _target_strike(underlying_price, moneyness)

        cache_key = (
            underlying,
            as_of.isoformat(),
            round(underlying_price, 2),
            min_dte,
            max_dte,
            moneyness,
        )
        if cache_key in self._contract_cache:
            return self._contract_cache[cache_key]

        try:
            response = _polygon_retry(
                requests.get,
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
        except Exception:
            self._contract_cache[cache_key] = None
            return None

        payload = response.json()
        contracts = payload.get("results") or []
        if not contracts:
            self._contract_cache[cache_key] = None
            return None

        selected = min(contracts, key=lambda c: _contract_score(c, target_expiration, target_strike))
        info = OptionContractInfo(
            ticker=str(selected["ticker"]),
            underlying=underlying,
            expiration=date.fromisoformat(selected["expiration_date"]),
            strike=float(selected["strike_price"]),
            contract_type="call",
        )
        self._contract_cache[cache_key] = info
        return info

    def fetch_option_contracts(
        self,
        underlying: str,
        as_of: date,
        start_expiration: date,
        end_expiration: date,
    ) -> list[dict[str, object]]:
        """Return ALL matching option contract dicts (not just best match)."""
        cache_key = (underlying, as_of.isoformat(), start_expiration.isoformat(), end_expiration.isoformat())
        if cache_key in self._contracts_cache:
            return self._contracts_cache[cache_key]

        try:
            response = _polygon_retry(
                requests.get,
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
        except Exception:
            self._contracts_cache[cache_key] = []
            return []

        payload = response.json()
        contracts = payload.get("results") or []
        self._contracts_cache[cache_key] = contracts
        return contracts

    def get_option_history(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> list[OptionBar]:
        cache_key = (ticker, start.isoformat(), end.isoformat())
        if cache_key in self._history_cache:
            return self._history_cache[cache_key]

        response = _polygon_retry(
            requests.get,
            f"{self.base_url}/v2/aggs/ticker/{ticker}/range/1/day/{start.isoformat()}/{end.isoformat()}",
            params={
                "adjusted": "true",
                "apiKey": self.api_key,
            },
            timeout=self.timeout,
        )
        payload = response.json()
        bars: list[OptionBar] = []
        for item in payload.get("results") or []:
            bars.append(
                OptionBar(
                    date=datetime.utcfromtimestamp(item["t"] / 1000).date(),
                    open=float(item.get("o") or 0),
                    high=float(item.get("h") or 0),
                    low=float(item.get("l") or 0),
                    close=float(item.get("c") or 0),
                    volume=float(item.get("v") or 0),
                )
            )
        bars.sort(key=lambda b: b.date)
        self._history_cache[cache_key] = bars
        return bars

    # ── compatibility helpers (used during refactor) ─────────────────

    def choose_call_contract(
        self,
        underlying: str,
        as_of: date,
        underlying_price: float,
        min_dte: int,
        max_dte: int,
        moneyness: str,
    ) -> Optional[dict[str, Any]]:
        """Legacy wrapper returning a plain dict — used while migrating callers."""
        info = self.get_option_chain(underlying, as_of, underlying_price, min_dte, max_dte, moneyness)
        if info is None:
            return None
        return {
            "ticker": info.ticker,
            "underlying": info.underlying,
            "expiration_date": info.expiration.isoformat(),
            "strike_price": info.strike,
            "contract_type": info.contract_type,
        }

    def option_history_raw(
        self,
        ticker: str,
        start_date: date,
        end_date: date,
    ) -> list[dict[str, Any]]:
        """Legacy wrapper returning plain dicts — used while migrating callers."""
        bars = self.get_option_history(ticker, start_date, end_date)
        return [
            {
                "date": b.date,
                "open": b.open,
                "high": b.high,
                "low": b.low,
                "close": b.close,
                "volume": b.volume,
            }
            for b in bars
        ]


# ── Longbridge provider ─────────────────────────────────────────────────


class LongbridgeOptionProvider(OptionDataProvider):
    """Uses Longbridge QuoteContext for option chain & quotes.

    get_option_history() tries Longbridge candlesticks first but may raise
    NotImplementedError if the API rejects OCC symbols. Callers should fall
    back to Polygon for history in that case.
    """

    def __init__(self, quote_ctx, timeout: int = 15):
        # quote_ctx is longbridge.openapi.QuoteContext (lazy import)
        self._ctx = quote_ctx
        self._timeout = timeout

    def get_option_chain(
        self,
        underlying: str,
        as_of: date,
        underlying_price: float,
        min_dte: int,
        max_dte: int,
        moneyness: str,
    ) -> Optional[OptionContractInfo]:
        try:
            expiry_dates = self._ctx.option_chain_expiry_date_list(underlying)
        except Exception:
            return None

        if not expiry_dates:
            return None

        start_limit = as_of + timedelta(days=min_dte)
        end_limit = as_of + timedelta(days=max_dte)
        target_dte = (min_dte + max_dte) // 2

        # Filter expiry dates in range
        valid: list[date] = []
        for ed in expiry_dates:
            if isinstance(ed, str):
                ed = date.fromisoformat(ed)
            elif hasattr(ed, "strftime"):
                ed = date(ed.year, ed.month, ed.day)
            if start_limit <= ed <= end_limit:
                valid.append(ed)

        if not valid:
            return None

        # Pick expiry closest to target_dte midpoint
        target_date = as_of + timedelta(days=target_dte)
        chosen_expiry = min(valid, key=lambda d: abs((d - target_date).days))

        try:
            chain_info = self._ctx.option_chain_info_by_date(underlying, chosen_expiry)
        except Exception:
            return None

        if not chain_info:
            return None

        target_strike = _target_strike(underlying_price, moneyness)

        # Find best strike
        strikes: list[tuple[float, str]] = []
        for item in chain_info:
            if hasattr(item, "price") and hasattr(item, "call_symbol") and item.call_symbol:
                strike = float(item.price)
                strikes.append((strike, str(item.call_symbol)))

        if not strikes:
            return None

        best_strike, best_symbol = min(strikes, key=lambda s: abs(s[0] - target_strike))

        return OptionContractInfo(
            ticker=best_symbol,
            underlying=underlying,
            expiration=chosen_expiry,
            strike=best_strike,
            contract_type="call",
        )

    def get_option_history(
        self,
        ticker: str,
        start: date,
        end: date,
    ) -> list[OptionBar]:
        # Longbridge history_candlesticks_by_date may not accept OCC symbols
        raise NotImplementedError(
            "LongbridgeOptionProvider.get_option_history() — "
            "Longbridge candlestick API 暂不支持 OCC 期权代码。"
            "请使用 Polygon 作为历史数据提供方。"
        )


# ── factory ──────────────────────────────────────────────────────────────


def create_option_provider(config: dict, lb_quote_ctx=None) -> OptionDataProvider:
    """Create the appropriate OptionDataProvider from config.

    config: result of ConfigManager.get_option_provider_config()
    lb_quote_ctx: Longbridge QuoteContext (lazy, for Longbridge provider)
    """
    provider_name = config.get("provider", "polygon")
    if provider_name == "longbridge" and lb_quote_ctx:
        return LongbridgeOptionProvider(lb_quote_ctx)
    return PolygonOptionProvider(config.get("polygon_api_key", ""))
