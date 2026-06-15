#!/usr/bin/env python3
"""Trace max_drawdown_pct through the entire pipeline:
1. GA evolves max_drawdown_pct=28.7%
2. Parameter lab saves it as strategy parameter
3. Packet API builds tasks with targets
4. JS engine's targetMaxDrawdown() reads target.max_drawdown_pct
5. buildTranches() uses max_drawdown_pct to build tranches

Question: Where does 50.0 override 28.7%?
"""
import sys
sys.path.insert(0, '/app')

from drawdown.position_strategy import (
    StrategyInputs,
    _resolve_scorecard_portfolios,
    _merge_universe_scorecard_portfolios,
    DEFAULT_INVESTMENT_UNIVERSE,
    SCORECARD_PORTFOLIOS,
    build_strategy_tranches,
    _inputs_for_target,
    PortfolioTarget,
)

# Step 1: GA evolves max_drawdown_pct=28.7%
print("=== Step 1: GA output ===")
print("max_drawdown_pct = 28.7%")

# Step 2: This becomes the strategy parameter (StrategyInputs.max_drawdown_pct)
inputs = StrategyInputs(max_drawdown_pct=28.7, step_pct=5.7)
print(f"\n=== Step 2: StrategyInputs ===")
print(f"max_drawdown_pct = {inputs.max_drawdown_pct}")

# Step 3: _resolve_scorecard_portfolios builds the task targets
# The investment_universe is the default one with max_drawdown_pct values
print(f"\n=== Step 3: DEFAULT_INVESTMENT_UNIVERSE ===")
for item in DEFAULT_INVESTMENT_UNIVERSE:
    print(f"  {item['symbol']}: max_drawdown_pct={item.get('max_drawdown_pct')}")

print(f"\n=== Step 3: SCORECARD_PORTFOLIOS (original) ===")
for p in SCORECARD_PORTFOLIOS:
    if p['key'] == 'tsla_100':
        print(f"  {p['key']}: targets={p['targets']}")
        break

# Resolve scorecard portfolios (what the packet API does)
portfolios = _resolve_scorecard_portfolios(
    core_targets=None,
    portfolio_keys=['tsla_100'],
    investment_universe=DEFAULT_INVESTMENT_UNIVERSE,
)
print(f"\n=== Step 3: After _resolve_scorecard_portfolios ===")
for p in portfolios:
    if p['key'] == 'tsla_100':
        print(f"  {p['key']}: targets={p['targets']}")
        for t in p['targets']:
            print(f"    target.max_drawdown_pct = {t.get('max_drawdown_pct')}")
        break

# Step 4: This target goes to _simulate_strategy → _inputs_for_target
print(f"\n=== Step 4: _inputs_for_target ===")
target = PortfolioTarget(
    symbol='TSLA.US', weight=100.0, name='TSLA',
    max_drawdown_pct=50.0  # <-- This is the problem
)
effective_inputs = _inputs_for_target(inputs, target)
print(f"StrategyInputs.max_drawdown_pct = {inputs.max_drawdown_pct}")
print(f"target.max_drawdown_pct = {target.max_drawdown_pct}")
print(f"effective_inputs.max_drawdown_pct = {effective_inputs.max_drawdown_pct}")
print(f"→ GA evolved 28.7% is OVERWRITTEN by target 50.0%!")

# Step 5: build_strategy_tranches uses effective_inputs
tranches_28 = build_strategy_tranches(inputs, 'linear_weighted_slice')
tranches_50 = build_strategy_tranches(effective_inputs, 'linear_weighted_slice')
print(f"\n=== Step 5: Tranches comparison ===")
print(f"With max_dd=28.7% (GA evolved): {len(tranches_28)} tranches")
for t in tranches_28:
    print(f"  {t.threshold_pct:.2f}%")
print(f"\nWith max_dd=50.0% (target override): {len(tranches_50)} tranches")
for t in tranches_50:
    print(f"  {t.threshold_pct:.2f}%")

# Now trace the _merge_universe_scorecard_portfolios specifically
print(f"\n\n=== ROOT CAUSE: _merge_universe_scorecard_portfolios ===")
print(f"SCORECARD_PORTFOLIOS originally has tsla_100 WITHOUT max_drawdown_pct:")
for p in SCORECARD_PORTFOLIOS:
    if p['key'] == 'tsla_100':
        print(f"  targets={p['targets']}")
        break

print(f"\nDEFAULT_INVESTMENT_UNIVERSE has TSLA WITH max_drawdown_pct=50.0:")
for item in DEFAULT_INVESTMENT_UNIVERSE:
    if 'TSLA' in item['symbol']:
        print(f"  {item}")
        break

print(f"\n_merge_universe_scorecard_portfolios finds existing tsla_100 portfolio,")
print(f"then REPLACES its targets with the universe entry (which has max_drawdown_pct=50.0)")
print(f"\nCode path:")
print(f'  existing = by_symbol.get(symbol)  # finds tsla_100')
print(f'  existing["targets"] = [target]     # replaces with {max_drawdown_pct: 50.0}')

# The comment in _resolve_scorecard_portfolios says:
# "Per-symbol max_drawdown_pct from scoring topics applies ONLY to the core portfolio."
# But _merge_universe_scorecard_portfolios ignores this rule!

print(f"\n=== THE COMMENT vs THE CODE ===")
print(f"Comment: 'Per-symbol max_drawdown_pct applies ONLY to the core portfolio.'")
print(f"Reality: _merge_universe_scorecard_portfolios injects max_drawdown_pct into ALL")
print(f"         scorecard portfolios, including 全仓 TSLA, overriding GA's evolution.")

# Also check: does the JS engine's targetMaxDrawdown exacerbate this?
print(f"\n=== JS ENGINE: targetMaxDrawdown ===")
print(f"function targetMaxDrawdown(targets, symbol, inputs) {{")
print(f"  const target = targets.find(item => item.symbol === symbol);")
print(f"  return target && target.max_drawdown_pct != null")
print(f"    ? num(target.max_drawdown_pct)   // returns 50.0")
print(f"    : num(inputs.max_drawdown_pct);   // would return 28.7")
print(f"}}")
print(f"\nIf target.max_drawdown_pct were absent (null), JS would fall through to")
print(f"inputs.max_drawdown_pct = 28.7%, which is the GA-evolved value.")
print(f"\nThe Python _inputs_for_target has the same issue:")
print(f"  if target.max_drawdown_pct is None: return inputs  (keeps 28.7%)")
print(f"  else: replace(max_drawdown_pct=50.0)               (overrides to 50.0)")
