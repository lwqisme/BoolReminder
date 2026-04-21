#!/usr/bin/env python3
"""Generate a draft drawdown visualization from a TSV or xlsx export.

Supported workflows:
1. Legacy TSV export with price and buy/sell markers.
2. Embedded xlsx export containing both price series and buy/sell blocks.
3. Operations-only xlsx export, with prices fetched from Longbridge.

The output is a single interactive HTML chart.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
import sys
from bisect import bisect_right
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET
from zipfile import ZipFile


SCRIPT_DIR = Path(__file__).resolve().parent
ROOT_DIR = SCRIPT_DIR.parent
if str(ROOT_DIR) not in sys.path:
    sys.path.insert(0, str(ROOT_DIR))

DEFAULT_INPUT = Path.home() / "Documents" / "TSLATradingLogs.xlsx"
DATE_KEYS = ("date", "trade_date", "buy_date")
AMOUNT_KEYS = ("amount", "add_amount", "trade_amount", "cash")
SHARE_KEYS = ("shares", "qty", "quantity")
TYPE_KEYS = ("type", "side", "event_type")
NS_MAIN = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
NS_BOOK = {
    "a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}
MARKET_SUFFIXES = (".US", ".HK", ".SH", ".SZ", ".SI", ".T")
TRADE_TABLE_SCAN_ROWS = 30
LONGBRIDGE_MAX_BARS = 1000
LONGBRIDGE_SHORT_RANGE_DAYS = 900
DATE_HEADER_CANDIDATES = (
    "date",
    "trade date",
    "trade_date",
    "日期",
    "交易日期",
    "成交日期",
    "时间",
    "买入日期",
    "卖出日期",
)
AMOUNT_HEADER_CANDIDATES = (
    "amount",
    "trade amount",
    "cash",
    "金额",
    "成交金额",
    "买入金额",
    "卖出金额",
)
SHARE_HEADER_CANDIDATES = (
    "shares",
    "share",
    "qty",
    "quantity",
    "股数",
    "数量",
    "成交数量",
    "买入股数",
    "卖出股数",
)
PRICE_HEADER_CANDIDATES = (
    "price",
    "trade price",
    "成交价",
    "成交价格",
    "买入价格",
    "卖出价格",
    "买入股价",
    "卖出股价",
    "真实卖出价格",
)
TYPE_HEADER_CANDIDATES = (
    "type",
    "side",
    "action",
    "买卖",
    "方向",
    "操作",
    "交易方向",
)
SYMBOL_HEADER_CANDIDATES = (
    "symbol",
    "ticker",
    "stock code",
    "code",
    "股票代码",
    "证券代码",
    "代码",
)


@dataclass
class PricePoint:
    date: datetime
    close: float
    is_buy: bool
    is_sell: bool
    rolling_peak: float
    drawdown_ath: float


@dataclass
class TradeOverlay:
    date: datetime
    amount: float | None
    shares: float | None
    event_type: str


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Generate an HTML draft chart for price, drawdown and add-position "
            "events from a TSV or xlsx export."
        )
    )
    parser.add_argument(
        "--input",
        default=str(DEFAULT_INPUT),
        help="Path to the current TSV/xlsx export. Default: %(default)s",
    )
    parser.add_argument(
        "--trades",
        help=(
            "Optional CSV with add-position amounts or share counts. "
            "Expected columns include date plus one of amount/shares."
        ),
    )
    parser.add_argument(
        "--output",
        help="Output HTML path. Default: drawdown/output/<ticker>_drawdown[_longbridge].html",
    )
    parser.add_argument(
        "--price-source",
        choices=("auto", "embedded", "longbridge"),
        default="auto",
        help=(
            "Where price history comes from for xlsx inputs. "
            "'auto' prefers embedded prices and falls back to Longbridge."
        ),
    )
    parser.add_argument(
        "--symbol",
        help=(
            "Ticker or Longbridge symbol to render, for example MSFT or MSFT.US. "
            "Required when the workbook contains multiple symbols and does not embed prices."
        ),
    )
    parser.add_argument(
        "--sheet",
        help="Optional xlsx sheet name. Defaults to the first sheet or auto-detected trade sheet.",
    )
    return parser.parse_args()


def parse_date(value: str) -> datetime:
    value = value.strip()
    for fmt in (
        "%Y/%m/%d",
        "%Y-%m-%d",
        "%Y.%m.%d",
        "%m/%d/%Y",
        "%m-%d-%Y",
    ):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unsupported date format: {value}")


def parse_excelish_date(value: str) -> datetime:
    raw = str(value).strip()
    if not raw:
        raise ValueError("Missing date value")
    if re.fullmatch(r"-?\d+(?:\.\d+)?", raw):
        return excel_serial_to_datetime(raw)
    return parse_date(raw)


def excel_serial_to_datetime(value: str) -> datetime:
    base = datetime(1899, 12, 30)
    return base + timedelta(days=float(value))


def normalize_header(value: str) -> str:
    return re.sub(r"[\s_\-]+", "", value.strip().lower())


def normalize_symbol_token(value: str) -> str:
    return re.sub(r"\s+", "", value.strip().upper())


def symbol_base(value: str) -> str:
    return normalize_symbol_token(value).split(".", 1)[0]


def normalize_longbridge_symbol(value: str) -> str:
    symbol = normalize_symbol_token(value)
    if not symbol:
        raise ValueError("Ticker/symbol is empty.")
    if any(symbol.endswith(suffix) for suffix in MARKET_SUFFIXES):
        return symbol
    return f"{symbol}.US"


def find_key(fieldnames: Iterable[str], candidates: Iterable[str]) -> str | None:
    lowered = {name.strip().lower(): name for name in fieldnames if name}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
    return None


def header_matches(header_value: str, candidates: Iterable[str]) -> bool:
    normalized = normalize_header(header_value)
    for candidate in candidates:
        candidate_normalized = normalize_header(candidate)
        if normalized == candidate_normalized or candidate_normalized in normalized:
            return True
    return False


def find_header_column(
    header_cells: dict[str, str], candidates: Iterable[str]
) -> str | None:
    for column, value in header_cells.items():
        if value and header_matches(value, candidates):
            return column
    return None


def col_letters(ref: str) -> str:
    match = re.match(r"([A-Z]+)", ref)
    if not match:
        raise ValueError(f"Bad cell reference: {ref}")
    return match.group(1)


def load_shared_strings(book: ZipFile) -> list[str]:
    if "xl/sharedStrings.xml" not in book.namelist():
        return []

    root = ET.fromstring(book.read("xl/sharedStrings.xml"))
    strings: list[str] = []
    for si in root.findall("a:si", NS_MAIN):
        strings.append("".join((t.text or "") for t in si.iterfind(".//a:t", NS_MAIN)))
    return strings


def workbook_sheet_targets(book: ZipFile) -> list[tuple[str, str]]:
    workbook = ET.fromstring(book.read("xl/workbook.xml"))
    rels = ET.fromstring(book.read("xl/_rels/workbook.xml.rels"))
    rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}

    targets: list[tuple[str, str]] = []
    for sheet in workbook.find("a:sheets", NS_BOOK):
        rel_id = sheet.attrib[
            "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
        ]
        target = rel_map[rel_id].lstrip("/")
        if not target.startswith("xl/"):
            target = f"xl/{target.lstrip('./')}"
        targets.append((sheet.attrib.get("name", ""), target))
    return targets


def read_sheet_rows_from_book(
    book: ZipFile, shared_strings: list[str], sheet_xml_path: str
) -> dict[int, dict[str, str]]:
    root = ET.fromstring(book.read(sheet_xml_path))
    rows: dict[int, dict[str, str]] = {}
    for row in root.findall(".//a:sheetData/a:row", NS_MAIN):
        row_idx = int(row.attrib["r"])
        cells: dict[str, str] = {}
        for cell in row.findall("a:c", NS_MAIN):
            ref = cell.attrib["r"]
            col = col_letters(ref)
            cell_type = cell.attrib.get("t")
            if cell_type == "inlineStr":
                value = "".join((t.text or "") for t in cell.iterfind(".//a:t", NS_MAIN))
            else:
                value_node = cell.find("a:v", NS_MAIN)
                value = value_node.text if value_node is not None else ""
                if cell_type == "s" and value:
                    value = shared_strings[int(value)]
            cells[col] = value
        rows[row_idx] = cells
    return rows


def list_xlsx_sheet_names(path: Path) -> list[str]:
    with ZipFile(path) as book:
        return [name for name, _ in workbook_sheet_targets(book)]


def read_xlsx_rows(path: Path, sheet_name: str | None = None) -> tuple[dict[int, dict[str, str]], str]:
    with ZipFile(path) as book:
        shared_strings = load_shared_strings(book)
        sheet_targets = workbook_sheet_targets(book)
        if not sheet_targets:
            raise ValueError(f"{path} does not contain any sheets.")

        if sheet_name:
            for actual_sheet_name, target in sheet_targets:
                if actual_sheet_name == sheet_name:
                    return read_sheet_rows_from_book(book, shared_strings, target), actual_sheet_name
            available = ", ".join(name for name, _ in sheet_targets)
            raise ValueError(f"Sheet '{sheet_name}' was not found in {path}. Available: {available}")

        actual_sheet_name, target = sheet_targets[0]
        return read_sheet_rows_from_book(book, shared_strings, target), actual_sheet_name


def infer_ticker_from_rows(rows: dict[int, dict[str, str]], fallback: str) -> str:
    ticker = str(rows.get(2, {}).get("I", "") or "").strip()
    if ticker and ticker not in {"股票代码", "Symbol", "Ticker"}:
        return symbol_base(ticker)
    return symbol_base(fallback) or fallback


def build_price_points_from_series(series: list[tuple[datetime, float]]) -> list[PricePoint]:
    if not series:
        return []

    points: list[PricePoint] = []
    rolling_peak = -math.inf
    for point_date, close in sorted(series, key=lambda item: item[0]):
        rolling_peak = max(rolling_peak, close)
        drawdown_ath = close / rolling_peak - 1.0
        points.append(
            PricePoint(
                date=point_date,
                close=close,
                is_buy=False,
                is_sell=False,
                rolling_peak=rolling_peak,
                drawdown_ath=drawdown_ath,
            )
        )
    return points


def extract_legacy_trade_overlays(rows: dict[int, dict[str, str]]) -> list[TradeOverlay]:
    overlays: list[TradeOverlay] = []
    for row_idx in sorted(rows):
        if row_idx < 3:
            continue
        row = rows[row_idx]

        if row.get("F") and row.get("G") and row.get("H"):
            shares = float(row["G"])
            price = float(row["H"])
            overlays.append(
                TradeOverlay(
                    date=parse_excelish_date(row["F"]),
                    amount=shares * price,
                    shares=shares,
                    event_type="buy",
                )
            )

        if row.get("K") and row.get("L") and row.get("M"):
            shares = float(row["L"])
            price = float(row["M"])
            overlays.append(
                TradeOverlay(
                    date=parse_excelish_date(row["K"]),
                    amount=shares * price,
                    shares=shares,
                    event_type="sell",
                )
            )
    return overlays


def load_embedded_xlsx_dataset(
    path: Path, sheet_name: str | None = None
) -> tuple[list[PricePoint], list[TradeOverlay], str]:
    rows, actual_sheet_name = read_xlsx_rows(path, sheet_name)
    ticker = infer_ticker_from_rows(rows, path.stem.replace("TradingLogs", "") or path.stem)
    series: list[tuple[datetime, float]] = []
    buy_dates = {
        parse_excelish_date(row["F"]).date()
        for row_idx, row in rows.items()
        if row_idx >= 3 and row.get("F") and row.get("G") and row.get("H")
    }
    sell_dates = {
        parse_excelish_date(row["K"]).date()
        for row_idx, row in rows.items()
        if row_idx >= 3 and row.get("K") and row.get("L") and row.get("M")
    }

    for row_idx in sorted(rows):
        row = rows[row_idx]
        if row_idx < 3 or not row.get("A") or not row.get("B"):
            continue
        try:
            point_date = parse_excelish_date(row["A"])
            close = float(row["B"])
        except ValueError:
            continue
        series.append((point_date, close))

    points = build_price_points_from_series(series)
    if not points:
        raise ValueError(f"No embedded price rows were parsed from {path} ({actual_sheet_name}).")

    overlays = extract_legacy_trade_overlays(rows)
    for point in points:
        if point.date.date() in buy_dates:
            point.is_buy = True
        if point.date.date() in sell_dates:
            point.is_sell = True

    return points, overlays, ticker


def detect_trade_table_header(
    rows: dict[int, dict[str, str]]
) -> tuple[int | None, dict[str, str]]:
    for row_idx in sorted(rows):
        if row_idx > TRADE_TABLE_SCAN_ROWS:
            break
        row = rows[row_idx]
        header_cells = {
            column: str(value).strip()
            for column, value in row.items()
            if str(value).strip()
        }
        if not header_cells:
            continue

        date_col = find_header_column(header_cells, DATE_HEADER_CANDIDATES)
        signal_cols = [
            find_header_column(header_cells, TYPE_HEADER_CANDIDATES),
            find_header_column(header_cells, AMOUNT_HEADER_CANDIDATES),
            find_header_column(header_cells, SHARE_HEADER_CANDIDATES),
            find_header_column(header_cells, PRICE_HEADER_CANDIDATES),
        ]
        if date_col and any(signal_cols):
            return row_idx, header_cells

    return None, {}


def infer_default_event_type(header_cells: dict[str, str]) -> str | None:
    header_text = " ".join(header_cells.values())
    if "买" in header_text or "buy" in header_text.lower():
        return "buy"
    if "卖" in header_text or "sell" in header_text.lower():
        return "sell"
    return None


def parse_event_type(value: str | None, default_type: str | None = None) -> str | None:
    raw = (value or "").strip().lower()
    if not raw:
        return default_type
    if any(token in raw for token in ("buy", "add", "long", "b")) or "买" in raw:
        return "buy"
    if any(token in raw for token in ("sell", "trim", "reduce", "s")) or "卖" in raw:
        return "sell"
    return default_type


def extract_generic_trade_overlays(
    rows: dict[int, dict[str, str]], symbol_filter: str | None
) -> tuple[list[TradeOverlay], set[str]]:
    header_row_idx, header_cells = detect_trade_table_header(rows)
    if header_row_idx is None:
        return [], set()

    date_col = find_header_column(header_cells, DATE_HEADER_CANDIDATES)
    amount_col = find_header_column(header_cells, AMOUNT_HEADER_CANDIDATES)
    share_col = find_header_column(header_cells, SHARE_HEADER_CANDIDATES)
    price_col = find_header_column(header_cells, PRICE_HEADER_CANDIDATES)
    type_col = find_header_column(header_cells, TYPE_HEADER_CANDIDATES)
    symbol_col = find_header_column(header_cells, SYMBOL_HEADER_CANDIDATES)
    default_type = infer_default_event_type(header_cells)

    if not date_col or not (amount_col or share_col or price_col):
        return [], set()

    overlays: list[TradeOverlay] = []
    seen_symbols: set[str] = set()
    requested_base = symbol_base(symbol_filter or "")

    for row_idx in sorted(rows):
        if row_idx <= header_row_idx:
            continue
        row = rows[row_idx]
        raw_date = (row.get(date_col) or "").strip()
        if not raw_date:
            continue

        current_symbol = normalize_symbol_token(row.get(symbol_col, "")) if symbol_col else ""
        if current_symbol:
            seen_symbols.add(current_symbol)
            if requested_base and symbol_base(current_symbol) != requested_base:
                continue
        elif requested_base and symbol_col:
            continue

        event_type = parse_event_type(row.get(type_col), default_type)
        if not event_type:
            continue

        shares = parse_optional_float(row.get(share_col)) if share_col else None
        price = parse_optional_float(row.get(price_col)) if price_col else None
        amount = parse_optional_float(row.get(amount_col)) if amount_col else None
        if amount is None and shares is not None and price is not None:
            amount = shares * price

        overlays.append(
            TradeOverlay(
                date=parse_excelish_date(raw_date),
                amount=amount,
                shares=shares,
                event_type=event_type,
            )
        )

    return overlays, seen_symbols


def load_trade_overlays_from_xlsx(
    path: Path, sheet_name: str | None = None, symbol_filter: str | None = None
) -> tuple[list[TradeOverlay], str]:
    candidate_sheets = [sheet_name] if sheet_name else list_xlsx_sheet_names(path)
    requested_base = symbol_base(symbol_filter or "")
    all_seen_symbols: set[str] = set()

    for candidate_sheet in candidate_sheets:
        rows, _ = read_xlsx_rows(path, candidate_sheet)

        legacy_overlays = extract_legacy_trade_overlays(rows)
        if legacy_overlays:
            ticker = infer_ticker_from_rows(rows, symbol_filter or path.stem)
            if requested_base and symbol_base(ticker) != requested_base:
                continue
            return legacy_overlays, ticker

        overlays, seen_symbols = extract_generic_trade_overlays(rows, symbol_filter)
        all_seen_symbols.update(seen_symbols)
        if overlays:
            if symbol_filter:
                return overlays, symbol_base(symbol_filter)
            if len(seen_symbols) == 1:
                return overlays, symbol_base(next(iter(seen_symbols)))
            if len(seen_symbols) > 1:
                raise ValueError(
                    f"{path} contains multiple symbols ({', '.join(sorted(seen_symbols))}). "
                    "Use --symbol to choose one."
                )
            return overlays, symbol_base(path.stem.replace("TradingLogs", "") or path.stem)

    if all_seen_symbols and not symbol_filter:
        raise ValueError(
            f"{path} contains multiple symbols ({', '.join(sorted(all_seen_symbols))}). "
            "Use --symbol to choose one."
        )

    raise ValueError(f"No trade overlays were parsed from {path}.")


def load_price_points(path: Path) -> list[PricePoint]:
    with path.open("r", encoding="utf-8") as handle:
        rows = list(csv.reader(handle, delimiter="\t"))

    if len(rows) < 3:
        raise ValueError(f"{path} does not look like a valid TSV export.")

    points: list[PricePoint] = []
    rolling_peak = -math.inf

    for row in rows[2:]:
        if len(row) < 4 or not row[0].strip() or not row[1].strip():
            continue

        point_date = parse_date(row[0])
        close = float(row[1])
        is_buy = bool(row[2].strip())
        is_sell = bool(row[3].strip())
        rolling_peak = max(rolling_peak, close)
        drawdown_ath = close / rolling_peak - 1.0

        points.append(
            PricePoint(
                date=point_date,
                close=close,
                is_buy=is_buy,
                is_sell=is_sell,
                rolling_peak=rolling_peak,
                drawdown_ath=drawdown_ath,
            )
        )

    if not points:
        raise ValueError(f"No price rows were parsed from {path}.")

    return points


def load_trade_overlays(path: Path) -> list[TradeOverlay]:
    with path.open("r", encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError(f"{path} is missing a CSV header row.")

        date_key = find_key(reader.fieldnames, DATE_KEYS)
        amount_key = find_key(reader.fieldnames, AMOUNT_KEYS)
        share_key = find_key(reader.fieldnames, SHARE_KEYS)
        type_key = find_key(reader.fieldnames, TYPE_KEYS)

        if not date_key:
            raise ValueError(f"{path} must contain a date column.")
        if not amount_key and not share_key:
            raise ValueError(f"{path} must contain an amount or shares column.")

        overlays: list[TradeOverlay] = []
        for row in reader:
            raw_date = (row.get(date_key) or "").strip()
            if not raw_date:
                continue

            event_type = parse_event_type(row.get(type_key), "buy") or "buy"
            amount = parse_optional_float(row.get(amount_key)) if amount_key else None
            shares = parse_optional_float(row.get(share_key)) if share_key else None

            overlays.append(
                TradeOverlay(
                    date=parse_date(raw_date),
                    amount=amount,
                    shares=shares,
                    event_type=event_type,
                )
            )

        return overlays


def parse_optional_float(value: str | None) -> float | None:
    if value is None:
        return None
    value = str(value).strip().replace(",", "")
    if not value:
        return None
    return float(value)


def align_trade_date(
    overlay_date: datetime, point_dates: list[str]
) -> tuple[str | None, str | None]:
    if not point_dates:
        return None, None

    overlay_key = overlay_date.strftime("%Y-%m-%d")
    if overlay_key in point_dates:
        return overlay_key, None

    position = bisect_right(point_dates, overlay_key)
    if position > 0:
        aligned = point_dates[position - 1]
        return aligned, f"{overlay_key} 对齐到最近交易日 {aligned}"

    aligned = point_dates[0]
    return aligned, f"{overlay_key} 早于价格序列起点，已对齐到 {aligned}"


def build_trade_summary(
    points: list[PricePoint], overlays: list[TradeOverlay]
) -> tuple[dict[str, dict[str, float]], list[str]]:
    point_dates = [point.date.strftime("%Y-%m-%d") for point in points]
    point_map = {point.date.strftime("%Y-%m-%d"): point for point in points}
    by_date: dict[str, dict[str, float]] = defaultdict(
        lambda: {
            "buy_amount": 0.0,
            "buy_shares": 0.0,
            "buy_count": 0.0,
            "sell_amount": 0.0,
            "sell_shares": 0.0,
            "sell_count": 0.0,
        }
    )
    warnings: list[str] = []

    for overlay in overlays:
        aligned_date, warning = align_trade_date(overlay.date, point_dates)
        if not aligned_date:
            warnings.append(f"{overlay.date.strftime('%Y-%m-%d')} 未匹配到价格序列，已跳过")
            continue
        if warning:
            warnings.append(warning)

        summary = by_date[aligned_date]
        point = point_map[aligned_date]
        if overlay.event_type in {"buy", "add", "buy_more"}:
            point.is_buy = True
            summary["buy_count"] += 1.0
            if overlay.amount is not None:
                summary["buy_amount"] += overlay.amount
            if overlay.shares is not None:
                summary["buy_shares"] += overlay.shares
        elif overlay.event_type in {"sell", "trim"}:
            point.is_sell = True
            summary["sell_count"] += 1.0
            if overlay.amount is not None:
                summary["sell_amount"] += overlay.amount
            if overlay.shares is not None:
                summary["sell_shares"] += overlay.shares

    return dict(by_date), sorted(set(warnings))


def marker_sizes(values: list[float], default_size: float) -> list[float]:
    non_zero = [value for value in values if value > 0]
    if not non_zero:
        return [default_size for _ in values]

    min_value = min(non_zero)
    max_value = max(non_zero)
    if math.isclose(min_value, max_value):
        return [default_size + 8 if value > 0 else default_size for value in values]

    sizes = []
    for value in values:
        if value <= 0:
            sizes.append(default_size)
            continue
        ratio = (value - min_value) / (max_value - min_value)
        sizes.append(10 + ratio * 18)
    return sizes


def rolling_window_drawdowns(closes: list[float], window_size: int) -> tuple[list[float], list[float]]:
    peaks: list[float] = []
    drawdowns: list[float] = []

    for index, close in enumerate(closes):
        start = max(0, index - window_size + 1)
        window_peak = max(closes[start : index + 1])
        peaks.append(window_peak)
        drawdowns.append(close / window_peak - 1.0)

    return peaks, drawdowns


def build_chart_payload(
    points: list[PricePoint], trade_summary: dict[str, dict[str, float]]
) -> dict[str, object]:
    dates = [point.date.strftime("%Y-%m-%d") for point in points]
    closes = [point.close for point in points]
    peaks = [point.rolling_peak for point in points]
    rolling_120_peaks, drawdowns_120_raw = rolling_window_drawdowns(closes, window_size=120)
    drawdowns_ath = [point.drawdown_ath * 100 for point in points]
    drawdowns_120 = [value * 100 for value in drawdowns_120_raw]

    buy_dates: list[str] = []
    buy_prices: list[float] = []
    buy_drawdowns_ath: list[float] = []
    buy_drawdowns_120: list[float] = []
    buy_amounts: list[float] = []
    buy_shares: list[float] = []
    buy_labels: list[str] = []
    buy_drawdown_labels: list[str] = []

    sell_dates: list[str] = []
    sell_prices: list[float] = []
    sell_drawdowns_ath: list[float] = []
    sell_drawdowns_120: list[float] = []
    sell_labels: list[str] = []
    sell_drawdown_labels: list[str] = []

    buy_bar_dates: list[str] = []
    buy_bar_values: list[float] = []
    buy_bar_labels: list[str] = []
    sell_bar_dates: list[str] = []
    sell_bar_values: list[float] = []
    sell_bar_labels: list[str] = []
    daily_buy_amounts: list[float] = []
    daily_buy_shares: list[float] = []
    daily_buy_counts: list[float] = []
    daily_sell_amounts: list[float] = []
    daily_sell_shares: list[float] = []
    daily_sell_counts: list[float] = []
    uses_trade_amounts = any(
        summary["buy_amount"] > 0
        or summary["buy_shares"] > 0
        or summary["sell_amount"] > 0
        or summary["sell_shares"] > 0
        for summary in trade_summary.values()
    )
    use_amount_bars = any(
        summary["buy_amount"] > 0 or summary["sell_amount"] > 0
        for summary in trade_summary.values()
    )

    for index, point in enumerate(points):
        date_key = point.date.strftime("%Y-%m-%d")
        summary = trade_summary.get(
            date_key,
            {
                "buy_amount": 0.0,
                "buy_shares": 0.0,
                "buy_count": 0.0,
                "sell_amount": 0.0,
                "sell_shares": 0.0,
                "sell_count": 0.0,
            },
        )
        drawdown_120_raw = drawdowns_120_raw[index]
        daily_buy_amounts.append(summary["buy_amount"])
        daily_buy_shares.append(summary["buy_shares"])
        daily_buy_counts.append(summary["buy_count"])
        daily_sell_amounts.append(summary["sell_amount"])
        daily_sell_shares.append(summary["sell_shares"])
        daily_sell_counts.append(summary["sell_count"])

        if point.is_buy:
            buy_dates.append(date_key)
            buy_prices.append(point.close)
            buy_drawdowns_ath.append(drawdowns_ath[index])
            buy_drawdowns_120.append(drawdowns_120[index])
            buy_amounts.append(summary["buy_amount"])
            buy_shares.append(summary["buy_shares"])
            if uses_trade_amounts:
                buy_labels.append(
                    format_buy_label(
                        point.close,
                        point.drawdown_ath,
                        drawdown_120_raw,
                        summary["buy_amount"],
                        summary["buy_shares"],
                    )
                )
                buy_drawdown_labels.append(
                    format_drawdown_marker_label(
                        "买入",
                        point.close,
                        summary["buy_amount"],
                        summary["buy_shares"],
                    )
                )
            else:
                buy_labels.append(
                    "<br>".join(
                        [
                            f"买点价格: {point.close:.2f}",
                            f"ATH 回撤: {point.drawdown_ath * 100:.2f}%",
                            f"120d 回撤: {drawdown_120_raw * 100:.2f}%",
                            "加仓金额: 待补充",
                        ]
                    )
                )
                buy_drawdown_labels.append(f"买入价: {point.close:.2f}")

        if point.is_sell:
            sell_dates.append(date_key)
            sell_prices.append(point.close)
            sell_drawdowns_ath.append(drawdowns_ath[index])
            sell_drawdowns_120.append(drawdowns_120[index])
            sell_labels.append(
                format_sell_label(
                    point.close,
                    point.drawdown_ath,
                    drawdown_120_raw,
                    summary["sell_amount"],
                    summary["sell_shares"],
                )
            )
            sell_drawdown_labels.append(
                format_drawdown_marker_label(
                    "卖出",
                    point.close,
                    summary["sell_amount"],
                    summary["sell_shares"],
                )
            )

        if uses_trade_amounts and (summary["buy_amount"] > 0 or summary["buy_shares"] > 0):
            buy_bar_dates.append(date_key)
            buy_bar_values.append(
                summary["buy_amount"] if use_amount_bars and summary["buy_amount"] > 0 else summary["buy_shares"]
            )
            buy_bar_labels.append(
                format_bar_label(
                    "买入",
                    summary["buy_amount"],
                    summary["buy_shares"],
                    point.drawdown_ath,
                    drawdown_120_raw,
                )
            )

        if uses_trade_amounts and (summary["sell_amount"] > 0 or summary["sell_shares"] > 0):
            sell_bar_dates.append(date_key)
            sell_value = (
                summary["sell_amount"] if use_amount_bars and summary["sell_amount"] > 0 else summary["sell_shares"]
            )
            sell_bar_values.append(-sell_value)
            sell_bar_labels.append(
                format_bar_label(
                    "卖出",
                    summary["sell_amount"],
                    summary["sell_shares"],
                    point.drawdown_ath,
                    drawdown_120_raw,
                )
            )

    size_basis = buy_amounts if any(value > 0 for value in buy_amounts) else buy_shares
    sizes = marker_sizes(size_basis, default_size=12)

    max_drawdown_ath = min(drawdowns_ath)
    max_drawdown_ath_index = drawdowns_ath.index(max_drawdown_ath)
    max_drawdown_120 = min(drawdowns_120)
    max_drawdown_120_index = drawdowns_120.index(max_drawdown_120)

    return {
        "dates": dates,
        "closes": closes,
        "peaks": peaks,
        "rolling_120_peaks": rolling_120_peaks,
        "drawdowns_ath": drawdowns_ath,
        "drawdowns_120": drawdowns_120,
        "buy_dates": buy_dates,
        "buy_prices": buy_prices,
        "buy_drawdowns_ath": buy_drawdowns_ath,
        "buy_drawdowns_120": buy_drawdowns_120,
        "buy_amounts": buy_amounts,
        "buy_shares": buy_shares,
        "buy_sizes": sizes,
        "buy_labels": buy_labels,
        "buy_drawdown_labels": buy_drawdown_labels,
        "sell_dates": sell_dates,
        "sell_prices": sell_prices,
        "sell_drawdowns_ath": sell_drawdowns_ath,
        "sell_drawdowns_120": sell_drawdowns_120,
        "sell_labels": sell_labels,
        "sell_drawdown_labels": sell_drawdown_labels,
        "buy_bar_dates": buy_bar_dates,
        "buy_bar_values": buy_bar_values,
        "buy_bar_labels": buy_bar_labels,
        "sell_bar_dates": sell_bar_dates,
        "sell_bar_values": sell_bar_values,
        "sell_bar_labels": sell_bar_labels,
        "daily_buy_amounts": daily_buy_amounts,
        "daily_buy_shares": daily_buy_shares,
        "daily_buy_counts": daily_buy_counts,
        "daily_sell_amounts": daily_sell_amounts,
        "daily_sell_shares": daily_sell_shares,
        "daily_sell_counts": daily_sell_counts,
        "bar_width_ms": 1000 * 60 * 60 * 24 * 4,
        "bar_unit_label": "Trade Amount" if use_amount_bars else "Trade Shares",
        "uses_trade_amounts": uses_trade_amounts,
        "max_drawdown_ath_date": dates[max_drawdown_ath_index],
        "max_drawdown_ath_value": max_drawdown_ath,
        "max_drawdown_120_date": dates[max_drawdown_120_index],
        "max_drawdown_120_value": max_drawdown_120,
    }


def format_buy_label(
    close: float, drawdown_ath: float, drawdown_120: float, amount: float, shares: float
) -> str:
    lines = [
        f"买点价格: {close:.2f}",
        f"ATH 回撤: {drawdown_ath * 100:.2f}%",
        f"120d 回撤: {drawdown_120 * 100:.2f}%",
    ]
    if amount > 0:
        lines.append(f"加仓金额: {amount:,.2f}")
    if shares > 0:
        lines.append(f"加仓股数: {shares:,.4f}")
    return "<br>".join(lines)


def format_sell_label(
    close: float, drawdown_ath: float, drawdown_120: float, amount: float, shares: float
) -> str:
    lines = [
        f"卖点价格: {close:.2f}",
        f"ATH 回撤: {drawdown_ath * 100:.2f}%",
        f"120d 回撤: {drawdown_120 * 100:.2f}%",
    ]
    if amount > 0:
        lines.append(f"卖出金额: {amount:,.2f}")
    if shares > 0:
        lines.append(f"卖出股数: {shares:,.4f}")
    return "<br>".join(lines)


def format_bar_label(
    action: str, amount: float, shares: float, drawdown_ath: float, drawdown_120: float
) -> str:
    lines = [
        f"动作: {action}",
        f"ATH 回撤: {drawdown_ath * 100:.2f}%",
        f"120d 回撤: {drawdown_120 * 100:.2f}%",
    ]
    if amount > 0:
        lines.append(f"{action}金额: {amount:,.2f}")
    if shares > 0:
        lines.append(f"{action}股数: {shares:,.4f}")
    return "<br>".join(lines)


def format_drawdown_marker_label(
    action: str, close: float, amount: float, shares: float
) -> str:
    lines = [f"{action}价: {close:.2f}"]
    if amount > 0:
        lines.append(f"{action}金额: {amount:,.2f}")
    if shares > 0:
        lines.append(f"{action}股数: {shares:,.4f}")
    return "<br>".join(lines)


def render_html(
    payload: dict[str, object], warnings: list[str], ticker: str, price_source_label: str
) -> str:
    title_suffix = "已接入交易金额/股数" if payload["uses_trade_amounts"] else "金额待补充"
    warning_html = ""
    if warnings:
        warning_html = (
            "<div class='warning'>以下日期已自动对齐或跳过: "
            + ", ".join(warnings)
            + "</div>"
        )

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
  <meta charset="utf-8">
  <meta name="viewport" content="width=device-width, initial-scale=1">
  <title>{ticker} 回撤与交易可视化</title>
  <script src="https://cdn.plot.ly/plotly-2.35.2.min.js"></script>
  <style>
    :root {{
      --bg: #f5f0e8;
      --card: rgba(255, 250, 243, 0.92);
      --ink: #172121;
      --muted: #5c605f;
      --accent: #1f6f78;
      --buy: #d95d39;
      --sell: #243b53;
      --drawdown: #d1495b;
      --peak: #7c8c77;
      --grid: rgba(23, 33, 33, 0.12);
      --warning: #7a2e0b;
    }}

    * {{
      box-sizing: border-box;
    }}

    body {{
      margin: 0;
      color: var(--ink);
      font-family: "Iowan Old Style", "Palatino Linotype", "Noto Serif SC", serif;
      background:
        radial-gradient(circle at top left, rgba(217, 93, 57, 0.18), transparent 32%),
        radial-gradient(circle at 85% 20%, rgba(31, 111, 120, 0.18), transparent 30%),
        linear-gradient(180deg, #fbf7f2 0%, var(--bg) 100%);
      min-height: 100vh;
    }}

    .shell {{
      max-width: 1200px;
      margin: 0 auto;
      padding: 32px 20px 40px;
    }}

    .header {{
      display: grid;
      gap: 10px;
      margin-bottom: 18px;
    }}

    .eyebrow {{
      letter-spacing: 0.14em;
      text-transform: uppercase;
      font-size: 12px;
      color: var(--muted);
    }}

    h1 {{
      margin: 0;
      font-size: clamp(30px, 6vw, 56px);
      line-height: 0.95;
      font-weight: 700;
    }}

    .sub {{
      max-width: 820px;
      color: var(--muted);
      font-size: 16px;
      line-height: 1.5;
    }}

    .stats {{
      display: flex;
      flex-wrap: wrap;
      gap: 12px;
      margin: 20px 0 16px;
    }}

    .stat {{
      background: var(--card);
      border: 1px solid rgba(23, 33, 33, 0.08);
      border-radius: 16px;
      padding: 12px 16px;
      min-width: 180px;
      backdrop-filter: blur(8px);
    }}

    .stat .label {{
      font-size: 12px;
      color: var(--muted);
      margin-bottom: 6px;
      text-transform: uppercase;
      letter-spacing: 0.08em;
    }}

    .stat .value {{
      font-size: 22px;
      font-weight: 700;
    }}

    .warning {{
      margin: 10px 0 18px;
      padding: 12px 14px;
      border-radius: 12px;
      background: rgba(217, 93, 57, 0.12);
      color: var(--warning);
      font-size: 14px;
    }}

    #chart {{
      height: 860px;
      border-radius: 22px;
      overflow: hidden;
      touch-action: pan-y;
      background: var(--card);
      border: 1px solid rgba(23, 33, 33, 0.08);
      box-shadow: 0 24px 60px rgba(23, 33, 33, 0.12);
    }}

    .controls {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      justify-content: space-between;
      gap: 12px;
      margin: 8px 0 14px;
    }}

    .toggle {{
      display: inline-flex;
      align-items: center;
      gap: 10px;
      padding: 10px 14px;
      border-radius: 999px;
      background: var(--card);
      border: 1px solid rgba(23, 33, 33, 0.08);
      color: var(--ink);
      font-size: 14px;
      cursor: pointer;
      user-select: none;
    }}

    .toggle input {{
      width: 16px;
      height: 16px;
      accent-color: var(--accent);
    }}

    .hint-box {{
      margin-top: 14px;
      padding: 16px 18px;
      border-radius: 16px;
      background: rgba(255, 250, 243, 0.88);
      border: 1px solid rgba(23, 33, 33, 0.08);
      color: var(--muted);
      font-size: 13px;
      line-height: 1.6;
    }}

    .hint-title {{
      margin: 0 0 8px;
      color: var(--ink);
      font-size: 14px;
      font-weight: 700;
    }}

    .hint-box p {{
      margin: 6px 0;
    }}

    .chart-stage {{
      display: grid;
      gap: 12px;
      align-items: start;
    }}

    .chart-toolbar {{
      display: flex;
      flex-wrap: wrap;
      align-items: center;
      gap: 12px;
      margin-bottom: 8px;
    }}

    .chart-stack {{
      position: relative;
      min-width: 0;
    }}

    .crosshair-overlay {{
      position: absolute;
      inset: 0;
      pointer-events: none;
      opacity: 0;
      transition: opacity 120ms ease;
    }}

    .crosshair-overlay.is-active {{
      opacity: 1;
    }}

    .crosshair-line {{
      position: absolute;
      display: none;
    }}

    .crosshair-line.is-active {{
      display: block;
    }}

    .crosshair-line--v {{
      width: 0;
      border-left: 2px dashed rgba(31, 111, 120, 0.75);
    }}

    .crosshair-line--h {{
      height: 0;
      border-top: 2px dashed rgba(31, 111, 120, 0.45);
    }}

    .hover-card {{
      position: absolute;
      top: 16px;
      left: 16px;
      z-index: 12;
      width: min(260px, calc(100% - 24px));
      padding: 12px 14px;
      border-radius: 16px;
      background: rgba(255, 250, 243, 0.96);
      border: 1px solid rgba(23, 33, 33, 0.08);
      box-shadow: 0 18px 38px rgba(23, 33, 33, 0.1);
      pointer-events: none;
      opacity: 0;
      transform: translate3d(0, 0, 0);
      transition: opacity 120ms ease;
    }}

    .hover-card.is-visible {{
      opacity: 1;
    }}

    .mode-switch {{
      display: inline-flex;
      flex-wrap: wrap;
      gap: 8px;
      align-items: center;
    }}

    .mode-button {{
      appearance: none;
      border: 1px solid rgba(23, 33, 33, 0.12);
      background: rgba(255, 250, 243, 0.88);
      color: var(--muted);
      border-radius: 999px;
      padding: 8px 12px;
      font-size: 13px;
      line-height: 1;
      cursor: pointer;
    }}

    .mode-button.is-active {{
      background: var(--accent);
      color: #fffaf3;
      border-color: var(--accent);
    }}

    .hover-card-label {{
      margin: 0 0 8px;
      color: var(--muted);
      font-size: 11px;
      letter-spacing: 0.08em;
      text-transform: uppercase;
    }}

    .hover-card-empty {{
      color: var(--muted);
      font-size: 12px;
      line-height: 1.5;
    }}

    .hover-card-body {{
      display: grid;
      gap: 8px;
    }}

    .hover-card-body[hidden] {{
      display: none;
    }}

    .hover-section {{
      display: grid;
      gap: 4px;
      padding-top: 8px;
      border-top: 1px solid rgba(23, 33, 33, 0.08);
    }}

    .hover-section:first-child {{
      padding-top: 0;
      border-top: 0;
    }}

    .hover-section-title {{
      color: var(--ink);
      font-size: 12px;
      font-weight: 700;
    }}

    .hover-row {{
      display: flex;
      justify-content: space-between;
      gap: 8px;
      font-size: 12px;
      line-height: 1.35;
    }}

    .hover-row span:first-child {{
      color: var(--muted);
    }}

    .hover-row span:last-child {{
      color: var(--ink);
      text-align: right;
      font-variant-numeric: tabular-nums;
    }}

    @media (max-width: 820px) {{
      .shell {{
        padding: 20px 12px 28px;
      }}

      .stats {{
        gap: 8px;
      }}

      .stat {{
        min-width: 132px;
        padding: 10px 12px;
      }}

      #chart {{
        height: 760px;
      }}

      .controls {{
        margin: 6px 0 10px;
      }}

      .toggle {{
        padding: 8px 12px;
        font-size: 13px;
      }}

      .chart-toolbar {{
        gap: 10px;
        margin-bottom: 6px;
      }}

      .mode-switch {{
        gap: 6px;
      }}

      .mode-button {{
        padding: 7px 10px;
        font-size: 12px;
      }}
    }}

    @media (max-width: 560px) {{
      #chart {{
        height: 700px;
      }}

      .hover-card {{
        width: min(220px, calc(100% - 20px));
        padding: 10px 12px;
      }}
    }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="header">
      <div class="eyebrow">{ticker} / Drawdown View</div>
      <h1>回撤与交易叠加视图</h1>
      <div class="sub">
        当前版本基于 {ticker} 的收盘价序列和交易日志生成。价格源: {price_source_label}。状态: {title_suffix}。
        上图看价格与峰值，下图提供窗口内 All-time High 与 Rolling 120d High 两套回撤口径，可在按钮里切换或共同显示。
      </div>
    </div>
    <div class="stats">
      <div class="stat">
        <div class="label">ATH 最大回撤</div>
        <div class="value">{payload["max_drawdown_ath_value"]:.2f}%</div>
      </div>
      <div class="stat">
        <div class="label">ATH 最大回撤日期</div>
        <div class="value">{payload["max_drawdown_ath_date"]}</div>
      </div>
      <div class="stat">
        <div class="label">120d 最大回撤</div>
        <div class="value">{payload["max_drawdown_120_value"]:.2f}%</div>
      </div>
      <div class="stat">
        <div class="label">120d 最大回撤日期</div>
        <div class="value">{payload["max_drawdown_120_date"]}</div>
      </div>
      <div class="stat">
        <div class="label">买点数</div>
        <div class="value">{len(payload["buy_dates"])}</div>
      </div>
      <div class="stat">
        <div class="label">卖点数</div>
        <div class="value">{len(payload["sell_dates"])}</div>
      </div>
    </div>
    {warning_html}
    <div class="chart-stage">
      <div class="chart-toolbar">
        <label class="toggle" for="crosshair-toggle">
          <input id="crosshair-toggle" type="checkbox" checked>
          <span>十字线</span>
        </label>
        <label class="toggle" for="snapshot-toggle">
          <input id="snapshot-toggle" type="checkbox" checked>
          <span>悬浮窗</span>
        </label>
        <div class="mode-switch" aria-label="Drawdown modes">
          <button class="mode-button" data-mode="alltime" type="button">All-time</button>
          <button class="mode-button" data-mode="rolling" type="button">Rolling 120d</button>
          <button class="mode-button is-active" data-mode="both" type="button">Both</button>
        </div>
      </div>
      <div class="chart-stack">
        <div id="chart"></div>
        <div id="crosshair-overlay" class="crosshair-overlay">
          <div id="crosshair-v" class="crosshair-line crosshair-line--v"></div>
          <div id="crosshair-h" class="crosshair-line crosshair-line--h"></div>
        </div>
        <aside id="hover-card" class="hover-card">
          <div class="hover-card-label">Cursor Snapshot</div>
          <div id="hover-card-empty" class="hover-card-empty">
            鼠标或手指移动到图表内时，这里会跟随显示最近交易日的价格、回撤和买卖信息。
          </div>
          <div id="hover-card-body" class="hover-card-body" hidden></div>
        </aside>
      </div>
    </div>
    <div class="hint-box">
      <div class="hint-title">图表说明</div>
      <p>价格源支持内嵌 xlsx 时序和 Longbridge 日线两种模式。当前默认显示 <code>Both</code>，也就是 All-time 与 Rolling 120d 两套回撤口径同时显示。</p>
      <p>Longbridge 模式当前使用前复权日线，这样股票拆分不会制造假性暴跌；同时这里的 <code>All-time</code> 仍然只指当前加载窗口内的历史高点，不是上市以来全历史高点。</p>
      <p>如果后续补充规范交易文件，建议字段使用 <code>date,amount,shares,type</code>。脚本会按日期自动合并，买卖点圆点按金额或股数缩放，底部成交柱同步展示买入和卖出。</p>
      <p>移动端默认保持页面滚动，长按图表后才进入十字线检查模式；松手或改为纵向拖动会退出检查模式。</p>
    </div>
  </div>
  <script>
    const payload = {json.dumps(payload, ensure_ascii=False)};
    const usesTradeAmounts = payload.uses_trade_amounts;
    const dateTimestamps = payload.dates.map((value) => Date.parse(`${{value}}T00:00:00Z`));
    const currencyFormatter = new Intl.NumberFormat("en-US", {{
      minimumFractionDigits: 2,
      maximumFractionDigits: 2
    }});
    const shareFormatter = new Intl.NumberFormat("en-US", {{
      minimumFractionDigits: 0,
      maximumFractionDigits: 4
    }});

    function formatPrice(value) {{
      return value == null ? " -" : currencyFormatter.format(value);
    }}

    function formatPercent(value) {{
      return value == null ? " -" : `${{value.toFixed(2)}}%`;
    }}

    function formatAmount(value) {{
      return value > 0 ? currencyFormatter.format(value) : " -";
    }}

    function formatShares(value) {{
      return value > 0 ? shareFormatter.format(value) : " -";
    }}

    function formatCount(value) {{
      return value > 0 ? `${{Math.round(value)}}` : " -";
    }}

    function hasTradeData(side, index) {{
      if (index < 0 || index >= payload.dates.length) {{
        return false;
      }}
      return (
        payload[`daily_${{side}}_counts`][index] > 0 ||
        payload[`daily_${{side}}_amounts`][index] > 0 ||
        payload[`daily_${{side}}_shares`][index] > 0
      );
    }}

    function findNearestTrade(side, index, maxOffset = 5) {{
      if (hasTradeData(side, index)) {{
        return {{ index, offset: 0 }};
      }}
      for (let distance = 1; distance <= maxOffset; distance += 1) {{
        const candidates = [index - distance, index + distance];
        for (const candidate of candidates) {{
          if (hasTradeData(side, candidate)) {{
            return {{ index: candidate, offset: candidate - index }};
          }}
        }}
      }}
      return null;
    }}

    function formatTradeOffset(offset) {{
      if (offset === 0) {{
        return "当前交易日";
      }}
      return offset < 0
        ? `前${{Math.abs(offset)}}个交易日`
        : `后${{Math.abs(offset)}}个交易日`;
    }}

    function renderTradeSection(side, anchorIndex) {{
      const match = findNearestTrade(side, anchorIndex, 5);
      if (!match) {{
        return "";
      }}
      const prefix = side === "buy" ? "买入" : "卖出";
      const tradeIndex = match.index;
      const rows = [
        `<div class="hover-row"><span>交易日期</span><span>${{payload.dates[tradeIndex]}}</span></div>`,
        `<div class="hover-row"><span>位置</span><span>${{formatTradeOffset(match.offset)}}</span></div>`,
        `<div class="hover-row"><span>笔数</span><span>${{formatCount(payload[`daily_${{side}}_counts`][tradeIndex])}}</span></div>`
      ];
      if (payload[`daily_${{side}}_amounts`][tradeIndex] > 0) {{
        rows.push(
          `<div class="hover-row"><span>金额</span><span>${{formatAmount(payload[`daily_${{side}}_amounts`][tradeIndex])}}</span></div>`
        );
      }}
      if (payload[`daily_${{side}}_shares`][tradeIndex] > 0) {{
        rows.push(
          `<div class="hover-row"><span>股数</span><span>${{formatShares(payload[`daily_${{side}}_shares`][tradeIndex])}}</span></div>`
        );
      }}
      return `
        <div class="hover-section">
          <div class="hover-section-title">${{prefix}}</div>
          ${{rows.join("")}}
        </div>
      `;
    }}

    function renderHoverCard(index) {{
      const hoverCardBody = document.getElementById("hover-card-body");
      const hoverCardEmpty = document.getElementById("hover-card-empty");
      hoverCardEmpty.hidden = true;
      hoverCardBody.hidden = false;
      const sections = [`
        <div class="hover-section">
          <div class="hover-section-title">${{payload.dates[index]}}</div>
          <div class="hover-row"><span>收盘价</span><span>${{formatPrice(payload.closes[index])}}</span></div>
          <div class="hover-row"><span>ATH 回撤</span><span>${{formatPercent(payload.drawdowns_ath[index])}}</span></div>
          <div class="hover-row"><span>120d 回撤</span><span>${{formatPercent(payload.drawdowns_120[index])}}</span></div>
          <div class="hover-row"><span>ATH Peak</span><span>${{formatPrice(payload.peaks[index])}}</span></div>
          <div class="hover-row"><span>120d Peak</span><span>${{formatPrice(payload.rolling_120_peaks[index])}}</span></div>
        </div>
      `];
      const buySection = renderTradeSection("buy", index);
      const sellSection = renderTradeSection("sell", index);
      if (buySection) {{
        sections.push(buySection);
      }}
      if (sellSection) {{
        sections.push(sellSection);
      }}
      hoverCardBody.innerHTML = sections.join("");
    }}

    const priceTrace = {{
      x: payload.dates,
      y: payload.closes,
      type: "scatter",
      mode: "lines",
      name: "Price",
      line: {{ color: "#172121", width: 2.4 }},
      hovertemplate: "日期: %{{x}}<br>收盘价: %{{y:.2f}}<extra></extra>",
      xaxis: "x",
      yaxis: "y"
    }};

    const peakTrace = {{
      x: payload.dates,
      y: payload.peaks,
      type: "scatter",
      mode: "lines",
      name: "ATH Peak",
      line: {{ color: "#7c8c77", width: 1.8, dash: "dot" }},
      hoverinfo: "skip",
      xaxis: "x",
      yaxis: "y"
    }};

    const rolling120PeakTrace = {{
      x: payload.dates,
      y: payload.rolling_120_peaks,
      type: "scatter",
      mode: "lines",
      name: "120d Peak",
      line: {{ color: "#1f6f78", width: 1.6, dash: "dot" }},
      hoverinfo: "skip",
      xaxis: "x",
      yaxis: "y"
    }};

    const buyTrace = {{
      x: payload.buy_dates,
      y: payload.buy_prices,
      type: "scatter",
      mode: "markers",
      name: "Buy",
      marker: {{
        color: "#d95d39",
        size: payload.buy_sizes,
        line: {{ color: "#fff7ee", width: 1.2 }},
        opacity: 0.95
      }},
      text: payload.buy_labels,
      hovertemplate: "日期: %{{x}}<br>%{{text}}<extra></extra>",
      xaxis: "x",
      yaxis: "y"
    }};

    const sellTrace = {{
      x: payload.sell_dates,
      y: payload.sell_prices,
      type: "scatter",
      mode: "markers",
      name: "Sell",
      marker: {{
        color: "#243b53",
        symbol: "diamond",
        size: 13,
        line: {{ color: "#fff7ee", width: 1.2 }}
      }},
      text: payload.sell_labels,
      hovertemplate: "日期: %{{x}}<br>%{{text}}<extra></extra>",
      xaxis: "x",
      yaxis: "y"
    }};

    const drawdownAthTrace = {{
      x: payload.dates,
      y: payload.drawdowns_ath,
      type: "scatter",
      mode: "lines",
      name: "ATH DD",
      fill: "tozeroy",
      line: {{ color: "#d1495b", width: 2 }},
      fillcolor: "rgba(209, 73, 91, 0.24)",
      hovertemplate: "日期: %{{x}}<br>ATH 回撤: %{{y:.2f}}%<extra></extra>",
      xaxis: "x2",
      yaxis: "y2"
    }};

    const drawdown120Trace = {{
      x: payload.dates,
      y: payload.drawdowns_120,
      type: "scatter",
      mode: "lines",
      name: "120d DD",
      line: {{ color: "#1f6f78", width: 2.2 }},
      hovertemplate: "日期: %{{x}}<br>120d 回撤: %{{y:.2f}}%<extra></extra>",
      xaxis: "x2",
      yaxis: "y2"
    }};

    const buyDrawdownAthTrace = {{
      x: payload.buy_dates,
      y: payload.buy_drawdowns_ath,
      type: "scatter",
      mode: "markers",
      name: "买点 / ATH 回撤",
      marker: {{
        color: "#d95d39",
        size: payload.buy_sizes,
        line: {{ color: "#fff7ee", width: 1.2 }},
        opacity: 0.95
      }},
      text: payload.buy_drawdown_labels,
      hovertemplate: "日期: %{{x}}<br>ATH 回撤: %{{y:.2f}}%<br>%{{text}}<extra></extra>",
      xaxis: "x2",
      yaxis: "y2",
      showlegend: false
    }};

    const buyDrawdown120Trace = {{
      x: payload.buy_dates,
      y: payload.buy_drawdowns_120,
      type: "scatter",
      mode: "markers",
      name: "买点 / 120d 回撤",
      marker: {{
        color: "#1f6f78",
        size: payload.buy_sizes.map(size => Math.max(8, size - 1)),
        symbol: "circle-open",
        line: {{ color: "#1f6f78", width: 1.8 }},
        opacity: 0.95
      }},
      text: payload.buy_drawdown_labels,
      hovertemplate: "日期: %{{x}}<br>120d 回撤: %{{y:.2f}}%<br>%{{text}}<extra></extra>",
      xaxis: "x2",
      yaxis: "y2",
      showlegend: false
    }};

    const sellDrawdownAthTrace = {{
      x: payload.sell_dates,
      y: payload.sell_drawdowns_ath,
      type: "scatter",
      mode: "markers",
      name: "卖点 / ATH 回撤",
      marker: {{
        color: "#243b53",
        symbol: "diamond",
        size: 13,
        line: {{ color: "#fff7ee", width: 1.2 }}
      }},
      text: payload.sell_drawdown_labels,
      hovertemplate: "日期: %{{x}}<br>ATH 回撤: %{{y:.2f}}%<br>%{{text}}<extra></extra>",
      xaxis: "x2",
      yaxis: "y2",
      showlegend: false
    }};

    const sellDrawdown120Trace = {{
      x: payload.sell_dates,
      y: payload.sell_drawdowns_120,
      type: "scatter",
      mode: "markers",
      name: "卖点 / 120d 回撤",
      marker: {{
        color: "#243b53",
        symbol: "diamond-open",
        size: 13,
        line: {{ color: "#243b53", width: 1.8 }}
      }},
      text: payload.sell_drawdown_labels,
      hovertemplate: "日期: %{{x}}<br>120d 回撤: %{{y:.2f}}%<br>%{{text}}<extra></extra>",
      xaxis: "x2",
      yaxis: "y2",
      showlegend: false
    }};

    const traces = [
      priceTrace,
      peakTrace,
      rolling120PeakTrace,
      buyTrace,
      sellTrace,
      drawdownAthTrace,
      drawdown120Trace,
      buyDrawdownAthTrace,
      buyDrawdown120Trace,
      sellDrawdownAthTrace,
      sellDrawdown120Trace
    ];

    if (usesTradeAmounts && payload.buy_bar_dates.length > 0) {{
      traces.push({{
        x: payload.buy_bar_dates,
        y: payload.buy_bar_values,
        type: "bar",
        name: "Buy Amt",
        marker: {{
          color: "#1f6f78",
          opacity: 0.82
        }},
        width: payload.buy_bar_dates.map(() => payload.bar_width_ms),
        text: payload.buy_bar_labels,
        hovertemplate: "日期: %{{x}}<br>%{{text}}<extra></extra>",
        xaxis: "x3",
        yaxis: "y3"
      }});
    }}

    if (usesTradeAmounts && payload.sell_bar_dates.length > 0) {{
      traces.push({{
        x: payload.sell_bar_dates,
        y: payload.sell_bar_values,
        type: "bar",
        name: "Sell Amt",
        marker: {{
          color: "#243b53",
          opacity: 0.78
        }},
        width: payload.sell_bar_dates.map(() => payload.bar_width_ms),
        text: payload.sell_bar_labels,
        hovertemplate: "日期: %{{x}}<br>%{{text}}<extra></extra>",
        xaxis: "x3",
        yaxis: "y3"
      }});
    }}

    const hasBuyBarTrace = usesTradeAmounts && payload.buy_bar_dates.length > 0;
    const hasSellBarTrace = usesTradeAmounts && payload.sell_bar_dates.length > 0;
    let currentMode = "both";

    function visibilityFor(mode) {{
      const visible = [
        true,
        mode !== "rolling",
        mode !== "alltime",
        true,
        true,
        mode !== "rolling",
        mode !== "alltime",
        mode !== "rolling",
        mode !== "alltime",
        mode !== "rolling",
        mode !== "alltime"
      ];

      if (hasBuyBarTrace) {{
        visible.push(true);
      }}

      if (hasSellBarTrace) {{
        visible.push(true);
      }}

      return visible;
    }}

    const layout = {{
      paper_bgcolor: "rgba(255,255,255,0)",
      plot_bgcolor: "rgba(255,255,255,0)",
      margin: {{ l: 70, r: 26, t: 128, b: 56 }},
      legend: {{
        orientation: "h",
        yanchor: "bottom",
        y: 1.08,
        xanchor: "left",
        x: 0,
        bgcolor: "rgba(255, 255, 255, 0)",
        borderwidth: 0,
        tracegroupgap: 8,
        font: {{ size: 11 }}
      }},
      showlegend: true,
      hovermode: "closest",
      dragmode: false,
      bargap: 0.1,
      xaxis: {{
        domain: [0, 1],
        anchor: "y",
        showgrid: true,
        gridcolor: "rgba(23, 33, 33, 0.12)",
        zeroline: false,
        showspikes: false,
        spikecolor: "rgba(23, 33, 33, 0.42)",
        spikemode: "across",
        spikesnap: "cursor",
        spikethickness: 1,
        showticklabels: false
      }},
      yaxis: {{
        domain: usesTradeAmounts ? [0.48, 1] : [0.38, 1],
        title: "Price",
        showgrid: true,
        gridcolor: "rgba(23, 33, 33, 0.12)",
        zeroline: false,
        showspikes: false,
        spikecolor: "rgba(23, 33, 33, 0.32)",
        spikemode: "across",
        spikesnap: "cursor",
        spikethickness: 1
      }},
      xaxis2: {{
        domain: [0, 1],
        anchor: "y2",
        matches: "x",
        showgrid: true,
        gridcolor: "rgba(23, 33, 33, 0.12)",
        zeroline: false,
        showspikes: false,
        spikecolor: "rgba(23, 33, 33, 0.42)",
        spikemode: "across",
        spikesnap: "cursor",
        spikethickness: 1,
        showticklabels: !usesTradeAmounts
      }},
      yaxis2: {{
        domain: usesTradeAmounts ? [0.22, 0.42] : [0.0, 0.30],
        title: "Drawdown %",
        showgrid: true,
        gridcolor: "rgba(23, 33, 33, 0.12)",
        zeroline: false,
        showspikes: false,
        spikecolor: "rgba(23, 33, 33, 0.32)",
        spikemode: "across",
        spikesnap: "cursor",
        spikethickness: 1
      }}
    }};

    if (usesTradeAmounts) {{
      layout.xaxis3 = {{
        domain: [0, 1],
        anchor: "y3",
        matches: "x",
        showgrid: true,
        gridcolor: "rgba(23, 33, 33, 0.12)",
        zeroline: false,
        showspikes: false,
        spikecolor: "rgba(23, 33, 33, 0.42)",
        spikemode: "across",
        spikesnap: "cursor",
        spikethickness: 1
      }};
      layout.yaxis3 = {{
        domain: [0.0, 0.16],
        title: payload.bar_unit_label,
        showgrid: true,
        gridcolor: "rgba(23, 33, 33, 0.12)",
        zeroline: false,
        showspikes: false,
        spikecolor: "rgba(23, 33, 33, 0.32)",
        spikemode: "across",
        spikesnap: "cursor",
        spikethickness: 1
      }};
    }}

    function crosshairRelayout(enabled) {{
      const relayout = {{
        hovermode: enabled ? false : "closest",
        "xaxis.showspikes": false,
        "yaxis.showspikes": false,
        "xaxis2.showspikes": false,
        "yaxis2.showspikes": false
      }};
      if (usesTradeAmounts) {{
        relayout["xaxis3.showspikes"] = false;
        relayout["yaxis3.showspikes"] = false;
      }}
      return relayout;
    }}

    function resetHoverCard() {{
      setHoverCardVisible(false);
    }}

    function hideCrosshair() {{
      document.getElementById("crosshair-overlay").classList.remove("is-active");
      document.getElementById("crosshair-v").classList.remove("is-active");
      document.getElementById("crosshair-h").classList.remove("is-active");
    }}

    function getPanels(plot) {{
      const full = plot._fullLayout;
      const panels = [
        {{ key: "price", axis: full.yaxis }},
        {{ key: "drawdown", axis: full.yaxis2 }}
      ];
      if (usesTradeAmounts && full.yaxis3) {{
        panels.push({{ key: "amount", axis: full.yaxis3 }});
      }}
      return panels.map((panel) => ({{
        key: panel.key,
        top: panel.axis._offset,
        bottom: panel.axis._offset + panel.axis._length
      }}));
    }}

    function getPlotArea(plot) {{
      const full = plot._fullLayout;
      const panels = getPanels(plot);
      return {{
        left: full.xaxis._offset,
        right: full.xaxis._offset + full.xaxis._length,
        top: Math.min(...panels.map((panel) => panel.top)),
        bottom: Math.max(...panels.map((panel) => panel.bottom)),
        panels
      }};
    }}

    function nearestIndexByTimestamp(timestamp) {{
      let low = 0;
      let high = dateTimestamps.length - 1;
      while (low < high) {{
        const mid = Math.floor((low + high) / 2);
        if (dateTimestamps[mid] < timestamp) {{
          low = mid + 1;
        }} else {{
          high = mid;
        }}
      }}
      if (low === 0) {{
        return 0;
      }}
      const prev = low - 1;
      return Math.abs(dateTimestamps[low] - timestamp) < Math.abs(dateTimestamps[prev] - timestamp)
        ? low
        : prev;
    }}

    function xPixelForIndex(index, plotArea, plot) {{
      const range = plot._fullLayout.xaxis.range || [payload.dates[0], payload.dates[payload.dates.length - 1]];
      const start = Date.parse(range[0]);
      const end = Date.parse(range[1]);
      const clampedSpan = Math.max(end - start, 1);
      return plotArea.left + ((dateTimestamps[index] - start) / clampedSpan) * (plotArea.right - plotArea.left);
    }}

    function isMobileViewport() {{
      return window.matchMedia("(max-width: 820px)").matches;
    }}

    function responsiveRelayout() {{
      const mobile = isMobileViewport();
      const relayout = {{
        height: mobile ? 760 : 860,
        "margin.l": mobile ? 52 : 70,
        "margin.r": mobile ? 16 : 26,
        "margin.t": mobile ? 146 : 128,
        "margin.b": mobile ? 40 : 56,
        "legend.orientation": "h",
        "legend.x": 0,
        "legend.y": mobile ? 1.1 : 1.08,
        "legend.xanchor": "left",
        "legend.yanchor": "bottom",
        "legend.font.size": mobile ? 10 : 11,
        "legend.tracegroupgap": 8,
        "showlegend": true
      }};
      return relayout;
    }}

    function applyMode(plot, mode) {{
      currentMode = mode;
      Plotly.restyle(plot, {{ visible: visibilityFor(mode) }});
      document.querySelectorAll(".mode-button").forEach((button) => {{
        button.classList.toggle("is-active", button.dataset.mode === mode);
      }});
    }}

    function setHoverCardVisible(visible) {{
      document.getElementById("hover-card").classList.toggle("is-visible", visible);
      if (!visible) {{
        document.getElementById("hover-card-empty").hidden = false;
        document.getElementById("hover-card-body").hidden = true;
      }}
    }}

    function placeHoverCard(pointerX, pointerY, plotArea) {{
      const hoverCard = document.getElementById("hover-card");
      const padding = 14;
      const cardWidth = hoverCard.offsetWidth || 240;
      const cardHeight = hoverCard.offsetHeight || 164;
      const maxLeft = Math.max(plotArea.left + 8, plotArea.right - cardWidth - 8);
      const maxTop = Math.max(plotArea.top + 8, plotArea.bottom - cardHeight - 8);

      let left = pointerX + 16;
      if (left > maxLeft) {{
        left = Math.max(plotArea.left + 8, pointerX - cardWidth - 16);
      }}

      let top = pointerY + 16;
      if (top > maxTop) {{
        top = Math.max(plotArea.top + 8, pointerY - cardHeight - 16);
      }}

      hoverCard.style.left = `${{Math.max(plotArea.left + 8, Math.min(left, maxLeft))}}px`;
      hoverCard.style.top = `${{Math.max(plotArea.top + 8, Math.min(top, maxTop))}}px`;
    }}

    function panelForY(plotY, plotArea) {{
      return plotArea.panels.find((panel) => plotY >= panel.top && plotY <= panel.bottom) || null;
    }}

    function updateCrosshairDisplay(plot, mouseX, mouseY) {{
      const overlay = document.getElementById("crosshair-overlay");
      const vertical = document.getElementById("crosshair-v");
      const horizontal = document.getElementById("crosshair-h");
      const plotArea = getPlotArea(plot);
      const snapshotToggle = document.getElementById("snapshot-toggle");
      const crosshairToggle = document.getElementById("crosshair-toggle");
      const activePanel = panelForY(mouseY, plotArea);
      if (mouseX < plotArea.left || mouseX > plotArea.right || !activePanel) {{
        if (crosshairToggle.checked) {{
          hideCrosshair();
        }}
        resetHoverCard();
        return;
      }}

      const range = plot._fullLayout.xaxis.range || [payload.dates[0], payload.dates[payload.dates.length - 1]];
      const start = Date.parse(range[0]);
      const end = Date.parse(range[1]);
      const clampedSpan = Math.max(end - start, 1);
      const ratio = (mouseX - plotArea.left) / (plotArea.right - plotArea.left);
      const targetTimestamp = start + ratio * clampedSpan;
      const index = nearestIndexByTimestamp(targetTimestamp);
      const snappedX = xPixelForIndex(index, plotArea, plot);

      if (crosshairToggle.checked) {{
        overlay.classList.add("is-active");
        vertical.classList.add("is-active");
        horizontal.classList.add("is-active");
        vertical.style.left = `${{snappedX}}px`;
        vertical.style.top = `${{plotArea.top}}px`;
        vertical.style.height = `${{plotArea.bottom - plotArea.top}}px`;
        horizontal.style.left = `${{plotArea.left}}px`;
        horizontal.style.top = `${{mouseY}}px`;
        horizontal.style.width = `${{plotArea.right - plotArea.left}}px`;
      }}

      if (snapshotToggle.checked) {{
        renderHoverCard(index);
        setHoverCardVisible(true);
        placeHoverCard(mouseX, mouseY, plotArea);
      }} else {{
        resetHoverCard();
      }}
    }}

    function pointerPositionFromTouch(event, plot) {{
      const touch = event.touches[0] || event.changedTouches[0];
      if (!touch) {{
        return null;
      }}
      const rect = plot.getBoundingClientRect();
      return {{
        x: touch.clientX - rect.left,
        y: touch.clientY - rect.top
      }};
    }}

    Plotly.newPlot("chart", traces, layout, {{
      responsive: true,
      displaylogo: false,
      displayModeBar: false,
      scrollZoom: false,
      doubleClick: false
    }}).then((plot) => {{
      applyMode(plot, currentMode);
      const crosshairToggle = document.getElementById("crosshair-toggle");
      const snapshotToggle = document.getElementById("snapshot-toggle");
      const LONG_PRESS_MS = 320;
      const TOUCH_MOVE_TOLERANCE = 10;
      const TOUCH_EXIT_VERTICAL_PX = 28;
      const TOUCH_EXIT_BIAS_PX = 8;
      let longPressTimer = null;
      let inspectMode = false;
      let touchStartPoint = null;
      let inspectStartPoint = null;
      let pendingTouchPoint = null;

      function clearLongPressTimer() {{
        if (longPressTimer != null) {{
          window.clearTimeout(longPressTimer);
          longPressTimer = null;
        }}
      }}

      function exitInspectMode() {{
        inspectMode = false;
        inspectStartPoint = null;
        if (crosshairToggle.checked) {{
          hideCrosshair();
        }}
        resetHoverCard();
      }}

      function activateInspectMode(point) {{
        if (!point || (!crosshairToggle.checked && !snapshotToggle.checked)) {{
          return;
        }}
        inspectMode = true;
        inspectStartPoint = point;
        updateCrosshairDisplay(plot, point.x, point.y);
      }}

      document.querySelectorAll(".mode-button").forEach((button) => {{
        button.addEventListener("click", () => {{
          applyMode(plot, button.dataset.mode || "both");
        }});
      }});
      const applyCrosshair = () => {{
        Plotly.relayout(plot, {{
          ...responsiveRelayout(),
          ...crosshairRelayout(crosshairToggle.checked)
        }});
        if (!crosshairToggle.checked) {{
          hideCrosshair();
          resetHoverCard();
        }}
      }};
      crosshairToggle.addEventListener("change", applyCrosshair);
      snapshotToggle.addEventListener("change", () => {{
        if (!snapshotToggle.checked) {{
          resetHoverCard();
        }}
      }});
      plot.addEventListener("mousemove", (event) => {{
        if (!crosshairToggle.checked && !snapshotToggle.checked) {{
          return;
        }}
        const rect = plot.getBoundingClientRect();
        updateCrosshairDisplay(plot, event.clientX - rect.left, event.clientY - rect.top);
      }});
      plot.addEventListener("click", (event) => {{
        if (isMobileViewport()) {{
          return;
        }}
        if (!crosshairToggle.checked && !snapshotToggle.checked) {{
          return;
        }}
        const rect = plot.getBoundingClientRect();
        updateCrosshairDisplay(plot, event.clientX - rect.left, event.clientY - rect.top);
      }});
      plot.addEventListener("touchstart", (event) => {{
        if (!crosshairToggle.checked && !snapshotToggle.checked) {{
          return;
        }}
        const point = pointerPositionFromTouch(event, plot);
        if (!point) {{
          return;
        }}
        pendingTouchPoint = point;
        touchStartPoint = point;
        inspectStartPoint = null;
        clearLongPressTimer();
        if (!isMobileViewport()) {{
          updateCrosshairDisplay(plot, point.x, point.y);
          return;
        }}
        longPressTimer = window.setTimeout(() => {{
          longPressTimer = null;
          activateInspectMode(pendingTouchPoint || touchStartPoint);
        }}, LONG_PRESS_MS);
      }}, {{ passive: true }});
      plot.addEventListener("touchmove", (event) => {{
        if (!crosshairToggle.checked && !snapshotToggle.checked) {{
          return;
        }}
        const point = pointerPositionFromTouch(event, plot);
        if (!point) {{
          return;
        }}
        pendingTouchPoint = point;
        if (!isMobileViewport()) {{
          if (event.cancelable) {{
            event.preventDefault();
          }}
          updateCrosshairDisplay(plot, point.x, point.y);
          return;
        }}
        if (!inspectMode) {{
          if (touchStartPoint) {{
            const deltaX = point.x - touchStartPoint.x;
            const deltaY = point.y - touchStartPoint.y;
            if (Math.hypot(deltaX, deltaY) > TOUCH_MOVE_TOLERANCE) {{
              clearLongPressTimer();
            }}
          }}
          return;
        }}
        if (inspectStartPoint) {{
          const verticalDrift = Math.abs(point.y - inspectStartPoint.y);
          const horizontalDrift = Math.abs(point.x - inspectStartPoint.x);
          if (verticalDrift > TOUCH_EXIT_VERTICAL_PX && verticalDrift > horizontalDrift + TOUCH_EXIT_BIAS_PX) {{
            exitInspectMode();
            return;
          }}
        }}
        if (event.cancelable) {{
          event.preventDefault();
        }}
        updateCrosshairDisplay(plot, point.x, point.y);
      }}, {{ passive: false }});
      plot.addEventListener("touchend", () => {{
        clearLongPressTimer();
        pendingTouchPoint = null;
        touchStartPoint = null;
        if (inspectMode) {{
          exitInspectMode();
        }}
      }}, {{ passive: true }});
      plot.addEventListener("touchcancel", () => {{
        clearLongPressTimer();
        pendingTouchPoint = null;
        touchStartPoint = null;
        if (inspectMode) {{
          exitInspectMode();
        }}
      }}, {{ passive: true }});
      plot.addEventListener("mouseleave", () => {{
        if (crosshairToggle.checked) {{
          hideCrosshair();
        }}
        resetHoverCard();
      }});
      window.addEventListener("resize", () => {{
        Plotly.relayout(plot, responsiveRelayout());
      }});
      applyCrosshair();
    }});
  </script>
</body>
</html>
"""


def normalize_candle_datetime(value: object) -> datetime:
    if isinstance(value, datetime):
        return value
    if isinstance(value, date):
        return datetime.combine(value, time.min)
    if isinstance(value, str):
        try:
            return parse_date(value)
        except ValueError:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    raise TypeError(f"Unsupported candle timestamp type: {type(value)}")


def candle_datetime(candle: object) -> datetime:
    for attribute in ("timestamp", "time", "datetime", "date"):
        value = getattr(candle, attribute, None)
        if value is not None:
            return normalize_candle_datetime(value)
    raise AttributeError("Candlestick is missing a datetime-like field.")


def build_longbridge_quote_context():
    try:
        from longbridge.openapi import Config, OAuthBuilder, QuoteContext  # type: ignore
    except ImportError as exc:
        raise RuntimeError("longbridge SDK 未安装，请先执行 pip install longbridge。") from exc

    from config.config_manager import ConfigManager

    config_manager = ConfigManager()
    lb_config = config_manager.get_longbridge_config()
    oauth_client_id = (lb_config.get("oauth_client_id") or "").strip()

    if oauth_client_id:
        oauth = OAuthBuilder(oauth_client_id).build(
            lambda url: (_ for _ in ()).throw(
                RuntimeError(f"Longbridge token 已过期，需要重新授权: {url}")
            )
        )
        config = Config.from_oauth(oauth)
    elif (
        lb_config.get("app_key")
        and lb_config.get("app_secret")
        and lb_config.get("access_token")
    ):
        config = Config(
            app_key=lb_config["app_key"],
            app_secret=lb_config["app_secret"],
            access_token=lb_config["access_token"],
        )
    else:
        raise RuntimeError("未找到可用的 Longbridge 配置。")

    return QuoteContext(config)


def fetch_longbridge_daily_candles(
    quote_ctx: object, symbol: str, start_date: date, end_date: date
) -> list[object]:
    from longbridge.openapi import AdjustType, Period  # type: ignore

    if (end_date - start_date).days <= LONGBRIDGE_SHORT_RANGE_DAYS:
        candles = quote_ctx.history_candlesticks_by_date(
            symbol,
            Period.Day,
            AdjustType.ForwardAdjust,
            start_date,
            end_date,
        )
        return list(candles)

    candles_by_day: dict[date, object] = {}
    cursor = datetime.combine(end_date, time.min)

    while True:
        chunk = list(
            quote_ctx.history_candlesticks_by_offset(
                symbol, Period.Day, AdjustType.ForwardAdjust, False, LONGBRIDGE_MAX_BARS, cursor
            )
        )
        if not chunk:
            break

        ordered_chunk = sorted(chunk, key=candle_datetime)
        earliest_in_chunk = candle_datetime(ordered_chunk[0]).date()

        for candle in ordered_chunk:
            candle_day = candle_datetime(candle).date()
            if start_date <= candle_day <= end_date:
                candles_by_day[candle_day] = candle

        if earliest_in_chunk <= start_date:
            break

        cursor = datetime.combine(earliest_in_chunk - timedelta(days=1), time.min)

    return [candles_by_day[day] for day in sorted(candles_by_day)]


def load_longbridge_price_points(
    ticker: str, overlays: list[TradeOverlay], symbol_override: str | None = None
) -> tuple[list[PricePoint], str]:
    quote_ctx = build_longbridge_quote_context()
    symbol = normalize_longbridge_symbol(symbol_override or ticker)

    if overlays:
        earliest_trade = min(overlay.date.date() for overlay in overlays)
        start_date = earliest_trade - timedelta(days=370)
    else:
        start_date = datetime.now().date() - timedelta(days=365 * 5)
    end_date = datetime.now().date()

    candles = fetch_longbridge_daily_candles(quote_ctx, symbol, start_date, end_date)
    if not candles:
        raise RuntimeError(f"Longbridge 没有返回 {symbol} 的历史日线。")

    series = [
        (candle_datetime(candle).replace(tzinfo=None), float(candle.close))
        for candle in candles
    ]
    points = build_price_points_from_series(series)
    if not points:
        raise RuntimeError(f"无法从 Longbridge 构建 {symbol} 的价格序列。")

    return points, symbol


def default_output_path(ticker: str, price_source: str) -> Path:
    suffix = "_drawdown_longbridge.html" if price_source == "longbridge" else "_drawdown_draft.html"
    return SCRIPT_DIR / "output" / f"{ticker.lower()}{suffix}"


def render_longbridge_drawdown_from_overlays(
    ticker: str, overlays: list[TradeOverlay], symbol_override: str | None = None
) -> tuple[str, list[str], str]:
    points, resolved_symbol = load_longbridge_price_points(ticker, overlays, symbol_override)
    trade_summary, warnings = build_trade_summary(points, overlays)
    payload = build_chart_payload(points, trade_summary)
    html = render_html(payload, warnings, ticker, "Longbridge Daily (Forward Adjusted)")
    return html, warnings, resolved_symbol


def main() -> None:
    args = parse_args()
    input_path = Path(args.input).expanduser()
    if not input_path.exists():
        raise FileNotFoundError(f"Input file was not found: {input_path}")

    overlays: list[TradeOverlay] = []
    warnings: list[str] = []
    ticker = symbol_base(args.symbol or input_path.stem.replace("TradingLogs", "") or input_path.stem)
    price_source_used = "embedded"

    if input_path.suffix.lower() == ".xlsx":
        if args.price_source == "embedded":
            points, overlays, ticker = load_embedded_xlsx_dataset(input_path, args.sheet)
            price_source_used = "embedded"
        elif args.price_source == "longbridge":
            overlays, inferred_ticker = load_trade_overlays_from_xlsx(
                input_path, args.sheet, args.symbol
            )
            ticker = symbol_base(args.symbol or inferred_ticker or ticker)
            points, resolved_symbol = load_longbridge_price_points(ticker, overlays, args.symbol)
            price_source_used = "longbridge"
        else:
            try:
                points, overlays, ticker = load_embedded_xlsx_dataset(input_path, args.sheet)
                price_source_used = "embedded"
            except ValueError:
                overlays, inferred_ticker = load_trade_overlays_from_xlsx(
                    input_path, args.sheet, args.symbol
                )
                ticker = symbol_base(args.symbol or inferred_ticker or ticker)
                points, resolved_symbol = load_longbridge_price_points(ticker, overlays, args.symbol)
                price_source_used = "longbridge"
    else:
        if args.price_source == "longbridge":
            raise ValueError("--price-source longbridge 目前只支持 xlsx 输入。")
        points = load_price_points(input_path)
        price_source_used = "tsv"

    if args.trades:
        overlays.extend(load_trade_overlays(Path(args.trades).expanduser()))

    trade_summary, trade_warnings = build_trade_summary(points, overlays)
    warnings.extend(trade_warnings)

    payload = build_chart_payload(points, trade_summary)
    output_path = Path(args.output).expanduser() if args.output else default_output_path(ticker, price_source_used)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    price_source_label = {
        "embedded": "Embedded xlsx",
        "longbridge": "Longbridge Daily (Forward Adjusted)",
        "tsv": "Legacy TSV",
    }[price_source_used]
    html = render_html(payload, sorted(set(warnings)), ticker, price_source_label)
    output_path.write_text(html, encoding="utf-8")

    print(f"Wrote draft chart to {output_path}")
    print(f"Price source: {price_source_label}")
    if args.trades:
        print(f"Loaded {len(overlays)} trade rows including {args.trades}")
    if warnings:
        print("Warnings:")
        for warning in sorted(set(warnings)):
            print(f" - {warning}")


if __name__ == "__main__":
    main()
