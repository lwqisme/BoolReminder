"""Storage for account-signal latest runs and send ledger."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


ROOT_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = ROOT_DIR / "data" / "account_signal"
LATEST_RUN_PATH = DATA_DIR / "latest_run.json"
RUN_HISTORY_PATH = DATA_DIR / "run_history.jsonl"
LEDGER_PATH = DATA_DIR / "ledger.jsonl"


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def ensure_account_signal_dir() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)


def signal_id(signal: dict[str, Any]) -> str:
    return "|".join(
        [
            str(signal.get("symbol", "")),
            str(signal.get("action", "")),
            str(signal.get("strategy", "")),
            str(signal.get("stage", "")),
            str(signal.get("trade_date", "")),
        ]
    )


def load_latest_run() -> dict[str, Any] | None:
    ensure_account_signal_dir()
    if not LATEST_RUN_PATH.exists():
        return None
    return json.loads(LATEST_RUN_PATH.read_text(encoding="utf-8"))


def save_latest_run(payload: dict[str, Any]) -> None:
    ensure_account_signal_dir()
    LATEST_RUN_PATH.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    append_run_history(payload)


def append_run_history(payload: dict[str, Any]) -> None:
    ensure_account_signal_dir()
    history_item = {
        "run_id": payload.get("run_id"),
        "generated_at": payload.get("generated_at"),
        "dry_run": payload.get("dry_run"),
        "send_email_requested": payload.get("send_email_requested"),
        "status": payload.get("status"),
        "success": payload.get("success"),
        "errors": payload.get("errors") or [],
        "warnings": payload.get("warnings") or [],
        "email": payload.get("email") or {},
        "signals": payload.get("signals") or [],
        "new_signals": payload.get("new_signals") or [],
    }
    with RUN_HISTORY_PATH.open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(history_item, ensure_ascii=False) + "\n")


def load_run_history(limit: int = 10) -> list[dict[str, Any]]:
    ensure_account_signal_dir()
    if not RUN_HISTORY_PATH.exists():
        latest = load_latest_run()
        return [latest] if latest else []
    lines = RUN_HISTORY_PATH.read_text(encoding="utf-8").splitlines()
    items: list[dict[str, Any]] = []
    for line in reversed(lines):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        items.append(item)
        if len(items) >= limit:
            break
    return items


def load_sent_signal_ids() -> set[str]:
    ensure_account_signal_dir()
    if not LEDGER_PATH.exists():
        return set()
    sent: set[str] = set()
    for line in LEDGER_PATH.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError:
            continue
        item_id = str(item.get("signal_id", "") or "")
        if item_id:
            sent.add(item_id)
    return sent


def append_sent_signals(signals: list[dict[str, Any]], run_id: str) -> None:
    if not signals:
        return
    ensure_account_signal_dir()
    with LEDGER_PATH.open("a", encoding="utf-8") as handle:
        for signal in signals:
            handle.write(
                json.dumps(
                    {
                        "sent_at": utc_now_iso(),
                        "run_id": run_id,
                        "signal_id": signal_id(signal),
                        "symbol": signal.get("symbol"),
                        "action": signal.get("action"),
                        "strategy": signal.get("strategy"),
                        "stage": signal.get("stage"),
                        "trade_date": signal.get("trade_date"),
                    },
                    ensure_ascii=False,
                )
                + "\n"
            )
