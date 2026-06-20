"""
QQ 指数基金抓取 CLI。

用法:
    python -m qq_index_fund                  # 抓取 Top25，存今日快照 + 派生 Top10 组合
    python -m qq_index_fund --top 50         # 抓取更多（受页面分页 25 限制）
    python -m qq_index_fund --dry-run        # 只抓取打印，不落盘
"""

from __future__ import annotations

import argparse
import logging
import sys

from .fetch_holdings import fetch_qqq_holdings
from .store import (
    build_portfolios, save_portfolios, save_snapshot,
    save_nport_snapshot, save_prospectus_snapshot,
)
from .edgar_nport import (
    fetch_nport_filings,
    fetch_nport_snapshot,
    build_ticker_index,
)
from .prospectus import (
    PROSPECTUS_FILINGS,
    fetch_prospectus_snapshot,
    verify_prospectus_holdings,
)
from .backtest import load_nport_snapshots_raw

logger = logging.getLogger(__name__)


def _run_current(args) -> int:
    """抓取 stockanalysis 当前快照。"""
    holdings = fetch_qqq_holdings(top_n=args.top)

    print(f"\n=== QQQ Holdings (top {len(holdings)}) ===")
    print(f"{'#':>2} {'Ticker':6} {'Weight':>7}  {'Symbol':10} Name")
    for h in holdings:
        print(f"{h.rank:>2} {h.ticker:6} {h.weight_pct:>6.2f}%  {h.symbol:10} {h.name}")

    portfolios = build_portfolios(holdings)
    print("\n=== qq_top10_equal (等权) ===")
    for t in portfolios["qq_top10_equal"]:
        print(f"  {t['symbol']:10} {t['weight']:>6.2f}%  {t['name']}")
    print("\n=== qq_top10_weighted (市值加权, 归一化到 100%) ===")
    for t in portfolios["qq_top10_weighted"]:
        print(f"  {t['symbol']:10} {t['weight']:>6.2f}%  {t['name']}")

    if args.dry_run:
        print("\n[dry-run] 未落盘。")
        return 0

    snap = save_snapshot(holdings)
    ports = save_portfolios(portfolios)
    print(f"\n快照: {snap}")
    print(f"组合: {ports[0]}")
    return 0


def _run_nport(args) -> int:
    """抓取 SEC EDGAR N-PORT 历史快照。"""
    filings = fetch_nport_filings()
    if args.nport_latest:
        filings = filings[-1:]
        print(f"\n=== 抓取最新 N-PORT: {filings[0].filing_date} ===")
    else:
        print(f"\n=== 回溯全部 {len(filings)} 份 N-PORT ({filings[0].filing_date}..{filings[-1].filing_date}) ===")

    ticker_index = build_ticker_index()
    paths = []
    for f in filings:
        snapshot = fetch_nport_snapshot(f, ticker_index)
        top = sorted(snapshot["holdings"], key=lambda h: h.get("val_usd", 0), reverse=True)[:10]
        print(f"\n--- {f.filing_date} (报告期 {snapshot['repd_date']}) "
              f"{snapshot['count']} 持仓, {snapshot['ticker_resolved']} 个已解析 ticker ---")
        print(f"{'#':>2} {'Ticker':6} {'pct%':>6} {'valUSD':>14}  Name")
        for h in top:
            print(f"{h['rank']:>2} {h['ticker'] or '??':6} {h['pct_val']:>6.2f} "
                  f"{h['val_usd']:>14.0f}  {h['name'][:40]}")
        if not args.dry_run:
            paths.append(save_nport_snapshot(snapshot))
    if args.dry_run:
        print("\n[dry-run] 未落盘。")
    elif paths:
        print(f"\n已落盘 {len(paths)} 份 N-PORT 快照 -> data/qq_index_fund/nport/")
    return 0


def _run_prospectus(args) -> int:
    """抓取 SEC 485BPOS 招募书历史快照（1999-2018 年度，补 N-PORT 之前）。"""
    print(f"\n=== 回溯 {len(PROSPECTUS_FILINGS)} 份招募书 (485BPOS) ===")
    ticker_index = build_ticker_index()
    # 双重验证：用已有 N-PORT 快照交叉核对
    nport_snaps = load_nport_snapshots_raw()
    paths = []
    saved = 0
    for acc, doc, fdate in PROSPECTUS_FILINGS:
        try:
            snapshot = fetch_prospectus_snapshot(acc, doc, fdate, ticker_index)
            # 只落盘解析出 ≥15 持仓的（过滤掉解析失败的早期/特殊格式）
            if snapshot["count"] < 15:
                print(f"  {fdate} (repd={snapshot['repd_date']}): 仅 {snapshot['count']} 持仓，跳过（疑似格式不兼容）")
                continue
            top = sorted(snapshot["holdings"], key=lambda h: h.get("value", 0), reverse=True)[:10]
            verify = verify_prospectus_holdings(snapshot, nport_snaps)
            print(f"  {fdate} (repd={snapshot['repd_date']}): {snapshot['count']} 持仓, "
                  f"{snapshot['ticker_resolved']} 已解析 | 验证: {verify['notes'][0]}")
            print(f"     Top3: {', '.join(h['ticker'] or '??' for h in top[:3])}")
            if not args.dry_run:
                save_prospectus_snapshot(snapshot)
                saved += 1
        except Exception as exc:
            print(f"  {fdate}: ERROR {exc}")
    if args.dry_run:
        print("\n[dry-run] 未落盘。")
    else:
        print(f"\n已落盘 {saved} 份招募书快照 -> data/qq_index_fund/prospectus/")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m qq_index_fund",
        description="抓取 QQQ 成分股并构建 QQ 指数基金。",
    )
    parser.add_argument(
        "--top", type=int, default=25,
        help="[当前快照] 抓取前 N 只成分股（默认 25）。",
    )
    parser.add_argument(
        "--dry-run", action="store_true",
        help="只抓取并打印，不写盘。",
    )
    parser.add_argument(
        "--nport", action="store_true",
        help="抓取 SEC EDGAR N-PORT 历史快照（月度，可回测）。",
    )
    parser.add_argument(
        "--nport-latest", action="store_true",
        help="仅抓最新一份 N-PORT（配合 --nport）。",
    )
    parser.add_argument(
        "--prospectus", action="store_true",
        help="抓取 SEC 485BPOS 招募书历史快照（1999-2018 年度，补 N-PORT 之前）。",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true",
        help="输出调试日志。",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )

    if args.nport or args.nport_latest:
        if args.nport_latest:
            args.nport = True
        return _run_nport(args)
    if args.prospectus:
        return _run_prospectus(args)
    return _run_current(args)


if __name__ == "__main__":
    sys.exit(main())
