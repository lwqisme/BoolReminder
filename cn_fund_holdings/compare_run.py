"""在容器内跑: 近一年 QQQ Top10(等权/加权) vs QQQ vs 270023 逐月曲线对比.

输出 JSON 给宿主绘图/分析. 270023 用单位净值剔汇到USD口径对齐.
"""
from __future__ import annotations

import csv
import json
import sys
from datetime import date
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from qq_index_fund.backtest import run_backtest


def load_270023_usd():
    """270023 单位净值剔汇到USD口径, 与QQQ共享交易日."""
    js = open("/tmp/pz_270023.js", encoding="utf-8").read()
    import re
    nu = json.loads(re.search(r"var Data_netWorthTrend\s*=\s*(\[.*?\])\s*;", js, re.S).group(1))
    U = {}
    for p in nu:
        d = __import__("datetime").datetime.fromtimestamp(p["x"]/1000, tz=__import__("datetime").UTC).strftime("%Y-%m-%d")
        U[d] = p["y"]
    fx = json.load(open("/tmp/usdcny.json"))
    return U, fx


def main():
    start, end = date(2025, 6, 16), date(2026, 6, 16)
    r = run_backtest(start, end)
    print(f"equal={r.total_return_equal:+.2f}% weighted={r.total_return_weighted:+.2f}% qqq={r.total_return_qqq:+.2f}%")
    print(f"days={len(r.dates)} rebalances={len(r.rebalances)}")

    U, fx = load_270023_usd()

    def at(d, m):
        ks = sorted(k for k in m if k <= d)
        return m[ks[-1]] if ks else None

    # 对齐到回测交易日, 270023 USD口径归一
    u0 = at(r.dates[0], U); f0 = at(r.dates[0], fx)
    nav_270023 = []
    for d in r.dates:
        u = at(d, U); f = at(d, fx)
        nav_270023.append((u / u0) / (f / f0))  # RMB净值 / 汇率 = USD口径

    out = {
        "start": r.dates[0], "end": r.dates[-1],
        "total": {
            "qqq_top10_equal": r.total_return_equal,
            "qqq_top10_weighted": r.total_return_weighted,
            "qqq": r.total_return_qqq,
            "270023_usd": (nav_270023[-1] - 1) * 100,
        },
        "dates": r.dates,
        "nav_qqq": r.nav_qqq,
        "nav_equal": r.nav_equal,
        "nav_weighted": r.nav_weighted,
        "nav_270023_usd": nav_270023,
        "rebalances": [{"date": getattr(rb, "date", None) or str(rb)} for rb in (r.rebalances or [])],
        "notes": r.notes,
    }
    p = Path("/tmp/compare_last1y.json")
    p.write_text(json.dumps(out, ensure_ascii=False))
    print(f"-> {p}")

    # 逐月采样超额
    print("\n逐月采样 (累计收益%):")
    print(f"{'日期':12}{'QQQ':>9}{'Top10等权':>11}{'Top10加权':>11}{'270023USD':>11}{'270023-QQQ':>11}{'等权-QQQ':>10}")
    seen = set()
    for i, d in enumerate(r.dates):
        ym = d[:7]
        if ym in seen:
            continue
        seen.add(ym)
        qq = r.nav_qqq[i]; eq = r.nav_equal[i]; we = r.nav_weighted[i]; f2 = nav_270023[i]
        print(f"{d:12}{(qq-1)*100:>+8.1f}{(eq-1)*100:>+10.1f}{(we-1)*100:>+10.1f}{(f2-1)*100:>+10.1f}{(f2-qq)*100:>+10.1f}{(eq-qq)*100:>+9.1f}")


if __name__ == "__main__":
    main()
