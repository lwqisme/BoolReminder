"""广发全球精选股票(QDII)A 270023 历史持仓抓取与解析.

数据源: 天天基金 FundArchivesDatas 接口 (季度披露的前十大重仓股).
  http://fundf10.eastmoney.com/FundArchivesDatas.aspx?type=jjcc&code=270023&topline=10&year=YYYY&month=

该接口一次返回某年全部 4 个季度的前十大持仓表 (季报披露, 每季度末截止).
半年报/年报的全量持仓需走 cninfo, 这里只取季报 Top10 (足以做风格/选股分析).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field, asdict
from pathlib import Path

import requests

CACHE_DIR = Path(__file__).resolve().parent / "cache"
DEFAULT_CODE = "270023"
UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"


@dataclass
class Holding:
    rank: int
    code: str          # 股票代码 (如 NVDA / 00700 / .HK 会带市场前缀)
    name: str          # 股票名称
    weight_pct: float  # 占净值比例 %
    shares_wan: float | None   # 持股数 (万股)
    value_wan_rmb: float | None  # 持仓市值 (万元人民币)


@dataclass
class QuarterHoldings:
    year: int
    quarter: int
    as_of: str        # YYYY-MM-DD
    holdings: list[Holding] = field(default_factory=list)

    @property
    def label(self) -> str:
        return f"{self.year}Q{self.quarter}"


def fetch_year_html(code: str, year: int, cache: bool = True) -> str:
    cache_dir = CACHE_DIR
    cache_dir.mkdir(parents=True, exist_ok=True)
    fp = cache_dir / f"cc_{code}_{year}.html"
    if cache and fp.exists():
        return fp.read_text(encoding="utf-8")
    url = (
        "http://fundf10.eastmoney.com/FundArchivesDatas.aspx"
        f"?type=jjcc&code={code}&topline=10&year={year}&month="
    )
    r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
    r.encoding = "utf-8"
    text = r.text
    if cache:
        fp.write_text(text, encoding="utf-8")
    return text


# 一行持仓: 序号 / 代码 / 名称 / [可选的最新价/涨跌幅/相关资讯等中间列] / 占净值 / 持股数 / 市值
# 中间列数随年报/季报模板不同而变 (2026 季报多了"最新价/涨跌幅"两列), 故用惰性匹配跳过.
_ROW_RE = re.compile(
    r"<tr><td>(\d+)</td>"           # 序号
    r"<td class='toc'>\s*<a[^>]*>([A-Za-z0-9.\-]+)</a>\s*</td>"  # 代码
    r"<td[^>]*>\s*<a[^>]*>([^<]+)</a>\s*</td>"                    # 名称
    r".*?"                          # 跳过任意中间列 (相关资讯/最新价/涨跌幅)
    r"<td class='toc'>([0-9.]+%)</td>"                            # 占净值比例
    r"<td class='toc'>([0-9,.]+)</td>"                            # 持股数(万股)
    r"<td class='toc'>([0-9,.]+)</td>\s*</tr>"                    # 持仓市值(万元)
    , re.S)


def _to_float(s: str) -> float:
    return float(s.replace(",", ""))


def parse_holdings(html: str) -> list[QuarterHoldings]:
    out: list[QuarterHoldings] = []
    boxes = re.split(r"boxitem w790", html)[1:]
    for b in boxes:
        mq = re.search(r"(\d{4})年(\d)季度股票投资明细", b)
        md = re.search(r"截止至：<font[^>]*>(\d{4}-\d{2}-\d{2})", b)
        if not mq or not md:
            continue
        qh = QuarterHoldings(
            year=int(mq.group(1)),
            quarter=int(mq.group(2)),
            as_of=md.group(1),
        )
        for m in _ROW_RE.finditer(b):
            qh.holdings.append(Holding(
                rank=int(m.group(1)),
                code=m.group(2).strip(),
                name=m.group(3).strip(),
                weight_pct=float(m.group(4).rstrip("%")),
                shares_wan=_to_float(m.group(5)),
                value_wan_rmb=_to_float(m.group(6)),
            ))
        out.append(qh)
    # 按时间正序
    out.sort(key=lambda x: (x.year, x.quarter))
    return out


def fetch_all(code: str = DEFAULT_CODE, years: range | list[int] | None = None) -> list[QuarterHoldings]:
    if years is None:
        years = range(2021, 2027)
    all_q: list[QuarterHoldings] = []
    for y in years:
        html = fetch_year_html(code, y, cache=True)
        all_q.extend(parse_holdings(html))
        time.sleep(0.3)
    all_q.sort(key=lambda x: (x.year, x.quarter))
    return all_q


def to_json(quarters: list[QuarterHoldings], path: Path | str) -> None:
    Path(path).write_text(
        json.dumps([asdict(q) for q in quarters], ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


if __name__ == "__main__":
    qs = fetch_all()
    out = Path(__file__).resolve().parent / "holdings_270023.json"
    to_json(qs, out)
    print(f"{len(qs)} quarters -> {out}")
    for q in qs:
        top3 = ", ".join(f"{h.code}({h.weight_pct:.1f}%)" for h in q.holdings[:3])
        tot = sum(h.weight_pct for h in q.holdings)
        print(f"{q.label} {q.as_of}  top10合计={tot:5.1f}%  头三: {top3}")
