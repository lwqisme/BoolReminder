#!/usr/bin/env python3
"""Explain: with max_dd=50%, step=5.7%, why does first buy after sell happen at 36.9%?

Key: the first buy cycle already executed all tranches up to 45.6%.
After sells complete a cost_deleverage cycle, rearm clears executed.
But the position still covers many tranches, so markConsumed blocks them.
Only deeper tranches (34.20%+) are not yet covered by the position.
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

# Target override: max_dd=50.0%
target = PortfolioTarget(symbol='TSLA.US', weight=100.0, name='TSLA', max_drawdown_pct=50.0)

# Show tranches with target override
from drawdown.position_strategy import _inputs_for_target
effective_inputs = _inputs_for_target(inputs, target)
tranches = build_strategy_tranches(effective_inputs, 'linear_weighted_slice')

print("=== 有效档位 (TSLA target max_dd=50.0%, step=5.7%) ===")
cum_alloc = 0
for i, t in enumerate(tranches):
    cum_alloc += t.allocation_pct
    print(f"  T{i+1}: threshold={t.threshold_pct:.2f}% allocation={t.allocation_pct:.2f}% cum_alloc={cum_alloc:.2f}%")

# Fetch data
quote_ctx = build_longbridge_quote_context()
symbol = 'TSLA.US'
candles = fetch_longbridge_daily_candles(quote_ctx, symbol, date(2023, 6, 1), date(2025, 6, 1))
series = [(candle_datetime(c).replace(tzinfo=None), float(c.close)) for c in candles]
points = build_price_points_from_series(series)

result = _simulate_strategy(
    {symbol: points}, [target], inputs,
    'linear_weighted_slice', 'cost_deleverage',
)
trades = result['trades']

# Show all trades with rearm state tracking
print("\n=== 完整交易时间线 ===")
prev_sell_date = None
for t in trades:
    if t['action'] == 'buy':
        anchor = t.get('buy_rearm_anchor_drawdown_pct')
        anchor_str = f"anchor={anchor:.2f}%" if anchor is not None else "anchor=None"
        print(f"  {t['date']} BUY  dd={t['drawdown_pct']:.2f}% eff_threshold={t.get('threshold_pct',0):.2f}% "
              f"base_threshold={t.get('base_threshold_pct',0):.2f}% {anchor_str} "
              f"rearmed={t.get('sell_cycle_rearmed', False)}")
    else:
        stage = t.get('stage', '-')
        print(f"  {t['date']} SELL dd={t['drawdown_pct']:.2f}% stage={stage} "
              f"profit={t.get('estimated_profit_pct',0):.2f}%")
        prev_sell_date = t['date']

# Now trace the KEY moment: why no buy between the sells and Feb 2025?
print("\n\n=== 关键分析：为什么 2024-12 到 2025-02 之间没有更早的买入？ ===")

# After the cost_3 sell on 2024-11-11, a full sell cycle completes.
# cost_deleverage: when cost_3 fires, sell_marks are cleared and cycle_anchor is updated.
# Then on 2024-12-10, another cost_1 sell happens (new cycle).
# After each sell, markBuyRearm sets buy_rearm_drawdown_pct.

# The question: what's the position state after all sells?
# After sells on 2024-11-06 to 2024-12-10, most shares are sold.
# Let me compute the position at that point.

total_buy_shares = 0
total_sell_shares = 0
total_buy_gross = 0
total_sell_net = 0
for t in trades:
    if t['action'] == 'buy' and t['date'] <= '2024-12-16':
        total_buy_shares += t['shares']
        total_buy_gross += t['gross_amount']
    elif t['action'] == 'sell' and t['date'] <= '2024-12-16':
        total_sell_shares += t['shares']
        total_sell_net += t['net_amount']

# Monthly contributions
from drawdown.position_strategy import _monthly_contribution_days
all_days = sorted({p.date.date() for p in points})
contrib_days = _monthly_contribution_days(all_days)
months = len([d for d in contrib_days if d <= date(2024, 12, 16)])
total_cash = inputs.initial_cash + months * inputs.monthly_contribution - total_buy_gross + total_sell_net

net_shares = total_buy_shares - total_sell_shares
# Find price on 2024-12-16
price_dec16 = None
for p in points:
    if p.date.date() == date(2024, 12, 16):
        price_dec16 = p.close
        break

if price_dec16:
    market_value = net_shares * price_dec16
    total_value = total_cash + market_value
    invested_ratio = market_value / total_value if total_value > 0 else 0

    print(f"  2024-12-16 持仓状态:")
    print(f"    净持仓: {net_shares:.4f} 股")
    print(f"    现金: ${total_cash:.2f}")
    print(f"    市值: ${market_value:.2f}")
    print(f"    总资产: ${total_value:.2f}")
    print(f"    投资比例: {invested_ratio*100:.2f}%")

    print(f"\n  markConsumed 分析:")
    cum = 0
    for t in sorted(tranches, key=lambda x: x.threshold_pct):
        cum += t.allocation_pct / 100
        consumed = invested_ratio >= cum - 1e-9
        print(f"    T({t.threshold_pct:.2f}%): cum_alloc={cum*100:.2f}% consumed={consumed}")

    print(f"\n  解释:")
    print(f"    卖出后投资比例仍为 {invested_ratio*100:.2f}%，")
    print(f"    这意味着持仓覆盖了累积分配比例低于 {invested_ratio*100:.2f}% 的所有档位。")
    
    # Find the first non-consumed tranche
    cum = 0
    first_unconsumed = None
    for t in sorted(tranches, key=lambda x: x.threshold_pct):
        cum += t.allocation_pct / 100
        if invested_ratio < cum - 1e-9:
            first_unconsumed = t.threshold_pct
            break
    
    if first_unconsumed is not None:
        print(f"    第一个未覆盖的档位: T({first_unconsumed:.2f}%)")
        print(f"    所以只有回撤 ≥ {first_unconsumed:.2f}% 时才会触发新的买入。")
    else:
        print(f"    所有档位都被覆盖！不会触发新买入。")

# Now let's also check: what about rearm + anchor?
# After sell on 2024-12-10 (cost_1), markBuyRearm sets buy_rearm_drawdown_pct
# dd=0% on 2024-12-10, so buy_rearm = 0 + 4.75 = 4.75%
# After sell on 2024-12-16 (another cost_1?), same

# The key: after sells complete, when does rearm fire?
# price recovery to dd <= 0.5% → rearm_after_repair fires
# Or dd >= buy_rearm_drawdown_pct → rearm_after_position_sell fires

print(f"\n\n=== Rearm 时间线 ===")
print(f"  2024-12-10 卖出 cost_1 → buy_rearm_drawdown_pct = 0% + 4.75% = 4.75%")
print(f"  但接下来 TSLA 开始大跌，dd 直接从 0% 跳到 36.9%")
print(f"  当 dd ≥ 4.75% 时，rearm_after_position_sell 触发")
print(f"  restart_from_rearm 模式 → anchor = 4.75% 时的 drawdown")

# Actually, let me check when dd first exceeds 4.75% after 2024-12-10
print(f"\n  2024-12-10 之后的回撤变化:")
for p in points:
    if date(2024, 12, 10) <= p.date.date() <= date(2025, 3, 15):
        dd120 = abs(p.drawdown_120) * 100
        if dd120 >= 4.0 or p.date.date() in [date(2025, 2, 25), date(2025, 2, 27), date(2025, 3, 10)]:
            anchor_note = ""
            if dd120 >= 4.75:
                anchor_note = " ← dd ≥ 4.75%, rearm 可能触发"
            print(f"    {p.date.date()} close={p.close:.2f} dd120={dd120:.2f}%{anchor_note}")
