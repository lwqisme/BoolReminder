"""Replay LEAPS-style signal outcomes with historical option bars."""

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
ALPACA_DATA_UNAVAILABLE_BEFORE_2024_02 = "Alpaca 期权历史数据仅支持 2024-02 以后"
UNSUPPORTED_UNDERLYING = "标的不支持"
NO_MONTHLY_CONTRACT = "无月度合约"
NO_200_300D_CONTRACT = "无 200-300D 合约"
NO_ENTRY_PRICE = "无可用入场价"
NO_EXIT_PRICE = "无可用卖出价"
NO_STOCK_SELL = "无下一次正股卖点"
NO_PRE_EXPIRATION_PRICE = "卖点晚于到期且无可用到期前价格"
API_LIMIT_OR_TIMEOUT = "API 限流/超时"
POLYGON_PERMISSION_DENIED = "Polygon API 无权限/套餐不支持期权历史K线"
ALPACA_PERMISSION_DENIED = "Alpaca API 无权限/套餐不支持期权历史K线"
ALPACA_OPTION_DATA_START = date(2024, 2, 1)
POLYGON_REQUEST_INTERVAL_SECONDS = 1.0
OPTION_CACHE_SCHEMA_VERSION = 1
LEAPS_OPTION_SELECTION_POLICY_VERSION = "monthly-call-200-300d-otm10-v2"
POLYGON_BATCH_429_CIRCUIT_BREAKER = 6
POLYGON_BATCH_403_CIRCUIT_BREAKER = 1
OUTCOME_CACHEABLE_FAILURES = {
    ALPACA_DATA_UNAVAILABLE_BEFORE_2024_02,
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
        "provider": "polygon",
        "provider_requests": 0,
        "outcome": {"memory_hit": 0, "disk_hit": 0, "miss": 0, "write": 0},
        "contract": {"memory_hit": 0, "disk_hit": 0, "miss": 0, "write": 0},
        "bars": {"memory_hit": 0, "disk_hit": 0, "miss": 0, "partial": 0, "write": 0},
        "polygon_requests": 0,
        "polygon_wait_ms": 0,
        "polygon_retries": 0,
        "polygon_429s": 0,
        "alpaca_requests": 0,
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


def _set_cache_stat(section: str, value: object) -> None:
    stats = _REQUEST_CACHE_STATS.get()
    if isinstance(stats, dict):
        stats[section] = value


def _with_cache_stats(stats: dict[str, object] | None):
    return _REQUEST_CACHE_STATS.set(stats)


def _reset_cache_stats(token) -> None:
    _REQUEST_CACHE_STATS.reset(token)


def _cache_stats_snapshot() -> dict[str, object]:
    stats = _REQUEST_CACHE_STATS.get()
    if not isinstance(stats, dict):
        return {}
    snapshot: dict[str, object] = {}
    for key, value in stats.items():
        snapshot[key] = dict(value) if isinstance(value, dict) else value
    return snapshot


def _cache_stats_delta(before: dict[str, object], after: dict[str, object] | None = None) -> dict[str, object]:
    after = after or _cache_stats_snapshot()
    delta: dict[str, object] = {}
    for key, value in after.items():
        previous = before.get(key, {} if isinstance(value, dict) else 0)
        if isinstance(value, dict):
            bucket: dict[str, object] = {}
            previous_bucket = previous if isinstance(previous, dict) else {}
            for inner_key, inner_value in value.items():
                bucket[inner_key] = inner_value - previous_bucket.get(inner_key, 0)
            delta[key] = bucket
        elif isinstance(value, (int, float)) and isinstance(previous, (int, float)):
            delta[key] = value - previous
        elif key not in before:
            delta[key] = value
    return delta


def _log_option_event(event: str, **fields: object) -> None:
    logger.info("LEAPS option %s %s", event, fields)


def _delta_429_count(delta: dict[str, object]) -> int:
    value = delta.get("polygon_429s", 0)
    return int(value) if isinstance(value, (int, float)) else 0


class OptionProviderPermissionError(Exception):
    pass


class OptionProviderTransientError(Exception):
    pass


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


def _finite_float_or_none(value: object) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number == number else None


def _utc_today() -> date:
    return datetime.now(timezone.utc).date()


def _is_completed_market_date(value: date) -> bool:
    if value.weekday() >= 5:
        return False
    return value not in observed_us_market_holidays(value.year) and value not in observed_us_market_holidays(value.year + 1)


def latest_completed_market_date(today: date | None = None) -> date:
    current = today or _utc_today()
    while not _is_completed_market_date(current):
        current -= timedelta(days=1)
    return current


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


def _normalized_outcome_signal_fields(
    signal: dict[str, object],
    provider_id: str = "polygon",
) -> dict[str, str]:
    stock_buy_price = _float_or_none(signal.get("stock_buy_price"))
    fields = {
        "provider": provider_id or "polygon",
        "symbol": str(signal.get("symbol") or "").strip().upper(),
        "date": str(signal.get("date") or "").strip()[:10],
        "stock_buy_price": "" if stock_buy_price is None else f"{stock_buy_price:.6f}".rstrip("0").rstrip("."),
        "next_stock_sell_date": str(signal.get("next_stock_sell_date") or "").strip()[:10],
        "selection_policy_version": LEAPS_OPTION_SELECTION_POLICY_VERSION,
    }
    return fields


def outcome_cache_key(
    signal: dict[str, object],
    mark_date: date | None = None,
    provider_id: str = "polygon",
) -> str:
    fields = _normalized_outcome_signal_fields(signal, provider_id=provider_id)
    fields["mark_date"] = mark_date.isoformat() if mark_date else ""
    return "__".join(f"{key}_{_safe_cache_name(value)}" for key, value in sorted(fields.items()))


def _legacy_outcome_cache_key(signal: dict[str, object], mark_date: date | None = None) -> str:
    fields = _normalized_outcome_signal_fields(signal, provider_id="")
    fields.pop("provider", None)
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


def _polygon_http_error_reason(status: int | None, exc: Exception) -> str:
    if status == 403:
        return POLYGON_PERMISSION_DENIED
    if status in {429, 502, 503, 504}:
        return API_LIMIT_OR_TIMEOUT
    return f"Polygon API 错误: {status or exc}"


def _safe_polygon_error_message(exc: Exception) -> str:
    message = str(exc)
    message = re.sub(r"([?&]apiKey=)[^&\s]+", r"\1<redacted>", message)
    message = re.sub(r"([?&]api_key=)[^&\s]+", r"\1<redacted>", message, flags=re.I)
    return message


def _outcome_cache_keys_for_read(
    signal: dict[str, object],
    provider_id: str = "polygon",
) -> list[str]:
    sell_date = parse_iso_date(signal.get("next_stock_sell_date"))
    if sell_date:
        keys = [outcome_cache_key(signal, provider_id=provider_id)]
        if provider_id == "polygon":
            keys.append(_legacy_outcome_cache_key(signal))
        return keys
    mark_date = latest_completed_market_date()
    keys = [
        outcome_cache_key(signal, provider_id=provider_id),
        outcome_cache_key(signal, mark_date, provider_id=provider_id),
    ]
    if provider_id == "polygon":
        keys.extend([_legacy_outcome_cache_key(signal), _legacy_outcome_cache_key(signal, mark_date)])
    return keys


def _outcome_cache_keys_for_write(
    signal: dict[str, object],
    outcome: dict[str, object],
    provider_id: str = "polygon",
) -> list[str]:
    if _outcome_is_api_limited_or_transient(outcome):
        return []
    reason = str(outcome.get("skipped_reason") or "")
    if outcome.get("status") != "success" and reason not in OUTCOME_CACHEABLE_FAILURES:
        return []
    sell_date = parse_iso_date(signal.get("next_stock_sell_date"))
    exit_status = str(outcome.get("exit_status") or "")
    if sell_date or exit_status in {"sold", "expired_before_stock_sell", "expired_without_stock_sell"}:
        return [outcome_cache_key(signal, provider_id=provider_id)]
    if outcome.get("status") != "success" and reason in OUTCOME_CACHEABLE_FAILURES:
        return [outcome_cache_key(signal, provider_id=provider_id)]
    return [outcome_cache_key(signal, latest_completed_market_date(), provider_id=provider_id)]


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
        request_started = time.perf_counter()
        try:
            wait_seconds = _POLYGON_RATE_LIMITER.wait()
            _increment_cache_stat("polygon_requests")
            _increment_cache_stat("provider_requests")
            _increment_cache_stat("polygon_wait_ms", amount=round(wait_seconds * 1000, 3))
            if attempt:
                _increment_cache_stat("polygon_retries")
            _log_option_event(
                "polygon_request_start",
                endpoint=url.replace("https://api.polygon.io", ""),
                attempt=attempt + 1,
                wait_ms=round(wait_seconds * 1000, 3),
            )
            response = requests.get(url, params=params, timeout=timeout)
            response.raise_for_status()
            _log_option_event(
                "polygon_request_done",
                endpoint=url.replace("https://api.polygon.io", ""),
                attempt=attempt + 1,
                status_code=response.status_code,
                elapsed_ms=round((time.perf_counter() - request_started) * 1000, 3),
            )
            return response.json()
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            _log_option_event(
                "polygon_request_retryable_error",
                endpoint=url.replace("https://api.polygon.io", ""),
                attempt=attempt + 1,
                error=type(exc).__name__,
                elapsed_ms=round((time.perf_counter() - request_started) * 1000, 3),
            )
        except requests.HTTPError as exc:
            last_exc = exc
            status = getattr(exc.response, "status_code", None)
            if status == 429:
                _increment_cache_stat("polygon_429s")
            if status not in {429, 502, 503, 504}:
                _log_option_event(
                    "polygon_request_http_error",
                    endpoint=url.replace("https://api.polygon.io", ""),
                    attempt=attempt + 1,
                    status_code=status,
                    elapsed_ms=round((time.perf_counter() - request_started) * 1000, 3),
                )
                raise
            _log_option_event(
                "polygon_request_retryable_http_error",
                endpoint=url.replace("https://api.polygon.io", ""),
                attempt=attempt + 1,
                status_code=status,
                elapsed_ms=round((time.perf_counter() - request_started) * 1000, 3),
            )
        if attempt < 2:
            sleep_seconds = 0.5 * (2**attempt)
            _log_option_event(
                "polygon_request_retry_sleep",
                endpoint=url.replace("https://api.polygon.io", ""),
                attempt=attempt + 1,
                sleep_ms=round(sleep_seconds * 1000, 3),
            )
            time.sleep(sleep_seconds)
    if last_exc:
        _log_option_event(
            "polygon_request_failed",
            endpoint=url.replace("https://api.polygon.io", ""),
            error=type(last_exc).__name__,
            message=_safe_polygon_error_message(last_exc),
        )
        raise last_exc
    return {}


OUTCOME_CACHE_MAX_AGE_DAYS = 30


def _file_is_stale(path: Path, max_age_days: int) -> bool:
    if max_age_days <= 0:
        return False
    try:
        mtime = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        return (datetime.now(timezone.utc) - mtime).days >= max_age_days
    except OSError:
        return True


class OutcomeCache:
    def __init__(self, cache_dir: Path | None = None, cache_enabled: bool = True, max_age_days: int = OUTCOME_CACHE_MAX_AGE_DAYS):
        self.cache_enabled = cache_enabled
        self.cache_dir = cache_dir or ROOT_DIR / "data" / "leaps_option_cache" / "outcomes"
        self.max_age_days = max_age_days
        self._memory: dict[str, dict[str, object]] = {}

    def _path(self, key: str) -> Path:
        return self.cache_dir / f"{_safe_cache_name(key)}.json"

    def _try_read_disk(self, key: str) -> dict[str, object] | None:
        path = self._path(key)
        if _file_is_stale(path, self.max_age_days):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return None
        payload = _read_json_file(path)
        if payload is None:
            return None
        query = payload.get("query")
        outcome = payload.get("outcome")
        if not isinstance(query, dict) or not isinstance(outcome, dict):
            logger.info("LEAPS option outcome cache ignored", extra={"key": key, "error": "missing fields"})
            return None
        return outcome

    def read(
        self,
        signal: dict[str, object],
        count_miss: bool = True,
        provider_id: str = "polygon",
    ) -> dict[str, object] | None:
        if not self.cache_enabled:
            return None
        for key in _outcome_cache_keys_for_read(signal, provider_id=provider_id):
            lock = _lock_for("outcome", key)
            with lock:
                cached = self._memory.get(key)
                if cached is not None:
                    _increment_cache_stat("outcome", "memory_hit")
                    _log_option_event(
                        "outcome_cache_memory_hit",
                        key=key,
                        signal_key=str(signal.get("signal_key") or ""),
                        symbol=str(signal.get("symbol") or ""),
                        date=str(signal.get("date") or ""),
                    )
                    return clone_outcome_for_signal(cached, signal)
                outcome = self._try_read_disk(key)
                if outcome is None:
                    continue
                self._memory[key] = outcome
                _increment_cache_stat("outcome", "disk_hit")
                _log_option_event(
                    "outcome_cache_disk_hit",
                    key=key,
                    signal_key=str(signal.get("signal_key") or ""),
                    symbol=str(signal.get("symbol") or ""),
                    date=str(signal.get("date") or ""),
                )
                return clone_outcome_for_signal(outcome, signal)
        if count_miss:
            _increment_cache_stat("outcome", "miss")
            _log_option_event(
                "outcome_cache_miss",
                signal_key=str(signal.get("signal_key") or ""),
                symbol=str(signal.get("symbol") or ""),
                date=str(signal.get("date") or ""),
                next_stock_sell_date=str(signal.get("next_stock_sell_date") or ""),
            )
        return None

    def write(
        self,
        signal: dict[str, object],
        outcome: dict[str, object],
        provider_id: str = "polygon",
    ) -> None:
        if not self.cache_enabled:
            return
        keys = _outcome_cache_keys_for_write(signal, outcome, provider_id=provider_id)
        if not keys:
            _log_option_event(
                "outcome_cache_skip_write",
                signal_key=str(signal.get("signal_key") or ""),
                symbol=str(signal.get("symbol") or ""),
                date=str(signal.get("date") or ""),
                status=str(outcome.get("status") or ""),
                skipped_reason=str(outcome.get("skipped_reason") or ""),
            )
            return
        for key in keys:
            stored = clone_outcome_for_signal(outcome, {**signal, "signal_key": ""})
            stored["signal_key"] = ""
            lock = _lock_for("outcome", key)
            with lock:
                self._memory[key] = stored
                payload: dict[str, object] = {
                    "schema_version": OPTION_CACHE_SCHEMA_VERSION,
                    "cache_date": _utc_today().isoformat(),
                    "query": _normalized_outcome_signal_fields(signal, provider_id=provider_id),
                    "outcome": stored,
                }
                _write_json_file(self._path(key), payload)
                _increment_cache_stat("outcome", "write")
                _log_option_event(
                    "outcome_cache_write",
                    key=key,
                    signal_key=str(signal.get("signal_key") or ""),
                    symbol=str(signal.get("symbol") or ""),
                    date=str(signal.get("date") or ""),
                    status=str(outcome.get("status") or ""),
                    exit_status=str(outcome.get("exit_status") or ""),
                    skipped_reason=str(outcome.get("skipped_reason") or ""),
                )

    def purge_stale(self) -> int:
        count = 0
        try:
            for path in self.cache_dir.glob("*.json"):
                if _file_is_stale(path, self.max_age_days):
                    try:
                        path.unlink()
                        count += 1
                    except OSError:
                        pass
        except OSError:
            pass
        if count:
            logger.info("LEAPS option outcome cache purged stale files", extra={"count": count, "max_age_days": self.max_age_days})
        return count


_OUTCOME_CACHE = OutcomeCache()


BARS_CONTRACTS_CACHE_MAX_AGE_DAYS = 90


class PolygonMonthlyOptionProvider:
    def __init__(self, api_key: str, timeout: int = 15, cache_dir: Path | None = None, cache_enabled: bool = True, max_age_days: int = BARS_CONTRACTS_CACHE_MAX_AGE_DAYS):
        self.api_key = api_key
        self.provider_name = "polygon"
        self.provider_label = "Polygon"
        self.cache_provider_id = "polygon"
        self.permission_denied_reason = POLYGON_PERMISSION_DENIED
        self.timeout = timeout
        self.base_url = "https://api.polygon.io"
        self._contracts_cache: dict[tuple[str, str, str, str], list[dict[str, object]]] = {}
        self._contracts_cache_dates: dict[tuple[str, str, str, str], str] = {}
        self._bars_cache: dict[tuple[str, str, str], list[OptionBar]] = {}
        self._bars_cache_dates: dict[tuple[str, str, str], str] = {}
        self.cache_enabled = cache_enabled
        self.max_age_days = max_age_days
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
        if _file_is_stale(path, self.max_age_days):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return None
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
        if _file_is_stale(path, self.max_age_days):
            try:
                path.unlink(missing_ok=True)
            except OSError:
                pass
            return None
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

    def purge_stale(self) -> dict[str, int]:
        counts: dict[str, int] = {}
        for label, directory in (("contracts", self._contracts_cache_dir), ("bars", self._bars_cache_dir)):
            count = 0
            try:
                for path in directory.glob("*.json"):
                    if _file_is_stale(path, self.max_age_days):
                        try:
                            path.unlink()
                            count += 1
                        except OSError:
                            pass
            except OSError:
                pass
            counts[label] = count
        total = sum(counts.values())
        if total:
            logger.info("LEAPS option provider cache purged stale files", extra={**counts, "max_age_days": self.max_age_days})
        return counts

    def fetch_contracts(self, underlying: str, as_of: date, start_expiration: date, end_expiration: date) -> list[dict[str, object]]:
        cache_key = (underlying, as_of.isoformat(), start_expiration.isoformat(), end_expiration.isoformat())
        lock = _lock_for("contract", "|".join(cache_key))
        with lock:
            started = time.perf_counter()
            today = _utc_today()
            if cache_key in self._contracts_cache and _is_cache_fresh(as_of, self._contracts_cache_dates.get(cache_key), today):
                _increment_cache_stat("contract", "memory_hit")
                _log_option_event(
                    "contracts_cache_memory_hit",
                    underlying=underlying,
                    as_of=as_of.isoformat(),
                    expiration_start=start_expiration.isoformat(),
                    expiration_end=end_expiration.isoformat(),
                    count=len(self._contracts_cache[cache_key]),
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
                )
                return self._contracts_cache[cache_key]
            cached_contracts = self._read_contracts_cache(underlying, as_of, start_expiration, end_expiration)
            if cached_contracts is not None:
                _increment_cache_stat("contract", "disk_hit")
                self._contracts_cache[cache_key] = cached_contracts
                self._contracts_cache_dates[cache_key] = today.isoformat()
                _log_option_event(
                    "contracts_cache_disk_hit",
                    underlying=underlying,
                    as_of=as_of.isoformat(),
                    expiration_start=start_expiration.isoformat(),
                    expiration_end=end_expiration.isoformat(),
                    count=len(cached_contracts),
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
                )
                return cached_contracts
            _increment_cache_stat("contract", "miss")
            _log_option_event(
                "contracts_cache_miss",
                underlying=underlying,
                as_of=as_of.isoformat(),
                expiration_start=start_expiration.isoformat(),
                expiration_end=end_expiration.isoformat(),
            )
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
            _log_option_event(
                "contracts_fetch_done",
                underlying=underlying,
                as_of=as_of.isoformat(),
                expiration_start=start_expiration.isoformat(),
                expiration_end=end_expiration.isoformat(),
                count=len(contracts),
                elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
            )
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
            started = time.perf_counter()
            today = _utc_today()
            if cache_key in self._bars_cache and _is_cache_fresh(end, self._bars_cache_dates.get(cache_key), today):
                _increment_cache_stat("bars", "memory_hit")
                _log_option_event(
                    "bars_cache_memory_hit",
                    ticker=ticker,
                    start=start.isoformat(),
                    end=end.isoformat(),
                    count=len(self._bars_cache[cache_key]),
                    elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
                )
                return self._bars_cache[cache_key]
            cached = self._read_bars_cache(ticker, start, end)
            if cached is not None:
                cached_bars, covered_ranges, _cache_date = cached
                if _ranges_cover(covered_ranges, start, end):
                    _increment_cache_stat("bars", "disk_hit")
                    bars = [bar for bar in cached_bars if start <= bar.date <= end]
                    self._bars_cache[cache_key] = bars
                    self._bars_cache_dates[cache_key] = today.isoformat()
                    _log_option_event(
                        "bars_cache_disk_hit",
                        ticker=ticker,
                        start=start.isoformat(),
                        end=end.isoformat(),
                        count=len(bars),
                        covered_range_count=len(covered_ranges),
                        elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
                    )
                    return bars
                _increment_cache_stat("bars", "partial")
                _log_option_event(
                    "bars_cache_partial",
                    ticker=ticker,
                    start=start.isoformat(),
                    end=end.isoformat(),
                    cached_bar_count=len(cached_bars),
                    covered_range_count=len(covered_ranges),
                )
            else:
                cached_bars = []
                covered_ranges = []
                _increment_cache_stat("bars", "miss")
                _log_option_event("bars_cache_miss", ticker=ticker, start=start.isoformat(), end=end.isoformat())

            fetched_bars: list[OptionBar] = []
            fetched_ranges = _missing_ranges(covered_ranges, start, end)
            _log_option_event(
                "bars_fetch_missing_ranges",
                ticker=ticker,
                start=start.isoformat(),
                end=end.isoformat(),
                missing_ranges=[(range_start.isoformat(), range_end.isoformat()) for range_start, range_end in fetched_ranges],
            )
            for fetch_start, fetch_end in fetched_ranges:
                range_started = time.perf_counter()
                payload = _polygon_retry_get(
                    f"{self.base_url}/v2/aggs/ticker/{ticker}/range/1/day/{fetch_start.isoformat()}/{fetch_end.isoformat()}",
                    {"adjusted": "true", "sort": "asc", "apiKey": self.api_key},
                    self.timeout,
                )
                range_count = 0
                for item in payload.get("results") or []:
                    close = _float_or_none(item.get("c"))
                    timestamp = item.get("t")
                    if close is None or timestamp is None:
                        continue
                    fetched_bars.append(OptionBar(date=datetime.fromtimestamp(float(timestamp) / 1000, timezone.utc).date(), close=close))
                    range_count += 1
                _log_option_event(
                    "bars_fetch_range_done",
                    ticker=ticker,
                    start=fetch_start.isoformat(),
                    end=fetch_end.isoformat(),
                    count=range_count,
                    elapsed_ms=round((time.perf_counter() - range_started) * 1000, 3),
                )
            fetched_bars.sort(key=lambda bar: bar.date)
            merged_by_date = {bar.date: bar for bar in cached_bars}
            merged_by_date.update({bar.date: bar for bar in fetched_bars})
            merged_bars = sorted(merged_by_date.values(), key=lambda bar: bar.date)
            merged_ranges = _merge_covered_ranges([*covered_ranges, *fetched_ranges])
            self._write_bars_cache(ticker, merged_bars, merged_ranges)
            requested_bars = [bar for bar in merged_bars if start <= bar.date <= end]
            self._bars_cache[cache_key] = requested_bars
            self._bars_cache_dates[cache_key] = today.isoformat()
            _log_option_event(
                "bars_fetch_done",
                ticker=ticker,
                start=start.isoformat(),
                end=end.isoformat(),
                fetched_bar_count=len(fetched_bars),
                requested_bar_count=len(requested_bars),
                merged_bar_count=len(merged_bars),
                covered_range_count=len(merged_ranges),
                elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
            )
            return requested_bars


def _provider_name(provider: Any | None) -> str:
    return str(getattr(provider, "provider_name", "") or "polygon")


def _provider_label(provider: Any | None) -> str:
    return str(getattr(provider, "provider_label", "") or ("Alpaca" if _provider_name(provider) == "alpaca" else "Polygon"))


def _provider_cache_id(provider: Any | None) -> str:
    return str(getattr(provider, "cache_provider_id", "") or _provider_name(provider))


def _provider_config(provider: Any | None) -> dict[str, object]:
    config = getattr(provider, "provider_config", None)
    return dict(config) if isinstance(config, dict) else {}


def _provider_permission_denied_reason(provider: Any | None) -> str:
    return str(getattr(provider, "permission_denied_reason", "") or POLYGON_PERMISSION_DENIED)


def _annotate_outcome_provider(outcome: dict[str, object], provider: Any | None) -> dict[str, object]:
    outcome["provider"] = _provider_name(provider)
    outcome["provider_label"] = _provider_label(provider)
    config = _provider_config(provider)
    if config:
        outcome["provider_config"] = config
    return outcome


def _exception_status(exc: Exception) -> int | None:
    for attr in ("status_code", "code", "status"):
        value = getattr(exc, attr, None)
        if isinstance(value, int):
            return value
        if isinstance(value, str) and value.isdigit():
            return int(value)
    response = getattr(exc, "response", None)
    status = getattr(response, "status_code", None)
    if isinstance(status, int):
        return status
    match = re.search(r"\b(401|403|429|502|503|504)\b", str(exc))
    return int(match.group(1)) if match else None


def _provider_http_error_reason(provider: Any | None, status: int | None, exc: Exception) -> str:
    provider_name = _provider_name(provider)
    if status in {401, 403}:
        return _provider_permission_denied_reason(provider)
    if status in {429, 502, 503, 504}:
        return API_LIMIT_OR_TIMEOUT
    label = _provider_label(provider) if provider_name != "polygon" else "Polygon"
    return f"{label} API 错误: {status or exc}"


def _monthly_expirations_between(start: date, end: date) -> list[date]:
    expirations: list[date] = []
    cursor = date(start.year, start.month, 1)
    while cursor <= end:
        expiration = third_friday(cursor.year, cursor.month)
        if expiration in observed_us_market_holidays(expiration.year):
            expiration = expiration - timedelta(days=1)
        if start <= expiration <= end:
            expirations.append(expiration)
        if cursor.month == 12:
            cursor = date(cursor.year + 1, 1, 1)
        else:
            cursor = date(cursor.year, cursor.month + 1, 1)
    return expirations


def _occ_strike(strike: float) -> str:
    return f"{int(round(strike * 1000)):08d}"


def occ_option_symbol(underlying: str, expiration: date, contract_type: str, strike: float) -> str:
    root = re.sub(r"[^A-Z0-9]", "", underlying.upper())
    cp = "C" if contract_type.lower().startswith("c") else "P"
    return f"{root}{expiration:%y%m%d}{cp}{_occ_strike(strike)}"


def parse_occ_option_symbol(symbol: str, expected_underlying: str | None = None) -> OptionContract | None:
    match = re.match(r"^(.+?)(\d{6})([CP])(\d{8})$", str(symbol or "").strip().upper())
    if not match:
        return None
    root, yymmdd, cp, strike_text = match.groups()
    expected = re.sub(r"[^A-Z0-9]", "", str(expected_underlying or "").upper())
    if expected and root != expected:
        return None
    try:
        expiration = datetime.strptime(yymmdd, "%y%m%d").date()
        strike = int(strike_text) / 1000
    except ValueError:
        return None
    return OptionContract(ticker=symbol.strip().upper(), underlying=expected_underlying or root, expiration=expiration, strike=strike, contract_type="call" if cp == "C" else "put")


def _strike_step(price: float) -> float:
    if price < 25:
        return 0.5
    if price < 100:
        return 1.0
    if price < 250:
        return 2.5
    return 5.0


class AlpacaMonthlyOptionProvider:
    def __init__(
        self,
        api_key: str,
        secret_key: str,
        option_data_feed: str = "indicative",
        timeout: int = 15,
        client: Any | None = None,
        option_bars_request_cls: Any | None = None,
        option_chain_request_cls: Any | None = None,
        timeframe_day: Any | None = None,
    ):
        self.api_key = api_key
        self.secret_key = secret_key
        self.option_data_feed = str(option_data_feed or "indicative").strip() or "indicative"
        self.timeout = timeout
        self.provider_name = "alpaca"
        self.provider_label = "Alpaca"
        self.cache_provider_id = f"alpaca-{self.option_data_feed.lower()}"
        self.permission_denied_reason = ALPACA_PERMISSION_DENIED
        self.min_signal_date = ALPACA_OPTION_DATA_START
        self.provider_config = {"option_data_feed": self.option_data_feed}
        self._client = client
        self._option_bars_request_cls = option_bars_request_cls
        self._option_chain_request_cls = option_chain_request_cls
        self._timeframe_day = timeframe_day
        self._bars_cache: dict[tuple[str, str, str], list[OptionBar]] = {}
        if self._client is None:
            self._load_alpaca_sdk()

    def _load_alpaca_sdk(self) -> None:
        try:
            from alpaca.data.historical.option import OptionHistoricalDataClient
            from alpaca.data.requests import OptionBarsRequest, OptionChainRequest
            from alpaca.data.timeframe import TimeFrame
        except ImportError as exc:
            raise RuntimeError("alpaca-py 未安装，请先安装 alpaca-py>=0.43.4,<0.44") from exc
        self._client = OptionHistoricalDataClient(self.api_key, self.secret_key)
        self._option_bars_request_cls = OptionBarsRequest
        self._option_chain_request_cls = OptionChainRequest
        self._timeframe_day = TimeFrame.Day

    def _request(self, event: str, fn, *args, **kwargs):
        _increment_cache_stat("provider_requests")
        _increment_cache_stat("alpaca_requests")
        _log_option_event(event, provider="alpaca")
        try:
            return fn(*args, **kwargs)
        except Exception as exc:
            status = _exception_status(exc)
            if status in {401, 403}:
                raise OptionProviderPermissionError(ALPACA_PERMISSION_DENIED) from exc
            if status in {429, 502, 503, 504}:
                raise OptionProviderTransientError(f"{API_LIMIT_OR_TIMEOUT}: Alpaca {status}") from exc
            raise

    def _bars_request(self, symbols: list[str], start: date, end: date):
        start_dt = datetime.combine(start, datetime.min.time(), tzinfo=timezone.utc)
        end_dt = datetime.combine(end + timedelta(days=1), datetime.min.time(), tzinfo=timezone.utc)
        if self._option_bars_request_cls is None:
            return {"symbol_or_symbols": symbols, "timeframe": self._timeframe_day, "start": start_dt, "end": end_dt, "feed": self.option_data_feed}
        kwargs = {
            "symbol_or_symbols": symbols,
            "timeframe": self._timeframe_day,
            "start": start_dt,
            "end": end_dt,
            "feed": self.option_data_feed,
        }
        try:
            return self._option_bars_request_cls(**kwargs)
        except TypeError:
            kwargs.pop("feed", None)
            return self._option_bars_request_cls(**kwargs)

    def _chain_request(self, underlying: str):
        if self._option_chain_request_cls is None:
            return {"underlying_symbol": underlying, "feed": self.option_data_feed}
        try:
            return self._option_chain_request_cls(underlying_symbol=underlying, feed=self.option_data_feed)
        except TypeError:
            return self._option_chain_request_cls(underlying_symbol=underlying)

    def _extract_bars_by_symbol(self, response: object) -> dict[str, list[OptionBar]]:
        data = getattr(response, "data", None)
        if data is None and isinstance(response, dict):
            data = response.get("bars") or response.get("data") or response
        if not isinstance(data, dict):
            return {}
        parsed: dict[str, list[OptionBar]] = {}
        for symbol, raw_bars in data.items():
            if raw_bars is None:
                continue
            items = raw_bars if isinstance(raw_bars, list) else list(raw_bars or [])
            bars: list[OptionBar] = []
            for item in items:
                timestamp = getattr(item, "timestamp", None)
                close = getattr(item, "close", None)
                if isinstance(item, dict):
                    timestamp = item.get("t") or item.get("timestamp") or item.get("time")
                    close = item.get("c") or item.get("close")
                close_value = _float_or_none(close)
                bar_date = parse_iso_date(timestamp)
                if bar_date is None and isinstance(timestamp, (int, float)):
                    bar_date = datetime.fromtimestamp(float(timestamp) / (1000 if timestamp > 10_000_000_000 else 1), timezone.utc).date()
                if close_value is None or bar_date is None:
                    continue
                bars.append(OptionBar(date=bar_date, close=close_value))
            if bars:
                parsed[str(symbol).upper()] = sorted(bars, key=lambda bar: bar.date)
        return parsed

    def _extract_chain_symbols(self, response: object) -> list[str]:
        data = getattr(response, "data", None)
        if data is None and isinstance(response, dict):
            data = response.get("snapshots") or response.get("data") or response
        if isinstance(data, dict):
            return [str(symbol).upper() for symbol in data.keys()]
        return []

    def _candidate_contracts_from_chain(self, underlying: str, as_of: date, start_expiration: date, end_expiration: date) -> list[OptionContract]:
        client = self._client
        if client is None or not hasattr(client, "get_option_chain"):
            return []
        try:
            response = self._request("alpaca_chain_request", client.get_option_chain, self._chain_request(underlying))
        except OptionProviderPermissionError:
            raise
        except Exception as exc:
            _log_option_event("alpaca_chain_unavailable", underlying=underlying, error=type(exc).__name__, message=str(exc))
            return []
        contracts: list[OptionContract] = []
        for symbol in self._extract_chain_symbols(response):
            contract = parse_occ_option_symbol(symbol, expected_underlying=underlying)
            if not contract:
                continue
            if contract.contract_type != "call":
                continue
            if start_expiration <= contract.expiration <= end_expiration and is_standard_monthly_expiration(contract.expiration):
                contracts.append(contract)
        return contracts

    def _generated_candidate_contracts(self, underlying: str, as_of: date, stock_price: float) -> list[OptionContract]:
        start_expiration = as_of + timedelta(days=200)
        end_expiration = as_of + timedelta(days=300)
        target_strike = stock_price * 1.10
        step = _strike_step(target_strike)
        strikes = sorted({round(max(step, target_strike + (offset * step)), 3) for offset in range(-20, 21)})
        candidates = [
            OptionContract(occ_option_symbol(underlying, expiration, "call", strike), underlying, expiration, strike)
            for expiration in _monthly_expirations_between(start_expiration, end_expiration)
            for strike in strikes
        ]
        target_expiration = as_of + timedelta(days=250)
        return sorted(candidates, key=lambda item: (abs(item.strike - target_strike), abs((item.expiration - target_expiration).days)))[:100]

    def select_monthly_call(self, underlying: str, as_of: date, stock_price: float) -> tuple[OptionContract | None, str]:
        start_expiration = as_of + timedelta(days=200)
        end_expiration = as_of + timedelta(days=300)
        target_strike = stock_price * 1.10
        target_expiration = as_of + timedelta(days=250)
        contracts = self._candidate_contracts_from_chain(underlying, as_of, start_expiration, end_expiration)
        if not contracts:
            contracts = self._generated_candidate_contracts(underlying, as_of, stock_price)
        if not contracts:
            return None, NO_200_300D_CONTRACT
        contracts = sorted(contracts, key=lambda item: (abs(item.strike - target_strike), abs((item.expiration - target_expiration).days)))[:100]
        availability = self._fetch_bars_for_symbols([contract.ticker for contract in contracts], as_of, min(as_of + timedelta(days=7), end_expiration))
        for contract in contracts:
            if first_bar_on_or_after(availability.get(contract.ticker, []), as_of, 7, latest=contract.expiration):
                return contract, ""
        return None, NO_ENTRY_PRICE

    def _fetch_bars_for_symbols(self, symbols: list[str], start: date, end: date) -> dict[str, list[OptionBar]]:
        if not symbols:
            return {}
        client = self._client
        if client is None:
            raise RuntimeError("Alpaca option data client 未初始化")
        response = self._request("alpaca_bars_request", client.get_option_bars, self._bars_request(symbols, start, end))
        bars_by_symbol = self._extract_bars_by_symbol(response)
        for symbol, bars in bars_by_symbol.items():
            self._bars_cache[(symbol, start.isoformat(), end.isoformat())] = [bar for bar in bars if start <= bar.date <= end]
        return bars_by_symbol

    def fetch_bars(self, ticker: str, start: date, end: date) -> list[OptionBar]:
        cache_key = (ticker.upper(), start.isoformat(), end.isoformat())
        cached = self._bars_cache.get(cache_key)
        if cached is not None:
            _increment_cache_stat("bars", "memory_hit")
            return cached
        _increment_cache_stat("bars", "miss")
        bars_by_symbol = self._fetch_bars_for_symbols([ticker.upper()], start, end)
        bars = [bar for bar in bars_by_symbol.get(ticker.upper(), []) if start <= bar.date <= end]
        self._bars_cache[cache_key] = bars
        if bars:
            _increment_cache_stat("bars", "write")
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


def last_bar_after_on_or_before(bars: list[OptionBar], after: date, target: date) -> OptionBar | None:
    candidates = [bar for bar in bars if after < bar.date <= target]
    return candidates[-1] if candidates else None


def stock_return_pct_for_signal(signal: dict[str, object]) -> float | None:
    stock_return_pct = _finite_float_or_none(signal.get("stock_return_pct"))
    if stock_return_pct is not None:
        return stock_return_pct
    stock_buy_price = _float_or_none(signal.get("stock_buy_price"))
    stock_exit_price = _float_or_none(signal.get("stock_sell_price")) or _float_or_none(signal.get("stock_mark_price"))
    if stock_buy_price is None or stock_exit_price is None:
        return None
    return (stock_exit_price / stock_buy_price - 1) * 100


def replay_signal(provider: Any, signal: dict[str, object]) -> dict[str, object]:
    started = time.perf_counter()
    signal_date = parse_iso_date(signal.get("date"))
    sell_date = parse_iso_date(signal.get("next_stock_sell_date"))
    stock_buy_price = _float_or_none(signal.get("stock_buy_price"))
    underlying = polygon_underlying(signal.get("symbol"))
    _log_option_event(
        "signal_replay_start",
        signal_key=str(signal.get("signal_key") or ""),
        symbol=str(signal.get("symbol") or ""),
        date=str(signal.get("date") or ""),
        next_stock_sell_date=str(signal.get("next_stock_sell_date") or ""),
        stock_buy_price=stock_buy_price,
        underlying=underlying or "",
    )
    if not signal_date or stock_buy_price is None:
        outcome = skipped_outcome(signal, NO_ENTRY_PRICE)
        _log_option_event("signal_replay_done", signal_key=str(signal.get("signal_key") or ""), status=outcome["status"], skipped_reason=NO_ENTRY_PRICE, elapsed_ms=round((time.perf_counter() - started) * 1000, 3))
        return _annotate_outcome_provider(outcome, provider)
    min_signal_date = getattr(provider, "min_signal_date", None)
    if isinstance(min_signal_date, date) and signal_date < min_signal_date:
        reason = ALPACA_DATA_UNAVAILABLE_BEFORE_2024_02 if _provider_name(provider) == "alpaca" else f"期权历史数据仅支持 {min_signal_date.isoformat()} 以后"
        outcome = skipped_outcome(signal, reason)
        _log_option_event("signal_replay_done", signal_key=str(signal.get("signal_key") or ""), status=outcome["status"], skipped_reason=reason, elapsed_ms=round((time.perf_counter() - started) * 1000, 3))
        return _annotate_outcome_provider(outcome, provider)
    if not underlying:
        outcome = skipped_outcome(signal, UNSUPPORTED_UNDERLYING)
        _log_option_event("signal_replay_done", signal_key=str(signal.get("signal_key") or ""), status=outcome["status"], skipped_reason=UNSUPPORTED_UNDERLYING, elapsed_ms=round((time.perf_counter() - started) * 1000, 3))
        return _annotate_outcome_provider(outcome, provider)
    if sell_date and sell_date <= signal_date:
        outcome = skipped_outcome(signal, NO_STOCK_SELL)
        _log_option_event("signal_replay_done", signal_key=str(signal.get("signal_key") or ""), status=outcome["status"], skipped_reason=NO_STOCK_SELL, elapsed_ms=round((time.perf_counter() - started) * 1000, 3))
        return _annotate_outcome_provider(outcome, provider)

    contract, reason = provider.select_monthly_call(underlying, signal_date, stock_buy_price)
    if contract is None:
        outcome = skipped_outcome(signal, reason)
        _log_option_event("signal_replay_done", signal_key=str(signal.get("signal_key") or ""), status=outcome["status"], skipped_reason=reason, elapsed_ms=round((time.perf_counter() - started) * 1000, 3))
        return _annotate_outcome_provider(outcome, provider)
    _log_option_event(
        "signal_contract_selected",
        signal_key=str(signal.get("signal_key") or ""),
        contract=contract.ticker,
        underlying=contract.underlying,
        expiration=contract.expiration.isoformat(),
        strike=contract.strike,
    )

    entry_end = min(signal_date + timedelta(days=7), contract.expiration)
    if sell_date:
        history_end = max(entry_end, min(sell_date + timedelta(days=7), contract.expiration))
    else:
        mark_date = latest_completed_market_date()
        history_end = max(entry_end, min(mark_date, contract.expiration))
    bars = provider.fetch_bars(contract.ticker, signal_date, history_end)
    _log_option_event(
        "signal_bars_loaded",
        signal_key=str(signal.get("signal_key") or ""),
        contract=contract.ticker,
        start=signal_date.isoformat(),
        end=history_end.isoformat(),
        bar_count=len(bars),
    )
    entry_bar = first_bar_on_or_after(bars, signal_date, 7, latest=contract.expiration)
    if not entry_bar:
        outcome = {**skipped_outcome(signal, NO_ENTRY_PRICE), **contract_payload(contract)}
        _log_option_event("signal_replay_done", signal_key=str(signal.get("signal_key") or ""), status=outcome["status"], skipped_reason=NO_ENTRY_PRICE, contract=contract.ticker, elapsed_ms=round((time.perf_counter() - started) * 1000, 3))
        return _annotate_outcome_provider(outcome, provider)

    if not sell_date:
        mark_date = latest_completed_market_date()
        exit_boundary = min(mark_date, contract.expiration)
        exit_bar = last_bar_after_on_or_before(bars, entry_bar.date, exit_boundary)
        if not exit_bar:
            outcome = {**skipped_outcome(signal, NO_EXIT_PRICE), **contract_payload(contract), "entry_price": entry_bar.close}
            _log_option_event("signal_replay_done", signal_key=str(signal.get("signal_key") or ""), status=outcome["status"], skipped_reason=NO_EXIT_PRICE, contract=contract.ticker, entry_date=entry_bar.date.isoformat(), elapsed_ms=round((time.perf_counter() - started) * 1000, 3))
            return _annotate_outcome_provider(outcome, provider)
        exit_status = "expired_without_stock_sell" if mark_date >= contract.expiration else "holding"
    elif sell_date > contract.expiration:
        exit_bar = last_bar_on_or_before(bars, contract.expiration, 7)
        if not exit_bar:
            outcome = {**skipped_outcome(signal, NO_PRE_EXPIRATION_PRICE), **contract_payload(contract), "entry_price": entry_bar.close}
            _log_option_event("signal_replay_done", signal_key=str(signal.get("signal_key") or ""), status=outcome["status"], skipped_reason=NO_PRE_EXPIRATION_PRICE, contract=contract.ticker, entry_date=entry_bar.date.isoformat(), elapsed_ms=round((time.perf_counter() - started) * 1000, 3))
            return _annotate_outcome_provider(outcome, provider)
        exit_status = "expired_before_stock_sell"
    else:
        exit_bar = first_bar_on_or_after(bars, sell_date, 7, latest=contract.expiration)
        if not exit_bar:
            outcome = {**skipped_outcome(signal, NO_EXIT_PRICE), **contract_payload(contract), "entry_price": entry_bar.close}
            _log_option_event("signal_replay_done", signal_key=str(signal.get("signal_key") or ""), status=outcome["status"], skipped_reason=NO_EXIT_PRICE, contract=contract.ticker, entry_date=entry_bar.date.isoformat(), elapsed_ms=round((time.perf_counter() - started) * 1000, 3))
            return _annotate_outcome_provider(outcome, provider)
        exit_status = "sold"

    option_roi_pct = (exit_bar.close / entry_bar.close - 1) * 100
    roi_pct = option_roi_pct
    roi_source = "option"
    stock_return_pct = stock_return_pct_for_signal(signal)
    if exit_status in {"expired_without_stock_sell", "expired_before_stock_sell"} and stock_return_pct is not None:
        roi_pct = stock_return_pct
        roi_source = "stock_return_substitute"
    dte = (contract.expiration - signal_date).days
    outcome = {
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
        "roi_source": roi_source,
        "stock_return_pct": stock_return_pct,
        "status": "success",
        "exit_status": exit_status,
        "skipped_reason": "",
    }
    if roi_source == "stock_return_substitute":
        outcome.update(
            {
                "option_roi_pct": option_roi_pct,
                "option_exit_price": exit_bar.close,
                "option_exit_date": exit_bar.date.isoformat(),
            }
        )
    _log_option_event(
        "signal_replay_done",
        signal_key=str(signal.get("signal_key") or ""),
        status=outcome["status"],
        exit_status=exit_status,
        contract=contract.ticker,
        entry_date=entry_bar.date.isoformat(),
        exit_date=exit_bar.date.isoformat(),
        roi_pct=round(roi_pct, 4),
        elapsed_ms=round((time.perf_counter() - started) * 1000, 3),
    )
    return _annotate_outcome_provider(outcome, provider)


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
    exit_status_counts: dict[str, int] = {}
    roi_source_counts: dict[str, int] = {}
    for item in outcomes:
        if item.get("status") == "success":
            exit_status = str(item.get("exit_status") or "sold")
            exit_status_counts[exit_status] = exit_status_counts.get(exit_status, 0) + 1
            roi_source = str(item.get("roi_source") or "option")
            roi_source_counts[roi_source] = roi_source_counts.get(roi_source, 0) + 1
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
        "stock_return_substitute_count": roi_source_counts.get("stock_return_substitute", 0),
        "roi_source_counts": roi_source_counts,
        "exit_status_counts": exit_status_counts,
        "top_failure_reason": top_reason,
        "failure_reasons": reason_counts,
    }


def replay_leaps_option_outcomes(
    signals: list[dict[str, object]],
    api_key: str,
    provider: Any | None = None,
    outcome_cache: OutcomeCache | None = None,
) -> dict[str, object]:
    stats = new_cache_stats()
    token = _with_cache_stats(stats)
    try:
        result = _replay_leaps_option_outcomes(
            signals,
            api_key,
            provider=provider,
            outcome_cache=outcome_cache,
            use_cache=True,
        )
        return {**result, "cache_stats": stats}
    finally:
        _reset_cache_stats(token)


def _replay_leaps_option_outcomes(
    signals: list[dict[str, object]],
    api_key: str,
    provider: Any | None = None,
    outcome_cache: OutcomeCache | None = None,
    use_cache: bool = True,
) -> dict[str, object]:
    if not api_key and provider is None:
        outcomes = [skipped_outcome(signal, NO_POLYGON_KEY) for signal in signals]
        return {"success": False, "message": "Polygon API key 未配置", "outcomes": outcomes, "summary": summarize_outcomes(outcomes)}
    active_provider = provider or PolygonMonthlyOptionProvider(api_key)
    _set_cache_stat("provider", _provider_name(active_provider))
    provider_cache_id = _provider_cache_id(active_provider)
    active_outcome_cache = outcome_cache if outcome_cache is not None else (_OUTCOME_CACHE if provider is None else None)
    outcomes: list[dict[str, object]] = []
    for signal in signals:
        if use_cache and active_outcome_cache is not None:
            cached = active_outcome_cache.read(signal, provider_id=provider_cache_id)
            if cached is not None:
                _annotate_outcome_provider(cached, active_provider)
                outcomes.append(cached)
                continue
        try:
            outcome = replay_signal(active_provider, signal)
            if use_cache and active_outcome_cache is not None:
                active_outcome_cache.write(signal, outcome, provider_id=provider_cache_id)
            outcomes.append(outcome)
        except OptionProviderPermissionError:
            outcomes.append(_annotate_outcome_provider(skipped_outcome(signal, _provider_permission_denied_reason(active_provider)), active_provider))
        except OptionProviderTransientError as exc:
            outcomes.append(_annotate_outcome_provider(skipped_outcome(signal, str(exc) or API_LIMIT_OR_TIMEOUT), active_provider))
        except (requests.Timeout, requests.ConnectionError) as exc:
            outcomes.append(_annotate_outcome_provider(skipped_outcome(signal, f"{API_LIMIT_OR_TIMEOUT}: {exc}"), active_provider))
        except requests.HTTPError as exc:
            status = getattr(exc.response, "status_code", None)
            reason = _provider_http_error_reason(active_provider, status, exc)
            outcomes.append(_annotate_outcome_provider(skipped_outcome(signal, reason), active_provider))
        except Exception as exc:
            outcomes.append(_annotate_outcome_provider(skipped_outcome(signal, f"{_provider_label(active_provider)} API 错误: {exc}"), active_provider))
    return {"success": True, "provider": _provider_name(active_provider), "outcomes": outcomes, "summary": summarize_outcomes(outcomes)}


def replay_leaps_option_outcomes_batch(
    signals: list[dict[str, object]],
    api_key: str,
    provider: Any | None = None,
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
    provider: Any | None = None,
    outcome_cache: OutcomeCache | None = None,
) -> dict[str, object]:
    if not api_key and provider is None:
        outcomes = [skipped_outcome(signal, NO_POLYGON_KEY) for signal in signals]
        return {"success": False, "message": "Polygon API key 未配置", "outcomes": outcomes, "summary": summarize_outcomes(outcomes)}

    active_provider = provider or PolygonMonthlyOptionProvider(api_key)
    _set_cache_stat("provider", _provider_name(active_provider))
    provider_cache_id = _provider_cache_id(active_provider)
    active_outcome_cache = outcome_cache if outcome_cache is not None else (_OUTCOME_CACHE if provider is None else None)
    unique_by_key: dict[str, dict[str, object]] = {}
    key_by_index: list[str] = []
    for signal in signals:
        mark_date = latest_completed_market_date() if not parse_iso_date(signal.get("next_stock_sell_date")) else None
        key = outcome_cache_key(signal, mark_date, provider_id=provider_cache_id)
        key_by_index.append(key)
        unique_by_key.setdefault(key, signal)

    _log_option_event(
        "batch_replay_start",
        signal_count=len(signals),
        unique_count=len(unique_by_key),
        provider=type(active_provider).__name__,
        outcome_cache_enabled=bool(active_outcome_cache and active_outcome_cache.cache_enabled),
    )
    batch_started = time.perf_counter()
    batch_429s = 0
    batch_403s = 0
    outcome_by_key: dict[str, dict[str, object]] = {}
    for key, signal in unique_by_key.items():
        if batch_403s >= POLYGON_BATCH_403_CIRCUIT_BREAKER:
            outcome_by_key[key] = _annotate_outcome_provider(skipped_outcome(signal, _provider_permission_denied_reason(active_provider)), active_provider)
            _log_option_event(
                "batch_unique_circuit_skipped",
                key=key,
                signal_key=str(signal.get("signal_key") or ""),
                symbol=str(signal.get("symbol") or ""),
                date=str(signal.get("date") or ""),
                reason=_provider_permission_denied_reason(active_provider),
                batch_403s=batch_403s,
            )
            continue
        if batch_429s >= POLYGON_BATCH_429_CIRCUIT_BREAKER:
            reason = f"{API_LIMIT_OR_TIMEOUT}: Polygon 429 熔断，稍后重试"
            outcome_by_key[key] = skipped_outcome(signal, reason)
            _log_option_event(
                "batch_unique_circuit_skipped",
                key=key,
                signal_key=str(signal.get("signal_key") or ""),
                symbol=str(signal.get("symbol") or ""),
                date=str(signal.get("date") or ""),
                reason=reason,
                batch_429s=batch_429s,
            )
            continue
        if active_outcome_cache is not None:
            cached = active_outcome_cache.read(signal, provider_id=provider_cache_id)
            if cached is not None:
                _annotate_outcome_provider(cached, active_provider)
                outcome_by_key[key] = cached
                continue
        lock = _lock_for("outcome-replay", key)
        with lock:
            signal_stats_before = _cache_stats_snapshot()
            signal_started = time.perf_counter()
            if active_outcome_cache is not None:
                cached = active_outcome_cache.read(signal, count_miss=False, provider_id=provider_cache_id)
                if cached is not None:
                    _annotate_outcome_provider(cached, active_provider)
                    outcome_by_key[key] = cached
                    signal_delta = _cache_stats_delta(signal_stats_before)
                    batch_429s += _delta_429_count(signal_delta)
                    _log_option_event(
                        "batch_unique_done",
                        key=key,
                        signal_key=str(signal.get("signal_key") or ""),
                        symbol=str(signal.get("symbol") or ""),
                        date=str(signal.get("date") or ""),
                        status=str(cached.get("status") or ""),
                        source="cache_after_lock",
                        elapsed_ms=round((time.perf_counter() - signal_started) * 1000, 3),
                        cache_delta=signal_delta,
                        batch_429s=batch_429s,
                    )
                    continue
            try:
                outcome = replay_signal(active_provider, signal)
                if active_outcome_cache is not None:
                    active_outcome_cache.write(signal, outcome, provider_id=provider_cache_id)
                outcome_by_key[key] = outcome
                signal_delta = _cache_stats_delta(signal_stats_before)
                batch_429s += _delta_429_count(signal_delta)
                _log_option_event(
                    "batch_unique_done",
                    key=key,
                    signal_key=str(signal.get("signal_key") or ""),
                    symbol=str(signal.get("symbol") or ""),
                    date=str(signal.get("date") or ""),
                    status=str(outcome.get("status") or ""),
                    exit_status=str(outcome.get("exit_status") or ""),
                    skipped_reason=str(outcome.get("skipped_reason") or ""),
                    source="replay",
                    elapsed_ms=round((time.perf_counter() - signal_started) * 1000, 3),
                    cache_delta=signal_delta,
                    batch_429s=batch_429s,
                )
            except OptionProviderPermissionError:
                outcome_by_key[key] = _annotate_outcome_provider(skipped_outcome(signal, _provider_permission_denied_reason(active_provider)), active_provider)
                batch_403s += 1
                signal_delta = _cache_stats_delta(signal_stats_before)
                batch_429s += _delta_429_count(signal_delta)
                _log_option_event("batch_unique_provider_permission_error", key=key, signal_key=str(signal.get("signal_key") or ""), reason=_provider_permission_denied_reason(active_provider), elapsed_ms=round((time.perf_counter() - signal_started) * 1000, 3), cache_delta=signal_delta, batch_429s=batch_429s, batch_403s=batch_403s)
            except OptionProviderTransientError as exc:
                outcome_by_key[key] = _annotate_outcome_provider(skipped_outcome(signal, str(exc) or API_LIMIT_OR_TIMEOUT), active_provider)
                signal_delta = _cache_stats_delta(signal_stats_before)
                batch_429s += _delta_429_count(signal_delta)
                _log_option_event("batch_unique_provider_transient_error", key=key, signal_key=str(signal.get("signal_key") or ""), message=str(exc), elapsed_ms=round((time.perf_counter() - signal_started) * 1000, 3), cache_delta=signal_delta, batch_429s=batch_429s)
            except (requests.Timeout, requests.ConnectionError) as exc:
                outcome_by_key[key] = _annotate_outcome_provider(skipped_outcome(signal, f"{API_LIMIT_OR_TIMEOUT}: {exc}"), active_provider)
                signal_delta = _cache_stats_delta(signal_stats_before)
                batch_429s += _delta_429_count(signal_delta)
                _log_option_event("batch_unique_error", key=key, signal_key=str(signal.get("signal_key") or ""), error=type(exc).__name__, message=str(exc), elapsed_ms=round((time.perf_counter() - signal_started) * 1000, 3), cache_delta=signal_delta, batch_429s=batch_429s)
            except requests.HTTPError as exc:
                status = getattr(exc.response, "status_code", None)
                reason = _provider_http_error_reason(active_provider, status, exc)
                outcome_by_key[key] = _annotate_outcome_provider(skipped_outcome(signal, reason), active_provider)
                if status in {401, 403}:
                    batch_403s += 1
                signal_delta = _cache_stats_delta(signal_stats_before)
                batch_429s += _delta_429_count(signal_delta)
                _log_option_event("batch_unique_http_error", key=key, signal_key=str(signal.get("signal_key") or ""), status_code=status, reason=reason, elapsed_ms=round((time.perf_counter() - signal_started) * 1000, 3), cache_delta=signal_delta, batch_429s=batch_429s, batch_403s=batch_403s)
            except Exception as exc:
                outcome_by_key[key] = _annotate_outcome_provider(skipped_outcome(signal, f"{_provider_label(active_provider)} API 错误: {exc}"), active_provider)
                signal_delta = _cache_stats_delta(signal_stats_before)
                batch_429s += _delta_429_count(signal_delta)
                _log_option_event("batch_unique_error", key=key, signal_key=str(signal.get("signal_key") or ""), error=type(exc).__name__, message=str(exc), elapsed_ms=round((time.perf_counter() - signal_started) * 1000, 3), cache_delta=signal_delta, batch_429s=batch_429s)

    outcomes = [
        clone_outcome_for_signal(outcome_by_key.get(key) or skipped_outcome(signal, "期权收益计算失败"), signal)
        for key, signal in zip(key_by_index, signals)
    ]
    _log_option_event(
        "batch_replay_done",
        signal_count=len(signals),
        unique_count=len(unique_by_key),
        elapsed_ms=round((time.perf_counter() - batch_started) * 1000, 3),
        batch_429s=batch_429s,
        batch_403s=batch_403s,
        summary=summarize_outcomes(outcomes),
        cache_stats=_cache_stats_snapshot(),
    )
    for outcome in outcomes:
        if isinstance(outcome, dict):
            _annotate_outcome_provider(outcome, active_provider)
    return {"success": True, "provider": _provider_name(active_provider), "outcomes": outcomes, "summary": summarize_outcomes(outcomes)}
