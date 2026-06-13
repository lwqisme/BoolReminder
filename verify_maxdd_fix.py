#!/usr/bin/env python3
"""After fix: verify that max_dd=28.7% is now used for TSLA full-position scoring.
Compare with the old behavior (max_dd=50.0% override).
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
    max_drawdown_pct=28.7, drawdown_basis='rolling_120',
    step_pct=5.7, equal_slice_allocation_pct=5.0,
    trade_fee=0.35, hkd_to_usd=0.128,
    reserve_position_pct=40.0, sell_min_profit_pct=10.0,
    repair_sell_cooldown_days=30, repair_stage_sell_pct=12.0,
    dca_rearm_drawdown_pct=4.75, sell_stage_rearm_drawdown_pct=12.1,
    cost_first_profit_pct=12.3, cost_second_profit_pct=21.1, cost_third_profit_pct=35.1,
    cost_first_sell_pct=20.0, cost_second_sell_pct=28.6, cost_third_sell_pct=20.0,
    cost_deleverage_cooldown_days=0,
    sell_allow_same_day_sell=True,
    buy_rearm_mode='restart_from_rearm',
)

quote_ctx = build_longbridge_quote_context()
symbol = 'TSLA.US'
candles = fetch_longbridge_daily_candles(quote_ctx, symbol, date(2023, 6, 1), date(2026, 6, 15))
series = [(candle_datetime(c).replace(tzinfo=None), float(c.close)) for c in candles]
points = build_price_points_from_series(series)

# Now: no target max_drawdown_pct override → uses GA's 28.7%
print("=== 修复后：max_dd=28.7% (GA 进化值) ===")
target = PortfolioTarget(symbol=symbol, weight=100.0, name='TSLA')
tranches = build_strategy_tranches(inputs, 'linear_weighted_slice')
print(f"Tranches: {len(tranches)}")
for t in tranches:
    print(f"  {t.threshold_pct:.2f}% / {t.allocation_pct:.2f}%")

result = _simulate_strategy(
    {symbol: points}, [target], inputs,
    'linear_weighted_slice', 'cost_deleverage',
)
trades = result['trades']
print(f"\nTrades: {len(trades)}, return: {result['metrics']['return_pct']:.2f}%")
for t in trades:
    if t['action'] == 'buy':
        anchor = t.get('buy_rearm_anchor_drawdown_pct')
        anchor_str = f"anchor={anchor:.2f}%" if anchor else "anchor=None"
        print(f"  {t['date']} BUY  dd={t['drawdown_pct']:.2f}% base={t.get('base_threshold_pct',0):.2f}% {anchor_str}")
    else:
        print(f"  {t['date']} SELL dd={t['drawdown_pct']:.2f}% stage={t.get('stage','-')} profit={t.get('estimated_profit_pct',0):.2f}%")

# Check user's specific trades
print("\n=== 用户提到的交易 ===")
user_dates = ['2024-12-10', '2024-12-16', '2025-02-25', '2025-02-27', '2025-03-10']
for d in user_dates:
    matching = [t for t in trades if t['date'] == d]
    if matching:
        for t in matching:
            if t['action'] == 'buy':
                print(f"  ✓ {d} BUY dd={t['drawdown_pct']:.2f}% base={t.get('base_threshold_pct',0):.2f}%")
            else:
                print(f"  ✓ {d} SELL stage={t.get('stage','-')} profit={t.get('estimated_profit_pct',0):.2f}%")
    else:
        print(f"  ✗ {d} 无交易")
