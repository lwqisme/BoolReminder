#!/usr/bin/env python3
"""Reproduce with correct timeframe: TSLA 近三年 starting ~2023-06-11
No 2019 data should be involved.
"""
import sys
from datetime import date, datetime, timedelta

sys.path.insert(0, '/app')

from drawdown.generate_drawdown_report import (
    build_longbridge_quote_context,
    build_price_points_from_series,
    fetch_longbridge_daily_candles,
    candle_datetime,
)
from drawdown.position_strategy import (
    StrategyInputs,
    PortfolioTarget,
    simulate_portfolio,
    build_strategy_tranches,
    _simulate_strategy,
    _inputs_for_target,
    point_drawdown_pct,
)

inputs = StrategyInputs(
    initial_cash=20000, monthly_contribution=1000,
    max_drawdown_pct=47.8, drawdown_basis='rolling_120',
    step_pct=9.95, equal_slice_allocation_pct=5.0,
    trade_fee=0.35, hkd_to_usd=0.128,
    reserve_position_pct=40.0, sell_min_profit_pct=10.0,
    repair_sell_cooldown_days=30, repair_stage_sell_pct=12.0,
    dca_rearm_drawdown_pct=4.21, sell_stage_rearm_drawdown_pct=16.68,
    cost_first_profit_pct=10.3, cost_second_profit_pct=15.0, cost_third_profit_pct=29.2,
    cost_first_sell_pct=40.0, cost_second_sell_pct=30.0, cost_third_sell_pct=21.6,
    cost_deleverage_cooldown_days=24,
    sell_allow_same_day_sell=True,
    buy_rearm_mode='restart_from_rearm',
)

tranches = build_strategy_tranches(inputs, 'linear_weighted_slice')
print("=== Tranches ===")
for t in tranches:
    print(f"  threshold={t.threshold_pct:.2f}% allocation={t.allocation_pct:.2f}%")

# Fetch TSLA with warmup for 120d drawdown
# 近三年: 2023-06-11 to 2026-06-11
# Need warmup: 120 trading days ≈ 180 calendar days before start
quote_ctx = build_longbridge_quote_context()
symbol = 'TSLA.US'
warmup_start = date(2022, 12, 1)  # ~6 months before 2023-06-11
sim_end = date(2026, 6, 11)

candles = fetch_longbridge_daily_candles(quote_ctx, symbol, warmup_start, sim_end)
series = [(candle_datetime(c).replace(tzinfo=None), float(c.close)) for c in candles]
all_points = build_price_points_from_series(series)

# Filter to simulation window (warmup points excluded from trading but included for drawdown calc)
sim_start = date(2023, 6, 11)
sim_points = [p for p in all_points if p.date.date() >= sim_start]

print(f"\nAll points (with warmup): {len(all_points)}, from {all_points[0].date.date()} to {all_points[-1].date.date()}")
print(f"Sim points (from {sim_start}): {len(sim_points)}, from {sim_points[0].date.date()} to {sim_points[-1].date.date()}")

# Find 2023-10-30
target_date = date(2023, 10, 30)
for p in sim_points:
    if p.date.date() == target_date:
        dd120 = abs(p.drawdown_120) * 100
        ddath = abs(p.drawdown_ath) * 100
        print(f"\n=== {target_date} ===")
        print(f"  close={p.close:.2f} dd120={dd120:.2f}% dd_ath={ddath:.2f}%")
        # Which tranches should trigger?
        triggered = [f"T({tr.threshold_pct:.2f}%)" for tr in tranches if dd120 + 1e-9 >= tr.threshold_pct]
        print(f"  Should trigger: {triggered}")
        break

# Run simulation - the engine needs warmup points for accurate drawdown
# But we want it to only trade within the sim window
# _simulate_strategy processes ALL points, so we pass all_points
# BUT we need to ensure it only starts trading from sim_start

# Actually, the simulate_portfolio function processes all points from the start.
# In the parameter lab, the packet API handles this by providing price_points
# that start from warmup, and the engine just processes all of them.
# The key is: the engine starts with initial_cash and begins buying from day 1.

# So if we pass all_points (starting from 2022-12-01), the engine will
# start trading from 2022-12-01, which is wrong.
# 
# We need to pass ONLY points from sim_start onwards, but with accurate drawdown.
# The build_price_points_from_series computes drawdown based on the provided points.
# If we only provide points from 2023-06-11, the 120d peak will be computed
# from 2023-06-11 onwards, missing the earlier peak.

# The correct approach: use the full points for accurate drawdown,
# but tell the engine to only start trading from sim_start.
# _simulate_strategy has `last_trade_date` parameter for this,
# but it's designed for signal generation, not backtesting.

# Actually, the parameter lab doesn't use last_trade_date.
# It just passes all points (with warmup) to simulate_portfolio.
# But the warmup points affect drawdown calculation without being traded on.
# 
# Wait, let me re-read the code. simulate_portfolio processes ALL days
# in point_by_day. So if we include warmup days, it WILL trade on those days.
#
# The parameter lab's solution is to use _rebuild_points_for_range which
# rebuilds the price points with accurate ATH/drawdown for the window only.
# But the 120d drawdown is computed from the window start, not from the true peak.
#
# Actually no - the parameter lab fetches warmup data separately and then
# _inject_warmup_into_tasks adds warmup data to each task for client-side
# drawdown calculation. The JS engine's rebuildPricePoints function handles
# this by using warmup days for drawdown calculation but only returning
# window points.

# For Python engine, the simulate_portfolio starts trading from the first
# point. So we need to provide ONLY the window points, but with accurate
# drawdown. This means we need to recompute drawdown with warmup data
# included but only provide window points to the engine.

# Let me check how the parameter lab's evaluate-batch does it:
# It fetches with 365-day warmup, builds full price points, then passes
# full_pts to simulate_portfolio. This means trading starts from warmup_start.

# Hmm, that would produce incorrect results for a 3-year backtest...
# Let me re-read the evaluate-batch code.

print("\n\n=== Running simulation from warmup_start (2022-12-01) ===")
# This is what evaluate-batch does - it includes warmup
target = PortfolioTarget(symbol=symbol, weight=100.0, name='TSLA', max_drawdown_pct=47.8)
result = _simulate_strategy(
    {symbol: all_points}, [target], inputs,
    'linear_weighted_slice', 'cost_deleverage',
)
trades = result['trades']
print(f"Trade count: {len(trades)}")
for t in trades:
    print(f"  {t['date']} {t['action']:4s} dd={t['drawdown_pct']:.2f}% eff={t.get('threshold_pct',0):.2f}% base={t.get('base_threshold_pct',0):.2f}% "
          f"anchor={t.get('buy_rearm_anchor_drawdown_pct')} stage={t.get('stage','-')} profit={t.get('estimated_profit_pct',0):.2f}%")

# The correct way: only provide points from sim_start but rebuild with warmup
# Actually, let me just rebuild price points only for the window
# This means drawdown calculation starts from sim_start
print(f"\n\n=== Running simulation from sim_start only (no warmup) ===")
result2 = _simulate_strategy(
    {symbol: sim_points}, [target], inputs,
    'linear_weighted_slice', 'cost_deleverage',
)
trades2 = result2['trades']
print(f"Trade count: {len(trades2)}")
for t in trades2:
    print(f"  {t['date']} {t['action']:4s} dd={t['drawdown_pct']:.2f}% eff={t.get('threshold_pct',0):.2f}% base={t.get('base_threshold_pct',0):.2f}% "
          f"anchor={t.get('buy_rearm_anchor_drawdown_pct')} stage={t.get('stage','-')} profit={t.get('estimated_profit_pct',0):.2f}%")

# Focus on 2023-10-30
print(f"\n=== Trades on/around 2023-10-30 (no warmup sim) ===")
for t in trades2:
    d = t['date']
    if '2023-10' in d or '2023-09' in d or '2023-11' in d:
        print(f"  {d} {t['action']:4s} dd={t['drawdown_pct']:.2f}% eff={t.get('threshold_pct',0):.2f}% base={t.get('base_threshold_pct',0):.2f}%")

# Check 2023-10-30 drawdown in sim_points
for p in sim_points:
    if p.date.date() == target_date:
        dd120 = abs(p.drawdown_120) * 100
        print(f"\n  {target_date} in sim_points: close={p.close:.2f} dd120={dd120:.2f}% (NO warmup - likely inaccurate)")
        break
