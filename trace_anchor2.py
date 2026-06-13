#!/usr/bin/env python3
"""Detailed trace of the second anchor issue.

After fix#1 (clearing buy_rearm_drawdown_pct in rearm_after_repair),
there's still an issue with restart_from_rearm mode:

Timeline:
1. 2023-09-11: sell cost_1 at dd=6.74%
2. markBuyRearmAfterPositionSell: buy_rearm_drawdown_pct = 6.74% + 4.21% = 10.95%
3. 2023-09-21: dd=12.83% >= 10.95% → rearm fires
4. restart_from_rearm → anchor = 12.83%
5. This shifts ALL thresholds up by 12.83%

On 2023-10-30, dd=32.72%:
  T1(9.95%) → eff=22.78% ≤ 32.72% ✓ but already consumed
  T2(19.90%) → eff=32.73% > 32.72% ✗ by 0.01%!
  T3(29.85%) → eff=42.68% > 32.72% ✗

The anchor=12.83% makes T2's effective threshold = 32.73%,
which is 0.01% above the actual drawdown of 32.72%.

Is this the intended behavior of restart_from_rearm?
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
    SymbolState,
    build_strategy_tranches,
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

quote_ctx = build_longbridge_quote_context()
symbol = 'TSLA.US'
candles = fetch_longbridge_daily_candles(quote_ctx, symbol, date(2023, 6, 1), date(2024, 6, 1))
series = [(candle_datetime(c).replace(tzinfo=None), float(c.close)) for c in candles]
points = build_price_points_from_series(series)

target = PortfolioTarget(symbol=symbol, weight=100.0, name='TSLA', max_drawdown_pct=47.8)
effective_inputs = _inputs_for_target(inputs, target)
tranches_sym = build_strategy_tranches(effective_inputs, 'linear_weighted_slice')

# Manual simulation
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

# Focus on the period around the second rearm
focus_start = date(2023, 9, 1)
focus_end = date(2023, 11, 30)

for current_day in all_days:
    if inputs.monthly_contribution > 0 and current_day in contrib_days:
        state.cash += inputs.monthly_contribution
        state.budget += inputs.monthly_contribution

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

    old_exec = dict(executed)
    old_anchor = state.buy_rearm_anchor_drawdown_pct
    old_rearm = state.buy_rearm_drawdown_pct

    _rearm_buy_tranches_after_repair(state, point, executed, inputs, tranches_sym)
    _rearm_buy_tranches_after_position_sell(state, point, executed, inputs, tranches_sym)

    if focus_start <= current_day <= focus_end:
        anchor_now = state.buy_rearm_anchor_drawdown_pct
        rearm_now = state.buy_rearm_drawdown_pct
        if executed != old_exec or old_anchor != anchor_now or old_rearm != rearm_now:
            print(f"[REARM] {current_day} dd={drawdown_pct:.2f}% "
                  f"exec_keys={sorted(executed.keys())} anchor={anchor_now} rearm_pct={rearm_now}")

    bought = _execute_crossed_tranches(
        state, point, tranches_sym, executed, inputs, trade_log,
        'linear_weighted_slice', 'cost_deleverage',
    )

    if not bought or inputs.sell_allow_same_day_sell:
        _execute_sell_strategy(state, point, inputs, 'linear_weighted_slice', 'cost_deleverage', trade_log, trade_index)

    if focus_start <= current_day <= focus_end:
        new_trades = [t for t in trade_log if t['date'] == current_day.isoformat()]
        for t in new_trades:
            if t['action'] == 'buy':
                anchor_val = t.get('buy_rearm_anchor_drawdown_pct')
                print(f"[BUY]  {current_day} dd={t['drawdown_pct']:.2f}% eff={t['threshold_pct']:.2f}% "
                      f"base={t.get('base_threshold_pct', 0):.2f}% anchor={anchor_val}")
            elif t['action'] == 'sell':
                rearm_dd = min(float(inputs.max_drawdown_pct), drawdown_pct + min(float(inputs.dca_rearm_drawdown_pct), float(inputs.max_drawdown_pct)))
                print(f"[SELL] {current_day} dd={t['drawdown_pct']:.2f}% stage={t.get('stage', '-')} "
                      f"profit={t.get('estimated_profit_pct', 0):.2f}% -> buy_rearm_drawdown_pct={rearm_dd:.2f}%")

# Detailed analysis of 2023-10-30
target_date = date(2023, 10, 30)
point = point_by_day.get(target_date)
if point:
    dd = abs(point.drawdown_120) * 100
    anchor = max(0.0, float(state.buy_rearm_anchor_drawdown_pct or 0.0))
    print(f"\n\n=== {target_date} ===")
    print(f"  dd120 = {dd:.2f}%")
    print(f"  anchor = {anchor:.4f}%")
    for tr in tranches_sym:
        eff = anchor + tr.threshold_pct
        triggered = dd + 1e-9 >= eff
        already = executed.get(round(tr.threshold_pct, 8), 0.0)
        gap = dd - eff
        print(f"  T({tr.threshold_pct:.2f}%): eff={eff:.4f}% gap={gap:.4f}% triggered={triggered} already={already:.2f}")

# The real question: is the anchor=12.83% correct?
# After sell on 2023-09-11 at dd=6.74%, markBuyRearm sets buy_rearm_drawdown_pct = 10.95%.
# On 2023-09-21, dd=12.83% >= 10.95%, so rearm fires.
# In restart_from_rearm mode, anchor = 12.83% (the drawdown at rearm moment).
# This means: "the position was re-armed when drawdown was 12.83%,
# so thresholds start from there" → T1 starts at 12.83+9.95=22.78%.
#
# The INTENT of restart_from_rearm is: after a sell+rearm, only buy at
# drawdown levels DEEPER than where the rearm happened.
# So if rearm happened at 12.83%, you don't want to buy at T1(9.95%)
# because that's a SHALLOWER drawdown than the rearm point.
#
# This makes sense as a design choice. But the user expects T3(29.85%)
# to trigger at dd=32.72%. With anchor=12.83%, T3's effective threshold
# becomes 42.68%, which is way above 32.72%.
#
# The issue is that the anchor is too high. 12.83% is the drawdown at
# rearm, but the rearm was triggered by buy_rearm_drawdown_pct = 10.95%.
# The gap between 10.95% and 12.83% is because the price kept dropping
# for a few days before reaching 10.95% drawdown.
#
# Wait no - the rearm triggers when dd >= buy_rearm_drawdown_pct.
# So on 2023-09-21, dd=12.83% >= 10.95%, and anchor is set to 12.83%.
# This is the drawdown on the day the rearm fires, not the buy_rearm_drawdown_pct.

print("\n\n=== ROOT CAUSE ANALYSIS ===")
print("Bug #1 (FIXED): _rearm_buy_tranches_after_repair didn't clear buy_rearm_drawdown_pct.")
print("  This caused a double rearm: first at dd<=0.5%, then again at dd>=buy_rearm_drawdown_pct.")
print()
print("Bug #2 (CURRENT): After fixing Bug #1, there's still an issue.")
print("  The sell on 2023-09-11 sets buy_rearm_drawdown_pct = dd(6.74%) + dca_rearm(4.21%) = 10.95%.")
print("  When dd reaches 12.83% on 2023-09-21, rearm fires with anchor=12.83%.")
print("  This shifts T3's effective threshold to 12.83% + 29.85% = 42.68%,")
print("  which exceeds the 32.72% drawdown on 2023-10-30.")
print()
print("  The question is: should the anchor be the drawdown at rearm moment,")
print("  or should it be the buy_rearm_drawdown_pct threshold?")
print()
print("  If anchor = buy_rearm_drawdown_pct (10.95% instead of 12.83%),")
print("  then T3's eff = 10.95% + 29.85% = 40.80%, still > 32.72%.")
print("  So this wouldn't fix the user's specific case.")
print()
print("  The fundamental issue: restart_from_rearm is designed to prevent")
print("  re-buying at shallower drawdowns than where the rearm happened.")
print("  But the user expects that a 32% drawdown should trigger T3(29.85%).")
print("  The anchor offsets the threshold, making it harder to trigger.")
print()
print("  With cumulative mode (anchor=None), T3 triggers correctly at 32.72%.")
print("  The user chose restart_from_rearm, which intentionally raises thresholds.")
print("  Is this really a bug, or is it the intended behavior of restart_from_rearm?")
print()
print("  Wait - let me re-check. The user said '只触发了19.90%的买入档位'.")
print("  This matches the ORIGINAL bug (before fix#1) where anchor=11.36%:")
print("  T2(19.90%) → eff=31.26% ≤ 32.72% ✓")
print("  T3(29.85%) → eff=41.21% > 32.72% ✗")
print("  So T2 was the deepest triggered tranche.")
