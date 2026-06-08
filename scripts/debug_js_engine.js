#!/usr/bin/env node
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

const { simulate, buildTaskContext, candidateInputs } = sandbox;

const data = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'data', 'js_test_3y.json'), 'utf-8'));
const { task, baseInputs, candidate } = data;

const ctx = buildTaskContext(task);

// First, check what candidateInputs produces
const merged = candidateInputs(baseInputs, candidate);
console.log('=== baseInputs ===');
console.log('  cash:', baseInputs.initial_cash, 'monthly:', baseInputs.monthly_contribution);
console.log('  step:', baseInputs.step_pct, 'alloc:', baseInputs.equal_slice_allocation_pct);
console.log('  sell_min:', baseInputs.sell_min_profit_pct, 'grid_rebound:', baseInputs.grid_rebound_step_pct);
console.log('  grid_sell:', baseInputs.grid_sell_pct, 'grid_min_sell:', baseInputs.grid_min_sell_amount);
console.log('  same_day:', baseInputs.sell_allow_same_day_sell);
console.log();

console.log('=== candidate ===');
console.log('  buy_strategy:', candidate.buy_strategy);
console.log('  sell_strategy:', candidate.sell_strategy);
console.log('  top-level step_pct:', candidate.step_pct);
console.log('  top-level sell_min_profit_pct:', candidate.sell_min_profit_pct);
console.log();

console.log('=== merged (candidateInputs result) ===');
console.log('  cash:', merged.initial_cash, 'monthly:', merged.monthly_contribution);
console.log('  step:', merged.step_pct, 'alloc:', merged.equal_slice_allocation_pct);
console.log('  sell_min:', merged.sell_min_profit_pct, 'grid_rebound:', merged.grid_rebound_step_pct);
console.log('  grid_sell:', merged.grid_sell_pct, 'grid_min_sell:', merged.grid_min_sell_amount);
console.log('  same_day:', merged.sell_allow_same_day_sell);
console.log();

// Run simulate
const result = simulate(ctx, baseInputs, candidate);
console.log('=== simulate result ===');
console.log('  return_pct:', result.return_pct);
console.log('  max_drawdown_pct:', result.max_drawdown_pct);
console.log('  trade_count:', result.trade_count);
console.log('  profit:', result.profit);
console.log('  final_value:', result.final_value);
console.log('  total_contributed:', result.total_contributed);
console.log('  contribution_count:', result.contribution_count);
console.log('  keys:', Object.keys(result));
