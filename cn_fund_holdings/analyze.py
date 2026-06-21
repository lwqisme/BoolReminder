"""270023 vs QQQ 持仓对比与超额收益归因分析."""
from __future__ import annotations

import sys
from collections import Counter, defaultdict
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cn_fund_holdings import scraper
from qq_index_fund import store
import datetime


def load_270023() -> list[scraper.QuarterHoldings]:
    return scraper.fetch_all()


def load_qqq_top(as_of: str, n: int = 10) -> dict[str, float]:
    """QQQ N-PORT 截止 as_of(YYYY-MM-DD) 的前 n 大权重 {ticker: weight}."""
    d = datetime.date.fromisoformat(as_of)
    try:
        sn = store.load_nport_snapshot(d)
    except Exception:
        return {}
    if not sn or not sn.get("holdings"):
        return {}
    def wp(h):
        if hasattr(h, "weight_pct"):
            return h.weight_pct
        return h.get("weight_pct") or h.get("pct_val") or 0.0
    def tk(h):
        return h.ticker if hasattr(h, "ticker") else h["ticker"]
    hs = sorted(sn["holdings"], key=lambda h: -wp(h))[:n]
    # 保留权重更大的: 同 ticker 取最大
    out: dict[str, float] = {}
    for h in hs:
        t = tk(h)
        if not t:
            continue
        out[t] = max(out.get(t, 0), wp(h))
    return out


# ---- 行业/市场分类 (粗分, 用于归因) ----
SEMI = {"NVDA", "AMD", "TSM", "ASML", "AVGO", "INTC", "QCOM", "TXN", "MU", "AMAT", "KLAC", "NXPI", "MRVL", "LRCX", "ADI"}
US_MEGACAP_TECH = {"AAPL", "MSFT", "GOOG", "GOOGL", "META", "AMZN", "NFLX", "TSLA", "GOOGL"}
SW_US_TECH = {"NOW", "CRM", "PLTR", "SNOW", "PANW", "CRWD", "INTU", "ADBE", "ISRG"}
HK_CN = lambda c: c.isdigit()  # 港股/A股代码为纯数字 (00700/03690/02513/00883...)


def classify(code: str) -> str:
    if code in SEMI:
        return "半导体"
    if code in US_MEGACAP_TECH:
        return "美股MegaCap科技"
    if code in SW_US_TECH:
        return "美股SaaS/软件"
    if HK_CN(code):
        return "中港股"
    return "其他美股"


def main():
    qs = load_270023()
    print("=" * 78)
    print("一、21 个季度前十大持仓频率 (出现次数 / 平均权重 when held)")
    print("=" * 78)
    freq = Counter()
    wsum = defaultdict(float)
    for q in qs:
        for h in q.holdings:
            freq[h.code] += 1
            wsum[h.code] += h.weight_pct
    rows = sorted(freq.items(), key=lambda kv: -kv[1])
    print(f"{'code':8}{'name':14}{'季次':>5}{'均权':>8}{'行业':>14}")
    for code, n in rows[:25]:
        name = next((h.name for q in qs for h in q.holdings if h.code == code), "")
        avg = wsum[code] / n
        print(f"{code:8}{name[:12]:14}{n:>5}{avg:>7.1f}%{classify(code):>14}")

    print("\n" + "=" * 78)
    print("二、与 QQQ 前十大重叠度逐季对照")
    print("=" * 78)
    print(f"{'季度':8}{'270023Top10合计':>15}{'与QQQ重叠数':>12}{'重叠权重':>10}{'QQQ内但270023无(例)':>30}")
    overlap_stats = []
    for q in qs:
        if not q.holdings:
            continue
        my = {h.code: h.weight_pct for h in q.holdings}
        qqq = load_qqq_top(q.as_of, 10)
        common = set(my) & set(qqq)
        overlap_w = sum(my[c] for c in common)
        only_qqq = sorted(set(qqq) - set(my))[:5]
        tot = sum(my.values())
        print(f"{q.label:8}{tot:>14.1f}%{len(common):>12}{overlap_w:>9.1f}%   {','.join(only_qqq):<28}")
        overlap_stats.append((q.label, len(common), overlap_w, tot))

    print("\n" + "=" * 78)
    print("三、行业敞口演变 (前十大按行业汇总权重 %)")
    print("=" * 78)
    cats = ["半导体", "美股MegaCap科技", "美股SaaS/软件", "中港股", "其他美股"]
    print(f"{'季度':8}" + "".join(f"{c:>16}" for c in cats))
    for q in qs:
        if not q.holdings:
            continue
        s = defaultdict(float)
        for h in q.holdings:
            s[classify(h.code)] += h.weight_pct
        print(f"{q.label:8}" + "".join(f"{s.get(c,0):>15.1f}%" for c in cats))

    print("\n" + "=" * 78)
    print("四、关键个股权重轨迹 (NVDA / TSLA / GOOG / META / AAPL / MSFT)")
    print("=" * 78)
    keys = ["NVDA", "TSLA", "GOOG", "META", "AAPL", "MSFT", "ASML", "TSM"]
    print(f"{'季度':8}" + "".join(f"{k:>8}" for k in keys))
    for q in qs:
        m = {h.code: h.weight_pct for h in q.holdings}
        print(f"{q.label:8}" + "".join(f"{m.get(k,0):>7.1f}%" for k in keys))


if __name__ == "__main__":
    main()
