#!/usr/bin/env node
/**
 * TSLA 1-year backtest with detailed trade points.
 * 策略: 线性递增加权细切 + 价格上涨网格
 */
const fs = require('fs');
const path = require('path');
const vm = require('vm');

const workerPath = path.join(__dirname, '..', 'web', 'static', 'strategy_parameter_lab_worker.js');
const workerCode = fs.readFileSync(workerPath, 'utf-8');

// ── Mock browser globals ──
const mockSelf = {
  onmessage: null,
  location: { href: 'http://localhost/' },
};

const mockPostMessage = (msg) => {
  if (msg.type === 'progress' || msg.type === 'ready' || msg.type === 'start' || msg.type === 'batch_start') return;
};

const mockImportScripts = () => {};

const mockPerformance = { now: () => Date.now() };

const mockConsole = {
  log: () => {},
  info: () => {},
  error: () => {},
  warn: () => {},
};

const sandbox = {
  self: mockSelf,
  importScripts: mockImportScripts,
  postMessage: mockPostMessage,
  performance: mockPerformance,
  console: mockConsole,
  setTimeout,
  clearTimeout,
  setInterval,
  URL,
  Promise,
  Error,
  Date,
  Math,
  Number,
  String,
  Array,
  Object,
  Boolean,
  Map,
  Set,
  JSON,
  parseFloat,
  parseInt,
  isNaN,
  isFinite,
  Infinity,
  NaN,
  undefined,
};

const context = vm.createContext(sandbox);
vm.runInContext(workerCode, context, { filename: 'strategy_parameter_lab_worker.js' });

const { simulate, buildTaskContext, candidateInputs, inflateTask } = sandbox;

// ── Set workerState through the VM context (let at top level may shadow sandbox prop) ──
vm.runInContext('workerState = { include_trades: true, include_series: true, include_leaps_signal_details: false }', context);

// ── Load data ──
const data = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'data', 'js_test_tsla_1y.json'), 'utf-8'));
const { label, task, baseInputs, candidate, market_data } = data;

// Inflate task (computes drawdown_120 via rebuildPricePoints)
const inflatedTask = inflateTask(task, market_data);

// Build context
const ctx = buildTaskContext(inflatedTask);

// Run simulate
const result = simulate(ctx, baseInputs, candidate);

// Extract trade log
const tradeLog = result.trade_log || [];
const buys = tradeLog.filter(t => t.action === 'buy');
const sells = tradeLog.filter(t => t.action === 'sell');

// ── Print results ──
console.log('='.repeat(80));
console.log(`  ${label}`);
console.log('='.repeat(80));
console.log(`  策略: 线性递增加权细切 (step=${baseInputs.step_pct}%, maxDD=${baseInputs.max_drawdown_pct}%)`);
console.log(`        价格上涨网格 (step=${baseInputs.grid_rebound_step_pct}%, sell=${baseInputs.grid_sell_pct}%` +
    `, minProfit=${baseInputs.sell_min_profit_pct}%)`);
console.log(`        卖后重启 ${baseInputs.dca_rearm_drawdown_pct}% / 卖档重启 ${baseInputs.sell_stage_rearm_drawdown_pct}%`);
console.log(`  初始资金: $${Number(baseInputs.initial_cash).toLocaleString()}`);
console.log('-'.repeat(80));
const finalValue = result.series ? result.series.portfolio_values[result.series.portfolio_values.length - 1] : 0;
const profit = finalValue - baseInputs.initial_cash;
console.log(`  最终价值: $${Math.round(finalValue).toLocaleString()}`);
console.log(`  总盈亏:   $${Math.round(profit).toLocaleString()}`);
console.log(`  收益率:   ${Number(result.return_pct || 0).toFixed(2)}%`);
console.log(`  最大回撤: ${Number(result.max_drawdown_pct || 0).toFixed(2)}%`);
console.log(`  交易次数: ${result.trade_count || 0}`);
console.log(`  买次数:   ${buys.length}`);
console.log(`  卖次数:   ${sells.length}`);
console.log('');
console.log('─'.repeat(80));
console.log('  交易明细 (chronological):');
console.log('─'.repeat(80));

if (tradeLog.length === 0) {
  console.log('    (无交易记录)');
} else {
  // Format each trade
  const header = '  日期        动作      价格      股数        金额        盈亏%    持仓  现金';
  console.log(header);
  console.log('  ' + '-'.repeat(76));
  
  let runningShares = 0;
  for (const t of tradeLog) {
    const date = (t.date || '').padEnd(10);
    const action = (t.action || '?').padEnd(4);
    const price = Number(t.price || 0).toFixed(2).padStart(8);
    const shares = Number(t.shares || 0).toFixed(2).padStart(9);
    const amount = '$' + Math.round(Number(t.gross_amount || 0)).toLocaleString('en-US').padStart(9);
    if (t.action === 'buy') runningShares += Number(t.shares || 0);
    else runningShares -= Number(t.shares || 0);
    const pnlPct = t.action === 'sell' ? (Number(t.estimated_profit_pct || 0).toFixed(1) + '%').padStart(7) : '       ';
    const cash = '$' + Math.round(Number(t.cash_after || 0)).toLocaleString('en-US').padStart(9);
    const dd = Number(t.drawdown_pct || 0).toFixed(1) + '%';
    
    console.log(`  ${date} ${action} ${price} ${shares} ${amount} ${pnlPct} dd=${dd.padStart(6)} cash=${cash}`);
  }
}

console.log('');
console.log('─'.repeat(80));

// Print buy/sell summary
if (buys.length > 0) {
  console.log(`  买入点 (${buys.length}笔):`);
  for (const b of buys) {
    const dd = Number(b.drawdown_pct || 0).toFixed(1);
    console.log(`    ${b.date}  $${Number(b.price).toFixed(2)}  买入 ${Number(b.shares).toFixed(2)}股  $${Math.round(Number(b.gross_amount||0)).toLocaleString()}  回撤 ${dd}%`);
  }
}

if (sells.length > 0) {
  console.log('');
  console.log(`  卖出点 (${sells.length}笔):`);
  for (const s of sells) {
    const pnl = Number(s.estimated_profit_pct || 0).toFixed(1);
    const trig = s.trigger_value || '';
    console.log(`    ${s.date}  $${Number(s.price).toFixed(2)}  卖出 ${Number(s.shares).toFixed(2)}股  $${Math.round(Number(s.gross_amount||0)).toLocaleString()}  盈利 ${pnl}%  触发: ${trig}`);
  }
}

console.log('');
console.log('='.repeat(80));

// Print tranches (buy ladder)
const numFn = sandbox.num;
const buildTranches = sandbox.buildTranches;
if (buildTranches) {
  const tranches = buildTranches(baseInputs, 'weighted_dca');
  console.log('');
  console.log('  买入梯度 (线性递增加权细切):');
  console.log('  ' + '-'.repeat(50));
  let cumPct = 0;
  for (let i = 0; i < tranches.length; i++) {
    const t = tranches[i];
    cumPct += t.allocation_pct;
    console.log(`  档${String(i+1).padStart(2)}  回撤 ${t.threshold_pct.toFixed(1)}%  分配 ${t.allocation_pct.toFixed(2)}%  累计 ${cumPct.toFixed(1)}%`);
  }
}
