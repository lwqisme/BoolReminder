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
