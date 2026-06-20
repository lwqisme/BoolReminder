"""
SEC EDGAR N-PORT 历史持仓抓取 —— QQQ 历史成分股重建

通过 SEC EDGAR 抓取 Invesco QQQ Trust (CIK 0001067839) 的 N-PORT-P 季度
申报，重建 QQQ 自 2019-11 起每季度末的完整持仓序列，用于回测任意时间段。

为什么用 N-PORT
---------------
ETF 每季度向 SEC 申报 N-PORT-P，含当时完整持仓（公司名、CUSIP、股数、
市值 USD、占 NAV %）。这是官方、结构化、免费、历史可回溯的权威来源，
远胜第三方抓取页（只给当前快照）。

⚠️ 与 stockanalysis 当前快照的关系：
  - N-PORT 是季度末快照、季度粒度，适合回测历史曲线。
  - 季度内的临时增删（如新股上市当日纳入）N-PORT 最快下季度才反映，
    需靠 stockanalysis 每日快照层补足。两层互补。

ticker 解析
-----------
N-PORT 持仓只有 name + cusip，没有 ticker。本模块：
  1. 优先用 CUSIP override 表（处理 GOOG/GOOGL 等 name 歧义）。
  2. 其次用 SEC 官方 company_tickers.json 的 title→ticker name 归一化匹配
     （实测 Top10 命中 10/10，全量 ~88%）。
  3. 匹配不上的 ticker 留 None（存 name+cusip，不阻塞 Top10 基金）。

SEC 礼仪
--------
EDGAR 要求 User-Agent 含联系方式，且建议 ≤10 req/s。本模块每次请求后
sleep，并带 3 次重试。
"""

from __future__ import annotations

import html as _html  # noqa: F401  (保留以备 name 归一化扩展)
import logging
import re
import time
import xml.etree.ElementTree as ET
from dataclasses import dataclass, asdict
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# Invesco QQQ Trust, Series 1 的 CIK（来自 SEC company_tickers.json 反查）。
QQQ_CIK = "0001067839"

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik}.json"
TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
ARCHIVES_BASE = "https://www.sec.gov/Archives/edgar/data/{cik_nodash}/{accn_nodash}/primary_doc.xml"

# SEC 要求 User-Agent 含联系方式；EDGAR 会拒绝无 UA 的请求。
_EDGAR_UA = "BoolReminder research lwqisme@example.com"
_EDGAR_HEADERS = {
    "User-Agent": _EDGAR_UA,
    "Accept-Encoding": "gzip, deflate",
}

# 请求间隔（秒）。SEC 政策 ≤10 req/s，留余量。
_REQUEST_GAP = 0.15

# CUSIP → ticker 手工 override：处理 N-PORT 里 name 相同但实为不同类别的股票，
# 或 SEC company_tickers.json title 与 N-PORT name 因重注册标记(/NEW /DE)不匹配的情况。
# CUSIP 是公开标识符，此处仅做映射不涉及授权数据。
_CUSIP_OVERRIDES: dict[str, str] = {
    "02079K305": "GOOG",   # Alphabet Class C
    "02079K107": "GOOGL",  # Alphabet Class A
    "22160K105": "COST",   # Costco — SEC 名册带 "/NEW" 标记，name 匹配失败
}


@dataclass
class NportFiling:
    """一份 N-PORT 申报的索引信息。"""

    filing_date: str        # SEC 申报日，如 2026-05-28
    accession: str          # 带连字符的 accession number，如 0001067839-26-000024
    primary_document: str   # 主文档相对路径（submissions 字段原值）

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class NportHolding:
    """N-PORT 解析出的单只持仓。"""

    rank: int
    name: str
    cusip: str
    balance: float          # 持仓股数
    val_usd: float          # 市值 USD
    pct_val: float          # 占 NAV %
    ticker: Optional[str]   # 解析出的 ticker；匹配失败为 None
    symbol: Optional[str]   # 归一化 Longbridge 代码（如 AAPL.US），无 ticker 时为 None

    def to_dict(self) -> dict:
        return asdict(self)


def _sleep() -> None:
    time.sleep(_REQUEST_GAP)


def _edgar_get(url: str, *, timeout: int = 25) -> str:
    """带重试的 EDGAR GET；返回响应文本。"""
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            _sleep()
            resp = requests.get(url, headers=_EDGAR_HEADERS, timeout=timeout)
            if resp.status_code in (429, 503) and attempt < 2:
                wait = 2.0 * (attempt + 1)
                logger.warning("EDGAR %s got %s, retrying in %.1fs", url, resp.status_code, wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.text
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            logger.warning("EDGAR %s network error: %s (attempt %d)", url, exc, attempt + 1)
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Failed EDGAR GET {url}: {last_exc}")


def fetch_nport_filings(cik: str = QQQ_CIK) -> list[NportFiling]:
    """拉取某 CIK 的全部 NPORT-P 申报索引（按申报日升序）。"""
    text = _edgar_get(SUBMISSIONS_URL.format(cik=cik))
    import json
    data = json.loads(text)
    recent = data["filings"]["recent"]
    forms = recent["form"]
    filings: list[NportFiling] = []
    for i, form in enumerate(forms):
        if form != "NPORT-P":
            continue
        filings.append(
            NportFiling(
                filing_date=recent["filingDate"][i],
                accession=recent["accessionNumber"][i],
                primary_document=recent["primaryDocument"][i],
            )
        )
    # submissions 通常按申报日降序，翻转为升序便于按时间回溯。
    filings.sort(key=lambda f: f.filing_date)
    logger.info("Found %d NPORT-P filings for CIK %s (%s..%s)",
                len(filings), cik,
                filings[0].filing_date if filings else "-",
                filings[-1].filing_date if filings else "-")
    return filings


def download_nport_xml(accession: str, cik: str = QQQ_CIK) -> str:
    """下载某份 N-PORT 的 primary_doc.xml 全文。"""
    cik_nodash = cik.lstrip("0") or "0"
    accn_nodash = accession.replace("-", "")
    url = ARCHIVES_BASE.format(cik_nodash=cik_nodash, accn_nodash=accn_nodash)
    return _edgar_get(url)


# ---------- ticker 解析 ----------

def _norm_name(name: str) -> str:
    """公司名归一化：去标点、去法律后缀、去重注册标记、小写、压空白。"""
    s = name.lower()
    # 去重注册/州标记，如 "/NEW"、"/DE"、"/MD"（SEC 名册常见，N-PORT 通常无）
    s = re.sub(r"\s*/[a-z]+\b", " ", s)
    s = re.sub(r"[.,&']", " ", s)
    s = re.sub(r"\b(inc|corp|corporation|co|ltd|limited|plc|holdings|group|the|class)\b", " ", s)
    s = re.sub(r"[^a-z0-9 ]", " ", s)
    return re.sub(r"\s+", " ", s).strip()


def build_ticker_index() -> dict[str, str]:
    """
    从 SEC company_tickers.json 构建 归一化公司名 → ticker 索引。

    company_tickers.json 含 ~8000 家在 SEC 注册的公司 title↔ticker。
    Nasdaq-100 成分均为大盘股，基本都在其中。
    """
    import json
    text = _edgar_get(TICKERS_URL)
    data = json.loads(text)
    index: dict[str, str] = {}
    for v in data.values():
        key = _norm_name(v.get("title", ""))
        if key:
            index.setdefault(key, v["ticker"])
    logger.info("Built ticker index: %d names", len(index))
    return index


def _resolve_ticker(name: str, cusip: str, index: dict[str, str]) -> Optional[str]:
    """先查 CUSIP override，再查 name 归一化匹配。"""
    cusip = (cusip or "").strip()
    if cusip and cusip in _CUSIP_OVERRIDES:
        return _CUSIP_OVERRIDES[cusip]
    return index.get(_norm_name(name))


def _to_symbol(ticker: Optional[str]) -> Optional[str]:
    """ticker → Longbridge 风格 *.US；无 ticker 返回 None。"""
    if not ticker:
        return None
    t = ticker.strip().upper()
    if t.endswith(".US"):
        return t
    return f"{t}.US"


# ---------- XML 解析 ----------

def _tag(elem) -> str:
    return elem.tag.split("}")[-1]


def parse_nport_holdings(
    xml_text: str,
    ticker_index: Optional[dict[str, str]] = None,
) -> tuple[str, list[NportHolding]]:
    """
    解析 N-PORT primary_doc.xml。

    Returns:
        (repd_date, holdings)  repd_date 为报告期（持仓所属月末），
        holdings 按市值降序、rank 从 1 起。
    """
    root = ET.fromstring(xml_text)
    # N-PORT 报告期日期 tag 为 repPdDate（持仓所属季度末，如 2026-03-31）。
    repd_date = ""
    for e in root.iter():
        if _tag(e) == "repPdDate":
            repd_date = (e.text or "").strip()
            if repd_date:
                break

    index = ticker_index if ticker_index is not None else {}

    rows: list[NportHolding] = []
    for sec in root.iter():
        if _tag(sec) != "invstOrSec":
            continue
        d = {_tag(c): (c.text or "").strip() for c in sec}
        name = d.get("name", "")
        cusip = d.get("cusip", "")
        try:
            val_usd = float(d.get("valUSD", "0") or 0)
        except ValueError:
            val_usd = 0.0
        try:
            pct_val = float(d.get("pctVal", "0") or 0)
        except ValueError:
            pct_val = 0.0
        try:
            balance = float(d.get("balance", "0") or 0)
        except ValueError:
            balance = 0.0
        ticker = _resolve_ticker(name, cusip, index)
        rows.append(NportHolding(
            rank=0,  # 排序后回填
            name=name,
            cusip=cusip,
            balance=balance,
            val_usd=val_usd,
            pct_val=pct_val,
            ticker=ticker,
            symbol=_to_symbol(ticker),
        ))

    # 按市值降序定 rank
    rows.sort(key=lambda h: h.val_usd, reverse=True)
    for i, h in enumerate(rows, 1):
        h.rank = i
    return repd_date, rows


def fetch_nport_snapshot(
    filing: NportFiling,
    ticker_index: dict[str, str],
) -> dict:
    """
    下载并解析一份 N-PORT 申报，组装成快照 payload。

    Returns:
        {fund, repd_date, filing_date, accession, source, count, holdings:[...]}
    """
    xml_text = download_nport_xml(filing.accession)
    repd_date, holdings = parse_nport_holdings(xml_text, ticker_index)
    resolved = sum(1 for h in holdings if h.ticker)
    logger.info(
        "N-PORT %s (repd=%s): %d holdings, %d ticker resolved",
        filing.filing_date, repd_date, len(holdings), resolved,
    )
    return {
        "fund": "QQQ",
        "source": "SEC EDGAR N-PORT-P",
        "repd_date": repd_date or filing.filing_date,
        "filing_date": filing.filing_date,
        "accession": filing.accession,
        "count": len(holdings),
        "ticker_resolved": resolved,
        "holdings": [h.to_dict() for h in holdings],
    }
