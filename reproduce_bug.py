#!/usr/bin/env python3
"""Reproduce bug: TSLA 2023-10-30, 120d drawdown ~32% but only 19.90% buy tranche triggered.

Params: 线性递增加权细切 (step 9.95% / max_dd 47.8%)
        cost_deleverage 10.3%/15%/29.2% profit, 40%/30%/21.6% sell
        24d cooldown, 买入日可卖, 卖后重启 4.21% rearm
        从首档 restart, 卖档重启 16.68% rearm
"""
import sys, json, math
from datetime import date, datetime, timedelta

# Setup path
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
    _slice_thresholds,
)

# Build inputs matching the user's parameter set
inputs = StrategyInputs(
    initial_cash=20000,
    monthly_contribution=1000,
    max_drawdown_pct=47.8,
    drawdown_basis='rolling_120',
    step_pct=9.95,
    equal_slice_allocation_pct=5.0,
    trade_fee=0.35,
    hkd_to_usd=0.128,
    reserve_position_pct=40.0,
    sell_min_profit_pct=10.0,
    repair_sell_cooldown_days=30,
    repair_stage_sell_pct=12.0,
    dca_rearm_drawdown_pct=4.21,
    sell_stage_rearm_drawdown_pct=16.68,
    cost_first_profit_pct=10.3,
    cost_second_profit_pct=15.0,
    cost_third_profit_pct=29.2,
    cost_first_sell_pct=40.0,
    cost_second_sell_pct=30.0,
    cost_third_sell_pct=21.6,
    cost_deleverage_cooldown_days=24,
    sell_allow_same_day_sell=True,
    buy_rearm_mode='restart_from_rearm',
)

# Print tranches
tranches = build_strategy_tranches(inputs, 'linear_weighted_slice')
print("=== Tranches for linear_weighted_slice (step=9.95, max_dd=47.8) ===")
for t in tranches:
    print(f"  threshold={t.threshold_pct:.2f}% allocation={t.allocation_pct:.2f}% label={t.label}")

# Also show raw thresholds
max_dd = 47.8
step = 9.95
thresholds = _slice_thresholds(max_dd, step)
print(f"\nRaw thresholds: {[f'{t:.2f}' for t in thresholds]}")

# Fetch TSLA price data
print("\nFetching TSLA price data...")
quote_ctx = build_longbridge_quote_context()
symbol = 'TSLA.US'

# Fetch with warmup for accurate 120d drawdown
end_date = date(2023, 11, 30)
start_date = date(2021, 6, 1)  # Extra warmup

candles = fetch_longbridge_daily_candles(quote_ctx, symbol, start_date, end_date)
if not candles:
    print("ERROR: No candles returned")
    sys.exit(1)

series = [(candle_datetime(c).replace(tzinfo=None), float(c.close)) for c in candles]
points = build_price_points_from_series(series)
print(f"Total price points: {len(points)}")

# Find 2023-10-30
target_date = date(2023, 10, 30)
target_point = None
for p in points:
    if p.date.date() == target_date:
        target_point = p
        break

if target_point:
    dd_120_pct = abs(target_point.drawdown_120) * 100
    dd_ath_pct = abs(target_point.drawdown_ath) * 100
    print(f"\n=== {target_date} ===")
    print(f"  close: {target_point.close}")
    print(f"  drawdown_120: {dd_120_pct:.2f}%")
    print(f"  drawdown_ath: {dd_ath_pct:.2f}%")
    print(f"  rolling_120_peak: {target_point.rolling_120_peak}")
    print(f"  rolling_peak: {target_point.rolling_peak}")
else:
    print(f"WARNING: {target_date} not found in price points")
    # Show nearby
    for p in points:
        if date(2023, 10, 25) <= p.date.date() <= date(2023, 11, 5):
            print(f"  {p.date.date()} close={p.close} dd120={abs(p.drawdown_120)*100:.2f}%")

# Now run the full simulation for the window the user described: ~3 years
print("\n\nRunning simulation: 2020-11-01 to 2023-11-30...")
sim_start = date(2020, 11, 1)
sim_end = date(2023, 11, 30)

# Refetch for this range with warmup
candles2 = fetch_longbridge_daily_candles(quote_ctx, symbol, date(2020, 1, 1), sim_end)
series2 = [(candle_datetime(c).replace(tzinfo=None), float(c.close)) for c in candles2]
points2 = build_price_points_from_series(series2)

# Filter to simulation window
sim_points = [p for p in points2 if sim_start <= p.date.date() <= sim_end]
print(f"Simulation points: {len(sim_points)} (from {sim_points[0].date.date()} to {sim_points[-1].date.date()})")

# Need to rebuild with full warmup for accurate 120d drawdown
# Actually use the full points with warmup, then the engine will process all days
target = PortfolioTarget(symbol=symbol, weight=100.0, name='TSLA', max_drawdown_pct=47.8)

result = simulate_portfolio(
    {symbol: points2},
    [target],
    inputs,
    strategies=['linear_weighted_slice'],
    sell_strategies=['cost_deleverage'],
)

strategy_result = result['strategies'][0]
trades = strategy_result['trades']
metrics = strategy_result['metrics']

print(f"\n=== Simulation Result ===")
print(f"  return_pct: {metrics['return_pct']:.2f}%")
print(f"  trade_count: {metrics['trade_count']}")
print(f"  buy_trades: {metrics['buy_trade_count']}")
print(f"  sell_trades: {metrics['sell_trade_count']}")

print(f"\n=== All Buy Trades ===")
for t in trades:
    if t['action'] == 'buy':
        print(f"  {t['date']} dd={t['drawdown_pct']:.2f}% eff_threshold={t['threshold_pct']:.2f}% base={t.get('base_threshold_pct', 0):.2f}% "
              f"anchor={t.get('buy_rearm_anchor_drawdown_pct')} shares={t['shares']:.4f} price={t['price']:.2f} "
              f"sell_rearmed={t.get('sell_cycle_rearmed', False)}")

print(f"\n=== All Sell Trades ===")
for t in trades:
    if t['action'] == 'sell':
        print(f"  {t['date']} dd={t['drawdown_pct']:.2f}% stage={t.get('stage', '-')} shares={t['shares']:.4f} "
              f"price={t['price']:.2f} profit_pct={t.get('estimated_profit_pct', 0):.2f}%")

# Focus on 2023-10-30
print(f"\n=== Trades on 2023-10-30 ===")
for t in trades:
    if t['date'] == '2023-10-30':
        print(f"  {t['action']} dd={t['drawdown_pct']:.2f}% threshold={t.get('threshold_pct', 0):.2f}% "
              f"base_threshold={t.get('base_threshold_pct', 0):.2f}% anchor={t.get('buy_rearm_anchor_drawdown_pct')}")

# Also show the state of 120d drawdown around that period
print(f"\n=== 120d Drawdown around 2023-10-30 ===")
for p in points2:
    if date(2023, 10, 20) <= p.date.date() <= date(2023, 11, 10):
        dd120 = abs(p.drawdown_120) * 100
        # Check which tranches should trigger
        triggered = [f"T{i+1}({tr.threshold_pct:.2f}%)" for i, tr in enumerate(tranches) if dd120 + 1e-9 >= tr.threshold_pct]
        print(f"  {p.date.date()} close={p.close:.2f} dd120={dd120:.2f}% triggered={triggered}")
