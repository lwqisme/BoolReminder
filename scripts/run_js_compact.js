#!/usr/bin/env node
/** JS engine test using COMPACT task format (like real GA flow).
 *  inflateTask -> rebuildPricePoints computes drawdown fields correctly.
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const workerPath = path.join(__dirname, '..', 'web', 'static', 'strategy_parameter_lab_worker.js');
const workerCode = fs.readFileSync(workerPath, 'utf-8');

const sandbox = {
  self: { onmessage: null, location: { href: 'http://localhost/' } },
  importScripts: () => {},
  postMessage: (msg) => {},
  performance: { now: () => Date.now() },
  console: { log: () => {}, info: () => {}, error: (...a) => console.error('[worker]', ...a), warn: () => {} },
  setTimeout, clearTimeout, setInterval,
  URL, Promise, Error, Date, Math, Number, String, Array, Object, Boolean, Map, Set, JSON,
  parseFloat, parseInt, isNaN, isFinite, Infinity, NaN, undefined,
};

const context = vm.createContext(sandbox);
vm.runInContext(workerCode, context, { filename: 'worker.js' });

const { simulate, buildTaskContext, inflateTask } = sandbox;

console.log('='.repeat(65));
console.log('JS engine (compact format, real GA flow):');
console.log('='.repeat(65));

for (const fn of ['js_test_compact_1y.json', 'js_test_compact_3y.json', 'js_test_compact_5y.json']) {
  const data = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'data', fn), 'utf-8'));
  const { label, task, market_data, baseInputs, candidate } = data;

  // Simulate actual GA flow: inflateTask -> buildTaskContext -> simulate
  const inflated = inflateTask(task, market_data);
  const ctx = buildTaskContext(inflated);

  // Verify drawdown fields are computed
  const firstSym = Object.keys(ctx.pointByDay || {})[0];
  if (firstSym && ctx.pointByDay[firstSym]) {
    const firstDay = Object.keys(ctx.pointByDay[firstSym])[0];
    const pt = ctx.pointByDay[firstSym][firstDay];
    console.log(`  [verify] drawdown_ath=${pt.drawdown_ath?.toFixed(4)} drawdown_120=${pt.drawdown_120?.toFixed(4)}`);
  }

  const result = simulate(ctx, baseInputs, candidate);
  const rp = Number(result.return_pct || 0);
  const dd = Number(result.max_drawdown_pct || 0);
  const tc = Number(result.trade_count || 0);

  console.log(`  ${label}: ret=${rp.toFixed(2)}% dd=${dd.toFixed(2)}% trades=${tc}`);
}

console.log();
console.log('Python engine (same params):');
console.log('  1年: ret=31.69% dd=-20.22% trades=5');
console.log('  3年: ret=392.54% dd=-36.88% trades=5');
console.log('  5年: ret=971.02% dd=-66.33% trades=5');
