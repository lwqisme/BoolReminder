/**
 * test_cost_deleverage_label.js
 *
 * Regression: every label-rendering site for cost_deleverage must produce
 * the unambiguous '盈利档 X/Y/Z × 减仓 A/B/C 冷却 N日' format.
 *
 * Pre-fix the worker emitted '成本去杠杆 X/Y/Z 盈利 A+B+C 卖出 N日冷却',
 * which mislabeled both groups (header attached to the wrong triplet) and
 * misled with '+' (suggesting cumulative reduction instead of compounded).
 */
'use strict';

const fs = require('fs');
const path = require('path');
const assert = require('assert');

const workerSrc = fs.readFileSync(
  path.join(__dirname, 'web', 'static', 'strategy_parameter_lab_worker.js'),
  'utf-8'
);

const sandbox = {
  self: { onmessage: null, postMessage: () => {}, location: { href: 'http://test/' } },
  performance: { now: () => Date.now() },
  console
};
const harness = workerSrc + '\n;sandbox.__buildSellLabel = buildSellLabel;';
new Function('sandbox', 'self', 'performance', 'console', harness)(
  sandbox, sandbox.self, sandbox.performance, console
);
const buildSellLabel = sandbox.__buildSellLabel;
assert.ok(typeof buildSellLabel === 'function', 'buildSellLabel must be exported');

const params = {
  cost_first_profit_pct: 14.8,
  cost_second_profit_pct: 25,
  cost_third_profit_pct: 33.1,
  cost_first_sell_pct: 24,
  cost_second_sell_pct: 30,
  cost_third_sell_pct: 23.4,
  cost_deleverage_cooldown_days: 0
};
const label = buildSellLabel('cost_deleverage', params, { cost_deleverage: '成本去杠杆' }, {});

console.log('label:', label);

// New format must contain the disambiguating headers and operators.
assert.ok(label.includes('盈利档'), `missing '盈利档' header: ${label}`);
assert.ok(label.includes('减仓'), `missing '减仓' header: ${label}`);
assert.ok(label.includes('×'), `missing '×' separator: ${label}`);
assert.ok(label.includes('冷却 0日'), `missing '冷却 0日' segment: ${label}`);

// Old ambiguous markers must be gone.
assert.ok(!/\d+%\+\d+%/.test(label), `'+' between sell ratios still present: ${label}`);
assert.ok(!/盈利 \d+%/.test(label) || /盈利档/.test(label),
  `bare '盈利 X%' still leads readers to think profit value: ${label}`);
assert.ok(!/卖出 \d+日冷却/.test(label),
  `legacy '卖出 N日冷却' still present: ${label}`);

// Permutation safety: out-of-order triplets are valid (per user decision)
// and must render without error or implicit reordering.
const messy = {
  cost_first_profit_pct: 25,
  cost_second_profit_pct: 14.8,
  cost_third_profit_pct: 33.1,
  cost_first_sell_pct: 40,
  cost_second_sell_pct: 30,
  cost_third_sell_pct: 20,
  cost_deleverage_cooldown_days: 5
};
const messyLabel = buildSellLabel('cost_deleverage', messy, { cost_deleverage: '成本去杠杆' }, {});
console.log('messy:', messyLabel);
// Order must match what the user typed — first stage value 25, not 14.8.
assert.ok(/盈利档 25%/.test(messyLabel), `triplet was silently reordered: ${messyLabel}`);
assert.ok(messyLabel.includes('冷却 5日'), `cooldown segment wrong: ${messyLabel}`);

console.log('OK — cost_deleverage label format guarded');
