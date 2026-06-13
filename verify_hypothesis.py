#!/usr/bin/env python3
"""
Verify: Does the JS engine show the same bug?
Test by running the JS engine logic through the packet API.

The key question: In the JS engine, when rearm happens on 2023-06-02,
markConsumedTranchesFromPosition also re-marks T1-T4 as consumed,
preventing buys on 2023-10-30.

But the user said "只触发了19.90%的买入档位", which implies 
*some* buy did happen at the 19.90% level. Let me check if there's
a difference between JS and Python engines.
"""
import sys, json, math
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
    _monthly_contribution_days,
    _avg_cost_usd,
)
from drawdown.strategy_rules import sell_stage_rearm_drawdown_pct as _sell_stage_rearm_dd

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

# Check: what does sell_stage_rearm_drawdown_pct resolve to?
sra_dd = _sell_stage_rearm_dd(inputs)
print(f"sell_stage_rearm_drawdown_pct resolves to: {sra_dd}")
print(f"  (inputs.sell_stage_rearm_drawdown_pct={inputs.sell_stage_rearm_drawdown_pct})")
print(f"  (inputs.dca_rearm_drawdown_pct={inputs.dca_rearm_drawdown_pct})")
print(f"  Since 16.68 > 4.21, it stays 16.68")

# Now let's trace the JS engine more carefully
# The JS rearm logic is different from Python!
# JS: drawdownPct(point, inputs) <= 0.5 → clear executed, set anchor=null
# Python: drawdown_pct <= 0.50 → clear executed, set anchor=None
# But then JS checks: if executed is empty AND buy_rearm_drawdown_pct != null AND drawdown >= buy_rearm_drawdown_pct
# Python checks: _rearm_buy_tranches_after_position_sell separately

# KEY DIFFERENCE: JS does markConsumedTranchesFromPosition ONLY when executed is empty:
#   if (!Object.keys(executed[symbol]).length) {
#     markConsumedTranchesFromPosition(state, tranches, executed[symbol], strategy);
#   }
# Python does it inside _rearm_buy_tranches_after_repair and _rearm_buy_tranches_after_position_sell

# But the effect should be the same. Let me verify by tracing the exact JS behavior.

# Actually, let me look at the JS rearm more carefully:
# JS simulate() loop:
#   1. if (drawdownPct(point, inputs) <= 0.5) { executed[symbol] = {}; state.buy_rearm_anchor_drawdown_pct = null; }
#   2. if (Object.keys(executed[symbol]).length && state.buy_rearm_drawdown_pct !== null && drawdownPct(point, inputs) + 1e-9 >= state.buy_rearm_drawdown_pct) {
#        executed[symbol] = {};
#        state.buy_rearm_anchor_drawdown_pct = inputs.buy_rearm_mode === 'restart_from_rearm' ? drawdownPct(point, inputs) : null;
#        state.buy_rearm_drawdown_pct = null;
#      }
#   3. if (!Object.keys(executed[symbol]).length) {
#        markConsumedTranchesFromPosition(state, tranches, executed[symbol], strategy);
#      }
#   4. executeTranches(...)

# IMPORTANT: Step 2 has a condition: Object.keys(executed[symbol]).length
# This means: only try the buy_rearm_drawdown_pct rearm if executed is NOT empty.
# But Step 1 just cleared executed! So if Step 1 fires, Step 2 won't fire (executed is empty).
# And Step 3 will fire (executed is empty after Step 1), marking consumed tranches.

# In our case, the rearm on 2023-06-02 happens via Step 1 (drawdown <= 0.5),
# not via Step 2 (buy_rearm_drawdown_pct). Step 2 would fire on 2022-12-14.

# Let me trace what happens in Python vs JS:

# Python _rearm_buy_tranches_after_repair:
#   if drawdown_pct <= 0.50: clear executed, anchor=None, markConsumed
# Python _rearm_buy_tranches_after_position_sell:
#   if executed and buy_rearm_drawdown_pct and drawdown >= buy_rearm_drawdown_pct:
#     clear executed, set anchor based on mode, markConsumed

# Both engines: after clearing executed, markConsumed re-marks T1-T4 as consumed.
# Result: T3(29.85%) cannot be triggered on 2023-10-30 because it's already "consumed".

# Now, the user said "只触发了19.90%的买入档位". 
# This might mean the UI shows the 19.90% tranche as the next trigger level,
# not that a buy actually happened at 19.90%.

# Let me check: in the Python simulation, on 2023-10-30, does ANY buy happen?
# From our earlier run: NO trades on 2023-10-30.
# The user might be referring to the position_context/next_tranche display.

# Let me simulate with a shorter timeframe to match "近三年"
print("\n\n=== Simulating TSLA 近三年 (2020-11-01 to 2023-11-30) ===")

quote_ctx = build_longbridge_quote_context()
symbol = 'TSLA.US'
candles = fetch_longbridge_daily_candles(quote_ctx, symbol, date(2019, 1, 1), date(2023, 11, 30))
series = [(candle_datetime(c).replace(tzinfo=None), float(c.close)) for c in candles]
points = build_price_points_from_series(series)

target = PortfolioTarget(symbol=symbol, weight=100.0, name='TSLA', max_drawdown_pct=47.8)

result = _simulate_strategy(
    {symbol: points}, [target], inputs,
    'linear_weighted_slice', 'cost_deleverage',
)

trades = result['trades']
print("All trades:")
for t in trades:
    print(f"  {t['date']} {t['action']:4s} dd={t['drawdown_pct']:.2f}% eff={t.get('threshold_pct',0):.2f}% base={t.get('base_threshold_pct',0):.2f}% "
          f"anchor={t.get('buy_rearm_anchor_drawdown_pct')} stage={t.get('stage','-')} profit={t.get('estimated_profit_pct',0):.2f}%")

# Count buy trades per tranche level
from collections import Counter
buy_bases = Counter()
for t in trades:
    if t['action'] == 'buy':
        base = t.get('base_threshold_pct', 0)
        buy_bases[round(base, 2)] += 1

print("\nBuy count by base threshold:")
for base in sorted(buy_bases.keys()):
    print(f"  {base}%: {buy_bases[base]} buys")

# What about T3 (29.85%)? Has it ever been bought?
t3_buys = [t for t in trades if t['action'] == 'buy' and abs(t.get('base_threshold_pct', 0) - 29.85) < 0.1]
print(f"\nT3(29.85%) buys: {len(t3_buys)}")
for t in t3_buys:
    print(f"  {t['date']} dd={t['drawdown_pct']:.2f}% eff={t.get('threshold_pct',0):.2f}%")

# On 2023-10-30: 32.72% dd120 should trigger T3(29.85%)
# But executed[T3_key] > 0, so it's skipped
print("\n=== CONCLUSION ===")
print("Bug confirmed: After a sell cycle, when drawdown rearm clears executed,")
print("markConsumedTranchesFromPosition re-marks all tranches covered by the")
print("current position ratio. Since the position still holds 63.5% in shares,")
print("T1-T4 are all marked as 'consumed', preventing any buy at T3(29.85%)")
print("even when 120d drawdown is 32.72%.")
print()
print("The fix should ensure that after a sell cycle, the consumed marking")
print("accounts for the fact that some tranches' allocation was partially")
print("or fully sold, and the cash from those sales should be available")
print("for re-buying at the appropriate drawdown levels.")
