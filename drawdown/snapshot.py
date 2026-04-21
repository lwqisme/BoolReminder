"""Helpers for lightweight drawdown snapshots used in emails and summaries."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date, datetime, timezone, timedelta

from drawdown.generate_drawdown_report import (
    build_longbridge_quote_context,
    build_price_points_from_series,
    candle_datetime,
    fetch_longbridge_daily_candles,
    normalize_longbridge_symbol,
    rolling_window_drawdowns,
)
from trade_sync.store import load_drawdown_snapshot_cache, save_drawdown_snapshot_cache


@dataclass
class DrawdownSnapshot:
    symbol: str
    resolved_symbol: str
    latest_date: str
    close: float
    drawdown_ath_pct: float
    drawdown_120_pct: float

    def to_dict(self) -> dict[str, object]:
        return {
            "symbol": self.symbol,
            "resolved_symbol": self.resolved_symbol,
            "latest_date": self.latest_date,
            "close": self.close,
            "drawdown_ath_pct": self.drawdown_ath_pct,
            "drawdown_120_pct": self.drawdown_120_pct,
        }

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "DrawdownSnapshot":
        return cls(
            symbol=str(payload["symbol"]),
            resolved_symbol=str(payload["resolved_symbol"]),
            latest_date=str(payload["latest_date"]),
            close=float(payload["close"]),
            drawdown_ath_pct=float(payload["drawdown_ath_pct"]),
            drawdown_120_pct=float(payload["drawdown_120_pct"]),
        )


def _today_utc_iso() -> str:
    return datetime.now(timezone.utc).date().isoformat()


def _snapshot_from_cache(symbol: str) -> DrawdownSnapshot | None:
    cached = load_drawdown_snapshot_cache(symbol)
    if not cached:
        return None
    if cached.get("cache_date") != _today_utc_iso():
        return None
    snapshot_payload = cached.get("snapshot")
    if not isinstance(snapshot_payload, dict):
        return None
    try:
        return DrawdownSnapshot.from_dict(snapshot_payload)
    except (KeyError, TypeError, ValueError):
        return None


def fetch_drawdown_snapshot(
    symbol: str,
    *,
    quote_ctx: object | None = None,
    history_years: int = 5,
) -> DrawdownSnapshot:
    cached = _snapshot_from_cache(symbol)
    if cached is not None:
        return cached

    local_quote_ctx = quote_ctx or build_longbridge_quote_context()
    resolved_symbol = normalize_longbridge_symbol(symbol)
    end_date = datetime.now().date()
    start_date = end_date - timedelta(days=365 * history_years)
    candles = fetch_longbridge_daily_candles(local_quote_ctx, resolved_symbol, start_date, end_date)
    if not candles:
        raise RuntimeError(f"Longbridge 没有返回 {resolved_symbol} 的历史日线。")

    series = [
        (candle_datetime(candle).replace(tzinfo=None), float(candle.close))
        for candle in candles
    ]
    points = build_price_points_from_series(series)
    if not points:
        raise RuntimeError(f"无法从 Longbridge 构建 {resolved_symbol} 的价格序列。")

    closes = [point.close for point in points]
    _rolling_peaks, drawdowns_120 = rolling_window_drawdowns(closes, window_size=120)
    latest = points[-1]
    snapshot = DrawdownSnapshot(
        symbol=symbol,
        resolved_symbol=resolved_symbol,
        latest_date=latest.date.strftime("%Y-%m-%d"),
        close=latest.close,
        drawdown_ath_pct=latest.drawdown_ath * 100,
        drawdown_120_pct=drawdowns_120[-1] * 100,
    )
    save_drawdown_snapshot_cache(
        symbol,
        {
            "cache_date": _today_utc_iso(),
            "snapshot": snapshot.to_dict(),
        },
    )
    return snapshot


def collect_drawdown_snapshots(symbols: list[str]) -> tuple[dict[str, DrawdownSnapshot], dict[str, str]]:
    if not symbols:
        return {}, {}

    unique_symbols = list(dict.fromkeys(symbols))
    snapshots: dict[str, DrawdownSnapshot] = {}
    errors: dict[str, str] = {}
    quote_ctx = build_longbridge_quote_context()

    for symbol in unique_symbols:
        try:
            snapshots[symbol] = fetch_drawdown_snapshot(symbol, quote_ctx=quote_ctx)
        except Exception as exc:
            errors[symbol] = str(exc)

    return snapshots, errors
