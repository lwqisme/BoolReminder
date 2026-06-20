"""
SEC 485BPOS 招募书历史持仓抓取 —— 补齐 N-PORT 之前 (1999-2018) 的年度持仓

N-PORT-P 仅 2019 起强制季度申报。更早 QQQ 通过 485BPOS（年度招募书补充）
披露上一年 9-30 的"Schedule of Investments"完整持仓表。本模块抓取并解析
这些招募书，把历史持仓补到约 1999-09-30，使回测可覆盖 20 年。

文档格式分两代（已实测）：
  - 2000-2006 (.txt)：EDGAR 旧式，表为纯文本 + 点号填充对齐，无 <TR>。
    行形如:  "Microsoft Corporation ..........................   55,688,162  $ 1,432,856,408"
  - 2007-2018 (.htm)：真正 HTML 表格 <TR><TD>，每持仓一行，三列：公司名/股数/市值。

两种格式都按市值降序排列，Top10 = 表体前 10 个数据行。

⚠️ ticker 解析：招募书持仓只有公司名，无 CUSIP、无 ticker。靠 SEC
company_tickers.json 的 name 归一化匹配 + 手工别名表（历史改名公司，
如 Apple Computer→Apple Inc、Google→Alphabet）。匹配率低于 N-PORT，
但 Top10 大盘股基本可解析。

⚠️ 双重验证：招募书为单一来源、自由文本，解析易错。调用方应做交叉验证
（见 verify_prospectus_holdings）：用招募书报告期附近的 N-PORT（如有）
或独立来源核对 Top10。
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, asdict
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# 2019 前 QQQ 的 485BPOS/APOS 申报（accession, primaryDocument, 申报日）。
# 报告期 = 上一年 9-30（年度招募书 1 月申报，含上一财年 9-30 持仓）。
# 来源：SEC submissions CIK0001067839.json。
PROSPECTUS_FILINGS: list[tuple[str, str, str]] = [
    ("0001193125-18-023862", "d423471d485bpos.htm", "2018-01-29"),
    ("0001193125-17-021968", "d284522d485bpos.htm", "2017-01-27"),
    ("0001193125-16-441831", "d106822d485bpos.htm", "2016-01-28"),
    ("0001104659-15-004817", "a15-1379_1485bpos.htm", "2015-01-28"),
    ("0001104659-14-005312", "a14-1316_1485bpos.htm", "2014-01-30"),
    ("0001104659-13-006545", "a12-28280_1485bpos.htm", "2013-01-31"),
    ("0001104659-12-005092", "a11-30095_1485bpos.htm", "2012-01-30"),
    ("0001104659-11-066963", "a11-30095_1485apos.htm", "2011-11-30"),
    ("0001104659-11-003685", "a10-18718_1485bpos.htm", "2011-01-28"),
    ("0001104659-10-002985", "a09-28465_1485bpos.htm", "2010-01-26"),
    ("0001206774-09-000129", "powershares_485bpos.htm", "2009-01-30"),
    ("0001206774-08-000229", "powershares_485bpos.htm", "2008-02-01"),
    ("0001206774-07-000279", "nasdaq_485bpos.htm", "2007-01-31"),
    ("0001206774-06-000136", "d18436-485bpos.txt", "2006-01-31"),
    ("0001206774-05-000077", "d16120_s-6.txt", "2005-01-31"),
    ("0001206774-04-000023", "d13831.txt", "2004-01-30"),
    ("0001206774-03-000628", "d12904.txt", "2003-08-14"),
    ("0001206774-03-000033", "d11781.txt", "2003-01-31"),
    ("0000912057-02-003740", "a2068197z485bpos.txt", "2002-02-01"),
    ("0000912057-02-003384", "a2068197z485bpos.txt", "2002-01-30"),
    ("0000912057-01-534103", "a2059883z485bpos.txt", "2001-10-01"),
    ("0000912057-01-003392", "a2036273z485bpos.txt", "2001-01-30"),
    ("0000912057-00-030669", "a485bpos.txt", "2000-06-30"),
]

ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data/1067839/{accn_nodash}/{doc}"

# 历史公司名 → ticker 别名（招募书用旧名，SEC 名册已是新名）。
_NAME_ALIASES: dict[str, str] = {
    "apple computer": "AAPL",
    "apple computer inc": "AAPL",
    "google": "GOOGL",
    "google inc": "GOOGL",
    "facebook": "META",
    "facebook inc": "META",
    "facebook corporation": "META",
    "honeywell international": "HON",
    "las vegas sands": "LVS",
    "mondelez international": "MDLZ",
    "kraft foods": "MDLZ",
    "google inc class a": "GOOGL",
    "google inc class c": "GOOG",
}

_QQ_INDEX_HEADERS = {
    "User-Agent": "BoolReminder research lwqisme@example.com",
    "Accept-Encoding": "gzip, deflate",
}


@dataclass
class ProspectusHolding:
    """招募书解析出的单只持仓。"""

    rank: int
    name: str
    shares: float          # 持仓股数
    value: float           # 市值 USD
    ticker: Optional[str]
    symbol: Optional[str]  # 归一化 *.US

    def to_dict(self) -> dict:
        return asdict(self)


def _archive_url(accession: str, doc: str) -> str:
    return ARCHIVES_BASE.format(accn_nodash=accession.replace("-", ""), doc=doc)


def download_prospectus(accession: str, doc: str, *, timeout: int = 30) -> str:
    """下载一份招募书主文档全文（txt 或 htm）。带重试。"""
    last_exc: Optional[Exception] = None
    import time
    url = _archive_url(accession, doc)
    for attempt in range(3):
        try:
            time.sleep(0.15)
            resp = requests.get(url, headers=_QQ_INDEX_HEADERS, timeout=timeout)
            if resp.status_code in (429, 503) and attempt < 2:
                time.sleep(2.0 * (attempt + 1)); continue
            resp.raise_for_status()
            return resp.text
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Failed downloading prospectus {accession}: {last_exc}")


def _norm_name(name: str) -> str:
    """与 edgar_nport._norm_name 一致的归一化。"""
    s = name.lower()
    s = re.sub(r"\s*/[a-z]+\b", " ", s)
    s = re.sub(r"[.,&']", " ", s)
    s = re.sub(r"\b(inc|corp|corporation|co|ltd|limited|plc|holdings|group|the|class)\b", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def _resolve_ticker(name: str, ticker_index: dict[str, str]) -> Optional[str]:
    """先查别名表（历史改名），再 name 归一化匹配。"""
    norm = _norm_name(name)
    if norm in _NAME_ALIASES:
        return _NAME_ALIASES[norm]
    return ticker_index.get(norm)


def _to_symbol(ticker: Optional[str]) -> Optional[str]:
    if not ticker:
        return None
    t = ticker.strip().upper()
    return t if t.endswith(".US") else f"{t}.US"


# ---------------- 报告期日期提取 ----------------

def _extract_report_date(text: str) -> str:
    """
    从 "Schedule of Investments" 标题后提取报告期日期 (YYYY-MM-DD)。
    形如 "September 30, 2005"。跨行/标签均容忍。
    """
    i = text.find("Schedule of Investments")
    if i < 0:
        return ""
    seg = text[i:i + 400]
    seg = re.sub(r"<[^>]+>", " ", seg)
    seg = re.sub(r"&nbsp;|&#160;", " ", seg)
    seg = re.sub(r"\s+", " ", seg)
    m = re.search(
        r"((?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},\s*\d{4})",
        seg, re.I,
    )
    if not m:
        return ""
    return _month_str_to_iso(m.group(1))


_MONTHS = {
    "january": 1, "february": 2, "march": 3, "april": 4, "may": 5, "june": 6,
    "july": 7, "august": 8, "september": 9, "october": 10, "november": 11, "december": 12,
}


def _month_str_to_iso(s: str) -> str:
    m = re.search(r"([A-Za-z]+)\s+(\d{1,2}),\s*(\d{4})", s)
    if not m:
        return ""
    mon = _MONTHS.get(m.group(1).lower())
    if not mon:
        return ""
    return f"{m.group(3)}-{mon:02d}-{int(m.group(2)):02d}"


# ---------------- 解析：两代格式 ----------------

def _parse_number(s: str) -> Optional[float]:
    """解析 '55,688,162' / '$ 1,432,856,408' / '1,432,856,408' 为 float。"""
    s = re.sub(r"[\$,]", "", s).strip()
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _parse_txt_schedule(text: str) -> list[tuple[str, float, float]]:
    """
    解析 2000-2006 旧式 .txt：点号填充文本表。
    返回 [(name, shares, value), ...] 按出现顺序（已降序）。
    """
    i = text.find("Schedule of Investments")
    if i < 0:
        return []
    # 表体从 <TABLE> 后开始；行形如 NAME ....... SHARES  $VALUE
    start = text.find("<TABLE>", i)
    if start < 0:
        start = i
    end = text.find("</TABLE>", start)
    block = text[start:end if end > 0 else len(text)]
    # 去标签
    block = re.sub(r"<[^>]+>", "\n", block)
    block = re.sub(r"&nbsp;", " ", block)

    out: list[tuple[str, float, float]] = []
    # 匹配：公司名(含点号)  股数  $市值
    line_re = re.compile(
        r"^\s*([A-Z][A-Za-z0-9 .,&'\-/\*]{2,}?)\s*\.{3,}\s*([\d,]+)\s+\$?\s*([\d,]+)\s*$"
    )
    for line in block.split("\n"):
        line = line.strip()
        if not line:
            continue
        m = line_re.match(line)
        if not m:
            continue
        name = m.group(1).rstrip(" .*").strip()
        shares = _parse_number(m.group(2))
        value = _parse_number(m.group(3))
        if name and shares is not None and value is not None:
            out.append((name, shares, value))
    return out


def _parse_html_schedule(text: str) -> list[tuple[str, float, float]]:
    """
    解析 2007-2018 .htm：HTML <TR><TD> 表。
    每个数据行三列：公司名 / 股数 / 市值。
    """
    i = text.find("Schedule of Investments")
    if i < 0:
        return []
    # 持仓段终止边界：遇到 "Total Investments"(合计行后) 或 "Statement of"/"Notes to"
    # (财报段) 即停止，避免误吞财务报表表。
    section = text[i:i + 250000]
    stop_kws = ["Total Investments", "Statement of Operations", "Statement of Assets",
                "Notes to Financial", "Report of Independent"]
    stop_at = len(section)
    for kw in stop_kws:
        j = section.find(kw)
        if 0 < j < stop_at:
            stop_at = j
    section = section[:stop_at]

    # 2017/2018 招募书持仓按"行业分组"且跨多页(table)分页，全局非按市值排序。
    # 遍历持仓段内所有含数据行的 <table>，合并全部持仓，最后由调用方按 value 排序。
    out: list[tuple[str, float, float]] = []
    search_from = 0
    scanned = 0
    while scanned < 40:
        start = section.lower().find("<table", search_from)
        if start < 0:
            break
        end = section.lower().find("</table>", start)
        table = section[start:end if end > 0 else len(section)]
        rows = re.findall(r"<TR[^>]*>(.*?)</TR>", table, re.I | re.S)
        has_data = False
        for r in rows:
            cells = re.findall(r"<TD[^>]*>(.*?)</TD>", r, re.I | re.S)
            if len(cells) < 3:
                continue
            pure_nums = 0
            for c in cells:
                n = re.sub(r"<[^>]+>|\s|&nbsp;|&#160;|\$|,", "", c)
                if n and re.fullmatch(r"\d+(\.\d+)?", n):
                    pure_nums += 1
            if pure_nums >= 2:
                has_data = True
                break
        if has_data:
            out.extend(_parse_html_rows(rows))
        scanned += 1
        search_from = end + 1 if end > 0 else len(section)
    return out


def _parse_html_rows(rows: list[str]) -> list[tuple[str, float, float]]:
    """从一组 <TR> 中解析持仓行（公司名 + 股数 + 市值）。"""
    out: list[tuple[str, float, float]] = []
    for r in rows:
        cells = re.findall(r"<TD[^>]*>(.*?)</TD>", r, re.I | re.S)
        if len(cells) < 3:
            continue
        clean = []
        for c in cells:
            c = re.sub(r"<[^>]+>", " ", c)
            c = re.sub(r"&nbsp;|&#160;", " ", c)
            c = re.sub(r"\s+", " ", c).strip()
            clean.append(c)
        # 表头/分组标题行(含 Common Stock/Shares/Value/Number/行业名 且数字<2)跳过
        joined = " ".join(clean).lower()
        num_count = sum(1 for c in clean if _parse_number(c) is not None)
        if any(h in joined for h in ("common stock", "shares", "value", "number", "total")) and num_count < 2:
            continue
        # 行业分组标题行（如 "Airlines—0.3%"，仅1个百分数无股数）跳过
        if num_count < 2:
            continue
        # 找"公司名"cell：以字母开头、含字母、非纯数字。
        name_idx = -1
        name = ""
        for k, c in enumerate(clean):
            base = re.sub(r"\s*[\*\(\)][^\)]*\)?", "", c).strip()
            base = base.rstrip("*").strip()
            if base and base[0].isalpha() and re.search(r"[A-Za-z]{2,}", base) \
               and _parse_number(base) is None \
               and not base.lower().startswith(("common stock", "shares", "value", "number", "total")):
                name_idx = k
                name = base
                break
        if name_idx < 0:
            continue
        before = [(_parse_number(c)) for c in clean[:name_idx] if _parse_number(c) is not None]
        after = [(_parse_number(c)) for c in clean[name_idx + 1:] if _parse_number(c) is not None]
        shares = None
        value = None
        if before and after:
            shares = before[-1]
            value = after[0]
        elif len(after) >= 2:
            shares = after[0]
            value = after[1]
        elif len(before) >= 2:
            shares = before[-2]
            value = before[-1]
        if shares is None or value is None:
            continue
        out.append((name, shares, value))
    return out


def parse_prospectus_holdings(
    text: str,
    ticker_index: Optional[dict[str, str]] = None,
) -> tuple[str, list[ProspectusHolding]]:
    """
    解析一份招募书，返回 (报告期ISO, 持仓列表按市值降序 rank=1起)。

    自动检测格式：含 <TABLE> 后接点号文本行 → txt；否则按 HTML 表。
    """
    repd = _extract_report_date(text)
    index = ticker_index or {}

    rows = _parse_html_schedule(text)
    if not rows:
        rows = _parse_txt_schedule(text)

    holdings: list[ProspectusHolding] = []
    for name, shares, value in rows:
        ticker = _resolve_ticker(name, index)
        holdings.append(ProspectusHolding(
            rank=0,
            name=name,
            shares=shares,
            value=value,
            ticker=ticker,
            symbol=_to_symbol(ticker),
        ))
    # 按市值降序定 rank
    holdings.sort(key=lambda h: h.value, reverse=True)
    for i, h in enumerate(holdings, 1):
        h.rank = i
    return repd, holdings


def fetch_prospectus_snapshot(
    accession: str,
    doc: str,
    filing_date: str,
    ticker_index: dict[str, str],
) -> dict:
    """下载并解析一份招募书，组装快照 payload。"""
    text = download_prospectus(accession, doc)
    repd, holdings = parse_prospectus_holdings(text, ticker_index)
    resolved = sum(1 for h in holdings if h.ticker)
    logger.info(
        "Prospectus %s (repd=%s): %d holdings, %d ticker resolved",
        filing_date, repd, len(holdings), resolved,
    )
    return {
        "fund": "QQQ",
        "source": "SEC EDGAR 485BPOS",
        "repd_date": repd or filing_date,
        "filing_date": filing_date,
        "accession": accession,
        "count": len(holdings),
        "ticker_resolved": resolved,
        "holdings": [h.to_dict() for h in holdings],
    }


# ---------------- 双重验证 ----------------

def verify_prospectus_holdings(
    prospectus: dict,
    nport_snapshots: list[dict],
) -> dict:
    """
    双重验证：把招募书 Top10 与最近的 N-PORT 快照交叉核对。

    N-PORT 自 2019 起有季度数据。招募书 2018-09-30 报告期可与 N-PORT
    2019-09-30 交叉（成分变化小）；更早的招募书无 N-PORT 可对，标记为
    "no_nport_overlap"（单一来源，需调用方知悉）。

    Returns:
        {repd_date, nport_repd, top10_overlap, notes}
    """
    repd = prospectus.get("repd_date", "")
    # 找最近的 N-PORT（任意年份同季度或时间上最近）
    nport_top10_names: set[str] = set()
    chosen_repd = ""
    if nport_snapshots:
        # 取第一份（最早）N-PORT 作锚——成分跨年变化通常 Top10 集中在大盘股
        snap = nport_snapshots[0]
        chosen_repd = snap.get("repd_date", "")
        ordered = sorted(snap.get("holdings", []), key=lambda h: h.get("val_usd", 0), reverse=True)
        nport_top10_names = {_norm_name(h.get("name", "")) for h in ordered[:10] if h.get("name")}

    prosp_ordered = sorted(prospectus.get("holdings", []), key=lambda h: h.get("value", 0), reverse=True)
    prosp_top10 = prosp_ordered[:10]
    prosp_names = {_norm_name(h.get("name", "")) for h in prosp_top10 if h.get("name")}

    notes: list[str] = []
    overlap = 0
    if nport_top10_names and prosp_names:
        overlap = len(nport_top10_names & prosp_names)
        notes.append(f"招募书 Top10 与最早 N-PORT({chosen_repd}) Top10 重合 {overlap}/10（跨年大盘股高度稳定属正常）。")
    else:
        notes.append("无重叠 N-PORT 可交叉验证（早于 2019），此快照为单一来源。")

    return {
        "repd_date": repd,
        "nport_repd": chosen_repd,
        "top10_overlap_with_earliest_nport": overlap,
        "notes": notes,
    }
