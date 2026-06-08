#!/usr/bin/env node
/**
 * JS engine comparison harness.
 * Mocks browser worker environment, loads strategy_parameter_lab_worker.js,
 * runs simulate() on the same test data as the Python engine.
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
  // suppress progress/start messages
  if (msg.type === 'progress' || msg.type === 'ready' || msg.type === 'start' || msg.type === 'batch_start') return;
  // console.log('[postMessage]', msg.type, msg.message || '');
};

const mockImportScripts = () => {}; // no-op (LEAPS engine not needed)

const mockPerformance = {
  now: () => Date.now(),
};

const mockConsole = {
  log: (...args) => {}, // suppress
  info: (...args) => {}, // suppress
  error: (...args) => console.error('[worker]', ...args),
  warn: (...args) => {},
};

// ── Run worker in sandbox ──
const sandbox = {
  self: mockSelf,
  importScripts: mockImportScripts,
  postMessage: mockPostMessage,
  performance: mockPerformance,
  console: mockConsole,
  setTimeout: setTimeout,
  clearTimeout: clearTimeout,
  setInterval: setInterval,
  URL: URL,
  Promise: Promise,
  Error: Error,
  Date: Date,
  Math: Math,
  Number: Number,
  String: String,
  Array: Array,
  Object: Object,
  Boolean: Boolean,
  Map: Map,
  Set: Set,
  JSON: JSON,
  parseFloat: parseFloat,
  parseInt: parseInt,
  isNaN: isNaN,
  isFinite: isFinite,
  Infinity: Infinity,
  NaN: NaN,
  undefined: undefined,
};

const context = vm.createContext(sandbox);

try {
  vm.runInContext(workerCode, context, { filename: 'strategy_parameter_lab_worker.js' });
} catch (e) {
  console.error('Failed to load worker:', e.message);
  process.exit(1);
}

// Extract simulate and buildTaskContext from sandbox
const { simulate, buildTaskContext, candidateInputs } = sandbox;

if (!simulate) {
  console.error('simulate() not found in worker export');
  process.exit(1);
}

// ── Run tests ──
const testFiles = ['js_test_1y.json', 'js_test_3y.json', 'js_test_5y.json'];

console.log('='.repeat(60));
console.log('JS engine results (Node.js sandbox):');
console.log('='.repeat(60));

for (const fn of testFiles) {
  const data = JSON.parse(fs.readFileSync(path.join(__dirname, '..', 'data', fn), 'utf-8'));
  const { label, task, baseInputs, candidate } = data;

  // Build task context (same as what the worker does in buildTaskContexts)
  const ctx = buildTaskContext(task);

  // Build candidate inputs (same as what candidateInputs does)
  const fullInputs = candidateInputs(baseInputs, candidate);

  // Run simulate
  const result = simulate(ctx, baseInputs, candidate);
  const returnPct = Number(result.return_pct || 0);
  const maxDd = Number(result.max_drawdown_pct || 0);
  const trades = Number(result.trade_count || 0);

  console.log(`  ${label}: ret=${returnPct.toFixed(2)}% dd=${maxDd.toFixed(2)}% trades=${trades}`);
  if (result.profit !== undefined) {
    console.log(`           profit=$${Number(result.profit || 0).toLocaleString()}`);
  }
  if (result.final_value !== undefined) {
    console.log(`           final=$${Number(result.final_value || 0).toLocaleString()}`);
  }
}
