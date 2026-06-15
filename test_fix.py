#!/usr/bin/env python3
"""Full simulation with fix applied, show all trades for the 3-year window.
Also compare with and without the fix.
"""
import sys
from datetime import date

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
    _simulate_strategy,
    build_strategy_tranches,
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

quote_ctx = build_longbridge_quote_context()
symbol = 'TSLA.US'
candles = fetch_longbridge_daily_candles(quote_ctx, symbol, date(2023, 6, 1), date(2026, 6, 15))
series = [(candle_datetime(c).replace(tzinfo=None), float(c.close)) for c in candles]
points = build_price_points_from_series(series)

target = PortfolioTarget(symbol=symbol, weight=100.0, name='TSLA', max_drawdown_pct=47.8)

print("=== WITH FIX ===")
result = _simulate_strategy(
    {symbol: points}, [target], inputs,
    'linear_weighted_slice', 'cost_deleverage',
)
trades = result['trades']
print(f"Trades: {len(trades)}, return: {result['metrics']['return_pct']:.2f}%")
for t in trades:
    print(f"  {t['date']} {t['action']:4s} dd={t['drawdown_pct']:.2f}% eff={t.get('threshold_pct',0):.2f}% base={t.get('base_threshold_pct',0):.2f}% "
          f"anchor={t.get('buy_rearm_anchor_drawdown_pct')} stage={t.get('stage','-')} profit={t.get('estimated_profit_pct',0):.2f}%")

# Focus on 2023-10-30
print("\n=== 2023-10-30 trades ===")
for t in trades:
    if t['date'] == '2023-10-30':
        print(f"  {t['action']} dd={t['drawdown_pct']:.2f}% eff={t.get('threshold_pct',0):.2f}% base={t.get('base_threshold_pct',0):.2f}% anchor={t.get('buy_rearm_anchor_drawdown_pct')}")

# Now test with buy_rearm_mode='cumulative' for comparison
print("\n\n=== WITH FIX + buy_rearm_mode=cumulative ===")
inputs_cum = StrategyInputs(
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
    buy_rearm_mode='cumulative',
)
result2 = _simulate_strategy(
    {symbol: points}, [target], inputs_cum,
    'linear_weighted_slice', 'cost_deleverage',
)
trades2 = result2['trades']
print(f"Trades: {len(trades2)}, return: {result2['metrics']['return_pct']:.2f}%")
for t in trades2:
    print(f"  {t['date']} {t['action']:4s} dd={t['drawdown_pct']:.2f}% eff={t.get('threshold_pct',0):.2f}% base={t.get('base_threshold_pct',0):.2f}% "
          f"anchor={t.get('buy_rearm_anchor_drawdown_pct')} stage={t.get('stage','-')} profit={t.get('estimated_profit_pct',0):.2f}%")

print("\n=== 2023-10-30 trades (cumulative) ===")
for t in trades2:
    if t['date'] == '2023-10-30':
        print(f"  {t['action']} dd={t['drawdown_pct']:.2f}% eff={t.get('threshold_pct',0):.2f}% base={t.get('base_threshold_pct',0):.2f}% anchor={t.get('buy_rearm_anchor_drawdown_pct')}")
