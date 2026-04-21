"""Cleanup helpers for synced trade snapshots and drawdown cache artifacts."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from trade_sync.store import DRAWDOWN_CACHE_DIR, RAW_DIR, REPORT_DRAWDOWN_DIR, ensure_storage_dirs


def _parse_iso_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None


def _safe_unlink(path: Path) -> bool:
    if not path.exists():
        return False
    path.unlink()
    return True


def _html_path_for_drawdown_meta(meta_path: Path) -> Path | None:
    if not meta_path.name.endswith(".meta.json"):
        return None

    cache_key = meta_path.name[: -len(".meta.json")]
    if "__" in cache_key:
        symbol, range_key = cache_key.split("__", 1)
        return REPORT_DRAWDOWN_DIR / f"{symbol.lower()}_drawdown_longbridge__{range_key}.html"
    return REPORT_DRAWDOWN_DIR / f"{cache_key.lower()}_drawdown_longbridge.html"


def _meta_path_for_drawdown_html(html_path: Path) -> Path:
    stem = html_path.stem
    range_key = None
    if "__" in stem:
        stem, range_key = stem.split("__", 1)

    symbol = stem.replace("_drawdown_longbridge", "").upper()
    if range_key:
        return DRAWDOWN_CACHE_DIR / f"{symbol}__{range_key}.meta.json"
    return DRAWDOWN_CACHE_DIR / f"{symbol}.meta.json"


def cleanup_trade_sync_raw(*, keep_days: int, keep_count: int) -> dict[str, Any]:
    ensure_storage_dirs()
    now = datetime.now(timezone.utc)
    candidates = sorted(
        RAW_DIR.glob("*.json"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )

    deleted_files: list[str] = []
    kept_files = 0
    for index, path in enumerate(candidates):
        should_delete = False
        file_dt = datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc)
        if keep_days >= 0 and file_dt < now - timedelta(days=keep_days):
            should_delete = True
        if keep_count >= 0 and index >= keep_count:
            should_delete = True

        if should_delete:
            if _safe_unlink(path):
                deleted_files.append(path.name)
        else:
            kept_files += 1

    return {
        "kept_files": kept_files,
        "deleted_count": len(deleted_files),
        "deleted_files": deleted_files,
    }


def cleanup_drawdown_cache(*, keep_days: int) -> dict[str, Any]:
    ensure_storage_dirs()
    now = datetime.now(timezone.utc)
    deleted_html: list[str] = []
    deleted_meta: list[str] = []

    for meta_path in sorted(DRAWDOWN_CACHE_DIR.glob("*.json")):
        meta_dt = datetime.fromtimestamp(meta_path.stat().st_mtime, tz=timezone.utc)
        if keep_days >= 0 and meta_dt >= now - timedelta(days=keep_days):
            continue

        html_path = _html_path_for_drawdown_meta(meta_path)
        if _safe_unlink(meta_path):
            deleted_meta.append(meta_path.name)
        if html_path and _safe_unlink(html_path):
            deleted_html.append(html_path.name)

    for html_path in sorted(REPORT_DRAWDOWN_DIR.glob("*.html")):
        sibling_meta = _meta_path_for_drawdown_html(html_path)
        if sibling_meta.exists():
            continue
        html_dt = datetime.fromtimestamp(html_path.stat().st_mtime, tz=timezone.utc)
        if keep_days >= 0 and html_dt < now - timedelta(days=keep_days):
            if _safe_unlink(html_path):
                deleted_html.append(html_path.name)

    return {
        "deleted_html_count": len(deleted_html),
        "deleted_meta_count": len(deleted_meta),
        "deleted_html_files": deleted_html,
        "deleted_meta_files": deleted_meta,
    }


def run_trade_sync_cleanup(cleanup_config: dict[str, Any]) -> dict[str, Any]:
    if not cleanup_config.get("enabled", True):
        return {
            "enabled": False,
            "raw": {"kept_files": 0, "deleted_count": 0, "deleted_files": []},
            "drawdown": {
                "deleted_html_count": 0,
                "deleted_meta_count": 0,
                "deleted_html_files": [],
                "deleted_meta_files": [],
            },
        }

    raw_summary = cleanup_trade_sync_raw(
        keep_days=int(cleanup_config.get("raw_keep_days", 14)),
        keep_count=int(cleanup_config.get("raw_keep_count", 400)),
    )
    drawdown_summary = cleanup_drawdown_cache(
        keep_days=int(cleanup_config.get("drawdown_keep_days", 30))
    )

    return {
        "enabled": True,
        "raw": raw_summary,
        "drawdown": drawdown_summary,
    }
