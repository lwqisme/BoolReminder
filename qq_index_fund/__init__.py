"""
QQ 指数基金 —— 成分股抓取模块

抓取 QQQ（Invesco NASDAQ-100 ETF）的成分股 Top-N，用于构建自定义
"QQ 指数基金"组合。本模块只负责"抓取 + 存储"，刻意与项目原有的
Strategy Lab / drawdown 逻辑解耦，不引入 longbridge / bs4 等依赖。

数据源
------
主源：https://stockanalysis.com/etf/qqq/holdings/
  - 服务端渲染 HTML 表格，无需 JS 执行（已在项目机器上验证可达 HTTP 200）。
  - 默认每页 25 条，取 Top25 足以覆盖 Top10 基金并有缓冲。
  - 权重为当前市值权重，每日漂移；成分股本身每年 12 月随 Nasdaq-100 重组。

备选/不可用源（已在项目机器上实测）：
  - Invesco 官方 CSV 端点：HTTP 406（WAF 封锁），不可用。
  - en.wikipedia.org/wiki/Nasdaq-100：仅 IPv6 路由且不通（curl 000），不可用。
  - nasdaq.com holdings：HTTP 200 但数据由 JS 注入，HTML 内无内联成分，较脆。
  - Yahoo/yfinance：HTTP 403 被墙，不可用。

关于"回测某段时间"与"精准跟踪"
------------------------------
两层抓取互补：
  - 当前层（fetch_holdings，stockanalysis）：每日快照 Top25，捕捉季内漂移与
    临时增删（如新股上市当日纳入）。从现在起定期累积。
  - 历史层（edgar_nport，SEC N-PORT-P）：季度末完整持仓，2019-11 起可回溯，
    用于回测任意历史时间段。QQQ 的 N-PORT-P 是季度申报（非月度）。
"""

from .fetch_holdings import (
    Holding,
    fetch_qqq_holdings,
)
from .edgar_nport import (
    NportFiling,
    NportHolding,
    fetch_nport_filings,
    fetch_nport_snapshot,
    build_ticker_index,
    parse_nport_holdings,
)
from .store import (
    save_snapshot,
    build_portfolios,
    save_portfolios,
    snapshot_path,
    save_nport_snapshot,
    nport_snapshot_path,
    load_latest_portfolios,
    load_latest_daily_snapshot,
    list_nport_snapshots,
    load_nport_snapshot,
    list_daily_snapshots,
    load_daily_snapshot,
)
from .backtest import (
    run_backtest,
    BacktestResult,
    RebalanceEvent,
    derive_top10_weights,
)

__all__ = [
    "Holding",
    "fetch_qqq_holdings",
    "save_snapshot",
    "build_portfolios",
    "save_portfolios",
    "snapshot_path",
    "NportFiling",
    "NportHolding",
    "fetch_nport_filings",
    "fetch_nport_snapshot",
    "build_ticker_index",
    "parse_nport_holdings",
    "save_nport_snapshot",
    "nport_snapshot_path",
    "load_latest_portfolios",
    "load_latest_daily_snapshot",
    "list_nport_snapshots",
    "load_nport_snapshot",
    "list_daily_snapshots",
    "load_daily_snapshot",
    "run_backtest",
    "BacktestResult",
    "RebalanceEvent",
    "derive_top10_weights",
]
