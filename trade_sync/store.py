"""Filesystem-backed storage for synced trades and drawdown cache."""

from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data"
TRADE_SYNC_DIR = DATA_DIR / "trade_sync"
RAW_DIR = TRADE_SYNC_DIR / "raw"
LATEST_DIR = TRADE_SYNC_DIR / "latest"
BY_SYMBOL_DIR = TRADE_SYNC_DIR / "by_symbol"
DRAWDOWN_CACHE_DIR = DATA_DIR / "drawdown_cache"
REPORT_DRAWDOWN_DIR = ROOT_DIR / "report" / "drawdown"


def ensure_storage_dirs() -> None:
    for directory in (
        DATA_DIR,
        TRADE_SYNC_DIR,
        RAW_DIR,
        LATEST_DIR,
        BY_SYMBOL_DIR,
        DRAWDOWN_CACHE_DIR,
        REPORT_DRAWDOWN_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def build_sync_version(exported_at: str | None) -> str:
    if exported_at:
        candidate = exported_at.strip().replace(":", "").replace("-", "")
        candidate = candidate.replace("+", "_plus_").replace(".", "")
        candidate = candidate.replace("T", "_").replace("Z", "_z")
        if candidate:
            return candidate
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _write_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def save_sync_payload(
    payload: dict[str, Any], normalized_rows: list[dict[str, Any]]
) -> dict[str, Any]:
    ensure_storage_dirs()
    sync_version = build_sync_version(payload.get("exported_at"))
    received_rows = len(payload.get("rows", []))
    symbols = sorted({row["symbol"] for row in normalized_rows})

    raw_snapshot = {
        "saved_at": utc_now_iso(),
        "sync_version": sync_version,
        "payload": payload,
    }
    _write_json(RAW_DIR / f"{sync_version}.json", raw_snapshot)

    grouped: dict[str, list[dict[str, Any]]] = {}
    for row in normalized_rows:
        grouped.setdefault(row["symbol"], []).append(row)

    by_symbol_summary: dict[str, Any] = {}
    for symbol, rows in grouped.items():
        longbridge_symbol = next(
            (row.get("longbridge_symbol") for row in rows if row.get("longbridge_symbol")),
            f"{symbol}.US",
        )
        symbol_payload = {
            "symbol": symbol,
            "longbridge_symbol": longbridge_symbol,
            "sync_version": sync_version,
            "updated_at": payload.get("exported_at") or utc_now_iso(),
            "trade_count": len(rows),
            "rows": rows,
        }
        _write_json(BY_SYMBOL_DIR / f"{symbol.upper()}.json", symbol_payload)
        by_symbol_summary[symbol] = {
            "trade_count": len(rows),
            "longbridge_symbol": longbridge_symbol,
            "sync_version": sync_version,
        }

    latest_payload = {
        "saved_at": utc_now_iso(),
        "sync_version": sync_version,
        "spreadsheet_id": payload.get("spreadsheet_id", ""),
        "spreadsheet_name": payload.get("spreadsheet_name", ""),
        "sheet_name": payload.get("sheet_name", ""),
        "exported_at": payload.get("exported_at", ""),
        "received_rows": received_rows,
        "normalized_rows": len(normalized_rows),
        "symbols": symbols,
        "dirty_symbols": symbols,
        "by_symbol": by_symbol_summary,
    }
    _write_json(LATEST_DIR / "trades_latest.json", latest_payload)

    return {
        "success": True,
        "sync_version": sync_version,
        "received_rows": received_rows,
        "normalized_rows": len(normalized_rows),
        "symbols": symbols,
        "dirty_symbols": symbols,
    }


def load_symbol_snapshot(symbol: str) -> dict[str, Any] | None:
    ensure_storage_dirs()
    path = BY_SYMBOL_DIR / f"{symbol.upper()}.json"
    if not path.exists():
        return None
    return _read_json(path)


def list_synced_symbols() -> list[str]:
    ensure_storage_dirs()
    return sorted(path.stem.upper() for path in BY_SYMBOL_DIR.glob("*.json"))


def drawdown_html_path(symbol: str) -> Path:
    ensure_storage_dirs()
    return REPORT_DRAWDOWN_DIR / f"{symbol.lower()}_drawdown_longbridge.html"


def drawdown_meta_path(symbol: str) -> Path:
    ensure_storage_dirs()
    return DRAWDOWN_CACHE_DIR / f"{symbol.upper()}.meta.json"


def load_drawdown_meta(symbol: str) -> dict[str, Any] | None:
    path = drawdown_meta_path(symbol)
    if not path.exists():
        return None
    return _read_json(path)


def save_drawdown_meta(symbol: str, payload: dict[str, Any]) -> None:
    _write_json(drawdown_meta_path(symbol), payload)


def is_drawdown_stale(
    symbol: str, source_version: str, cache_ttl_minutes: int, force: bool = False
) -> bool:
    if force:
        return True

    html_path = drawdown_html_path(symbol)
    if not html_path.exists():
        return True

    meta = load_drawdown_meta(symbol)
    if not meta:
        return True

    if meta.get("source_version") != source_version:
        return True

    generated_at = meta.get("generated_at")
    if not generated_at:
        return True

    try:
        generated_dt = datetime.fromisoformat(generated_at.replace("Z", "+00:00"))
    except ValueError:
        return True

    expires_at = generated_dt + timedelta(minutes=cache_ttl_minutes)
    return expires_at < datetime.now(timezone.utc)
