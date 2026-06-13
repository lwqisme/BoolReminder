/**
 * test_sell_stage_rearm_parity.js
 *
 * Cross-engine parity guard for `drop_from_last_sell` rearm threshold logic.
 * Python `sell_stage_rearm_drawdown_pct(inputs)` falls back to dca_rearm in
 * two cases:
 *   1. raw is None
 *   2. raw <= dca_threshold  ← previously not mirrored in JS
 * JS `rearmAfterDcaBuy` historically only handled case (1) via `??`.
 * This test exercises both engines through their real entry points and
 * asserts the rearm decision matches.
 */
'use strict';

const fs = require('fs');
const path = require('path');
const assert = require('assert');
const { execFileSync } = require('child_process');

// Load worker source as a string and eval it under a fake `self` so we can
// reach into rearmAfterDcaBuy directly.
const workerSrc = fs.readFileSync(
  path.join(__dirname, 'web', 'static', 'strategy_parameter_lab_worker.js'),
  'utf-8'
);

const sandbox = {
  self: { onmessage: null, location: { href: 'http://test/' } },
  performance: { now: () => Date.now() },
  console
};
// Stub Worker / postMessage references so eval doesn't blow up at load time.
sandbox.self.postMessage = () => {};

// Pull rearmAfterDcaBuy out by suffixing an export hook to the loaded source.
const harness = workerSrc + '\n;sandbox.__rearm = rearmAfterDcaBuy;';
const fn = new Function('sandbox', 'self', 'performance', 'console', harness);
fn(sandbox, sandbox.self, sandbox.performance, console);
const rearmAfterDcaBuy = sandbox.__rearm;
assert.ok(typeof rearmAfterDcaBuy === 'function', 'rearmAfterDcaBuy must be exported');

function makeState(opts = {}) {
  return {
    sell_marks: { cost_1: true },
    last_position_sell_price: opts.lastSell ?? 100.0,
    grid_rebound_cycle_anchor_drawdown_pct: null,
    grid_rebound_last_sell_drawdown_pct: null
  };
}

function makeInputs(opts = {}) {
  return {
    max_drawdown_pct: opts.max_drawdown_pct ?? 50,
    dca_rearm_drawdown_pct: opts.dca_rearm_drawdown_pct ?? 0,
    sell_stage_rearm_drawdown_pct: opts.sell_stage_rearm_drawdown_pct,
    sell_stage_rearm_mode: opts.sell_stage_rearm_mode ?? 'drop_from_last_sell',
    sell_allow_same_day_sell: false
  };
}

function callPython(inputs, currentPrice) {
  const out = execFileSync('python3', ['-c', `
import json, sys
sys.path.insert(0, '.')
from drawdown.position_strategy import StrategyInputs, SymbolState, _rearm_position_sell_cycle_after_dca_buy
inp = StrategyInputs(
    max_drawdown_pct=${inputs.max_drawdown_pct},
    dca_rearm_drawdown_pct=${inputs.dca_rearm_drawdown_pct},
    sell_stage_rearm_drawdown_pct=${inputs.sell_stage_rearm_drawdown_pct === null ? 'None' : inputs.sell_stage_rearm_drawdown_pct},
    sell_stage_rearm_mode=${JSON.stringify(inputs.sell_stage_rearm_mode)},
)
state = SymbolState(
    symbol='TSLA.US', name='TSLA', weight=100.0, budget=10000, cash=10000,
    sell_marks={'cost_1'}, last_position_sell_price=100.0,
)
ok = _rearm_position_sell_cycle_after_dca_buy(state, drawdown_pct=0.0, inputs=inp,
    sell_strategy='cost_deleverage', current_price=${currentPrice})
print(json.dumps({'rearmed': bool(ok)}))
`], { encoding: 'utf-8' });
  return JSON.parse(out.trim());
}

function check(label, inputsOpts, currentPrice, expected) {
  const stateJs = makeState();
  const inputs = makeInputs(inputsOpts);
  const jsRearmed = rearmAfterDcaBuy(stateJs, /*drawdown=*/0, inputs, 'cost_deleverage', currentPrice);
  const pyResult = callPython(inputs, currentPrice);
  console.log(`  ${label}: js=${jsRearmed} python=${pyResult.rearmed} expected=${expected}`);
  assert.strictEqual(jsRearmed, expected, `JS expected ${expected}, got ${jsRearmed}`);
  assert.strictEqual(pyResult.rearmed, expected, `Python expected ${expected}, got ${pyResult.rearmed}`);
  assert.strictEqual(jsRearmed, pyResult.rearmed, `parity broken on ${label}`);
}

console.log('drop_from_last_sell rearm parity');

// Case A: raw=null (already passing for both)
check('raw=null, dca=10, drop=12% → rearm', { dca_rearm_drawdown_pct: 10, sell_stage_rearm_drawdown_pct: null }, 88.0, true);
check('raw=null, dca=10, drop=8% → no rearm', { dca_rearm_drawdown_pct: 10, sell_stage_rearm_drawdown_pct: null }, 92.0, false);

// Case B: raw <= dca → THIS is the parity bug.
// raw=2, dca=5; price drops 4% — Python uses threshold=5 → no rearm.
// Old JS used threshold=2 → would (wrongly) rearm.
check('raw(2) <= dca(5), drop=4% → falls back to dca → no rearm',
  { dca_rearm_drawdown_pct: 5, sell_stage_rearm_drawdown_pct: 2 }, 96.0, false);
check('raw(2) <= dca(5), drop=6% → meets dca threshold → rearm',
  { dca_rearm_drawdown_pct: 5, sell_stage_rearm_drawdown_pct: 2 }, 94.0, true);

// Case C: raw > dca → threshold=raw on both sides.
check('raw(15) > dca(5), drop=10% → no rearm',
  { dca_rearm_drawdown_pct: 5, sell_stage_rearm_drawdown_pct: 15 }, 90.0, false);
check('raw(15) > dca(5), drop=20% → rearm',
  { dca_rearm_drawdown_pct: 5, sell_stage_rearm_drawdown_pct: 15 }, 80.0, true);

// Case D: max_drawdown_pct clamps the threshold
check('threshold clamped by max_dd: raw=80, max=50, drop=60% → rearm (clamped to 50)',
  { dca_rearm_drawdown_pct: 0, sell_stage_rearm_drawdown_pct: 80, max_drawdown_pct: 50 }, 40.0, true);

console.log('OK — parity holds');
