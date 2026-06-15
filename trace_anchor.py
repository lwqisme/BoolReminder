#!/usr/bin/env python3
"""Trace the exact bug: on 2023-10-30, why is anchor=11.36% instead of None?

With sim_start=2023-06-11 (no warmup), the simulation starts with no position.
The first buy is T1(9.95%) on 2023-06-26.
Then a sell on 2023-07-03 (cost_1).
After sell, markBuyRearmAfterPositionSell sets buy_rearm_drawdown_pct.
When drawdown drops to 0% (price recovers), rearm fires and sets anchor.
This anchor shifts all subsequent thresholds up by anchor%.
"""
import sys
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
    build_strategy_tranches,
    _simulate_strategy,
    _inputs_for_target,
    point_drawdown_pct,
    _position_value_usd,
    _price_usd,
    _mark_consumed_tranches_from_position,
    _execute_crossed_tranches,
    _execute_sell_strategy,
    _rearm_buy_tranches_after_repair,
    _rearm_buy_tranches_after_position_sell,
    _mark_buy_rearm_after_position_sell,
    _monthly_contribution_days,
    _avg_cost_usd,
)
from drawdown.strategy_rules import sell_stage_rearm_drawdown_pct

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

# Fetch and build points - just from 2023-06-11 (no warmup, matching what user sees)
quote_ctx = build_longbridge_quote_context()
symbol = 'TSLA.US'
candles = fetch_longbridge_daily_candles(quote_ctx, symbol, date(2023, 6, 1), date(2024, 6, 1))
series = [(candle_datetime(c).replace(tzinfo=None), float(c.close)) for c in candles]
points = build_price_points_from_series(series)

# Manual simulation with detailed tracing
target = PortfolioTarget(symbol=symbol, weight=100.0, name='TSLA', max_drawdown_pct=47.8)
effective_inputs = _inputs_for_target(inputs, target)
tranches_sym = build_strategy_tranches(effective_inputs, 'linear_weighted_slice')

state = SymbolState(
    symbol=symbol, name='TSLA', weight=100.0,
    budget=inputs.initial_cash, cash=inputs.initial_cash,
    lots=[], sell_marks=set(), price_history=points,
)

executed = {}
trade_log = []
point_by_day = {p.date.date(): p for p in points}
trading_index = {p.date.date(): i for i, p in enumerate(points)}
all_days = sorted(point_by_day.keys())
contrib_days = _monthly_contribution_days(all_days)
position_applied = False

focus_start = date(2023, 6, 11)
focus_end = date(2023, 12, 31)

for current_day in all_days:
    if inputs.monthly_contribution > 0 and current_day in contrib_days:
        contribution = inputs.monthly_contribution * target.weight / 100.0
        state.cash += contribution
        state.budget += contribution

    point = point_by_day.get(current_day)
    if point is None:
        continue

    state.last_price = point.close
    state.last_value = _position_value_usd(symbol, state.shares, point.close, inputs)
    trade_index = trading_index[current_day]
    drawdown_pct = point_drawdown_pct(point, inputs)

    if not position_applied:
        _mark_consumed_tranches_from_position(state, tranches_sym, executed)
        position_applied = True

    # Save state before rearm
    old_exec = dict(executed)
    old_anchor = state.buy_rearm_anchor_drawdown_pct
    old_rearm = state.buy_rearm_drawdown_pct

    _rearm_buy_tranches_after_repair(state, point, executed, inputs, tranches_sym)
    _rearm_buy_tranches_after_position_sell(state, point, executed, inputs, tranches_sym)

    # Log ALL days in focus period with state changes
    if focus_start <= current_day <= focus_end:
        anchor_now = state.buy_rearm_anchor_drawdown_pct
        rearm_now = state.buy_rearm_drawdown_pct
        exec_changed = executed != old_exec
        anchor_changed = old_anchor != anchor_now
        rearm_changed = old_rearm != rearm_now

        if exec_changed or anchor_changed or rearm_changed or drawdown_pct <= 1.0:
            print(f"[REARM] {current_day} dd={drawdown_pct:.2f}% "
                  f"exec_keys={sorted(executed.keys())} anchor={anchor_now} rearm_pct={rearm_now} "
                  f"(changed: exec={exec_changed} anchor={anchor_changed} rearm={rearm_changed})")

    # Buy
    bought = _execute_crossed_tranches(
        state, point, tranches_sym, executed, inputs, trade_log,
        'linear_weighted_slice', 'cost_deleverage',
    )

    # Sell
    if not bought or inputs.sell_allow_same_day_sell:
        _execute_sell_strategy(state, point, inputs, 'linear_weighted_slice', 'cost_deleverage', trade_log, trade_index)

    # Log trades
    if focus_start <= current_day <= focus_end:
        new_trades = [t for t in trade_log if t['date'] == current_day.isoformat()]
        for t in new_trades:
            if t['action'] == 'buy':
                anchor_val = t.get('buy_rearm_anchor_drawdown_pct')
                print(f"[BUY]  {current_day} dd={t['drawdown_pct']:.2f}% eff={t['threshold_pct']:.2f}% "
                      f"base={t.get('base_threshold_pct', 0):.2f}% anchor={anchor_val} "
                      f"rearmed={t.get('sell_cycle_rearmed', False)} "
                      f"shares={t['shares']:.2f} price={t['price']:.2f}")
            elif t['action'] == 'sell':
                print(f"[SELL] {current_day} dd={t['drawdown_pct']:.2f}% stage={t.get('stage', '-')} "
                      f"shares={t['shares']:.2f} profit={t.get('estimated_profit_pct', 0):.2f}%")
                # Show what _mark_buy_rearm_after_position_sell sets
                rearm_dd = min(float(inputs.max_drawdown_pct), drawdown_pct + min(float(inputs.dca_rearm_drawdown_pct), float(inputs.max_drawdown_pct)))
                print(f"       -> markBuyRearm: buy_rearm_drawdown_pct = {rearm_dd:.2f}% "
                      f"(dd={drawdown_pct:.2f}% + dca_rearm={inputs.dca_rearm_drawdown_pct}%)")

# Now show exactly what happens on 2023-10-30
target_date = date(2023, 10, 30)
point_oct30 = point_by_day.get(target_date)
if point_oct30:
    dd = abs(point_oct30.drawdown_120) * 100
    anchor = max(0.0, float(state.buy_rearm_anchor_drawdown_pct or 0.0))
    print(f"\n\n=== 2023-10-30 Analysis ===")
    print(f"  drawdown_120 = {dd:.2f}%")
    print(f"  current anchor = {anchor:.4f}%")
    print(f"  executed keys = {sorted(executed.keys())}")
    print()
    for tr in tranches_sym:
        eff = anchor + tr.threshold_pct
        triggered = dd + 1e-9 >= eff
        already = executed.get(round(tr.threshold_pct, 8), 0.0)
        print(f"  T({tr.threshold_pct:.2f}%): eff_threshold={eff:.2f}% triggered={triggered} already_executed={already:.2f}")

    print(f"\n  Expected by user: T3(29.85%) should trigger because dd=32.72% > 29.85%")
    print(f"  Actual: anchor={anchor:.2f}% shifts T3 eff to {anchor+29.85:.2f}% > 32.72%, so NOT triggered")
    print(f"  Instead T2(19.90%) triggers because eff={anchor+19.90:.2f}% <= 32.72%")

# Now let's also check: the no-warmup run shows anchor=11.36%
# Where does 11.36% come from?
# Answer: After sell on 2023-07-03, markBuyRearmAfterPositionSell sets
# buy_rearm_drawdown_pct. Then when drawdown drops to ~0%, 
# _rearm_buy_tranches_after_repair fires (dd <= 0.5%).
# This clears executed and sets anchor=None.
# BUT _rearm_buy_tranches_after_position_sell might also fire.
# Let me trace the exact sequence.

# Actually, looking at the output from the "no warmup" run:
# 2023-06-26 buy T1(9.95%) anchor=None
# 2023-07-03 sell cost_1
# 2023-10-30 buy base=19.90% anchor=11.36%
#
# The anchor=11.36% must come from a rearm event between 2023-07-03 and 2023-10-30.
# In restart_from_rearm mode, the anchor is set to the drawdown_pct at the moment
# the buy_rearm_drawdown_pct threshold is reached.

# Let me compute: after sell on 2023-07-03, what is buy_rearm_drawdown_pct?
# markBuyRearmAfterPositionSell:
#   threshold = min(max_dd, dd + min(dca_rearm, max_dd))
#   = min(47.8, 0 + min(4.21, 47.8))
#   = min(47.8, 4.21)
#   = 4.21
# Wait, dd on sell date is the drawdown on that day. Let me check.

print(f"\n\n=== Tracing the anchor origin ===")
print(f"After sell on 2023-07-03, buy_rearm_drawdown_pct is set.")
print(f"The sell happens when drawdown is low (price recovered).")
print(f"When drawdown later reaches 4.21%, rearm fires.")
print(f"In restart_from_rearm mode, anchor = drawdown_pct at rearm moment = 4.21%?")
print(f"But the actual anchor was 11.36%. So something else happened...")
print()
print("Let me check: maybe it's the sell_stage_rearm_drawdown_pct (16.68%)")
print("that triggers a different rearm path, or maybe there are multiple sells")
print("that each set a different buy_rearm_drawdown_pct.")
