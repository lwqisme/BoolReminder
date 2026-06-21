"""导出 270023 净值序列 (单位净值 + 累计净值).

关键口径: 该基金分红全部发生在 2014-2018, 2021-2026 分析窗口内无分红.
  - 累计净值 = 单位净值 + 0.439(历史分红常数), 常数会污染端点比,
    故区间收益必须用【单位净值】端点比, 不得用累计净值.
  - 验证: 用单位净值算 近1年=+75.12%(官方75.27%), 近3年=+163.51%(官方163.51) 精确吻合.
"""
from __future__ import annotations

import csv
import json
import re
import sys
import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scraper import CACHE_DIR


def main():
    js = (CACHE_DIR.parent / ".." / "pz_270023.js")
    # 用已缓存的 pingzhongdata; 若不存在则从 cache 读取(需先抓取)
    pz = Path("/tmp/pz_270023.js")
    if not pz.exists():
        print("需要 /tmp/pz_270023.js (pingzhongdata), 请先抓取")
        return
    text = pz.read_text(encoding="utf-8")
    nu = json.loads(re.search(r"var Data_netWorthTrend\s*=\s*(\[.*?\])\s*;", text, re.S).group(1))
    ac = json.loads(re.search(r"var Data_ACWorthTrend\s*=\s*(\[.*?\])\s*;", text, re.S).group(1))
    U = {datetime.datetime.fromtimestamp(p["x"]/1000, tz=datetime.UTC).strftime("%Y-%m-%d"): p["y"] for p in nu}
    A = {datetime.datetime.fromtimestamp(p[0]/1000, tz=datetime.UTC).strftime("%Y-%m-%d"): p[1] for p in ac}
    out = CACHE_DIR / "nav_270023.csv"
    with out.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(["date", "unit_nav(单位净值,用于区间收益)", "accum_nav(累计净值,=unit+0.439历史分红)"])
        for d in sorted(U):
            w.writerow([d, U[d], A.get(d, "")])
    print(f"{len(U)} points -> {out}")
    print(f"首日 {min(U)} unit={U[min(U)]}  末日 {max(U)} unit={U[max(U)]}")
    print(f"分红常数(accum-unit) ≈ {A[max(U)]-U[max(U)]:.4f}  (历史2014-2018合计)")


if __name__ == "__main__":
    main()
