#!/usr/bin/env python3
"""Diagnose why 32% drawdown only triggers 19.90% tranche on 2023-10-30.

Add debug instrumentation to track rearm state and executed thresholds.
"""
import sys, json, math
from datetime import date, datetime, timedelta

sys.path.insert(0, '/app')

from drawdown.generate_drawdown_report import (
    build_longbridge_quote_context,
    build_price_points_from_series,
    fetch_longbridge_daily_candles,
    candle_datetime,
    PricePoint,
)
from drawdown.position_strategy import (
    StrategyInputs,
    PortfolioTarget,
    SymbolState,
    _simulate_strategy,
    build_strategy_tranches,
    _slice_thresholds,
    _inputs_for_target,
)
from drawdown.strategy_rules import (
    sell_stage_rearm_drawdown_pct,
)

# Build inputs
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

tranches = build_strategy_tranches(inputs, 'linear_weighted_slice')

# Fetch data
quote_ctx = build_longbridge_quote_context()
symbol = 'TSLA.US'
candles = fetch_longbridge_daily_candles(quote_ctx, symbol, date(2019, 1, 1), date(2023, 11, 30))
series = [(candle_datetime(c).replace(tzinfo=None), float(c.close)) for c in candles]
points = build_price_points_from_series(series)

# Run simulation manually with instrumentation
target = PortfolioTarget(symbol=symbol, weight=100.0, name='TSLA', max_drawdown_pct=47.8)
effective_inputs = _inputs_for_target(inputs, target)

# Initialize state (mirroring _simulate_strategy)
state = SymbolState(
    symbol=symbol,
    name='TSLA',
    weight=100.0,
    budget=inputs.initial_cash,
    cash=inputs.initial_cash,
    lots=[],
    sell_marks=set(),
    price_history=points,
)

executed = {}
trade_log = []
tranches_for_symbol = build_strategy_tranches(effective_inputs, 'linear_weighted_slice')

point_by_day = {point.date.date(): point for point in points}
trading_index = {point.date.date(): i for i, point in enumerate(points)}
all_days = sorted(point_by_day.keys())

# Track rearm events
focus_start = date(2022, 5, 1)
focus_end = date(2023, 11, 30)
focus_date = date(2023, 10, 30)

# Monthly contribution days
from drawdown.position_strategy import _monthly_contribution_days
contrib_days = _monthly_contribution_days(all_days)

# Import needed functions
from drawdown.position_strategy import (
    _execute_crossed_tranches,
    _execute_sell_strategy,
    _rearm_buy_tranches_after_repair,
    _rearm_buy_tranches_after_position_sell,
    _mark_consumed_tranches_from_position,
    _position_value_usd,
    _price_usd,
    _avg_cost_usd,
)
from drawdown.strategy_rules import point_drawdown_pct

# Manual simulation loop with debug logging
position_applied = False
total_monthly = 0
portfolio_values = []

for current_day in all_days:
    if inputs.monthly_contribution > 0 and current_day in contrib_days:
        for tgt in [target]:
            contribution = inputs.monthly_contribution * tgt.weight / 100.0
            state.cash += contribution
            state.budget += contribution
            total_monthly += contribution

    point = point_by_day.get(current_day)
    if point is None:
        continue

    state.last_price = point.close
    state.last_value = _position_value_usd(symbol, state.shares, point.close, inputs)
    trade_index = trading_index[current_day]
    drawdown_pct = point_drawdown_pct(point, inputs)

    # Mark consumed from position on first non-DCA day
    if not position_applied:
        _mark_consumed_tranches_from_position(state, tranches_for_symbol, executed)
        position_applied = True

    # Rearm logic
    old_executed = dict(executed)
    old_anchor = state.buy_rearm_anchor_drawdown_pct
    old_rearm = state.buy_rearm_drawdown_pct

    _rearm_buy_tranches_after_repair(state, point, executed, inputs, tranches_for_symbol)
    _rearm_buy_tranches_after_position_sell(state, point, executed, inputs, tranches_for_symbol)

    # Log rearm events in focus period
    if focus_start <= current_day <= focus_end:
        if executed != old_executed or old_anchor != state.buy_rearm_anchor_drawdown_pct or old_rearm != state.buy_rearm_drawdown_pct:
            print(f"[REARM] {current_day} dd={drawdown_pct:.2f}% executed_keys={list(executed.keys())} "
                  f"anchor={state.buy_rearm_anchor_drawdown_pct} rearm_pct={state.buy_rearm_drawdown_pct}")

    # Buy logic
    bought = _execute_crossed_tranches(
        state, point, tranches_for_symbol, executed, inputs, trade_log,
        'linear_weighted_slice', 'cost_deleverage',
    )

    # Sell logic
    if not bought or inputs.sell_allow_same_day_sell:
        _execute_sell_strategy(state, point, inputs, 'linear_weighted_slice', 'cost_deleverage', trade_log, trade_index)

    # Log buy/sell in focus period
    if focus_start <= current_day <= focus_end:
        new_trades = [t for t in trade_log if t['date'] == current_day.isoformat()]
        for t in new_trades:
            if t['action'] == 'buy':
                print(f"[BUY]  {current_day} dd={t['drawdown_pct']:.2f}% eff_threshold={t['threshold_pct']:.2f}% "
                      f"base={t.get('base_threshold_pct', 0):.2f}% anchor={t.get('buy_rearm_anchor_drawdown_pct')} "
                      f"sell_rearmed={t.get('sell_cycle_rearmed', False)}")
            elif t['action'] == 'sell':
                print(f"[SELL] {current_day} dd={t['drawdown_pct']:.2f}% stage={t.get('stage', '-')} "
                      f"profit={t.get('estimated_profit_pct', 0):.2f}%")

    # Log state on target date
    if current_day == focus_date:
        print(f"\n=== STATE ON {focus_date} ===")
        print(f"  drawdown_120: {abs(point.drawdown_120)*100:.2f}%")
        print(f"  shares: {state.shares:.4f}")
        print(f"  cash: {state.cash:.2f}")
        print(f"  lots: {len(state.lots)}")
        print(f"  sell_marks: {state.sell_marks}")
        print(f"  executed keys: {list(executed.keys())}")
        for k, v in sorted(executed.items()):
            print(f"    executed[{k}] = {v}")
        print(f"  buy_rearm_anchor: {state.buy_rearm_anchor_drawdown_pct}")
        print(f"  buy_rearm_drawdown_pct: {state.buy_rearm_drawdown_pct}")
        print(f"  cost_deleverage_cycle_anchor: {state.cost_deleverage_cycle_anchor_price}")
        
        # Which tranches would trigger?
        anchor = max(0.0, float(state.buy_rearm_anchor_drawdown_pct or 0.0))
        print(f"\n  Tranche trigger analysis (anchor={anchor:.4f}%):")
        for tr in tranches_for_symbol:
            eff = anchor + tr.threshold_pct
            triggered = drawdown_pct + 1e-9 >= eff
            already = executed.get(round(tr.threshold_pct, 8), 0.0)
            print(f"    T({tr.threshold_pct:.2f}%) -> eff={eff:.2f}% triggered={triggered} already_executed={already:.4f}")

# Also show what the Python engine result is
print("\n\n=== Running full _simulate_strategy for comparison ===")
result = _simulate_strategy(
    {symbol: points}, [target], inputs,
    'linear_weighted_slice', 'cost_deleverage',
)
for t in result['trades']:
    if focus_start <= date.fromisoformat(t['date']) <= focus_end:
        action = t['action'].upper()
        if action == 'BUY':
            print(f"  [{action}] {t['date']} dd={t['drawdown_pct']:.2f}% eff={t['threshold_pct']:.2f}% "
                  f"base={t.get('base_threshold_pct', 0):.2f}% anchor={t.get('buy_rearm_anchor_drawdown_pct')} "
                  f"rearmed={t.get('sell_cycle_rearmed', False)}")
        else:
            print(f"  [{action}] {t['date']} dd={t['drawdown_pct']:.2f}% stage={t.get('stage', '-')} "
                  f"profit={t.get('estimated_profit_pct', 0):.2f}%")
