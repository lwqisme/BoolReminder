#!/usr/bin/env python3
"""Generate a draft drawdown visualization from a TSV or xlsx export.

This script reads either:
1. A legacy TSV export with price and buy/sell markers, or
2. An xlsx export that contains both the price series and the buy/sell logs.

It then exports an interactive HTML chart. It also supports an optional CSV file
with extra trade amounts or share counts so later overlays can be added without
changing the code.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import re
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable
from xml.etree import ElementTree as ET
from zipfile import ZipFile


SCRIPT_DIR = Path(__file__).resolve().parent
DATE_KEYS = ("date", "trade_date", "buy_date")
AMOUNT_KEYS = ("amount", "add_amount", "trade_amount", "cash")
SHARE_KEYS = ("shares", "qty", "quantity")
TYPE_KEYS = ("type", "side", "event_type")
NS_MAIN = {"a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main"}
NS_BOOK = {
    "a": "http://schemas.openxmlformats.org/spreadsheetml/2006/main",
    "r": "http://schemas.openxmlformats.org/officeDocument/2006/relationships",
}


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
        default=str(Path.home() / "Documents" / "TSLATradingLogs.xlsx"),
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
        help="Output HTML path. Default: drawdown/output/<ticker>_drawdown_draft.html",
    )
    return parser.parse_args()


def parse_date(value: str) -> datetime:
    value = value.strip()
    for fmt in ("%Y/%m/%d", "%Y-%m-%d"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    raise ValueError(f"Unsupported date format: {value}")


def excel_serial_to_datetime(value: str) -> datetime:
    base = datetime(1899, 12, 30)
    return base + timedelta(days=float(value))


def find_key(fieldnames: Iterable[str], candidates: Iterable[str]) -> str | None:
    lowered = {name.strip().lower(): name for name in fieldnames if name}
    for candidate in candidates:
        if candidate in lowered:
            return lowered[candidate]
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


def first_sheet_xml_path(book: ZipFile) -> str:
    workbook = ET.fromstring(book.read("xl/workbook.xml"))
    rels = ET.fromstring(book.read("xl/_rels/workbook.xml.rels"))
    rel_map = {rel.attrib["Id"]: rel.attrib["Target"] for rel in rels}
    first_sheet = workbook.find("a:sheets", NS_BOOK)[0]
    rel_id = first_sheet.attrib[
        "{http://schemas.openxmlformats.org/officeDocument/2006/relationships}id"
    ]
    return "xl/" + rel_map[rel_id]


def read_xlsx_rows(path: Path) -> dict[int, dict[str, str]]:
    with ZipFile(path) as book:
        shared_strings = load_shared_strings(book)
        sheet_path = first_sheet_xml_path(book)
        root = ET.fromstring(book.read(sheet_path))

    rows: dict[int, dict[str, str]] = {}
    for row in root.findall(".//a:sheetData/a:row", NS_MAIN):
        row_idx = int(row.attrib["r"])
        cells: dict[str, str] = {}
        for cell in row.findall("a:c", NS_MAIN):
            ref = cell.attrib["r"]
            col = col_letters(ref)
            value_node = cell.find("a:v", NS_MAIN)
            value = value_node.text if value_node is not None else ""
            if cell.attrib.get("t") == "s" and value:
                value = shared_strings[int(value)]
            cells[col] = value
        rows[row_idx] = cells
    return rows


def load_xlsx_dataset(path: Path) -> tuple[list[PricePoint], list[TradeOverlay], str]:
    rows = read_xlsx_rows(path)
    ticker = rows.get(2, {}).get("I", "") or path.stem
    buy_dates = {
        excel_serial_to_datetime(row["F"]).date()
        for row_idx, row in rows.items()
        if row_idx >= 3 and row.get("F") and row.get("G") and row.get("H")
    }
    sell_dates = {
        excel_serial_to_datetime(row["K"]).date()
        for row_idx, row in rows.items()
        if row_idx >= 3 and row.get("K") and row.get("L") and row.get("M")
    }

    points: list[PricePoint] = []
    rolling_peak = -math.inf
    overlays: list[TradeOverlay] = []

    for row_idx in sorted(rows):
        row = rows[row_idx]
        if row_idx < 3:
            continue

        if row.get("A") and row.get("B"):
            date = excel_serial_to_datetime(row["A"])
            close = float(row["B"])
            rolling_peak = max(rolling_peak, close)
            drawdown_ath = close / rolling_peak - 1.0
            points.append(
                PricePoint(
                    date=date,
                    close=close,
                    is_buy=date.date() in buy_dates,
                    is_sell=date.date() in sell_dates,
                    rolling_peak=rolling_peak,
                    drawdown_ath=drawdown_ath,
                )
            )

        if row.get("F") and row.get("G") and row.get("H"):
            shares = float(row["G"])
            price = float(row["H"])
            overlays.append(
                TradeOverlay(
                    date=excel_serial_to_datetime(row["F"]),
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
                    date=excel_serial_to_datetime(row["K"]),
                    amount=shares * price,
                    shares=shares,
                    event_type="sell",
                )
            )

    if not points:
        raise ValueError(f"No price rows were parsed from {path}.")

    return points, overlays, ticker


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

        date = parse_date(row[0])
        close = float(row[1])
        is_buy = bool(row[2].strip())
        is_sell = bool(row[3].strip())
        rolling_peak = max(rolling_peak, close)
        drawdown_ath = close / rolling_peak - 1.0

        points.append(
            PricePoint(
                date=date,
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

            event_type = (row.get(type_key) or "buy").strip().lower()
            amount = parse_optional_float(row.get(amount_key)) if amount_key else None
            shares = parse_optional_float(row.get(share_key)) if share_key else None

            overlays.append(
                TradeOverlay(
                    date=parse_date(raw_date),
                    amount=amount,
                    shares=shares,
                    event_type=event_type or "buy",
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


def build_trade_summary(
    points: list[PricePoint], overlays: list[TradeOverlay]
) -> tuple[dict[str, dict[str, float]], list[str]]:
    point_dates = {point.date.strftime("%Y-%m-%d") for point in points}
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
    unmatched_dates: list[str] = []

    for overlay in overlays:
        date_key = overlay.date.strftime("%Y-%m-%d")
        if date_key not in point_dates:
            unmatched_dates.append(date_key)
            continue
        summary = by_date[date_key]
        if overlay.event_type in {"buy", "add", "buy_more"}:
            summary["buy_count"] += 1.0
            if overlay.amount is not None:
                summary["buy_amount"] += overlay.amount
            if overlay.shares is not None:
                summary["buy_shares"] += overlay.shares
        elif overlay.event_type in {"sell", "trim"}:
            summary["sell_count"] += 1.0
            if overlay.amount is not None:
                summary["sell_amount"] += overlay.amount
            if overlay.shares is not None:
                summary["sell_shares"] += overlay.shares

    return dict(by_date), sorted(set(unmatched_dates))


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


def render_html(payload: dict[str, object], warnings: list[str], ticker: str) -> str:
    title_suffix = "已接入交易金额/股数" if payload["uses_trade_amounts"] else "金额待补充"
    warning_html = ""
    if warnings:
        warning_html = (
            "<div class='warning'>以下交易日期未匹配到价格序列，已跳过: "
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
        当前版本基于 {ticker} 的收盘价序列和主表里的买卖标记生成。状态: {title_suffix}。
        上图看价格与峰值，下图提供 All-time High 与 Rolling 120d High 两套回撤口径，可在按钮里切换或共同显示。
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
      如果你补一份 CSV，例如字段为 <code>date,amount,shares,type</code>，脚本会按日期合并；
      买点圆点会按金额或股数缩放，底部会增加对应柱状层。当前默认模式为 <code>Both</code>。
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
      name: usesTradeAmounts ? "Buy" : "Buy",
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


def main() -> None:
    args = parse_args()
    input_path = Path(args.input).expanduser()

    overlays: list[TradeOverlay] = []
    warnings: list[str] = []
    ticker = input_path.stem.replace("TradingLogs", "") or input_path.stem

    if input_path.suffix.lower() == ".xlsx":
        points, overlays, ticker = load_xlsx_dataset(input_path)
    else:
        points = load_price_points(input_path)

    if args.trades:
        overlays.extend(load_trade_overlays(Path(args.trades).expanduser()))

    trade_summary, unmatched_dates = build_trade_summary(points, overlays)
    if unmatched_dates:
        warnings.extend(unmatched_dates)

    payload = build_chart_payload(points, trade_summary)
    output_path = (
        Path(args.output).expanduser()
        if args.output
        else SCRIPT_DIR / "output" / f"{ticker.lower()}_drawdown_draft.html"
    )
    output_path.parent.mkdir(parents=True, exist_ok=True)
    html = render_html(payload, warnings, ticker)
    output_path.write_text(html, encoding="utf-8")

    print(f"Wrote draft chart to {output_path}")
    if args.trades:
        print(f"Loaded {len(overlays)} trade rows from {args.trades}")
    if unmatched_dates:
        print("Skipped unmatched trade dates:", ", ".join(unmatched_dates))


if __name__ == "__main__":
    main()
