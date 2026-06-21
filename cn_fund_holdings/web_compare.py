"""270023 (广发全球精选 QDII) vs QQQ 全面对比 —— 数据组装层.

聚合四类已有数据, 供 /cn-fund 页面与 /api/cn-fund/series 使用:
  - 270023 单位净值: cn_fund_holdings/cache/nav_270023.csv (2010-2026, 区间收益用单位净值)
  - 270023 持仓:     cn_fund_holdings/holdings_270023.json (21季, 10份全量半年报/年报 + 11份季报前十)
  - QQQ 前复权价:    data/longbridge_daily_candles/QQQ.US.json (2004-2026)
  - QQQ N-PORT 持仓: qq_index_fund.store (季度全量 ~102 只)
  - USD/CNY 汇率:    frankfurter (ECB) 带本地缓存

口径要点 (与 analyze/rolling_lead 一致):
  - RMB 口径: 270023 本就 RMB 计价; QQQ 用 USD价 * USDCNY 折 RMB.
  - USD 口径 (剔汇): 270023 RMB / USDCNY 折回 USD, 纯选股 alpha.
  - 逐日以 QQQ 美股交易日为基准, 基金净值/FX 前向填充对齐 (避免中美日历交集推后端点).
"""
from __future__ import annotations

import csv
import datetime
import json
import logging
import os
from collections import defaultdict
from pathlib import Path
from typing import Optional

logger = logging.getLogger(__name__)

_CN_DIR = Path(__file__).resolve().parent
_REPO = _CN_DIR.parent
NAV_CSV = _CN_DIR / "cache" / "nav_270023.csv"
HOLDINGS_JSON = _CN_DIR / "holdings_270023.json"
QQQ_JSON = _REPO / "data" / "longbridge_daily_candles" / "QQQ.US.json"
FX_CACHE = _CN_DIR / "cache" / "usdcny.json"

UA = "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36"

# 行业/市场分类 (粗分, 用于敞口归因; 与 analyze.py 同步)
SEMI = {"NVDA", "AMD", "TSM", "ASML", "AVGO", "INTC", "QCOM", "TXN", "MU",
        "AMAT", "KLAC", "NXPI", "MRVL", "LRCX", "ADI", "ON", "GFS", "SYNA"}
US_MEGACAP_TECH = {"AAPL", "MSFT", "GOOG", "GOOGL", "META", "AMZN", "NFLX", "TSLA"}
SW_US_TECH = {"NOW", "CRM", "PLTR", "SNOW", "PANW", "CRWD", "INTU", "ADBE",
              "ISRG", "CDNS", "SNPS", "ANET", "FTNT", "ABNB", "MDB", "DDOG",
              "NET", "ZS", "OKTA", "TEAM", "BILL"}
SECTOR_ORDER = ["半导体", "美股MegaCap科技", "美股SaaS/软件", "中港股", "A股", "其他美股"]
SECTOR_COLORS = {
    "半导体": "#b8412e",
    "美股MegaCap科技": "#a8761c",
    "美股SaaS/软件": "#2d6a4f",
    "中港股": "#3a6ea5",
    "A股": "#7a5c8f",
    "其他美股": "#8a8170",
}

# 关键个股轨迹
KEY_STOCKS = ["NVDA", "TSLA", "GOOG", "META", "AAPL", "MSFT", "ASML", "TSM", "AVGO"]

WINDOWS = [
    ("5年", "2021-06-16", "2026-06-16"),
    ("3年", "2023-06-17", "2026-06-16"),
    ("2年", "2024-06-16", "2026-06-16"),
    ("1年", "2025-06-16", "2026-06-16"),
    ("YTD", "2025-12-31", "2026-06-16"),
]


def qlabel(q: dict) -> str:
    return f"{q['year']}Q{q['quarter']}"


def classify(code: str) -> str:
    if code in SEMI:
        return "半导体"
    if code in US_MEGACAP_TECH:
        return "美股MegaCap科技"
    if code in SW_US_TECH:
        return "美股SaaS/软件"
    if code.isdigit():
        # 6位=A股(60/00/30开头), 5位=港股(00700/03690)
        if len(code) == 5:
            return "中港股"
        return "A股"
    if code in US_MEGACAP_TECH:
        return "美股MegaCap科技"
    return "其他美股"


# ---------------- 数据加载 ----------------


def _load_nav() -> dict[str, float]:
    """{date: unit_nav} 单位净值."""
    if not NAV_CSV.exists():
        return {}
    out: dict[str, float] = {}
    with open(NAV_CSV, encoding="utf-8-sig") as fh:
        for row in csv.reader(fh):
            if not row or row[0] in ("date", "") or row[0].startswith("﻿"):
                continue
            try:
                out[row[0]] = float(row[1])
            except (ValueError, IndexError):
                continue
    return out


def _load_qqq() -> dict[str, float]:
    """{date: close} QQQ 前复权收盘."""
    if not QQQ_JSON.exists():
        return {}
    data = json.load(open(QQQ_JSON, encoding="utf-8"))
    return {c["date"]: float(c["close"]) for c in data.get("candles", [])}


def load_fx() -> dict[str, float]:
    """{date: usdcny} 优先本地缓存, 缺则抓 frankfurter."""
    if FX_CACHE.exists():
        try:
            return json.load(open(FX_CACHE, encoding="utf-8"))
        except json.JSONDecodeError:
            pass
    # 抓 2010-01-01 ~ 今天
    today = datetime.date.today().isoformat()
    url = f"https://api.frankfurter.app/2010-01-01..{today}?from=USD&to=CNY"
    try:
        import requests
        r = requests.get(url, headers={"User-Agent": UA}, timeout=20)
        r.raise_for_status()
        data = r.json()
        out = {d: v["CNY"] for d, v in data.get("rates", {}).items()}
        if out:
            FX_CACHE.parent.mkdir(parents=True, exist_ok=True)
            json.dump(out, open(FX_CACHE, "w", encoding="utf-8"), ensure_ascii=False)
            logger.info("Fetched USD/CNY %d days -> %s", len(out), FX_CACHE)
        return out
    except Exception as exc:
        logger.warning("frankfurter fx fetch failed: %s", exc)
        return {}


def _load_holdings() -> list[dict]:
    if not HOLDINGS_JSON.exists():
        return []
    return json.load(open(HOLDINGS_JSON, encoding="utf-8"))


def _at(d: str, m: dict[str, float]):
    """最近已知 ≤ d 的值 (前向填充)."""
    ks = [k for k in m if k <= d]
    return m[max(ks)] if ks else None


# ---------------- 净值曲线 ----------------


def build_nav_series(window: Optional[tuple[str, str, str]] = None) -> dict:
    """构建 270023 vs QQQ 归一化净值曲线 + 滚动领先统计.

    window: (label, start, end); None 则用全区间 (NAV 首日 ~ QQQ 末日).
    """
    U = _load_nav()
    Q = _load_qqq()
    fx = load_fx()
    if not U or not Q:
        return {}

    if window:
        _, start, end = window
    else:
        start = max(min(U), min(Q))
        end = min(max(U), max(Q))

    # 以 QQQ 美股交易日为基准
    days = sorted(d for d in Q if start <= d <= end)
    if not days:
        return {}

    u0 = _at(days[0], U)
    f0 = _at(days[0], fx)
    q0 = _at(days[0], Q)
    if not (u0 and f0 and q0):
        return {}

    dates, nav_rmb, qqq_rmb, nav_usd, qqq_usd = [], [], [], [], []
    lead_rmb = lead_usd = 0
    for d in days:
        u = _at(d, U)
        f = _at(d, fx)
        q = Q[d]
        if not (u and f):
            continue
        nr = u / u0
        qr = (q / q0) * (f / f0)
        nu = (u / u0) / (f / f0)
        qu = q / q0
        dates.append(d)
        nav_rmb.append(round(nr, 6))
        qqq_rmb.append(round(qr, 6))
        nav_usd.append(round(nu, 6))
        qqq_usd.append(round(qu, 6))
        lead_rmb += nr >= qr
        lead_usd += nu >= qu

    n = len(dates)
    return {
        "label": window[0] if window else "全部",
        "start": dates[0],
        "end": dates[-1],
        "n_days": n,
        "dates": dates,
        "nav_rmb": nav_rmb,
        "qqq_rmb": qqq_rmb,
        "nav_usd": nav_usd,
        "qqq_usd": qqq_usd,
        "lead_rmb_pct": round(lead_rmb / n * 100, 1) if n else 0,
        "lead_usd_pct": round(lead_usd / n * 100, 1) if n else 0,
        "ret_nav_rmb": round((nav_rmb[-1] - 1) * 100, 1),
        "ret_qqq_rmb": round((qqq_rmb[-1] - 1) * 100, 1),
        "ret_nav_usd": round((nav_usd[-1] - 1) * 100, 1),
        "ret_qqq_usd": round((qqq_usd[-1] - 1) * 100, 1),
        "alpha_rmb": round((nav_rmb[-1] - qqq_rmb[-1]) * 100, 1),
        "alpha_usd": round((nav_usd[-1] - qqq_usd[-1]) * 100, 1),
    }


def build_windows_table() -> list[dict]:
    """各窗口的区间收益 + 滚动领先占比 (表格用)."""
    rows = []
    for label, s, e in WINDOWS:
        r = build_nav_series((label, s, e))
        if not r:
            continue
        rows.append(r)
    return rows


# ---------------- 持仓重叠度 ----------------


def _load_qqq_top(as_of: str, n: int = 10) -> dict[str, float]:
    """QQQ N-PORT 截止 as_of 最近一份的前 n 大权重 {ticker: pct}."""
    from qq_index_fund import store
    d = datetime.date.fromisoformat(as_of)
    try:
        sn = store.load_nport_snapshot(d)
    except Exception:
        return {}
    if not sn or not sn.get("holdings"):
        return {}
    def wp(h):
        return h.get("pct_val") or h.get("weight_pct") or 0.0
    def tk(h):
        return h.get("ticker") or h.get("symbol")
    hs = sorted(sn["holdings"], key=lambda h: -wp(h))[:n]
    out: dict[str, float] = {}
    for h in hs:
        t = tk(h)
        if t:
            out[t] = max(out.get(t, 0), wp(h))
    return out


def build_overlap_series() -> dict:
    """逐季 270023 前十 vs QQQ 前十重叠度演变."""
    qs = _load_holdings()
    labels, overlap_n, overlap_w, my_tot, only_qqq = [], [], [], [], []
    table_rows = []
    for q in qs:
        if not q.get("holdings"):
            continue
        my = {h["code"]: h["weight_pct"] for h in q["holdings"][:10]}
        qqq = _load_qqq_top(q["as_of"], 10)
        common = set(my) & set(qqq)
        ow = sum(my[c] for c in common)
        tot = sum(my.values())
        labels.append(qlabel(q))
        overlap_n.append(len(common))
        overlap_w.append(round(ow, 1))
        my_tot.append(round(tot, 1))
        only = sorted(set(qqq) - set(my))[:6]
        only_qqq.append(", ".join(only) if only else "—")
        table_rows.append({
            "label": qlabel(q),
            "as_of": q["as_of"],
            "is_full": q["quarter"] in (2, 4),
            "my_top10_tot": round(tot, 1),
            "overlap_n": len(common),
            "overlap_w": round(ow, 1),
            "only_qqq": ", ".join(only) if only else "—",
        })
    return {
        "labels": labels,
        "overlap_n": overlap_n,
        "overlap_w": overlap_w,
        "my_tot": my_tot,
        "table": table_rows,
    }


# ---------------- 行业敞口 (用全量持仓) ----------------


def build_sector_series() -> dict:
    """逐季行业敞口演变. 全量报告用全持仓, 季报用前十."""
    qs = _load_holdings()
    labels = []
    by_sector: dict[str, list[float]] = {s: [] for s in SECTOR_ORDER}
    for q in qs:
        if not q.get("holdings"):
            continue
        labels.append(qlabel(q))
        s = defaultdict(float)
        for h in q["holdings"]:
            s[classify(h["code"])] += h["weight_pct"]
        for sec in SECTOR_ORDER:
            by_sector[sec].append(round(s.get(sec, 0), 1))
    return {
        "labels": labels,
        "sectors": SECTOR_ORDER,
        "colors": [SECTOR_COLORS[s] for s in SECTOR_ORDER],
        "series": [{"name": s, "color": SECTOR_COLORS[s], "y": by_sector[s]} for s in SECTOR_ORDER],
    }


def build_key_stock_series() -> dict:
    """关键个股权重轨迹."""
    qs = _load_holdings()
    labels = []
    series = []
    for k in KEY_STOCKS:
        series.append({"name": k, "y": []})
    for q in qs:
        if not q.get("holdings"):
            continue
        labels.append(qlabel(q))
        m = {h["code"]: h["weight_pct"] for h in q["holdings"]}
        for i, k in enumerate(KEY_STOCKS):
            series[i]["y"].append(round(m.get(k, 0), 2))
    return {"labels": labels, "series": series, "keys": KEY_STOCKS}


# ---------------- 最新全量持仓表 ----------------


def build_latest_full_holdings() -> dict:
    """最新一份全量报告 (半年报/年报) 的全持仓表 + 行业分布."""
    qs = _load_holdings()
    full = [q for q in qs if q["quarter"] in (2, 4) and q.get("holdings")]
    if not full:
        return {}
    latest = max(full, key=lambda q: (q["year"], q["quarter"]))
    rows = []
    sec_dist = defaultdict(lambda: {"count": 0, "weight": 0.0})
    for h in latest["holdings"]:
        sec = classify(h["code"])
        sec_dist[sec]["count"] += 1
        sec_dist[sec]["weight"] += h["weight_pct"]
        rows.append({
            "rank": h["rank"],
            "code": h["code"],
            "name": h["name"],
            "weight": round(h["weight_pct"], 2),
            "shares_wan": h.get("shares_wan"),
            "value_wan": h.get("value_wan_rmb"),
            "sector": sec,
        })
    sector_rows = sorted(
        [{"sector": s, "count": v["count"], "weight": round(v["weight"], 1)}
         for s, v in sec_dist.items()],
        key=lambda x: -x["weight"],
    )
    return {
        "label": qlabel(latest),
        "as_of": latest["as_of"],
        "n": len(rows),
        "total_weight": round(sum(h["weight_pct"] for h in latest["holdings"]), 1),
        "rows": rows,
        "sector_dist": sector_rows,
    }


# ---------------- 页面上下文 ----------------


def page_context() -> dict:
    """服务端渲染用: 窗口表 + 最新全量持仓 + 概要."""
    windows = build_windows_table()
    latest = build_latest_full_holdings()
    w5 = next((w for w in windows if w["label"] == "5年"), {})
    w3 = next((w for w in windows if w["label"] == "3年"), {})
    w1 = next((w for w in windows if w["label"] == "1年"), {})
    ytd = next((w for w in windows if w["label"] == "YTD"), {})
    qs = _load_holdings()
    full_count = sum(1 for q in qs if q["quarter"] in (2, 4) and q.get("holdings"))
    return {
        "windows": windows,
        "w5": w5,
        "w3": w3,
        "w1": w1,
        "ytd": ytd,
        "latest_full": latest,
        "total_quarters": len(qs),
        "full_reports": full_count,
        "nav_range": f"{min(_load_nav())} ~ {max(_load_nav())}" if _load_nav() else "—",
    }


def series_payload() -> dict:
    """API 用: 全部图表数据."""
    return {
        "nav_all": build_nav_series(None),
        "overlap": build_overlap_series(),
        "sector": build_sector_series(),
        "key_stocks": build_key_stock_series(),
        "windows": build_windows_table(),
    }


if __name__ == "__main__":
    import pprint
    ctx = page_context()
    print("windows:", len(ctx["windows"]))
    for w in ctx["windows"]:
        print(f"  {w['label']:5} {w['n_days']:4}天  领先RMB{w['lead_rmb_pct']:5.1f}%/USD{w['lead_usd_pct']:5.1f}%  "
              f"alpha RMB{w['alpha_rmb']:+6.1f}pp/USD{w['alpha_usd']:+6.1f}pp")
    lf = ctx["latest_full"]
    print(f"\n最新全量: {lf['label']} {lf['as_of']}  {lf['n']}只 合计{lf['total_weight']}%")
    print("行业分布:", lf["sector_dist"])
