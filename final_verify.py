#!/usr/bin/env python3
"""Final verification: with fix applied, does the original bug scenario work correctly?
Test both:
1. The original bug scenario (with warmup, 2022-12 data)
2. The correct 3-year scenario (from 2023-06)
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
target = PortfolioTarget(symbol=symbol, weight=100.0, name='TSLA', max_drawdown_pct=47.8)

# Scenario 2: 3-year from 2023-06 (no warmup for drawdown)
print("=== 3-year window (2023-06-11 to 2026-06-11) ===")
candles2 = fetch_longbridge_daily_candles(quote_ctx, symbol, date(2023, 6, 1), date(2026, 6, 15))
series2 = [(candle_datetime(c).replace(tzinfo=None), float(c.close)) for c in candles2]
points2 = build_price_points_from_series(series2)

result2 = _simulate_strategy(
    {symbol: points2}, [target], inputs,
    'linear_weighted_slice', 'cost_deleverage',
)
trades2 = result2['trades']
print(f"Trades: {len(trades2)}, return: {result2['metrics']['return_pct']:.2f}%")

# Focus on 2023-10-30
oct30_trades = [t for t in trades2 if t['date'] == '2023-10-30']
print(f"\nTrades on 2023-10-30: {len(oct30_trades)}")
for t in oct30_trades:
    print(f"  {t['action']} dd={t['drawdown_pct']:.2f}% eff={t.get('threshold_pct',0):.2f}% base={t.get('base_threshold_pct',0):.2f}% anchor={t.get('buy_rearm_anchor_drawdown_pct')}")

# All trades near 2023-10
print("\nTrades around 2023-10:")
for t in trades2:
    d = t['date']
    if '2023-09' in d or '2023-10' in d or '2023-11' in d:
        print(f"  {d} {t['action']:4s} dd={t['drawdown_pct']:.2f}% eff={t.get('threshold_pct',0):.2f}% base={t.get('base_threshold_pct',0):.2f}% anchor={t.get('buy_rearm_anchor_drawdown_pct')}")

# All buy trades
print("\nAll buy trades:")
for t in trades2:
    if t['action'] == 'buy':
        print(f"  {t['date']} dd={t['drawdown_pct']:.2f}% eff={t.get('threshold_pct',0):.2f}% base={t.get('base_threshold_pct',0):.2f}% anchor={t.get('buy_rearm_anchor_drawdown_pct')}")
