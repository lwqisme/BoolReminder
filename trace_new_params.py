#!/usr/bin/env python3
"""Trace the exact buy/sell/rearm timeline for the user's new parameter set.

线性递增加权细切 (步长 5.7% / 最大回撤 28.7%)
成本去杠杆 12.3%/21.1%/35.1% 盈利 20%+28.6%+20% 卖出
0日冷却 / 卖后重启 4.75%回撤 / 重启后从首档 / 卖档重启 12.1%回撤
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
    SymbolState,
    _inputs_for_target,
    point_drawdown_pct,
    _position_value_usd,
    _mark_consumed_tranches_from_position,
    _execute_crossed_tranches,
    _execute_sell_strategy,
    _rearm_buy_tranches_after_repair,
    _rearm_buy_tranches_after_position_sell,
    _mark_buy_rearm_after_position_sell,
    _monthly_contribution_days,
)

# User's exact parameters
inputs = StrategyInputs(
    initial_cash=20000,
    monthly_contribution=1000,
    max_drawdown_pct=28.7,
    drawdown_basis='rolling_120',
    step_pct=5.7,
    equal_slice_allocation_pct=5.0,
    trade_fee=0.35,
    hkd_to_usd=0.128,
    reserve_position_pct=40.0,
    sell_min_profit_pct=10.0,
    repair_sell_cooldown_days=30,
    repair_stage_sell_pct=12.0,
    dca_rearm_drawdown_pct=4.75,
    sell_stage_rearm_drawdown_pct=12.1,
    cost_first_profit_pct=12.3,
    cost_second_profit_pct=21.1,
    cost_third_profit_pct=35.1,
    cost_first_sell_pct=20.0,
    cost_second_sell_pct=28.6,
    cost_third_sell_pct=20.0,
    cost_deleverage_cooldown_days=0,
    sell_allow_same_day_sell=True,
    buy_rearm_mode='restart_from_rearm',
)

# First: check what tranches look like with strategy max_dd=28.7
tranches_28 = build_strategy_tranches(inputs, 'linear_weighted_slice')
print("=== Tranches with max_dd=28.7% ===")
for t in tranches_28:
    print(f"  threshold={t.threshold_pct:.2f}% allocation={t.allocation_pct:.2f}%")

# But TSLA target may have its own max_drawdown_pct override!
# Default investment universe: TSLA has max_drawdown_pct=50.0
# Let's check both scenarios

print("\n=== Tranches with max_dd=50.0% (TSLA target override) ===")
from drawdown.position_strategy import StrategyInputs as SI
inputs_50 = StrategyInputs(
    initial_cash=20000, monthly_contribution=1000,
    max_drawdown_pct=50.0, drawdown_basis='rolling_120',
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
tranches_50 = build_strategy_tranches(inputs_50, 'linear_weighted_slice')
for t in tranches_50:
    print(f"  threshold={t.threshold_pct:.2f}% allocation={t.allocation_pct:.2f}%")

# Now run simulation with BOTH scenarios
quote_ctx = build_longbridge_quote_context()
symbol = 'TSLA.US'
candles = fetch_longbridge_daily_candles(quote_ctx, symbol, date(2023, 6, 1), date(2026, 6, 15))
series = [(candle_datetime(c).replace(tzinfo=None), float(c.close)) for c in candles]
points = build_price_points_from_series(series)

# Scenario 1: max_dd=28.7% (no target override)
print("\n\n========== SCENARIO 1: max_dd=28.7% ==========")
target_28 = PortfolioTarget(symbol=symbol, weight=100.0, name='TSLA')
result_28 = _simulate_strategy(
    {symbol: points}, [target_28], inputs,
    'linear_weighted_slice', 'cost_deleverage',
)
trades_28 = result_28['trades']
print(f"Trades: {len(trades_28)}, return: {result_28['metrics']['return_pct']:.2f}%")
for t in trades_28:
    anchor = t.get('buy_rearm_anchor_drawdown_pct')
    anchor_str = f"anchor={anchor:.2f}%" if anchor is not None else "anchor=None"
    if t['action'] == 'buy':
        print(f"  {t['date']} BUY  dd={t['drawdown_pct']:.2f}% eff={t.get('threshold_pct',0):.2f}% base={t.get('base_threshold_pct',0):.2f}% {anchor_str} stage={t.get('stage','-')}")
    else:
        print(f"  {t['date']} SELL dd={t['drawdown_pct']:.2f}% stage={t.get('stage','-')} profit={t.get('estimated_profit_pct',0):.2f}%")

# Scenario 2: max_dd=50.0% (TSLA target override)
print("\n\n========== SCENARIO 2: max_dd=50.0% (target override) ==========")
target_50 = PortfolioTarget(symbol=symbol, weight=100.0, name='TSLA', max_drawdown_pct=50.0)
result_50 = _simulate_strategy(
    {symbol: points}, [target_50], inputs,
    'linear_weighted_slice', 'cost_deleverage',
)
trades_50 = result_50['trades']
print(f"Trades: {len(trades_50)}, return: {result_50['metrics']['return_pct']:.2f}%")
for t in trades_50:
    anchor = t.get('buy_rearm_anchor_drawdown_pct')
    anchor_str = f"anchor={anchor:.2f}%" if anchor is not None else "anchor=None"
    if t['action'] == 'buy':
        print(f"  {t['date']} BUY  dd={t['drawdown_pct']:.2f}% eff={t.get('threshold_pct',0):.2f}% base={t.get('base_threshold_pct',0):.2f}% {anchor_str} stage={t.get('stage','-')}")
    else:
        print(f"  {t['date']} SELL dd={t['drawdown_pct']:.2f}% stage={t.get('stage','-')} profit={t.get('estimated_profit_pct',0):.2f}%")

# Focus on the user's specific trades
print("\n\n========== Matching user's trades ==========")
print("User's trades:")
print("  2024-12-10 sell 0% cost_3(35.1)")
print("  2024-12-16 sell 0% cost_1(12.3)")
print("  2025-02-25 buy  36.9%")
print("  2025-02-27 buy  41.24%")
print("  2025-03-10 buy  53.71%")

# Check which scenario matches
for label, trades in [("max_dd=28.7%", trades_28), ("max_dd=50.0%", trades_50)]:
    matches_sell_dec10 = any(t['date'] == '2024-12-10' and t['action'] == 'sell' for t in trades)
    matches_buy_feb25 = any(t['date'] == '2025-02-25' and t['action'] == 'buy' for t in trades)
    print(f"\n{label}: sell on 2024-12-10={matches_sell_dec10}, buy on 2025-02-25={matches_buy_feb25}")
