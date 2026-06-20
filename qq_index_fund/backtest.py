"""
QQ 指数基金 —— 回测引擎

基于已落盘的 N-PORT 季度快照序列 + Longbridge 前复权日 K，回测任意
[start, end] 时间段内 QQ 指数基金（QQQ Top10）的净值曲线。

计算模型
--------
按用户选择：
  - 再平衡时点 = N-PORT 的 filing_date（申报日，晚季末约 2 个月），
    严格无前视偏差——回测当天只能用"已公开"的持仓。
  - 两种权重曲线并列：等权（Top10 各 10%，缺则再归一化）+ 市值加权
    （按 N-PORT pctVal 归一化到 100%）。

再平衡调度
--------
  - 初始组合 = filing_date ≤ start 的最近一份快照的 Top10 权重，在
    [start, end] 内第一个交易日应用。
  - 窗口内每个 filing_date ∈ (start, end] 触发一次再平衡，对齐到
    ≥ 该 filing_date 的第一个交易日（> end 则跳过，持有到 end）。
  - 段内不调仓、权重随价格漂移（用"股数追踪"自动实现）。

价格
----
复用 drawdown.generate_drawdown_report.fetch_longbridge_daily_candles
（前复权 AdjustType.ForwardAdjust，已带本地 JSON 缓存）。缺失交易日
按"最近已知收盘价"前向填充；若某成分在再平衡日尚无价格，则剔除该
成分并对剩余权重再归一化（Top10 大盘股极少触发）。
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import date, timedelta
from typing import Optional

from .store import _DEFAULT_DATA_DIR, _read_json

logger = logging.getLogger(__name__)

TOP_N = 10  # "QQ 指数基金"成分数


@dataclass
class RebalanceEvent:
    """一次再平衡事件。"""

    apply_date: str          # 实际应用日（对齐到的交易日，ISO）
    filing_date: str         # N-PORT 申报日
    repd_date: str           # N-PORT 报告期
    symbols_equal: list[tuple[str, float]]     # [(symbol, weight)] 等权
    symbols_weighted: list[tuple[str, float]]  # [(symbol, weight)] 市值加权


@dataclass
class BacktestResult:
    start: str
    end: str
    dates: list[str]
    nav_equal: list[float]
    nav_weighted: list[float]
    nav_qqq: list[float]            # QQQ 原始曲线（前复权，归一化到 1.0）作基准对照
    total_return_equal: float
    total_return_weighted: float
    total_return_qqq: float
    rebalances: list[RebalanceEvent]
    symbols_used: list[str]
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "start": self.start,
            "end": self.end,
            "dates": self.dates,
            "nav_equal": self.nav_equal,
            "nav_weighted": self.nav_weighted,
            "nav_qqq": self.nav_qqq,
            "total_return_equal": round(self.total_return_equal, 4),
            "total_return_weighted": round(self.total_return_weighted, 4),
            "total_return_qqq": round(self.total_return_qqq, 4),
            "rebalance_count": len(self.rebalances),
            "rebalances": [
                {
                    "apply_date": r.apply_date,
                    "filing_date": r.filing_date,
                    "repd_date": r.repd_date,
                    "n_holdings_equal": len(r.symbols_equal),
                    "n_holdings_weighted": len(r.symbols_weighted),
                }
                for r in self.rebalances
            ],
            "symbols_used": self.symbols_used,
            "notes": self.notes,
        }


# ---------------- 快照加载 ----------------


def load_nport_snapshots_raw(data_dir: Optional[str] = None) -> list[dict]:
    """读取全部 N-PORT 快照（含完整 holdings），按 filing_date 升序。"""
    import os
    ndir = os.path.join(data_dir or _DEFAULT_DATA_DIR, "nport")
    if not os.path.isdir(ndir):
        return []
    out: list[dict] = []
    for name in os.listdir(ndir):
        if not name.endswith(".json"):
            continue
        snap = _read_json(os.path.join(ndir, name))
        if snap and snap.get("filing_date") and snap.get("holdings"):
            out.append(snap)
    out.sort(key=lambda s: s["filing_date"])
    return out


# ---------------- 权重派生 ----------------


def derive_top10_weights(
    holdings: list[dict],
    mode: str,
) -> list[tuple[str, float]]:
    """
    由一份快照的 holdings 派生 Top10 目标权重。

    Args:
        holdings: N-PORT 快照 holdings 列表（含 val_usd / pct_val / symbol）。
        mode: 'equal' 或 'weighted'。

    Returns:
        [(symbol, weight_pct), ...] 权重已归一化到合计 100。
        跳过 symbol 为空的成分（改名/退市），对剩余再归一化。
    """
    ordered = sorted(holdings, key=lambda h: h.get("val_usd", 0), reverse=True)
    top = [h for h in ordered[:TOP_N] if h.get("symbol")]
    if not top:
        return []

    if mode == "equal":
        raw = [(h["symbol"], 1.0) for h in top]
    else:  # weighted
        raw = [(h["symbol"], float(h.get("pct_val", 0) or 0)) for h in top]
        if sum(w for _, w in raw) <= 0:
            raw = [(s, 1.0) for s, _ in raw]

    total = sum(w for _, w in raw)
    return [(s, w * 100.0 / total) for s, w in raw]


# ---------------- 调度 ----------------


def build_rebalance_schedule(
    snapshots: list[dict],
    start: date,
    end: date,
    trading_days: list[str],
) -> list[RebalanceEvent]:
    """
    构建再平衡调度：初始（filing_date ≤ start 最近一份）+ 窗口内
    filing_date ∈ (start, end] 的各份，按 filing_date 升序，应用日对齐到
    ≥ 目标日的第一个交易日（≤ end，否则跳过）。
    """
    by_filing = sorted(snapshots, key=lambda s: s["filing_date"])

    # 初始：filing_date ≤ start 的最近一份
    initial = None
    for s in by_filing:
        if s["filing_date"] <= start.isoformat():
            initial = s
    # 窗口内再平衡
    in_window = [s for s in by_filing if start.isoformat() < s["filing_date"] <= end.isoformat()]

    ordered = ([initial] if initial else []) + in_window
    # 去重保序（initial 不会与 in_window 重叠）
    seen = set()
    seq = []
    for s in ordered:
        if s["filing_date"] in seen:
            continue
        seen.add(s["filing_date"])
        seq.append(s)

    def align(target_iso: str) -> Optional[str]:
        """≥ target 的第一个交易日；要求 ≤ end。"""
        for d in trading_days:
            if d >= target_iso and d <= end.isoformat():
                return d
        return None

    events: list[RebalanceEvent] = []
    for i, s in enumerate(seq):
        target = start.isoformat() if i == 0 else s["filing_date"]
        apply_day = align(target)
        if not apply_day:
            continue
        holdings = s.get("holdings", [])
        events.append(RebalanceEvent(
            apply_date=apply_day,
            filing_date=s["filing_date"],
            repd_date=s.get("repd_date", ""),
            symbols_equal=derive_top10_weights(holdings, "equal"),
            symbols_weighted=derive_top10_weights(holdings, "weighted"),
        ))
    return events


# ---------------- 模拟核心（纯函数，可测） ----------------


def simulate(
    trading_days: list[str],
    prices: dict[str, dict[str, float]],  # {symbol: {date_iso: close}}
    rebalances: list[RebalanceEvent],
    mode: str,                            # 'equal' | 'weighted'
) -> list[float]:
    """
    股数追踪模拟，返回每日 NAV 序列（起始归一化为 1.0）。

    缺失价格按"最近已知"前向填充；再平衡日某成分无价格则剔除并再归一化。
    """
    def weights_of(ev: RebalanceEvent) -> list[tuple[str, float]]:
        return ev.symbols_equal if mode == "equal" else ev.symbols_weighted

    def last_price(symbol: str, on_date: str) -> Optional[float]:
        series = prices.get(symbol)
        if not series:
            return None
        # 最近已知 ≤ on_date
        best = None
        for d, c in series.items():
            if d <= on_date and (best is None or d > best[0]):
                best = (d, c)
        return best[1] if best else None

    # 再平衡按 apply_date 升序
    evs = sorted(rebalances, key=lambda e: e.apply_date)
    nav_series: list[float] = []
    shares: dict[str, float] = {}
    last_nav = 1.0

    ev_idx = 0
    # 预计算每个交易日的再平衡（同一天可能无事件）
    apply_dates = {e.apply_date for e in evs}

    for d in trading_days:
        # 触发再平衡（初始组合在第一个交易日已通过 ev[0].apply_date 触发）
        if d in apply_dates:
            # 找该日的事件
            ev = next(e for e in evs if e.apply_date == d)
            targets = weights_of(ev)
            # 当前 NAV = Σ shares * price(d)
            cur_nav = 0.0
            for sym, sh in shares.items():
                p = last_price(sym, d)
                if p is not None:
                    cur_nav += sh * p
            if cur_nav <= 0 and shares:
                cur_nav = last_nav  # 价格全缺，沿用上次 NAV
            base_nav = cur_nav if cur_nav > 0 else last_nav

            # 解析目标成分价格，剔除无价格的，再归一化
            resolved = []
            for sym, w in targets:
                p = last_price(sym, d)
                if p is not None and p > 0:
                    resolved.append((sym, w, p))
            if not resolved:
                # 无可建仓成分，保持原持仓
                nav_series.append(_nav_from_shares(shares, d, last_price) or last_nav)
                last_nav = nav_series[-1]
                continue
            wsum = sum(w for _, w, _ in resolved)
            new_shares: dict[str, float] = {}
            for sym, w, p in resolved:
                new_shares[sym] = (base_nav * (w / wsum)) / p
            shares = new_shares

        # 当日 NAV
        nav = _nav_from_shares(shares, d, last_price)
        if nav is None or nav <= 0:
            nav = last_nav
        nav_series.append(round(nav, 6))
        last_nav = nav

    return nav_series


def _nav_from_shares(shares: dict[str, float], d: str, last_price) -> Optional[float]:
    total = 0.0
    any_price = False
    for sym, sh in shares.items():
        p = last_price(sym, d)
        if p is not None:
            total += sh * p
            any_price = True
    return total if any_price else None


# ---------------- 价格获取 ----------------


def fetch_prices(
    symbols: list[str],
    start: date,
    end: date,
    quote_ctx=None,
    *,
    include_qqq: bool = False,
) -> dict[str, dict[str, float]]:
    """
    为每个 symbol 抓取 [start, end] 前复权日 K，返回 {symbol: {date_iso: close}}。
    quote_ctx 为 None 时自动构建（需 Longbridge 凭证）。
    include_qqq=True 时额外抓 QQQ.US 作基准对照（不参与组合）。
    """
    syms = list(symbols)
    if include_qqq and "QQQ.US" not in syms:
        syms.append("QQQ.US")
    from drawdown.generate_drawdown_report import (
        build_longbridge_quote_context,
        fetch_longbridge_daily_candles,
        candle_datetime,
    )

    owns_ctx = quote_ctx is None
    if owns_ctx:
        quote_ctx = build_longbridge_quote_context()

    out: dict[str, dict[str, float]] = {}
    for sym in syms:
        try:
            candles = fetch_longbridge_daily_candles(quote_ctx, sym, start, end)
        except Exception as exc:
            logger.warning("Failed candles for %s: %s", sym, exc)
            continue
        series: dict[str, float] = {}
        for c in candles:
            try:
                series[candle_datetime(c).date().isoformat()] = float(c.close)
            except (AttributeError, TypeError, ValueError):
                continue
        out[sym] = series
    return out


# ---------------- 主入口 ----------------


def run_backtest(
    start: date,
    end: date,
    *,
    quote_ctx=None,
    data_dir: Optional[str] = None,
) -> BacktestResult:
    """
    运行 QQ 指数基金回测。

    Args:
        start: 回测起始日。
        end: 回测结束日。
        quote_ctx: 可选 Longbridge QuoteContext（None 则自动构建）。
        data_dir: 数据目录（None 用默认）。

    Returns:
        BacktestResult（含等权/加权双净值序列）。

    Raises:
        ValueError: 无可用 N-PORT 快照覆盖 start 之前（早于 2019-11）。
    """
    notes: list[str] = []
    snapshots = load_nport_snapshots_raw(data_dir)
    if not snapshots:
        raise ValueError("无 N-PORT 历史快照，请先运行回溯。")

    earliest_filing = snapshots[0]["filing_date"]
    initial_available = [s for s in snapshots if s["filing_date"] <= start.isoformat()]
    if not initial_available:
        raise ValueError(
            f"回测起始 {start} 早于最早一份 N-PORT 申报日 {earliest_filing}，"
            f"无法确定初始成分（历史仅可回溯至约 2019-11）。"
        )

    # 先抓价格（需要 symbols 集合）——但 symbols 取决于调度，调度又依赖交易日历。
    # 解决：先抓一份"候选 symbols"（初始 + 窗口内所有快照的 Top10 并集）的价格，
    # 用其交易日作日历，再据此对齐再平衡日。
    by_filing = sorted(snapshots, key=lambda s: s["filing_date"])
    initial = None
    for s in by_filing:
        if s["filing_date"] <= start.isoformat():
            initial = s
    in_window = [s for s in by_filing if start.isoformat() < s["filing_date"] <= end.isoformat()]
    candidate_snaps = ([initial] if initial else []) + in_window

    candidate_symbols: set[str] = set()
    for s in candidate_snaps:
        for sym, _ in derive_top10_weights(s.get("holdings", []), "equal"):
            candidate_symbols.add(sym)
        for sym, _ in derive_top10_weights(s.get("holdings", []), "weighted"):
            candidate_symbols.add(sym)

    if not candidate_symbols:
        raise ValueError("候选成分 symbol 为空（快照可能全部 ticker 未解析）。")

    # 价格抓取区间向前扩几天，确保再平衡对齐日有价
    fetch_start = start - timedelta(days=10)
    prices = fetch_prices(sorted(candidate_symbols), fetch_start, end, quote_ctx=quote_ctx, include_qqq=True)

    # 交易日历 = 所有 symbol 价格日期的并集，落在 [start, end]
    day_set: set[str] = set()
    for series in prices.values():
        for d in series:
            if start.isoformat() <= d <= end.isoformat():
                day_set.add(d)
    trading_days = sorted(day_set)
    if not trading_days:
        raise ValueError("回测区间内无任何交易日价格数据。")

    rebalances = build_rebalance_schedule(snapshots, start, end, trading_days)
    if not rebalances:
        raise ValueError("未能构建再平衡调度（无初始成分）。")

    notes.append(f"候选成分 {len(candidate_symbols)} 只，交易日 {len(trading_days)} 天，再平衡 {len(rebalances)} 次。")
    missing = candidate_symbols - set(prices.keys())
    if missing:
        notes.append(f"以下成分未取到价格已忽略: {', '.join(sorted(missing))}")

    nav_equal = simulate(trading_days, prices, rebalances, "equal")
    nav_weighted = simulate(trading_days, prices, rebalances, "weighted")

    # QQQ 基准曲线：前复权收盘价归一化到首个交易日=1.0，缺失日前向填充，
    # 与组合曲线共享同一交易日历。QQQ 本身不参与组合。
    qqq_series = prices.get("QQQ.US", {})
    nav_qqq = _normalize_benchmark(qqq_series, trading_days)
    if not qqq_series:
        notes.append("QQQ 基准价格未取到，对照曲线为空。")

    def total_ret(series: list[float]) -> float:
        if not series or series[0] <= 0:
            return 0.0
        return (series[-1] / series[0] - 1.0) * 100.0

    # symbols_used 为组合成分（不含 QQQ 基准）
    portfolio_symbols = sorted(s for s in prices.keys() if s != "QQQ.US")
    return BacktestResult(
        start=start.isoformat(),
        end=end.isoformat(),
        dates=trading_days,
        nav_equal=nav_equal,
        nav_weighted=nav_weighted,
        nav_qqq=nav_qqq,
        total_return_equal=total_ret(nav_equal),
        total_return_weighted=total_ret(nav_weighted),
        total_return_qqq=total_ret(nav_qqq),
        rebalances=rebalances,
        symbols_used=portfolio_symbols,
        notes=notes,
    )


def _normalize_benchmark(
    series: dict[str, float],
    trading_days: list[str],
) -> list[float]:
    """
    将基准（如 QQQ）前复权收盘价归一化为 NAV 曲线：首个交易日=1.0。
    缺失日按最近已知价前向填充（与组合模拟的 last_price 一致）。
    无任何价格时返回与交易日等长的 1.0 列表（基准缺省）。
    """
    if not series:
        return [1.0] * len(trading_days)
    # 最近已知价（≤ 当日）
    def last_known(on_date: str) -> Optional[float]:
        best = None
        for d, c in series.items():
            if d <= on_date and (best is None or d > best[0]):
                best = (d, c)
        return best[1] if best else None

    out: list[float] = []
    base: Optional[float] = None
    for d in trading_days:
        p = last_known(d)
        if p is None:
            # 该日尚无价（极少见，区间起始前扩了10天）；沿用前值或占位
            out.append(out[-1] if out else 1.0)
            continue
        if base is None:
            base = p
        out.append(round(p / base, 6) if base else 1.0)
    return out
