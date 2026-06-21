"""270023 vs QQQ 滚动领先占比分析.

不只看端点收益 (端点被 2022 深坑拖累), 而是把两条净值曲线归一化到同一起点,
逐交易日比较谁更高, 统计 270023 领先 QQQ 的时间占比. 这才是"大部分时间领先"的量化.

两种口径:
  - RMB 口径: 投资者实际拿到的 (270023 本就 RMB 计价; QQQ 用 USD 价格 * USDCNY 换成 RMB)
  - USD 口径 (剔汇): 纯选股 alpha, 剔除汇率干扰
"""
from __future__ import annotations

import json
import re
import sys
import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))


def load_series():
    js = open("/tmp/pz_270023.js", encoding="utf-8").read()
    nu = json.loads(re.search(r"var Data_netWorthTrend\s*=\s*(\[.*?\])\s*;", js, re.S).group(1))
    U = {datetime.datetime.fromtimestamp(p["x"]/1000, tz=datetime.UTC).strftime("%Y-%m-%d"): p["y"] for p in nu}
    fx = json.load(open("/tmp/usdcny.json"))
    qq = json.load(open("/home/ubuntu/projects/BoolReminder/data/longbridge_daily_candles/QQQ.US.json"))
    Q = {c["date"]: c["close"] for c in qq["candles"]}
    return U, fx, Q


def at(d, m):
    ks = sorted(k for k in m if k <= d)
    return m[ks[-1]] if ks else None


def rolling_lead(start: str, end: str):
    U, fx, Q = load_series()
    # 以 QQQ 美股交易日为基准 (最全), 基金净值/FX 用前向填充对齐.
    # 否则中美交易日历交集会把起点推后, 端点收益算错.
    days = sorted(d for d in Q if start <= d <= end)
    u0 = at(days[0], U); f0 = at(days[0], fx); q0 = at(days[0], Q)
    lead_rmb = lead_usd = 0
    series = []
    for d in days:
        u = at(d, U); f = at(d, fx); q = Q[d]
        nav_rmb = u / u0                  # 270023 RMB 归一
        qqq_rmb = (q / q0) * (f / f0)     # QQQ RMB 归一
        nav_usd = (u / u0) / (f / f0)     # 270023 剔汇 USD 归一
        qqq_usd = q / q0                  # QQQ USD 归一
        lead_rmb += nav_rmb >= qqq_rmb
        lead_usd += nav_usd >= qqq_usd
        series.append((d, nav_rmb, qqq_rmb, nav_usd, qqq_usd))
    n = len(days)
    return {
        "n_days": n, "lead_rmb": lead_rmb, "lead_usd": lead_usd,
        "pct_rmb": lead_rmb / n, "pct_usd": lead_usd / n,
        "end_rmb": series[-1][1] - 1, "end_qqq_rmb": series[-1][2] - 1,
        "end_usd": series[-1][3] - 1, "end_qqq_usd": series[-1][4] - 1,
        "series": series,
    }


def main():
    print("逐日滚动领先占比 (270023 归一净值 >= QQQ 归一净值 的天数比例)\n")
    print(f"{'窗口':26}{'交易日':>7}{'RMB领先%':>10}{'USD领先%':>10}{'端点270023RMB':>14}{'端点QQQ RMB':>13}{'端点270023USD':>14}{'端点QQQ USD':>13}")
    windows = [
        ("5年", "2021-06-16", "2026-06-16"),
        ("3年", "2023-06-17", "2026-06-16"),
        ("2年", "2024-06-16", "2026-06-16"),
        ("1年", "2025-06-16", "2026-06-16"),
        ("YTD", "2025-12-31", "2026-06-16"),
    ]
    results = {}
    for label, s, e in windows:
        r = rolling_lead(s, e)
        results[label] = r
        print(f"{label+' '+s+'~'+e:26}{r['n_days']:>7}"
              f"{r['pct_rmb']:>9.1%}{r['pct_usd']:>10.1%}"
              f"{r['end_rmb']:>+13.1%}{r['end_qqq_rmb']:>+13.1%}"
              f"{r['end_usd']:>+13.1%}{r['end_qqq_usd']:>+13.1%}")

    # 5年窗口里, 找 270023 领先最久 / 落后最深的时段
    print("\n5年窗口: 270023 vs QQQ (USD口径, 剔汇) 逐月采样:")
    r5 = results["5年"]
    ser = r5["series"]
    # 每月第一交易日采样
    seen = set()
    print(f"{'日期':12}{'270023USD':>11}{'QQQ USD':>10}{'超额':>9}{'领先?':>6}")
    for d, nr, qr, nu, qu in ser:
        ym = d[:7]
        if ym in seen:
            continue
        seen.add(ym)
        lead = "✓" if nu >= qu else "✗"
        print(f"{d:12}{nu-1:>+10.1%}{qu-1:>+10.1%}{nu-qu:>+8.1%}{lead:>6}")

    # 找连续领先/落后最长区间
    print("\n5年窗口连续领先/落后统计 (USD口径):")
    cur_lead = cur_lag = 0; max_lead = max_lag = 0
    for d, nr, qr, nu, qu in ser:
        if nu >= qu:
            cur_lead += 1; cur_lag = 0
            max_lead = max(max_lead, cur_lead)
        else:
            cur_lag += 1; cur_lead = 0
            max_lag = max(max_lag, cur_lag)
    print(f"  最长连续领先: {max_lead} 交易日 (~{max_lead/252:.1f}年)")
    print(f"  最长连续落后: {max_lag} 交易日 (~{max_lag/252:.1f}年)")


if __name__ == "__main__":
    main()
