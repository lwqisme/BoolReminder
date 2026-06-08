#!/usr/bin/env node
/** Deep engine comparison: trace trade-by-trade for 1yr NVDA. */
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
  console: { log: () => {}, info: () => {}, error: (...a) => {}, warn: () => {} },
  setTimeout, clearTimeout, setInterval,
  URL, Promise, Error, Date, Math, Number, String, Array, Object, Boolean, Map, Set, JSON,
  parseFloat, parseInt, isNaN, isFinite, Infinity, NaN, undefined,
};

const context = vm.createContext(sandbox);
vm.runInContext(workerCode, context, { filename: 'worker.js' });

// Set workerState to include trades
sandbox.workerState = { include_trades: true };

const { simulate, buildTaskContext, inflateTask, candidateInputs } = sandbox;

// Load 1yr test data
const data = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'data', 'js_test_compact_1y.json'), 'utf-8'));
const { task, market_data, baseInputs, candidate } = data;

const inflated = inflateTask(task, market_data);
const ctx = buildTaskContext(inflated);
const merged = candidateInputs(baseInputs, candidate);

// Run simulate
const result = simulate(ctx, baseInputs, candidate);

console.log('=== JS 1yr NVDA ===');
console.log(`return_pct: ${result.return_pct?.toFixed(2)}%`);
console.log(`max_drawdown_pct: ${result.max_drawdown_pct?.toFixed(2)}%`);
console.log(`trade_count: ${result.trade_count}`);

// Print first 20 trades
const tradeLog = result.trade_log || [];
console.log(`\nTotal trades: ${tradeLog.length}`);
console.log('First 20 trades:');
for (let i = 0; i < Math.min(20, tradeLog.length); i++) {
  const t = tradeLog[i];
  console.log(`  ${t.date} ${t.action} ${t.symbol} price=${t.price?.toFixed(2)} shares=${t.shares?.toFixed(1)} amount=${t.amount?.toFixed(0)} drawdown_pct=${t.drawdown_pct?.toFixed(2)} trigger=${t.trigger || ''}`);
}

// Print buy/sell summary
const buys = tradeLog.filter(t => t.action === 'buy');
const sells = tradeLog.filter(t => t.action === 'sell');
console.log(`\nBuys: ${buys.length}, Sells: ${sells.length}`);
console.log(`Total buy amount: $${buys.reduce((s,t) => s + (Number(t.amount)||0), 0).toFixed(0)}`);
console.log(`Total sell gross: $${sells.reduce((s,t) => s + (Number(t.gross_amount)||0), 0).toFixed(0)}`);

// Print key params used
console.log('\n=== Inputs actually used ===');
console.log(`step_pct: ${merged.step_pct}`);
console.log(`equal_slice_allocation_pct: ${merged.equal_slice_allocation_pct}`);
console.log(`sell_min_profit_pct: ${merged.sell_min_profit_pct}`);
console.log(`grid_rebound_step_pct: ${merged.grid_rebound_step_pct}`);
console.log(`grid_sell_pct: ${merged.grid_sell_pct}`);
console.log(`grid_min_sell_amount: ${merged.grid_min_sell_amount}`);
console.log(`sell_allow_same_day_sell: ${merged.sell_allow_same_day_sell}`);
console.log(`reserve_position_pct: ${merged.reserve_position_pct}`);
console.log(`initial_cash: ${merged.initial_cash}`);
console.log(`monthly_contribution: ${merged.monthly_contribution}`);
console.log(`drawdown_basis: ${merged.drawdown_basis}`);
console.log(`max_drawdown_pct: ${merged.max_drawdown_pct}`);
