#!/usr/bin/env node
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const workerPath = path.join(__dirname, '..', 'web', 'static', 'strategy_parameter_lab_worker.js');
const workerCode = fs.readFileSync(workerPath, 'utf-8');

// Override simulate to capture trade_log and final values
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

// Patch worker: set workerState.include_trades = true before simulate
const patchedCode = workerCode + '\n' +
  'function simulateWithTrades(task, baseInputs, candidate) {\n' +
  '  const saved = typeof workerState !== "undefined" ? workerState : null;\n' +
  '  if (!self._workerState) self._workerState = { include_trades: true };\n' +
  '  const origWorkerState = typeof workerState !== "undefined" ? workerState : undefined;\n' +
  '  // Monkey-patch: simulate checks workerState?.include_trades\n' +
  '  // We need to set it before calling\n' +
  '  return simulate(task, baseInputs, candidate);\n' +
  '}\n';

vm.runInContext(patchedCode, context, { filename: 'worker.js' });

const { simulate, buildTaskContext, candidateInputs } = sandbox;

const data = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'data', 'js_test_3y.json'), 'utf-8'));
const { task, baseInputs, candidate } = data;

const ctx = buildTaskContext(task);
const merged = candidateInputs(baseInputs, candidate);

// Modify the sendbox to enable trade logging
// simulate checks workerState?.include_trades
// We can't easily modify the closure, so let's just look at
// what happens with the simulation

// Run simulate
const result = simulate(ctx, baseInputs, candidate);

console.log('JS 3yr result:');
console.log(`  return_pct: ${result.return_pct?.toFixed(2)}%`);
console.log(`  max_drawdown: ${result.max_drawdown_pct?.toFixed(2)}%`);
console.log(`  trades: ${result.trade_count}`);
console.log(`  contribution_count: ${result.contribution_count}`);
console.log(`  avg_buy_drawdown: ${result.avg_buy_drawdown_pct?.toFixed(2)}%`);
console.log(`  avg_sell_profit: ${result.avg_sell_profit_pct?.toFixed(2)}%`);
console.log(`  sell_quality: ${result.sell_quality_score?.toFixed(4)}`);

// Calculate final_value and profit from return_pct
const totalContributed = Number(baseInputs.initial_cash) + Number(baseInputs.monthly_contribution || 0) * Number(result.contribution_count || 0);
const finalValue = totalContributed * (1 + Number(result.return_pct || 0) / 100);
console.log();
console.log(`  computed final_value: $${finalValue.toLocaleString(undefined, {maximumFractionDigits: 0})}`);
console.log(`  computed total_contributed: $${totalContributed.toLocaleString(undefined, {maximumFractionDigits: 0})}`);
console.log(`  computed profit: $${(finalValue - totalContributed).toLocaleString(undefined, {maximumFractionDigits: 0})}`);

// Python comparison
console.log();
console.log('Python 3yr result:');
console.log('  ret=392.54% profit=$196,268 dd=-36.88% trades=5');
console.log('  final=$246,268 contributed=$50,000');
