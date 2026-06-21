"""导出 270023 全部季度持仓为扁平 CSV (每一次更新一行)."""
from __future__ import annotations

import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from cn_fund_holdings import scraper


def main():
    qs = scraper.fetch_all()
    out = Path(__file__).resolve().parent / "holdings_270023.csv"
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["季度", "截止日期", "披露类型", "序号", "股票代码", "股票名称",
                    "占净值比例%", "持股数_万股", "持仓市值_万元人民币"])
        for q in qs:
            kind = "全量" if q.is_full else "前十"
            for h in q.holdings:
                w.writerow([q.label, q.as_of, kind, h.rank, h.code, h.name,
                            f"{h.weight_pct:.2f}", h.shares_wan, h.value_wan_rmb])
            if not q.holdings:
                w.writerow([q.label, q.as_of, "", "(无披露)", "", "", "", ""])
    print(f"{sum(len(q.holdings) for q in qs)} 行 -> {out}")


if __name__ == "__main__":
    main()
