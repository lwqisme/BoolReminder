/**
 * End-to-end test for GA categorical restrictions in the browser JS.
 * Verifies mutateGa respects gaConfig.buy_rearm_mode.
 * Run: node test_ga_categorical_restrict.js
 */

const fs = require('fs');

// Extract mutateGa and crossoverGa from the HTML
const html = fs.readFileSync('/tmp/plab_latest.js', 'utf8');

// We need: mutateGa, crossoverGa, tournamentSelectGa
// These are global functions in the page. Wrap in a test harness.

const script = html + `
// Test harness
const testResults = [];

function assert(cond, msg) {
    if (!cond) throw new Error('FAIL: ' + msg);
    testResults.push(msg);
}

// Mock Math.random to be deterministic
let mockRandomValues = [];
let mockIdx = 0;
const origRandom = Math.random;
Math.random = function() {
    if (mockIdx < mockRandomValues.length) return mockRandomValues[mockIdx++];
    return origRandom.call(Math);
};

function setMockRandoms(vals) {
    mockRandomValues = vals;
    mockIdx = 0;
}

// ─── Test 1: mutateGa respects buy_rearm_mode restriction ───
const ind = {
    buy_strategy: 'equal_slice',
    sell_strategy: 'price_rise_grid',
    step_pct: 2.0,
    equal_slice_allocation_pct: 20,
    grid_rebound_step_pct: 5.0,
    grid_sell_pct: 12,
    grid_min_sell_amount: 200,
    sell_min_profit_pct: 15,
    dca_rearm_drawdown_pct: 2.37,
    sell_stage_rearm_drawdown_pct: 3,
    buy_rearm_mode: 'cumulative',
    sell_allow_same_day_sell: false,
};

const paramRanges = {
    sell_ranges: {
        buy_rearm_mode: ['cumulative', 'restart_from_rearm'],
        sell_allow_same_day_sell: [false, true],
    },
    sell_fields: {
        price_rise_grid: ['grid_rebound_step_pct', 'grid_sell_pct', 'grid_min_sell_amount', 'sell_min_profit_pct'],
    },
    buy_ranges: {},
    buy_fields: { equal_slice: ['step_pct', 'equal_slice_allocation_pct'] },
    bounds: {},
    precision: {},
    int_fields: [],
};

// With restriction: only 'restart_from_rearm' allowed
const gaConfigRestricted = {
    mutation_rate: 1.0,   // 100% mutation rate to force changes
    continuous_mutation: false,
    buy_rearm_mode: 'restart_from_rearm',
};

// Force sell_allow_same_day_sell mutation off, buy_rearm_mode mutation on
setMockRandoms([
    0.01, 0.01, 0.01, 0.01, 0.01,  // buy fields: all mutate (but use default values from ranges)
    0.01, 0.01, 0.01, 0.01, 0.01,  // sell fields: first 4 mutate
    0.01,                           // buy_rearm_mode mutates (random for value)
    0.3,                            // random for mode pick → index 0 = 'restart_from_rearm'
]);

// Run 10 mutations
for (let i = 0; i < 10; i++) {
    setMockRandoms([0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.3]);
    const child = mutateGa(ind, 1.0, paramRanges, gaConfigRestricted, false);
    if (child.buy_rearm_mode !== 'restart_from_rearm') {
        throw new Error('FAIL: Expected restart_from_rearm, got ' + child.buy_rearm_mode);
    }
}
testResults.push('PASS: mutateGa respects buy_rearm_mode="restart_from_rearm"');

// ─── Test 2: Without restriction, both modes appear ───
const gaConfigNoRestrict = {
    mutation_rate: 1.0,
    continuous_mutation: false,
    buy_rearm_mode: '',
};

let seen = new Set();
for (let i = 0; i < 20; i++) {
    setMockRandoms([0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, 0.01, i < 10 ? 0.3 : 0.7]);
    const child = mutateGa(ind, 1.0, paramRanges, gaConfigNoRestrict, false);
    if (child.buy_rearm_mode) seen.add(child.buy_rearm_mode);
}
if (seen.size >= 2) {
    testResults.push('PASS: Without restriction, both modes generated');
} else {
    throw new Error('FAIL: Only saw ' + [...seen] + ' without restriction');
}

// ─── Test 3: Crossover preserves buy_rearm_mode ───
const p1 = { ...ind, buy_rearm_mode: 'cumulative', step_pct: 1.0 };
const p2 = { ...ind, buy_rearm_mode: 'restart_from_rearm', step_pct: 3.0 };
setMockRandoms([0.9, 0.9, 0.1]);  // pick p2 for buy_rearm_mode, p2 for step_pct
const child = crossoverGa(p1, p2);
assert(['cumulative', 'restart_from_rearm'].includes(child.buy_rearm_mode),
    'crossover preserves buy_rearm_mode');

console.log(testResults.join('\\n'));
console.log('All tests passed!');
`;

// Run
try {
    eval(script);
    console.log('DONE');
} catch(e) {
    console.error(e.message);
    process.exit(1);
}
