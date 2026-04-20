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
    ticker = rows.get(2, {}).get("I", "").strip()
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

    sell_dates: list[str] = []
    sell_prices: list[float] = []
    sell_labels: list[str] = []

    buy_bar_dates: list[str] = []
    buy_bar_values: list[float] = []
    buy_bar_labels: list[str] = []
    sell_bar_dates: list[str] = []
    sell_bar_values: list[float] = []
    sell_bar_labels: list[str] = []
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

        if point.is_sell:
            sell_dates.append(date_key)
            sell_prices.append(point.close)
            sell_labels.append(
                format_sell_label(
                    point.close,
                    point.drawdown_ath,
                    drawdown_120_raw,
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
        "sell_dates": sell_dates,
        "sell_prices": sell_prices,
        "sell_labels": sell_labels,
        "buy_bar_dates": buy_bar_dates,
        "buy_bar_values": buy_bar_values,
        "buy_bar_labels": buy_bar_labels,
        "sell_bar_dates": sell_bar_dates,
        "sell_bar_values": sell_bar_values,
        "sell_bar_labels": sell_bar_labels,
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
  <title>{ticker} 回撤与交易初稿</title>
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
      background: var(--card);
      border: 1px solid rgba(23, 33, 33, 0.08);
      box-shadow: 0 24px 60px rgba(23, 33, 33, 0.12);
    }}

    .footnote {{
      margin-top: 14px;
      color: var(--muted);
      font-size: 13px;
      line-height: 1.5;
    }}
  </style>
</head>
<body>
  <div class="shell">
    <div class="header">
      <div class="eyebrow">{ticker} / Drawdown Draft</div>
      <h1>回撤水位和加仓动作放在同一条时间轴里看</h1>
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
    <div id="chart"></div>
    <div class="footnote">
      价格源支持内嵌 xlsx 时序和 Longbridge 日线两种模式。当前默认模式为 <code>Both</code>。
      Longbridge 模式下当前会从首笔交易日前大约 370 天开始拉取到今天，所以这里的 <code>All-time</code> 是当前加载窗口内的历史高点，不是上市以来全历史高点。
      如果你再补一份 CSV，例如字段为 <code>date,amount,shares,type</code>，脚本会按日期合并；
      买点圆点会按金额或股数缩放，底部会增加对应柱状层。
    </div>
  </div>
  <script>
    const payload = {json.dumps(payload, ensure_ascii=False)};
    const usesTradeAmounts = payload.uses_trade_amounts;

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
      text: payload.buy_labels,
      hovertemplate: "日期: %{{x}}<br>%{{text}}<extra></extra>",
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
      text: payload.buy_labels,
      hovertemplate: "日期: %{{x}}<br>%{{text}}<extra></extra>",
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
      buyDrawdown120Trace
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
        text: payload.sell_bar_labels,
        hovertemplate: "日期: %{{x}}<br>%{{text}}<extra></extra>",
        xaxis: "x3",
        yaxis: "y3"
      }});
    }}

    const hasBuyBarTrace = usesTradeAmounts && payload.buy_bar_dates.length > 0;
    const hasSellBarTrace = usesTradeAmounts && payload.sell_bar_dates.length > 0;

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
      margin: {{ l: 70, r: 30, t: 126, b: 56 }},
      legend: {{
        orientation: "h",
        yanchor: "bottom",
        y: 1.03,
        xanchor: "left",
        x: 0,
        entrywidthmode: "pixels",
        entrywidth: 86,
        bgcolor: "rgba(255, 250, 243, 0.88)",
        bordercolor: "rgba(23, 33, 33, 0.08)",
        borderwidth: 1
      }},
      hovermode: "closest",
      updatemenus: [
        {{
          type: "buttons",
          direction: "right",
          x: 0,
          y: 1.15,
          xanchor: "left",
          yanchor: "top",
          showactive: true,
          buttons: [
            {{
              label: "All-time",
              method: "update",
              args: [{{ visible: visibilityFor("alltime") }}]
            }},
            {{
              label: "Rolling 120d",
              method: "update",
              args: [{{ visible: visibilityFor("rolling") }}]
            }},
            {{
              label: "Both",
              method: "update",
              args: [{{ visible: visibilityFor("both") }}]
            }}
          ]
        }}
      ],
      xaxis: {{
        domain: [0, 1],
        anchor: "y",
        showgrid: true,
        gridcolor: "rgba(23, 33, 33, 0.12)",
        zeroline: false,
        showticklabels: false
      }},
      yaxis: {{
        domain: usesTradeAmounts ? [0.48, 1] : [0.38, 1],
        title: "Price",
        showgrid: true,
        gridcolor: "rgba(23, 33, 33, 0.12)",
        zeroline: false
      }},
      xaxis2: {{
        domain: [0, 1],
        anchor: "y2",
        matches: "x",
        showgrid: true,
        gridcolor: "rgba(23, 33, 33, 0.12)",
        zeroline: false,
        showticklabels: !usesTradeAmounts
      }},
      yaxis2: {{
        domain: usesTradeAmounts ? [0.22, 0.42] : [0.0, 0.30],
        title: "Drawdown %",
        showgrid: true,
        gridcolor: "rgba(23, 33, 33, 0.12)",
        zeroline: false
      }},
      annotations: [
        {{
          xref: "paper",
          yref: "paper",
          x: 0.01,
          y: 1.17,
          text: "Price + Buy/Sell Markers",
          showarrow: false,
          font: {{ size: 12, color: "#5c605f" }}
        }},
        {{
          xref: "paper",
          yref: "paper",
          x: 0.01,
          y: usesTradeAmounts ? 0.45 : 0.35,
          text: "Drawdown Modes",
          showarrow: false,
          font: {{ size: 12, color: "#5c605f" }}
        }}
      ]
    }};

    if (usesTradeAmounts) {{
      layout.xaxis3 = {{
        domain: [0, 1],
        anchor: "y3",
        matches: "x",
        showgrid: true,
        gridcolor: "rgba(23, 33, 33, 0.12)",
        zeroline: false
      }};
      layout.yaxis3 = {{
        domain: [0.0, 0.16],
        title: payload.bar_unit_label,
        showgrid: true,
        gridcolor: "rgba(23, 33, 33, 0.12)",
        zeroline: false
      }};
      layout.annotations.push({{
        xref: "paper",
        yref: "paper",
        x: 0.01,
        y: 0.19,
        text: payload.bar_unit_label,
        showarrow: false,
        font: {{ size: 12, color: "#5c605f" }}
      }});
    }}

    Plotly.newPlot("chart", traces, layout, {{
      responsive: true,
      displaylogo: false
    }}).then((plot) => {{
      Plotly.restyle(plot, {{ visible: visibilityFor("both") }});
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
            AdjustType.NoAdjust,
            start_date,
            end_date,
        )
        return list(candles)

    candles_by_day: dict[date, object] = {}
    cursor = datetime.combine(end_date, time.min)

    while True:
        chunk = list(
            quote_ctx.history_candlesticks_by_offset(
                symbol, Period.Day, AdjustType.NoAdjust, False, LONGBRIDGE_MAX_BARS, cursor
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
    html = render_html(payload, warnings, ticker, "Longbridge Daily")
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
        "longbridge": "Longbridge Daily",
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
