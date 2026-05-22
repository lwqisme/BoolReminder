"""Replay LEAPS-style signal outcomes with Polygon historical option bars."""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from contextvars import ContextVar
from dataclasses import dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

import requests


ROOT_DIR = Path(__file__).resolve().parent.parent
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
OPTION_CACHE_SCHEMA_VERSION = 1
LEAPS_OPTION_SELECTION_POLICY_VERSION = "monthly-call-200-300d-otm10-v1"
OUTCOME_CACHEABLE_FAILURES = {
    UNSUPPORTED_UNDERLYING,
    NO_MONTHLY_CONTRACT,
    NO_200_300D_CONTRACT,
    NO_ENTRY_PRICE,
    NO_STOCK_SELL,
    NO_PRE_EXPIRATION_PRICE,
}
logger = logging.getLogger(__name__)
_REQUEST_CACHE_STATS: ContextVar[dict[str, object] | None] = ContextVar("leaps_option_cache_stats", default=None)


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


def new_cache_stats() -> dict[str, object]:
    return {
        "outcome": {"memory_hit": 0, "disk_hit": 0, "miss": 0, "write": 0},
        "contract": {"memory_hit": 0, "disk_hit": 0, "miss": 0, "write": 0},
        "bars": {"memory_hit": 0, "disk_hit": 0, "miss": 0, "partial": 0, "write": 0},
        "polygon_requests": 0,
        "polygon_wait_ms": 0,
        "polygon_retries": 0,
        "polygon_429s": 0,
    }


def _increment_cache_stat(section: str, key: str | None = None, amount: int | float = 1) -> None:
    stats = _REQUEST_CACHE_STATS.get()
    if not isinstance(stats, dict):
        return
    if key is None:
        current = stats.get(section, 0)
        if isinstance(current, (int, float)):
            stats[section] = current + amount
        return
    bucket = stats.get(section)
    if not isinstance(bucket, dict):
        return
    current = bucket.get(key, 0)
    if isinstance(current, (int, float)):
        bucket[key] = current + amount


def _with_cache_stats(stats: dict[str, object] | None):
    return _REQUEST_CACHE_STATS.set(stats)


def _reset_cache_stats(token) -> None:
    _REQUEST_CACHE_STATS.reset(token)


_LOCKS_LOCK = threading.Lock()
_KEY_LOCKS: dict[str, threading.Lock] = {}


def _lock_for(prefix: str, key: str) -> threading.Lock:
    lock_key = f"{prefix}:{key}"
    with _LOCKS_LOCK:
        lock = _KEY_LOCKS.get(lock_key)
        if lock is None:
            lock = threading.Lock()
            _KEY_LOCKS[lock_key] = lock
        return lock


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


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _safe_cache_name(value: str) -> str:
    normalized = re.sub(r"[^A-Za-z0-9._-]+", "_", value.strip())
    normalized = normalized.strip("._")
    return normalized or "empty"


def _read_json_file(path: Path) -> dict[str, object] | None:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        logger.info("LEAPS option cache ignored", extra={"path": str(path), "error": str(exc)})
        return None
    if not isinstance(payload, dict):
        logger.info("LEAPS option cache ignored", extra={"path": str(path), "error": "payload is not object"})
        return None
    if payload.get("schema_version") != OPTION_CACHE_SCHEMA_VERSION:
        logger.info("LEAPS option cache ignored", extra={"path": str(path), "error": "schema_version mismatch"})
        return None
    return payload


def _write_json_file(path: Path, payload: dict[str, object]) -> None:
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_name(f".{path.name}.{threading.get_ident()}.tmp")
        tmp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True), encoding="utf-8")
        tmp_path.replace(path)
    except OSError as exc:
        logger.info("LEAPS option cache write failed", extra={"path": str(path), "error": str(exc)})


def _is_cache_fresh(boundary: date, cache_date: object, today: date) -> bool:
    if boundary < today:
        return True
    return cache_date == today.isoformat()


def _bar_payload_to_bars(items: object) -> list[OptionBar] | None:
    if not isinstance(items, list):
        return None
    bars: list[OptionBar] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        bar_date = parse_iso_date(item.get("date"))
        close = _float_or_none(item.get("close"))
        if bar_date is None or close is None:
            return None
        bars.append(OptionBar(date=bar_date, close=close))
    bars.sort(key=lambda bar: bar.date)
    return bars


def _bars_to_payload(bars: list[OptionBar]) -> list[dict[str, object]]:
    return [{"date": bar.date.isoformat(), "close": bar.close} for bar in sorted(bars, key=lambda item: item.date)]


def _parse_covered_ranges(items: object) -> list[tuple[date, date]] | None:
    if not isinstance(items, list):
        return None
    ranges: list[tuple[date, date]] = []
    for item in items:
        if not isinstance(item, dict):
            return None
        start = parse_iso_date(item.get("start"))
        end = parse_iso_date(item.get("end"))
        if start is None or end is None or end < start:
            return None
        ranges.append((start, end))
    return _merge_covered_ranges(ranges)


def _merge_covered_ranges(ranges: list[tuple[date, date]]) -> list[tuple[date, date]]:
    if not ranges:
        return []
    ordered = sorted(ranges, key=lambda item: (item[0], item[1]))
    merged: list[tuple[date, date]] = [ordered[0]]
    for start, end in ordered[1:]:
        prev_start, prev_end = merged[-1]
        if start <= prev_end + timedelta(days=1):
            merged[-1] = (prev_start, max(prev_end, end))
        else:
            merged.append((start, end))
    return merged


def _covered_ranges_to_payload(ranges: list[tuple[date, date]]) -> list[dict[str, str]]:
    return [{"start": start.isoformat(), "end": end.isoformat()} for start, end in _merge_covered_ranges(ranges)]


def _ranges_cover(ranges: list[tuple[date, date]], start: date, end: date) -> bool:
    return any(range_start <= start and end <= range_end for range_start, range_end in ranges)


def _missing_ranges(covered_ranges: list[tuple[date, date]], start: date, end: date) -> list[tuple[date, date]]:
    missing: list[tuple[date, date]] = []
    cursor = start
    for range_start, range_end in _merge_covered_ranges(covered_ranges):
        if range_end < cursor:
            continue
        if range_start > end:
            break
        if cursor < range_start:
            missing.append((cursor, min(end, range_start - timedelta(days=1))))
        cursor = max(cursor, range_end + timedelta(days=1))
        if cursor > end:
            break
    if cursor <= end:
        missing.append((cursor, end))
    return missing


def _fresh_covered_ranges(ranges: list[tuple[date, date]], cache_date: object, today: date) -> list[tuple[date, date]]:
    if cache_date == today.isoformat():
        return ranges
    fresh_ranges: list[tuple[date, date]] = []
    stale_start = today
    for start, end in ranges:
        if start >= stale_start:
            continue
        fresh_ranges.append((start, min(end, stale_start - timedelta(days=1))))
    return _merge_covered_ranges(fresh_ranges)


def _normalized_outcome_signal_fields(signal: dict[str, object]) -> dict[str, str]:
    stock_buy_price = _float_or_none(signal.get("stock_buy_price"))
    return {
        "symbol": str(signal.get("symbol") or "").strip().upper(),
        "date": str(signal.get("date") or "").strip()[:10],
        "stock_buy_price": "" if stock_buy_price is None else f"{stock_buy_price:.6f}".rstrip("0").rstrip("."),
        "next_stock_sell_date": str(signal.get("next_stock_sell_date") or "").strip()[:10],
        "selection_policy_version": LEAPS_OPTION_SELECTION_POLICY_VERSION,
    }


def outcome_cache_key(signal: dict[str, object], mark_date: date | None = None) -> str:
    fields = _normalized_outcome_signal_fields(signal)
    fields["mark_date"] = mark_date.isoformat() if mark_date else ""
    return "__".join(f"{key}_{_safe_cache_name(value)}" for key, value in sorted(fields.items()))


def clone_outcome_for_signal(outcome: dict[str, object], signal: dict[str, object]) -> dict[str, object]:
    return {
        **outcome,
        "signal_key": str(signal.get("signal_key") or outcome.get("signal_key") or ""),
        "date": str(signal.get("date") or outcome.get("date") or outcome.get("entry_date") or ""),
        "symbol": str(signal.get("symbol") or outcome.get("symbol") or ""),
        "entry_date": str(outcome.get("entry_date") or signal.get("date") or ""),
        "stock_sell_date": str(signal.get("next_stock_sell_date") or outcome.get("stock_sell_date") or ""),
    }


def _outcome_is_api_limited_or_transient(outcome: dict[str, object]) -> bool:
    reason = str(outcome.get("skipped_reason") or "")
    return bool(reason.startswith(API_LIMIT_OR_TIMEOUT) or "429" in reason or "timeout" in reason.lower() or "timed out" in reason.lower())


def _outcome_cache_keys_for_read(signal: dict[str, object]) -> list[str]:
    sell_date = parse_iso_date(signal.get("next_stock_sell_date"))
    if sell_date:
        return [outcome_cache_key(signal)]
    return [outcome_cache_key(signal), outcome_cache_key(signal, _utc_today())]


def _outcome_cache_keys_for_write(signal: dict[str, object], outcome: dict[str, object]) -> list[str]:
    if _outcome_is_api_limited_or_transient(outcome):
        return []
    reason = str(outcome.get("skipped_reason") or "")
    if outcome.get("status") != "success" and reason not in OUTCOME_CACHEABLE_FAILURES:
        return []
    sell_date = parse_iso_date(signal.get("next_stock_sell_date"))
    exit_status = str(outcome.get("exit_status") or "")
    if sell_date or exit_status in {"sold", "expired_before_stock_sell", "expired_without_stock_sell"}:
        return [outcome_cache_key(signal)]
    if outcome.get("status") != "success" and reason in OUTCOME_CACHEABLE_FAILURES:
        return [outcome_cache_key(signal)]
    return [outcome_cache_key(signal, _utc_today())]


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
            _increment_cache_stat("polygon_requests")
            _increment_cache_stat("polygon_wait_ms", amount=round(wait_seconds * 1000, 3))
            if attempt:
                _increment_cache_stat("polygon_retries")
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
            if status == 429:
                _increment_cache_stat("polygon_429s")
            if status not in {429, 502, 503, 504}:
                raise
        if attempt < 2:
            time.sleep(0.5 * (2**attempt))
    if last_exc:
        raise last_exc
    return {}


class OutcomeCache:
    def __init__(self, cache_dir: Path | None = None, cache_enabled: bool = True):
        self.cache_enabled = cache_enabled
        self.cache_dir = cache_dir or ROOT_DIR / "data" / "leaps_option_cache" / "outcomes"
        self._memory: dict[str, dict[str, object]] = {}

    def _path(self, key: str) -> Path:
        return self.cache_dir / f"{_safe_cache_name(key)}.json"

    def read(self, signal: dict[str, object]) -> dict[str, object] | None:
        if not self.cache_enabled:
            return None
        for key in _outcome_cache_keys_for_read(signal):
            lock = _lock_for("outcome", key)
            with lock:
                cached = self._memory.get(key)
                if cached is not None:
                    _increment_cache_stat("outcome", "memory_hit")
                    return clone_outcome_for_signal(cached, signal)
                payload = _read_json_file(self._path(key))
                if payload is None:
                    continue
                query = payload.get("query")
                outcome = payload.get("outcome")
                if not isinstance(query, dict) or not isinstance(outcome, dict):
                    logger.info("LEAPS option outcome cache ignored", extra={"key": key, "error": "missing fields"})
                    continue
                self._memory[key] = outcome
                _increment_cache_stat("outcome", "disk_hit")
                return clone_outcome_for_signal(outcome, signal)
        _increment_cache_stat("outcome", "miss")
        return None

    def write(self, signal: dict[str, object], outcome: dict[str, object]) -> None:
        if not self.cache_enabled:
            return
        for key in _outcome_cache_keys_for_write(signal, outcome):
            stored = clone_outcome_for_signal(outcome, {**signal, "signal_key": ""})
            stored["signal_key"] = ""
            lock = _lock_for("outcome", key)
            with lock:
                self._memory[key] = stored
                payload: dict[str, object] = {
                    "schema_version": OPTION_CACHE_SCHEMA_VERSION,
                    "cache_date": _utc_today().isoformat(),
                    "query": _normalized_outcome_signal_fields(signal),
                    "outcome": stored,
                }
                _write_json_file(self._path(key), payload)
                _increment_cache_stat("outcome", "write")


_OUTCOME_CACHE = OutcomeCache()


class PolygonMonthlyOptionProvider:
    def __init__(self, api_key: str, timeout: int = 15, cache_dir: Path | None = None, cache_enabled: bool = True):
        self.api_key = api_key
        self.timeout = timeout
        self.base_url = "https://api.polygon.io"
        self._contracts_cache: dict[tuple[str, str, str, str], list[dict[str, object]]] = {}
        self._contracts_cache_dates: dict[tuple[str, str, str, str], str] = {}
        self._bars_cache: dict[tuple[str, str, str], list[OptionBar]] = {}
        self._bars_cache_dates: dict[tuple[str, str, str], str] = {}
        self.cache_enabled = cache_enabled
        self.cache_dir = cache_dir or ROOT_DIR / "data" / "leaps_option_cache"
        self._contracts_cache_dir = self.cache_dir / "contracts"
        self._bars_cache_dir = self.cache_dir / "bars"

    def _contracts_cache_path(self, underlying: str, as_of: date, start_expiration: date, end_expiration: date) -> Path:
        filename = _safe_cache_name(
            f"{underlying}__asof_{as_of.isoformat()}__exp_{start_expiration.isoformat()}_to_{end_expiration.isoformat()}.json"
        )
        return self._contracts_cache_dir / filename

    def _bars_cache_path(self, ticker: str) -> Path:
        return self._bars_cache_dir / f"{_safe_cache_name(ticker)}.json"

    def _read_contracts_cache(
        self,
        underlying: str,
        as_of: date,
        start_expiration: date,
        end_expiration: date,
    ) -> list[dict[str, object]] | None:
        if not self.cache_enabled:
            return None
        path = self._contracts_cache_path(underlying, as_of, start_expiration, end_expiration)
        payload = _read_json_file(path)
        today = _utc_today()
        if payload is None or not _is_cache_fresh(as_of, payload.get("cache_date"), today):
            if payload is not None:
                logger.info(
                    "LEAPS option contracts cache stale",
                    extra={"underlying": underlying, "as_of": as_of.isoformat(), "path": str(path)},
                )
            return None
        query = payload.get("query")
        contracts = payload.get("contracts")
        if not isinstance(query, dict) or not isinstance(contracts, list):
            logger.info("LEAPS option contracts cache ignored", extra={"path": str(path), "error": "missing fields"})
            return None
        expected_query = {
            "underlying": underlying,
            "as_of": as_of.isoformat(),
            "expiration_start": start_expiration.isoformat(),
            "expiration_end": end_expiration.isoformat(),
        }
        if any(query.get(key) != value for key, value in expected_query.items()):
            logger.info("LEAPS option contracts cache ignored", extra={"path": str(path), "error": "query mismatch"})
            return None
        normalized = [item for item in contracts if isinstance(item, dict)]
        if len(normalized) != len(contracts):
            logger.info("LEAPS option contracts cache ignored", extra={"path": str(path), "error": "invalid contracts"})
            return None
        logger.debug("LEAPS option contracts cache hit", extra={"underlying": underlying, "path": str(path)})
        return normalized

    def _write_contracts_cache(
        self,
        underlying: str,
        as_of: date,
        start_expiration: date,
        end_expiration: date,
        contracts: list[dict[str, object]],
    ) -> None:
        if not self.cache_enabled:
            return
        path = self._contracts_cache_path(underlying, as_of, start_expiration, end_expiration)
        payload: dict[str, object] = {
            "schema_version": OPTION_CACHE_SCHEMA_VERSION,
            "cache_date": _utc_today().isoformat(),
            "query": {
                "underlying": underlying,
                "as_of": as_of.isoformat(),
                "expiration_start": start_expiration.isoformat(),
                "expiration_end": end_expiration.isoformat(),
            },
            "contracts": contracts,
        }
        _write_json_file(path, payload)
        _increment_cache_stat("contract", "write")
        logger.debug("LEAPS option contracts cache written", extra={"underlying": underlying, "path": str(path), "count": len(contracts)})

    def _read_bars_cache(self, ticker: str, start: date, end: date) -> tuple[list[OptionBar], list[tuple[date, date]], str | None] | None:
        if not self.cache_enabled:
            return None
        path = self._bars_cache_path(ticker)
        payload = _read_json_file(path)
        today = _utc_today()
        if payload is None:
            return None
        if payload.get("ticker") != ticker:
            logger.info("LEAPS option bars cache ignored", extra={"path": str(path), "error": "ticker mismatch"})
            return None
        bars = _bar_payload_to_bars(payload.get("bars"))
        covered_ranges = _parse_covered_ranges(payload.get("covered_ranges"))
        if bars is None or covered_ranges is None:
            logger.info("LEAPS option bars cache ignored", extra={"path": str(path), "error": "missing fields"})
            return None
        cache_date = payload.get("cache_date")
        covered_ranges = _fresh_covered_ranges(covered_ranges, cache_date, today)
        return bars, covered_ranges, str(cache_date or "")

    def _write_bars_cache(self, ticker: str, bars: list[OptionBar], covered_ranges: list[tuple[date, date]]) -> None:
        if not self.cache_enabled:
            return
        path = self._bars_cache_path(ticker)
        unique_by_date = {bar.date: bar for bar in bars}
        payload: dict[str, object] = {
            "schema_version": OPTION_CACHE_SCHEMA_VERSION,
            "cache_date": _utc_today().isoformat(),
            "ticker": ticker,
            "covered_ranges": _covered_ranges_to_payload(covered_ranges),
            "bars": _bars_to_payload(list(unique_by_date.values())),
        }
        _write_json_file(path, payload)
        _increment_cache_stat("bars", "write")
        logger.debug("LEAPS option bars cache written", extra={"ticker": ticker, "path": str(path), "count": len(unique_by_date)})

    def fetch_contracts(self, underlying: str, as_of: date, start_expiration: date, end_expiration: date) -> list[dict[str, object]]:
        cache_key = (underlying, as_of.isoformat(), start_expiration.isoformat(), end_expiration.isoformat())
        lock = _lock_for("contract", "|".join(cache_key))
        with lock:
            today = _utc_today()
            if cache_key in self._contracts_cache and _is_cache_fresh(as_of, self._contracts_cache_dates.get(cache_key), today):
                _increment_cache_stat("contract", "memory_hit")
                logger.debug("LEAPS option contracts memory cache hit", extra={"underlying": underlying, "as_of": as_of.isoformat()})
                return self._contracts_cache[cache_key]
            cached_contracts = self._read_contracts_cache(underlying, as_of, start_expiration, end_expiration)
            if cached_contracts is not None:
                _increment_cache_stat("contract", "disk_hit")
                self._contracts_cache[cache_key] = cached_contracts
                self._contracts_cache_dates[cache_key] = today.isoformat()
                return cached_contracts
            _increment_cache_stat("contract", "miss")
            logger.info("LEAPS option contracts cache miss", extra={"underlying": underlying, "as_of": as_of.isoformat()})
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
            self._contracts_cache_dates[cache_key] = today.isoformat()
            self._write_contracts_cache(underlying, as_of, start_expiration, end_expiration, contracts)
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
        lock = _lock_for("bars", ticker)
        with lock:
            today = _utc_today()
            if cache_key in self._bars_cache and _is_cache_fresh(end, self._bars_cache_dates.get(cache_key), today):
                _increment_cache_stat("bars", "memory_hit")
                logger.debug("LEAPS option bars memory cache hit", extra={"ticker": ticker, "start": start.isoformat(), "end": end.isoformat()})
                return self._bars_cache[cache_key]
            cached = self._read_bars_cache(ticker, start, end)
            if cached is not None:
                cached_bars, covered_ranges, _cache_date = cached
                if _ranges_cover(covered_ranges, start, end):
                    _increment_cache_stat("bars", "disk_hit")
                    bars = [bar for bar in cached_bars if start <= bar.date <= end]
                    self._bars_cache[cache_key] = bars
                    self._bars_cache_dates[cache_key] = today.isoformat()
                    logger.debug("LEAPS option bars cache hit", extra={"ticker": ticker, "start": start.isoformat(), "end": end.isoformat()})
                    return bars
                _increment_cache_stat("bars", "partial")
                logger.info("LEAPS option bars cache partial", extra={"ticker": ticker, "start": start.isoformat(), "end": end.isoformat()})
            else:
                cached_bars = []
                covered_ranges = []
                _increment_cache_stat("bars", "miss")
                logger.info("LEAPS option bars cache miss", extra={"ticker": ticker, "start": start.isoformat(), "end": end.isoformat()})

            fetched_bars: list[OptionBar] = []
            fetched_ranges = _missing_ranges(covered_ranges, start, end)
            for fetch_start, fetch_end in fetched_ranges:
                payload = _polygon_retry_get(
                    f"{self.base_url}/v2/aggs/ticker/{ticker}/range/1/day/{fetch_start.isoformat()}/{fetch_end.isoformat()}",
                    {"adjusted": "true", "sort": "asc", "apiKey": self.api_key},
                    self.timeout,
                )
                for item in payload.get("results") or []:
                    close = _float_or_none(item.get("c"))
                    timestamp = item.get("t")
                    if close is None or timestamp is None:
                        continue
                    fetched_bars.append(OptionBar(date=datetime.fromtimestamp(float(timestamp) / 1000, timezone.utc).date(), close=close))
            fetched_bars.sort(key=lambda bar: bar.date)
            merged_by_date = {bar.date: bar for bar in cached_bars}
            merged_by_date.update({bar.date: bar for bar in fetched_bars})
            merged_bars = sorted(merged_by_date.values(), key=lambda bar: bar.date)
            merged_ranges = _merge_covered_ranges([*covered_ranges, *fetched_ranges])
            self._write_bars_cache(ticker, merged_bars, merged_ranges)
            requested_bars = [bar for bar in merged_bars if start <= bar.date <= end]
            self._bars_cache[cache_key] = requested_bars
            self._bars_cache_dates[cache_key] = today.isoformat()
            return requested_bars


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


def last_bar_after_on_or_before(bars: list[OptionBar], after: date, target: date) -> OptionBar | None:
    candidates = [bar for bar in bars if after < bar.date <= target]
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
    if sell_date and sell_date <= signal_date:
        return skipped_outcome(signal, NO_STOCK_SELL)

    contract, reason = provider.select_monthly_call(underlying, signal_date, stock_buy_price)
    if contract is None:
        return skipped_outcome(signal, reason)

    entry_end = min(signal_date + timedelta(days=7), contract.expiration)
    if sell_date:
        history_end = max(entry_end, min(sell_date + timedelta(days=7), contract.expiration))
    else:
        today = _utc_today()
        history_end = max(entry_end, min(today, contract.expiration))
    bars = provider.fetch_bars(contract.ticker, signal_date, history_end)
    entry_bar = first_bar_on_or_after(bars, signal_date, 7, latest=contract.expiration)
    if not entry_bar:
        return {**skipped_outcome(signal, NO_ENTRY_PRICE), **contract_payload(contract)}

    if not sell_date:
        today = _utc_today()
        exit_boundary = min(today, contract.expiration)
        exit_bar = last_bar_after_on_or_before(bars, entry_bar.date, exit_boundary)
        if not exit_bar:
            return {**skipped_outcome(signal, NO_EXIT_PRICE), **contract_payload(contract), "entry_price": entry_bar.close}
        exit_status = "expired_without_stock_sell" if today >= contract.expiration else "holding"
    elif sell_date > contract.expiration:
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
        "stock_sell_date": sell_date.isoformat() if sell_date else "",
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
    outcome_cache: OutcomeCache | None = None,
) -> dict[str, object]:
    stats = new_cache_stats()
    token = _with_cache_stats(stats)
    try:
        result = _replay_leaps_option_outcomes(signals, api_key, provider=provider, outcome_cache=outcome_cache, use_cache=True)
        return {**result, "cache_stats": stats}
    finally:
        _reset_cache_stats(token)


def _replay_leaps_option_outcomes(
    signals: list[dict[str, object]],
    api_key: str,
    provider: PolygonMonthlyOptionProvider | None = None,
    outcome_cache: OutcomeCache | None = None,
    use_cache: bool = True,
) -> dict[str, object]:
    if not api_key and provider is None:
        outcomes = [skipped_outcome(signal, NO_POLYGON_KEY) for signal in signals]
        return {"success": False, "message": "Polygon API key 未配置", "outcomes": outcomes, "summary": summarize_outcomes(outcomes)}
    active_provider = provider or PolygonMonthlyOptionProvider(api_key)
    active_outcome_cache = outcome_cache if outcome_cache is not None else (_OUTCOME_CACHE if provider is None else None)
    outcomes: list[dict[str, object]] = []
    for signal in signals:
        if use_cache and active_outcome_cache is not None:
            cached = active_outcome_cache.read(signal)
            if cached is not None:
                outcomes.append(cached)
                continue
        try:
            outcome = replay_signal(active_provider, signal)
            if use_cache and active_outcome_cache is not None:
                active_outcome_cache.write(signal, outcome)
            outcomes.append(outcome)
        except (requests.Timeout, requests.ConnectionError) as exc:
            outcomes.append(skipped_outcome(signal, f"{API_LIMIT_OR_TIMEOUT}: {exc}"))
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            reason = API_LIMIT_OR_TIMEOUT if status in {429, 502, 503, 504} else f"Polygon API 错误: {status or exc}"
            outcomes.append(skipped_outcome(signal, reason))
        except Exception as exc:
            outcomes.append(skipped_outcome(signal, f"Polygon API 错误: {exc}"))
    return {"success": True, "outcomes": outcomes, "summary": summarize_outcomes(outcomes)}


def replay_leaps_option_outcomes_batch(
    signals: list[dict[str, object]],
    api_key: str,
    provider: PolygonMonthlyOptionProvider | None = None,
    outcome_cache: OutcomeCache | None = None,
) -> dict[str, object]:
    stats = new_cache_stats()
    token = _with_cache_stats(stats)
    try:
        result = _replay_leaps_option_outcomes_batch(signals, api_key, provider=provider, outcome_cache=outcome_cache)
        return {**result, "cache_stats": stats}
    finally:
        _reset_cache_stats(token)


def _replay_leaps_option_outcomes_batch(
    signals: list[dict[str, object]],
    api_key: str,
    provider: PolygonMonthlyOptionProvider | None = None,
    outcome_cache: OutcomeCache | None = None,
) -> dict[str, object]:
    if not api_key and provider is None:
        outcomes = [skipped_outcome(signal, NO_POLYGON_KEY) for signal in signals]
        return {"success": False, "message": "Polygon API key 未配置", "outcomes": outcomes, "summary": summarize_outcomes(outcomes)}

    active_provider = provider or PolygonMonthlyOptionProvider(api_key)
    active_outcome_cache = outcome_cache if outcome_cache is not None else (_OUTCOME_CACHE if provider is None else None)
    unique_by_key: dict[str, dict[str, object]] = {}
    key_by_index: list[str] = []
    for signal in signals:
        key = outcome_cache_key(signal, _utc_today() if not parse_iso_date(signal.get("next_stock_sell_date")) else None)
        key_by_index.append(key)
        unique_by_key.setdefault(key, signal)

    outcome_by_key: dict[str, dict[str, object]] = {}
    for key, signal in unique_by_key.items():
        if active_outcome_cache is not None:
            cached = active_outcome_cache.read(signal)
            if cached is not None:
                outcome_by_key[key] = cached
                continue
        lock = _lock_for("outcome-replay", key)
        with lock:
            if active_outcome_cache is not None:
                cached = active_outcome_cache.read(signal)
                if cached is not None:
                    outcome_by_key[key] = cached
                    continue
            try:
                outcome = replay_signal(active_provider, signal)
                if active_outcome_cache is not None:
                    active_outcome_cache.write(signal, outcome)
                outcome_by_key[key] = outcome
            except (requests.Timeout, requests.ConnectionError) as exc:
                outcome_by_key[key] = skipped_outcome(signal, f"{API_LIMIT_OR_TIMEOUT}: {exc}")
            except requests.HTTPError as exc:
                status = getattr(exc.response, "status_code", None)
                reason = API_LIMIT_OR_TIMEOUT if status in {429, 502, 503, 504} else f"Polygon API 错误: {status or exc}"
                outcome_by_key[key] = skipped_outcome(signal, reason)
            except Exception as exc:
                outcome_by_key[key] = skipped_outcome(signal, f"Polygon API 错误: {exc}")

    outcomes = [
        clone_outcome_for_signal(outcome_by_key.get(key) or skipped_outcome(signal, "期权收益计算失败"), signal)
        for key, signal in zip(key_by_index, signals)
    ]
    return {"success": True, "outcomes": outcomes, "summary": summarize_outcomes(outcomes)}
