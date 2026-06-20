"""
QQQ 成分股抓取与解析

从 stockanalysis.com 抓取 QQQ holdings 页面，解析出 Top-N 成分股
（代码、名称、市值权重），并归一化为 Longbridge 风格的 *.US 代码。

零外部解析依赖：仅用 requests + 标准库 re/html。页面为 Svelte 服务端
渲染，含大量 <!-- --> 注释噪声，解析策略是先去注释、再把标签替换为
分隔符，最后按 (序号, TICKER, 名称, X.XX%) 四元组扫描。
"""

from __future__ import annotations

import html
import logging
import re
import time
from dataclasses import dataclass, asdict
from typing import Optional

import requests

logger = logging.getLogger(__name__)

# stockanalysis.com 的 QQQ holdings 页面。服务端渲染，HTML 内直接含成分表。
QQQ_HOLDINGS_URL = "https://stockanalysis.com/etf/qqq/holdings/"

# 浏览器 UA + Accept，避免被 Cloudflare/WAF 拦截（Invesco 端点即因 406 失败）。
_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
}

# 常见市场后缀；已带后缀的代码不再追加 .US。
_MARKET_SUFFIXES = (".US", ".HK", ".SH", ".SZ", ".SG")


@dataclass
class Holding:
    """单只成分股。"""

    rank: int
    ticker: str           # 原始代码，如 NVDA
    name: str             # 公司全称
    weight_pct: float     # 市值权重（%），如 8.11
    symbol: str           # 归一化后的 Longbridge 代码，如 NVDA.US

    def to_dict(self) -> dict:
        return asdict(self)


def normalize_symbol(ticker: str) -> str:
    """
    将原始 ticker 归一化为 Longbridge 风格代码。

    与 drawdown.generate_drawdown_report.normalize_longbridge_symbol 行为一致，
    但本模块刻意自包含、不导入 drawdown（避免拉起 longbridge SDK 依赖）。
    """
    symbol = (ticker or "").strip().upper()
    if not symbol:
        raise ValueError("Ticker is empty.")
    if any(symbol.endswith(suffix) for suffix in _MARKET_SUFFIXES):
        return symbol
    return f"{symbol}.US"


def _fetch_html(url: str, timeout: int, headers: dict) -> str:
    """带重试的 HTTP GET；仅对临时性错误（超时/连接/5xx）重试。"""
    last_exc: Optional[Exception] = None
    for attempt in range(3):
        try:
            response = requests.get(url, headers=headers, timeout=timeout)
            if response.status_code >= 500 and attempt < 2:
                logger.warning(
                    "qqq_holdings fetch got %s, retrying (attempt %d)",
                    response.status_code, attempt + 1,
                )
                time.sleep(1.5 * (attempt + 1))
                continue
            response.raise_for_status()
            return response.text
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_exc = exc
            logger.warning(
                "qqq_holdings fetch network error: %s (attempt %d)",
                exc, attempt + 1,
            )
            time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"Failed to fetch QQQ holdings after retries: {last_exc}")


def _parse_holdings(html_text: str, top_n: int) -> list[Holding]:
    """
    从 stockanalysis.com HTML 解析成分股。

    页面结构（去注释后）：每行为 <td>序号</td><td><a>TICKER</a></td>
    <td>名称</td><td>X.XX%</td>...。标签替换为分隔符后按四元组扫描。
    """
    cleaned = re.sub(r"<!--.*?-->", "", html_text, flags=re.S)
    cleaned = re.sub(r"<[^>]+>", "|", cleaned)
    cleaned = html.unescape(cleaned)
    cleaned = re.sub(r"\|+", "|", cleaned)
    tokens = [t.strip() for t in cleaned.split("|") if t.strip()]

    holdings: list[Holding] = []
    i = 0
    while i < len(toks := tokens) - 3 and len(holdings) < top_n:
        if (
            re.fullmatch(r"\d+", toks[i])
            and re.fullmatch(r"[A-Z.]{1,6}", toks[i + 1])
            and re.fullmatch(r"[0-9]+\.[0-9]+%", toks[i + 3])
        ):
            rank = int(toks[i])
            ticker = toks[i + 1]
            name = toks[i + 2]
            weight = float(toks[i + 3].rstrip("%"))
            holdings.append(
                Holding(
                    rank=rank,
                    ticker=ticker,
                    name=name,
                    weight_pct=weight,
                    symbol=normalize_symbol(ticker),
                )
            )
            i += 4
        else:
            i += 1
    return holdings


def fetch_qqq_holdings(
    top_n: int = 25,
    *,
    timeout: int = 20,
    url: str = QQQ_HOLDINGS_URL,
    headers: Optional[dict] = None,
) -> list[Holding]:
    """
    抓取并解析 QQQ 成分股 Top-N。

    Args:
        top_n: 取前 N 只（页面每页 25 条，默认 25）。
        timeout: 单次请求超时秒数。
        url: 数据源 URL（可注入，便于测试）。
        headers: 自定义请求头。

    Returns:
        按 rank 升序的 Holding 列表。

    Raises:
        RuntimeError: 多次重试仍失败。
        ValueError: 解析到的成分股数量为 0（页面结构变更告警）。
    """
    text = _fetch_html(url, timeout, headers or dict(_DEFAULT_HEADERS))
    holdings = _parse_holdings(text, top_n)
    if not holdings:
        raise ValueError(
            "Parsed 0 holdings from QQQ page — source layout may have changed."
        )
    logger.info("Fetched %d QQQ holdings (top=%s)", len(holdings), holdings[0].ticker)
    return holdings


if __name__ == "__main__":
    # 直接运行本文件：抓取并打印，便于手动验证。
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    for h in fetch_qqq_holdings():
        print(f"{h.rank:>2} {h.ticker:6} {h.weight_pct:>6.2f}%  {h.symbol:10} {h.name}")
