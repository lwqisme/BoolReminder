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

关于"回测某段时间"与"精准跟踪"
------------------------------
本模块每次抓取都会落盘一条带 as_of_date 的完整快照（Top25）。回测需要
"某时点成分"序列，因此正确的做法是从现在起**定期累积快照**，未来任意
被快照覆盖的日期都能重算 Top10。注意：
  - 本模块无法重建"今天之前"的历史成分（上述历史/变更源在该机器不可达）。
  - QQQ 跟踪 Nasdaq-100；中途临时增删（如新股上市当日纳入）只有靠高频
    快照才能捕捉——这正是本模块为定时调度而设计的原因。
"""

from .fetch_holdings import (
    Holding,
    fetch_qqq_holdings,
)
from .store import (
    save_snapshot,
    build_portfolios,
    save_portfolios,
    snapshot_path,
)

__all__ = [
    "Holding",
    "fetch_qqq_holdings",
    "save_snapshot",
    "build_portfolios",
    "save_portfolios",
    "snapshot_path",
]
