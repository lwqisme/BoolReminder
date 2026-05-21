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
ACCOUNT_LATEST_PATH = LATEST_DIR / "account_latest.json"
SIGNAL_TARGETS_LATEST_PATH = LATEST_DIR / "signal_targets_latest.json"
DRAWDOWN_CACHE_DIR = DATA_DIR / "drawdown_cache"
DRAWDOWN_SNAPSHOT_CACHE_DIR = DATA_DIR / "drawdown_snapshot_cache"
REPORT_DRAWDOWN_DIR = ROOT_DIR / "report" / "drawdown"


def ensure_storage_dirs() -> None:
    for directory in (
        DATA_DIR,
        TRADE_SYNC_DIR,
        RAW_DIR,
        LATEST_DIR,
        BY_SYMBOL_DIR,
        DRAWDOWN_CACHE_DIR,
        DRAWDOWN_SNAPSHOT_CACHE_DIR,
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


def save_account_payload(payload: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    ensure_storage_dirs()
    sync_version = build_sync_version(payload.get("exported_at"))
    account_payload = {
        "saved_at": utc_now_iso(),
        "updated_at": payload.get("exported_at", ""),
        "sync_version": sync_version,
        "spreadsheet_id": payload.get("spreadsheet_id", ""),
        "spreadsheet_name": payload.get("spreadsheet_name", ""),
        "sheet_name": "account",
        "rows": rows,
    }
    _write_json(ACCOUNT_LATEST_PATH, account_payload)
    return {"account_rows": len(rows)}


def save_signal_targets_payload(payload: dict[str, Any], rows: list[dict[str, Any]]) -> dict[str, Any]:
    ensure_storage_dirs()
    sync_version = build_sync_version(payload.get("exported_at"))
    targets_payload = {
        "saved_at": utc_now_iso(),
        "updated_at": payload.get("exported_at", ""),
        "sync_version": sync_version,
        "spreadsheet_id": payload.get("spreadsheet_id", ""),
        "spreadsheet_name": payload.get("spreadsheet_name", ""),
        "sheet_name": "signal_targets",
        "rows": rows,
    }
    _write_json(SIGNAL_TARGETS_LATEST_PATH, targets_payload)
    return {"signal_target_rows": len(rows)}


def load_account_snapshot() -> dict[str, Any] | None:
    ensure_storage_dirs()
    if not ACCOUNT_LATEST_PATH.exists():
        return None
    return _read_json(ACCOUNT_LATEST_PATH)


def load_signal_targets_snapshot() -> dict[str, Any] | None:
    ensure_storage_dirs()
    if not SIGNAL_TARGETS_LATEST_PATH.exists():
        return None
    return _read_json(SIGNAL_TARGETS_LATEST_PATH)


def load_symbol_snapshot(symbol: str) -> dict[str, Any] | None:
    ensure_storage_dirs()
    path = BY_SYMBOL_DIR / f"{symbol.upper()}.json"
    if not path.exists():
        return None
    return _read_json(path)


def list_synced_symbols() -> list[str]:
    ensure_storage_dirs()
    return sorted(path.stem.upper() for path in BY_SYMBOL_DIR.glob("*.json"))


def _drawdown_range_key(start_date: str | None = None, end_date: str | None = None) -> str | None:
    if not start_date and not end_date:
        return None
    return f"{start_date or 'open'}_to_{end_date or 'open'}"


def drawdown_html_path(
    symbol: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> Path:
    ensure_storage_dirs()
    range_key = _drawdown_range_key(start_date, end_date)
    if not range_key:
        return REPORT_DRAWDOWN_DIR / f"{symbol.lower()}_drawdown_longbridge.html"
    return REPORT_DRAWDOWN_DIR / f"{symbol.lower()}_drawdown_longbridge__{range_key}.html"


def drawdown_meta_path(
    symbol: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> Path:
    ensure_storage_dirs()
    range_key = _drawdown_range_key(start_date, end_date)
    if not range_key:
        return DRAWDOWN_CACHE_DIR / f"{symbol.upper()}.meta.json"
    return DRAWDOWN_CACHE_DIR / f"{symbol.upper()}__{range_key}.meta.json"


def load_drawdown_meta(
    symbol: str,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, Any] | None:
    path = drawdown_meta_path(symbol, start_date, end_date)
    if not path.exists():
        return None
    return _read_json(path)


def save_drawdown_meta(
    symbol: str,
    payload: dict[str, Any],
    start_date: str | None = None,
    end_date: str | None = None,
) -> None:
    _write_json(drawdown_meta_path(symbol, start_date, end_date), payload)


def is_drawdown_stale(
    symbol: str,
    source_version: str,
    cache_ttl_minutes: int,
    force: bool = False,
    start_date: str | None = None,
    end_date: str | None = None,
) -> bool:
    if force:
        return True

    html_path = drawdown_html_path(symbol, start_date, end_date)
    if not html_path.exists():
        return True

    meta = load_drawdown_meta(symbol, start_date, end_date)
    if not meta:
        return True

    if meta.get("source_version") != source_version:
        return True

    if meta.get("requested_start_date") != start_date:
        return True

    if meta.get("requested_end_date") != end_date:
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


def drawdown_snapshot_cache_path(symbol: str) -> Path:
    ensure_storage_dirs()
    return DRAWDOWN_SNAPSHOT_CACHE_DIR / f"{symbol.upper()}.json"


def load_drawdown_snapshot_cache(symbol: str) -> dict[str, Any] | None:
    path = drawdown_snapshot_cache_path(symbol)
    if not path.exists():
        return None
    return _read_json(path)


def save_drawdown_snapshot_cache(symbol: str, payload: dict[str, Any]) -> None:
    _write_json(drawdown_snapshot_cache_path(symbol), payload)
