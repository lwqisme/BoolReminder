"""Replay LEAPS-style signal outcomes with Polygon historical option bars."""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any

import requests


NO_POLYGON_KEY = "无 Polygon key"
UNSUPPORTED_UNDERLYING = "标的不支持"
NO_MONTHLY_CONTRACT = "无月度合约"
NO_200_300D_CONTRACT = "无 200-300D 合约"
NO_ENTRY_PRICE = "无可用入场价"
NO_EXIT_PRICE = "无可用卖出价"
NO_STOCK_SELL = "无下一次正股卖点"
NO_PRE_EXPIRATION_PRICE = "卖点晚于到期且无可用到期前价格"
API_LIMIT_OR_TIMEOUT = "API 限流/超时"
POLYGON_REQUEST_INTERVAL_SECONDS = 1.0
logger = logging.getLogger(__name__)


class PolygonRequestRateLimiter:
    def __init__(self, interval_seconds: float = POLYGON_REQUEST_INTERVAL_SECONDS):
        self.interval_seconds = interval_seconds
        self._lock = threading.Lock()
        self._next_request_at = 0.0

    def wait(self) -> float:
        with self._lock:
            now = time.monotonic()
            wait_seconds = max(0.0, self._next_request_at - now)
            request_at = now + wait_seconds
            self._next_request_at = request_at + self.interval_seconds
        if wait_seconds > 0:
            logger.info("Polygon request rate limited", extra={"wait_seconds": round(wait_seconds, 3)})
            time.sleep(wait_seconds)
        return wait_seconds


_POLYGON_RATE_LIMITER = PolygonRequestRateLimiter()


@dataclass(frozen=True)
class OptionContract:
    ticker: str
    underlying: str
    expiration: date
    strike: float
    contract_type: str = "call"


@dataclass(frozen=True)
class OptionBar:
    date: date
    close: float


def parse_iso_date(value: object) -> date | None:
    match = str(value or "")[:10]
    try:
        return date.fromisoformat(match)
    except ValueError:
        return None


def third_friday(year: int, month: int) -> date:
    first = date(year, month, 1)
    first_friday_offset = (4 - first.weekday()) % 7
    return first + timedelta(days=first_friday_offset + 14)


def easter_sunday(year: int) -> date:
    """Gregorian computus for holiday-aware monthly expiration checks."""
    a = year % 19
    b = year // 100
    c = year % 100
    d = b // 4
    e = b % 4
    f = (b + 8) // 25
    g = (b - f + 1) // 3
    h = (19 * a + b - d - g + 15) % 30
    i = c // 4
    k = c % 4
    l = (32 + 2 * e + 2 * i - h - k) % 7
    m = (a + 11 * h + 22 * l) // 451
    month = (h + l - 7 * m + 114) // 31
    day = ((h + l - 7 * m + 114) % 31) + 1
    return date(year, month, day)


def observed_us_market_holidays(year: int) -> set[date]:
    holidays: set[date] = {easter_sunday(year) - timedelta(days=2)}

    def observed(month: int, day: int) -> date:
        raw = date(year, month, day)
        if raw.weekday() == 5:
            return raw - timedelta(days=1)
        if raw.weekday() == 6:
            return raw + timedelta(days=1)
        return raw

    holidays.update(
        {
            observed(1, 1),
            observed(6, 19),
            observed(7, 4),
            observed(12, 25),
        }
    )

    # Thanksgiving: fourth Thursday in November.
    first_nov = date(year, 11, 1)
    first_thursday = first_nov + timedelta(days=(3 - first_nov.weekday()) % 7)
    holidays.add(first_thursday + timedelta(days=21))
    return holidays


def is_standard_monthly_expiration(expiration: date) -> bool:
    friday = third_friday(expiration.year, expiration.month)
    if expiration == friday:
        return True
    if friday in observed_us_market_holidays(expiration.year) and expiration == friday - timedelta(days=1):
        return True
    return False


def polygon_underlying(symbol: object) -> str | None:
    normalized = str(symbol or "").strip().upper()
    if not normalized:
        return None
    if "." in normalized:
        base, suffix = normalized.rsplit(".", 1)
        if suffix != "US":
            return None
        normalized = base
    if not normalized or any(not (ch.isalnum() or ch in {"-", "."}) for ch in normalized):
        return None
    return normalized.replace(".", "-")


def _float_or_none(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def skipped_outcome(signal: dict[str, object], reason: str) -> dict[str, object]:
    return {
        "signal_key": str(signal.get("signal_key") or ""),
        "date": str(signal.get("date") or ""),
        "symbol": str(signal.get("symbol") or ""),
        "entry_date": str(signal.get("date") or ""),
        "stock_sell_date": str(signal.get("next_stock_sell_date") or ""),
        "status": "skipped",
        "skipped_reason": reason,
    }


def _polygon_retry_get(url: str, params: dict[str, object], timeout: int) -> dict[str, object]:
    last_exc: Exception | None = None
    for attempt in range(3):
        try:
            wait_seconds = _POLYGON_RATE_LIMITER.wait()
            logger.debug(
                "Polygon request starting",
                extra={"url": url, "attempt": attempt + 1, "wait_seconds": round(wait_seconds, 3)},
            )
            response = requests.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            logger.debug("Polygon request completed", extra={"url": url, "attempt": attempt + 1, "status_code": response.status_code})
            return response.json()
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
        except requests.HTTPError as exc:
            last_exc = exc
            status = getattr(exc.response, "status_code", None)
            if status not in {429, 502, 503, 504}:
                raise
        if attempt < 2:
            time.sleep(0.5 * (2**attempt))
    if last_exc:
        raise last_exc
    return {}


class PolygonMonthlyOptionProvider:
    def __init__(self, api_key: str, timeout: int = 15):
        self.api_key = api_key
        self.timeout = timeout
        self.base_url = "https://api.polygon.io"
        self._contracts_cache: dict[tuple[str, str, str, str], list[dict[str, object]]] = {}
        self._bars_cache: dict[tuple[str, str, str], list[OptionBar]] = {}

    def fetch_contracts(self, underlying: str, as_of: date, start_expiration: date, end_expiration: date) -> list[dict[str, object]]:
        cache_key = (underlying, as_of.isoformat(), start_expiration.isoformat(), end_expiration.isoformat())
        if cache_key in self._contracts_cache:
            return self._contracts_cache[cache_key]
        payload = _polygon_retry_get(
            f"{self.base_url}/v3/reference/options/contracts",
            {
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
            self.timeout,
        )
        contracts = list(payload.get("results") or [])
        self._contracts_cache[cache_key] = contracts
        return contracts

    def select_monthly_call(self, underlying: str, as_of: date, stock_price: float) -> tuple[OptionContract | None, str]:
        start_expiration = as_of + timedelta(days=200)
        end_expiration = as_of + timedelta(days=300)
        contracts = self.fetch_contracts(underlying, as_of, start_expiration, end_expiration)
        if not contracts:
            return None, NO_200_300D_CONTRACT
        monthly: list[dict[str, object]] = []
        for contract in contracts:
            expiration = parse_iso_date(contract.get("expiration_date"))
            if not expiration or not (start_expiration <= expiration <= end_expiration):
                continue
            if str(contract.get("contract_type") or "call").lower() != "call":
                continue
            if not is_standard_monthly_expiration(expiration):
                continue
            if _float_or_none(contract.get("strike_price")) is None:
                continue
            monthly.append(contract)
        if not monthly:
            return None, NO_MONTHLY_CONTRACT
        target_strike = stock_price * 1.10
        target_expiration = as_of + timedelta(days=250)
        selected = min(
            monthly,
            key=lambda item: (
                abs(float(item.get("strike_price") or 0) - target_strike),
                abs((parse_iso_date(item.get("expiration_date")) or target_expiration) - target_expiration),
            ),
        )
        expiration = parse_iso_date(selected.get("expiration_date"))
        strike = _float_or_none(selected.get("strike_price"))
        ticker = str(selected.get("ticker") or "").strip()
        if not expiration or strike is None or not ticker:
            return None, NO_200_300D_CONTRACT
        return OptionContract(ticker=ticker, underlying=underlying, expiration=expiration, strike=strike), ""

    def fetch_bars(self, ticker: str, start: date, end: date) -> list[OptionBar]:
        cache_key = (ticker, start.isoformat(), end.isoformat())
        if cache_key in self._bars_cache:
            return self._bars_cache[cache_key]
        payload = _polygon_retry_get(
            f"{self.base_url}/v2/aggs/ticker/{ticker}/range/1/day/{start.isoformat()}/{end.isoformat()}",
            {"adjusted": "true", "sort": "asc", "apiKey": self.api_key},
            self.timeout,
        )
        bars: list[OptionBar] = []
        for item in payload.get("results") or []:
            close = _float_or_none(item.get("c"))
            timestamp = item.get("t")
            if close is None or timestamp is None:
                continue
            bars.append(OptionBar(date=datetime.utcfromtimestamp(float(timestamp) / 1000).date(), close=close))
        bars.sort(key=lambda bar: bar.date)
        self._bars_cache[cache_key] = bars
        return bars


def first_bar_on_or_after(bars: list[OptionBar], target: date, max_days: int, latest: date | None = None) -> OptionBar | None:
    limit = min(target + timedelta(days=max_days), latest) if latest else target + timedelta(days=max_days)
    for bar in bars:
        if target <= bar.date <= limit:
            return bar
    return None


def last_bar_on_or_before(bars: list[OptionBar], target: date, max_days: int) -> OptionBar | None:
    floor = target - timedelta(days=max_days)
    candidates = [bar for bar in bars if floor <= bar.date <= target]
    return candidates[-1] if candidates else None


def replay_signal(provider: PolygonMonthlyOptionProvider, signal: dict[str, object]) -> dict[str, object]:
    signal_date = parse_iso_date(signal.get("date"))
    sell_date = parse_iso_date(signal.get("next_stock_sell_date"))
    stock_buy_price = _float_or_none(signal.get("stock_buy_price"))
    underlying = polygon_underlying(signal.get("symbol"))
    if not signal_date or stock_buy_price is None:
        return skipped_outcome(signal, NO_ENTRY_PRICE)
    if not underlying:
        return skipped_outcome(signal, UNSUPPORTED_UNDERLYING)
    if not sell_date or sell_date <= signal_date:
        return skipped_outcome(signal, NO_STOCK_SELL)

    contract, reason = provider.select_monthly_call(underlying, signal_date, stock_buy_price)
    if contract is None:
        return skipped_outcome(signal, reason)

    entry_end = min(signal_date + timedelta(days=7), contract.expiration)
    history_end = max(entry_end, min(sell_date + timedelta(days=7), contract.expiration))
    bars = provider.fetch_bars(contract.ticker, signal_date, history_end)
    entry_bar = first_bar_on_or_after(bars, signal_date, 7, latest=contract.expiration)
    if not entry_bar:
        return {**skipped_outcome(signal, NO_ENTRY_PRICE), **contract_payload(contract)}

    if sell_date > contract.expiration:
        exit_bar = last_bar_on_or_before(bars, contract.expiration, 7)
        if not exit_bar:
            return {**skipped_outcome(signal, NO_PRE_EXPIRATION_PRICE), **contract_payload(contract), "entry_price": entry_bar.close}
        exit_status = "expired_before_stock_sell"
    else:
        exit_bar = first_bar_on_or_after(bars, sell_date, 7, latest=contract.expiration)
        if not exit_bar:
            return {**skipped_outcome(signal, NO_EXIT_PRICE), **contract_payload(contract), "entry_price": entry_bar.close}
        exit_status = "sold"

    roi_pct = (exit_bar.close / entry_bar.close - 1) * 100
    dte = (contract.expiration - signal_date).days
    return {
        "signal_key": str(signal.get("signal_key") or ""),
        "date": signal_date.isoformat(),
        "symbol": str(signal.get("symbol") or ""),
        "contract": contract.ticker,
        "underlying": contract.underlying,
        "expiration": contract.expiration.isoformat(),
        "strike": contract.strike,
        "dte": dte,
        "entry_date": entry_bar.date.isoformat(),
        "stock_sell_date": sell_date.isoformat(),
        "exit_date": exit_bar.date.isoformat(),
        "entry_price": entry_bar.close,
        "exit_price": exit_bar.close,
        "roi_pct": roi_pct,
        "status": "success",
        "exit_status": exit_status,
        "skipped_reason": "",
    }


def contract_payload(contract: OptionContract) -> dict[str, object]:
    return {
        "contract": contract.ticker,
        "underlying": contract.underlying,
        "expiration": contract.expiration.isoformat(),
        "strike": contract.strike,
        "dte": None,
    }


def summarize_outcomes(outcomes: list[dict[str, object]]) -> dict[str, object]:
    roi_values = sorted(float(item["roi_pct"]) for item in outcomes if item.get("status") == "success" and isinstance(item.get("roi_pct"), (int, float)))
    success_count = len(roi_values)
    total = len(outcomes)
    mean = sum(roi_values) / success_count if success_count else None
    if success_count:
        mid = success_count // 2
        median = roi_values[mid] if success_count % 2 else (roi_values[mid - 1] + roi_values[mid]) / 2
    else:
        median = None
    reason_counts: dict[str, int] = {}
    for item in outcomes:
        if item.get("status") == "success":
            continue
        reason = str(item.get("skipped_reason") or "未知失败")
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    top_reason = ""
    if reason_counts:
        top_reason = sorted(reason_counts.items(), key=lambda pair: (-pair[1], pair[0]))[0][0]
    return {
        "total": total,
        "success_count": success_count,
        "skipped_count": total - success_count,
        "roi_mean_pct": mean,
        "roi_median_pct": median,
        "top_failure_reason": top_reason,
        "failure_reasons": reason_counts,
    }


def replay_leaps_option_outcomes(
    signals: list[dict[str, object]],
    api_key: str,
    provider: PolygonMonthlyOptionProvider | None = None,
) -> dict[str, object]:
    if not api_key and provider is None:
        outcomes = [skipped_outcome(signal, NO_POLYGON_KEY) for signal in signals]
        return {"success": False, "message": "Polygon API key 未配置", "outcomes": outcomes, "summary": summarize_outcomes(outcomes)}
    active_provider = provider or PolygonMonthlyOptionProvider(api_key)
    outcomes: list[dict[str, object]] = []
    for signal in signals:
        try:
            outcomes.append(replay_signal(active_provider, signal))
        except (requests.Timeout, requests.ConnectionError) as exc:
            outcomes.append(skipped_outcome(signal, f"{API_LIMIT_OR_TIMEOUT}: {exc}"))
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            reason = API_LIMIT_OR_TIMEOUT if status in {429, 502, 503, 504} else f"Polygon API 错误: {status or exc}"
            outcomes.append(skipped_outcome(signal, reason))
        except Exception as exc:
            outcomes.append(skipped_outcome(signal, f"Polygon API 错误: {exc}"))
    return {"success": True, "outcomes": outcomes, "summary": summarize_outcomes(outcomes)}
