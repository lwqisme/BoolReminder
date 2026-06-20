"""
QQ 指数基金 —— 快照存储与组合构建

把抓取到的成分股落盘为带日期的快照（用于未来回测重算 Top10），并派生
两种加权模式的组合：
  - qq_top10_equal   : Top10 等权（各 10%）
  - qq_top10_weighted: Top10 按市值权重再归一化到 100%

组合结构与项目 PortfolioTarget 一致（{symbol, weight, name}），便于将来
接入 Strategy Lab 或独立页面；但本模块不依赖 drawdown，保持解耦。

存储约定（文件系统 JSON，与项目 data/ 惯例一致）：
  data/qq_index_fund/holdings/YYYY-MM-DD.json   # 每日快照（幂等覆盖）
  data/qq_index_fund/portfolios/latest.json     # 最新派生组合
  data/qq_index_fund/portfolios/YYYY-MM-DD.json # 按日归档的派生组合
"""

from __future__ import annotations

import json
import logging
import os
from datetime import date, datetime, timezone
from typing import Optional

from .fetch_holdings import Holding


def _read_json(path: str) -> Optional[dict]:
    """安全读取 JSON 文件；不存在或损坏返回 None。"""
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except (FileNotFoundError, json.JSONDecodeError):
        return None

logger = logging.getLogger(__name__)

# 项目数据根目录（data/ 与本模块的相对位置固定）。
_DEFAULT_DATA_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
    "data",
    "qq_index_fund",
)

TOP_N_FOR_FUND = 10  # "QQ 指数基金"取 Top10


def _today_str(as_of: Optional[date]) -> str:
    return (as_of or date.today()).isoformat()


def snapshot_path(as_of: Optional[date] = None, data_dir: Optional[str] = None) -> str:
    """返回某日快照文件路径。"""
    base = data_dir or _DEFAULT_DATA_DIR
    return os.path.join(base, "holdings", f"{_today_str(as_of)}.json")


def save_snapshot(
    holdings: list[Holding],
    *,
    as_of: Optional[date] = None,
    source: str = "stockanalysis.com",
    data_dir: Optional[str] = None,
) -> str:
    """
    把完整成分股快照落盘（按日幂等覆盖）。

    存全量（抓到的 Top25），而非仅 Top10——这样未来任意被快照覆盖的
    日期都能重算 Top10，满足"回测某段时间"的需求。

    Returns:
        写入的快照文件路径。
    """
    path = snapshot_path(as_of, data_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    payload = {
        "fund": "QQQ",
        "as_of_date": _today_str(as_of),
        "source": source,
        "count": len(holdings),
        "fetched_at": datetime.now(timezone.utc).isoformat(),
        "holdings": [h.to_dict() for h in holdings],
    }
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(payload, fh, ensure_ascii=False, indent=2)
    logger.info("Saved QQQ snapshot -> %s (%d holdings)", path, len(holdings))
    return path


def build_portfolios(
    holdings: list[Holding],
    *,
    top_n: int = TOP_N_FOR_FUND,
) -> dict[str, list[dict]]:
    """
    由成分股派生两种加权组合。

    Args:
        holdings: 抓取到的成分股（取前 top_n 只构建基金）。
        top_n: 基金成分数，默认 10。

    Returns:
        {
          "qq_top10_equal":    [{symbol, weight, name}, ...],   # 各 10%
          "qq_top10_weighted": [{symbol, weight, name}, ...],   # 原始权重再归一化到 100
        }
    """
    top = holdings[:top_n]
    if not top:
        raise ValueError("No holdings to build portfolios from.")

    equal = [
        {"symbol": h.symbol, "weight": round(100.0 / top_n, 4), "name": h.name}
        for h in top
    ]

    raw_sum = sum(h.weight_pct for h in top)
    if raw_sum <= 0:
        raise ValueError("Sum of raw weights is non-positive; cannot normalize.")
    weighted = [
        {
            "symbol": h.symbol,
            "weight": round(h.weight_pct * 100.0 / raw_sum, 4),
            "name": h.name,
        }
        for h in top
    ]

    return {"qq_top10_equal": equal, "qq_top10_weighted": weighted}


def nport_snapshot_path(repd_date: str, data_dir: Optional[str] = None) -> str:
    """返回某月度 N-PORT 快照文件路径（按报告期 repd_date 命名）。"""
    base = data_dir or _DEFAULT_DATA_DIR
    return os.path.join(base, "nport", f"{repd_date}.json")


def save_nport_snapshot(
    snapshot: dict,
    *,
    data_dir: Optional[str] = None,
) -> str:
    """
    落盘一份 N-PORT 历史快照（按报告期 repd_date 幂等覆盖）。

    与每日 stockanalysis 快照分开存（nport/ 子目录），互不干扰。
    用于回测任意历史时间段的成分股序列。

    Returns:
        写入的快照文件路径。
    """
    repd = snapshot.get("repd_date") or snapshot.get("filing_date")
    if not repd:
        raise ValueError("N-PORT snapshot missing repd_date/filing_date.")
    path = nport_snapshot_path(repd, data_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, ensure_ascii=False, indent=2)
    logger.info(
        "Saved N-PORT snapshot -> %s (repd=%s, %d holdings)",
        path, repd, snapshot.get("count", 0),
    )
    return path


def save_portfolios(
    portfolios: dict[str, list[dict]],
    *,
    as_of: Optional[date] = None,
    data_dir: Optional[str] = None,
) -> list[str]:
    """
    落盘派生组合：latest.json + 按日归档。

    Returns:
        写入的文件路径列表。
    """
    base = data_dir or _DEFAULT_DATA_DIR
    port_dir = os.path.join(base, "portfolios")
    os.makedirs(port_dir, exist_ok=True)
    day = _today_str(as_of)
    payload = {
        "as_of_date": day,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "portfolios": portfolios,
    }
    paths = [
        os.path.join(port_dir, "latest.json"),
        os.path.join(port_dir, f"{day}.json"),
    ]
    for path in paths:
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, ensure_ascii=False, indent=2)
    logger.info("Saved QQ-index portfolios -> %s", paths[0])
    return paths


# ---------------- 读取（供 web 页面使用） ----------------


def load_latest_portfolios(data_dir: Optional[str] = None) -> Optional[dict]:
    """读取最新派生组合快照（latest.json）。无则返回 None。"""
    base = data_dir or _DEFAULT_DATA_DIR
    return _read_json(os.path.join(base, "portfolios", "latest.json"))


def load_latest_daily_snapshot(data_dir: Optional[str] = None) -> Optional[dict]:
    """读取最近一份 stockanalysis 每日快照（按文件名日期降序）。"""
    base = data_dir or _DEFAULT_DATA_DIR
    hdir = os.path.join(base, "holdings")
    if not os.path.isdir(hdir):
        return None
    files = sorted(
        (f for f in os.listdir(hdir) if f.endswith(".json")),
        reverse=True,
    )
    if not files:
        return None
    return _read_json(os.path.join(hdir, files[0]))


def _nport_dir(data_dir: Optional[str] = None) -> str:
    return os.path.join(data_dir or _DEFAULT_DATA_DIR, "nport")


def list_nport_snapshots(data_dir: Optional[str] = None) -> list[dict]:
    """
    列出所有 N-PORT 历史快照的摘要（按报告期降序）。

    每条含 repd_date / filing_date / count / ticker_resolved / top10（前10 ticker）。
    """
    ndir = _nport_dir(data_dir)
    if not os.path.isdir(ndir):
        return []
    out: list[dict] = []
    for name in os.listdir(ndir):
        if not name.endswith(".json"):
            continue
        snap = _read_json(os.path.join(ndir, name))
        if not snap:
            continue
        holdings = snap.get("holdings", [])
        holdings_sorted = sorted(holdings, key=lambda h: h.get("val_usd", 0), reverse=True)
        top10 = [
            {
                "rank": h.get("rank"),
                "ticker": h.get("ticker"),
                "symbol": h.get("symbol"),
                "name": h.get("name"),
                "pct_val": h.get("pct_val"),
            }
            for h in holdings_sorted[:10]
        ]
        out.append({
            "repd_date": snap.get("repd_date") or name[:-5],
            "filing_date": snap.get("filing_date"),
            "accession": snap.get("accession"),
            "count": snap.get("count"),
            "ticker_resolved": snap.get("ticker_resolved"),
            "top10": top10,
        })
    out.sort(key=lambda x: x["repd_date"], reverse=True)
    return out


def load_nport_snapshot(repd_date: str, data_dir: Optional[str] = None) -> Optional[dict]:
    """读取指定报告期的 N-PORT 完整快照。"""
    path = nport_snapshot_path(repd_date, data_dir)
    return _read_json(path)


# ---------------- 招募书（485BPOS）历史快照 ----------------


def prospectus_snapshot_path(repd_date: str, data_dir: Optional[str] = None) -> str:
    """招募书历史快照路径（按报告期命名，与 N-PORT 分目录存）。"""
    base = data_dir or _DEFAULT_DATA_DIR
    return os.path.join(base, "prospectus", f"{repd_date}.json")


def save_prospectus_snapshot(
    snapshot: dict,
    *,
    data_dir: Optional[str] = None,
) -> str:
    """落盘一份招募书历史快照（按报告期幂等覆盖）。"""
    repd = snapshot.get("repd_date") or snapshot.get("filing_date")
    if not repd:
        raise ValueError("Prospectus snapshot missing repd_date/filing_date.")
    path = prospectus_snapshot_path(repd, data_dir)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as fh:
        json.dump(snapshot, fh, ensure_ascii=False, indent=2)
    logger.info(
        "Saved prospectus snapshot -> %s (repd=%s, %d holdings)",
        path, repd, snapshot.get("count", 0),
    )
    return path


def load_prospectus_snapshots_raw(data_dir: Optional[str] = None) -> list[dict]:
    """读取全部招募书快照（含完整 holdings），按报告期升序。"""
    import os
    base = data_dir or _DEFAULT_DATA_DIR
    pdir = os.path.join(base, "prospectus")
    if not os.path.isdir(pdir):
        return []
    out: list[dict] = []
    for name in os.listdir(pdir):
        if not name.endswith(".json"):
            continue
        snap = _read_json(os.path.join(pdir, name))
        if snap and snap.get("repd_date") and snap.get("holdings"):
            out.append(snap)
    out.sort(key=lambda s: s["repd_date"])
    return out


def list_daily_snapshots(data_dir: Optional[str] = None) -> list[str]:
    """列出每日快照的日期（降序），供历史浏览。"""
    base = data_dir or _DEFAULT_DATA_DIR
    hdir = os.path.join(base, "holdings")
    if not os.path.isdir(hdir):
        return []
    return sorted(
        (f[:-5] for f in os.listdir(hdir) if f.endswith(".json")),
        reverse=True,
    )


def load_daily_snapshot(day: str, data_dir: Optional[str] = None) -> Optional[dict]:
    """读取指定日期的每日快照。"""
    base = data_dir or _DEFAULT_DATA_DIR
    return _read_json(os.path.join(base, "holdings", f"{day}.json"))
