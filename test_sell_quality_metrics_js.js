/**
 * TDD tests for buy_quality_score / sell_quality_score in the JS worker.
 * Run: node test_sell_quality_metrics_js.js
 */

const fs = require('fs');
const vm = require('vm');
const assert = require('assert');

const WORKER_PATH = __dirname + '/web/static/strategy_parameter_lab_worker.js';

// ── Load worker functions into a VM context ──
const source = fs.readFileSync(WORKER_PATH, 'utf8');
const globals = {
  console: { info() {}, warn() {}, error() {} },
  postMessage() {},
  performance: { now: () => 0 },
  setTimeout
};
globals.self = globals;
const context = vm.createContext(globals);
vm.runInContext(source, context);

// ── Helpers to construct test data ──
function makeSlice(buyPrice, shares, periodLow, periodHigh, pricePointCount) {
  const amplitude = periodHigh - periodLow;
  const spread = periodHigh - buyPrice;
  return {
    buy_price_usd: buyPrice,
    shares,
    holding_period_high_usd: periodHigh,
    holding_period_low_usd: periodLow,
    holding_period_price_point_count: pricePointCount != null ? pricePointCount : 5,
    holding_period_had_intermediate_points: true,
    price_spread_efficiency: amplitude > 1e-9 ? (periodHigh - buyPrice) / amplitude : 0,
    sell_timing_efficiency: amplitude > 1e-9 ? (periodHigh - buyPrice) / (periodHigh - buyPrice > 1e-9 ? periodHigh - buyPrice : 1) : 0,
  };
}

function makeSellTrade(price, slices, netAmount = 0, grossAmount = 0) {
  const totalShares = slices.reduce((s, sl) => s + sl.shares, 0);
  return {
    action: 'sell',
    date: '2025-01-10',
    price,
    drawdown_pct: 0,
    estimated_profit_pct: 0,
    price_spread_efficiency: context.weightedSliceMetric(slices, 'price_spread_efficiency'),
    sell_timing_efficiency: context.weightedSliceMetric(slices, 'sell_timing_efficiency'),
    net_amount: netAmount || totalShares * price,
    gross_amount: grossAmount || totalShares * price,
    sold_lot_slices: slices,
  };
}

function makeBuyTrade(price, drawdownPct = 0, grossAmount = 0) {
  return {
    action: 'buy',
    date: '2025-01-01',
    price,
    drawdown_pct: drawdownPct,
    gross_amount: grossAmount || price * 10,
  };
}

// ═══════════════════════════════════════════════════════════
// TEST 1 (Tracer Bullet): Perfect buy at bottom, sell at top
// ═══════════════════════════════════════════════════════════

(function test_perfect_buy_low_sell_high() {
  const tradeLog = [
    makeBuyTrade(100),
    makeSellTrade(200, [makeSlice(100, 10, 100, 200)]),
  ];
  const portfolioValues = [1000, 2000];
  const cashValues = [0, 2000];

  const result = context.sellMetrics(tradeLog, portfolioValues, cashValues);

  assert.strictEqual(result.buy_quality_score, 100,
    `buy_quality should be 100 (bought at absolute bottom), got ${result.buy_quality_score}`);
  assert.strictEqual(result.sell_quality_score, 100,
    `sell_quality should be 100 (sold at absolute top), got ${result.sell_quality_score}`);

  console.log('PASS test_perfect_buy_low_sell_high');
})();

// ═══════════════════════════════════════════════════════════
// TEST 2: Buy at top, sell at bottom (worst case)
// ═══════════════════════════════════════════════════════════

(function test_buy_high_sell_low() {
  const tradeLog = [
    makeBuyTrade(200),
    makeSellTrade(100, [makeSlice(200, 10, 100, 200)]),
  ];
  const portfolioValues = [1000, 500];
  const cashValues = [0, 500];

  const result = context.sellMetrics(tradeLog, portfolioValues, cashValues);

  assert.strictEqual(result.buy_quality_score, 0,
    `buy_quality should be 0 (bought at absolute top), got ${result.buy_quality_score}`);
  assert.strictEqual(result.sell_quality_score, 0,
    `sell_quality should be 0 (sold at absolute bottom), got ${result.sell_quality_score}`);

  console.log('PASS test_buy_high_sell_low');
})();

// ═══════════════════════════════════════════════════════════
// TEST 3: Partial capture – buy at 25%, sell at 75%
// ═══════════════════════════════════════════════════════════

(function test_partial_capture_mid_range() {
  const low = 100, high = 200;
  const buyPrice = 125; // 25% from bottom: (200-125)/(200-100) = 75/100 = 0.75
  const sellPrice = 175; // 75% from bottom: (175-100)/(200-100) = 75/100 = 0.75

  const tradeLog = [
    makeBuyTrade(buyPrice),
    makeSellTrade(sellPrice, [makeSlice(buyPrice, 10, low, high)]),
  ];
  const portfolioValues = [1000, 1500];
  const cashValues = [0, 1500];

  const result = context.sellMetrics(tradeLog, portfolioValues, cashValues);

  // buy_quality = (200 - 125) / 100 = 0.75 → 75
  assert.strictEqual(result.buy_quality_score, 75,
    `buy_quality should be 75, got ${result.buy_quality_score}`);
  // sell_quality = (175 - 100) / 100 = 0.75 → 75
  assert.strictEqual(result.sell_quality_score, 75,
    `sell_quality should be 75, got ${result.sell_quality_score}`);

  console.log('PASS test_partial_capture_mid_range');
})();

// ═══════════════════════════════════════════════════════════
// TEST 4: Multiple lots averaged by shares
// ═══════════════════════════════════════════════════════════

(function test_weighted_average_multiple_lots() {
  // Lot 1: bought at 90 (very low), sell at 150. buyQ = (200-90)/100 = 1.0, sellQ = (150-100)/100 = 0.5
  // Lot 2: bought at 150 (mid), sell at 150. buyQ = (200-150)/100 = 0.5, sellQ = (150-100)/100 = 0.5
  const slices = [
    makeSlice(90, 2, 100, 200),   // 2 shares
    makeSlice(150, 8, 100, 200),  // 8 shares
  ];
  const tradeLog = [
    makeBuyTrade(90),
    makeBuyTrade(150),
    makeSellTrade(150, slices),
  ];
  const portfolioValues = [1000, 1500];
  const cashValues = [0, 1500];

  const result = context.sellMetrics(tradeLog, portfolioValues, cashValues);

  // Weighted buy_quality: (2 * 1.0 + 8 * 0.5) / 10 = (2 + 4) / 10 = 0.6 → 60
  assert.strictEqual(result.buy_quality_score, 60,
    `weighted buy_quality should be 60, got ${result.buy_quality_score}`);
  // Weighted sell_quality: (2 * 0.5 + 8 * 0.5) / 10 = 0.5 → 50
  assert.strictEqual(result.sell_quality_score, 50,
    `weighted sell_quality should be 50, got ${result.sell_quality_score}`);

  console.log('PASS test_weighted_average_multiple_lots');
})();

// ═══════════════════════════════════════════════════════════
// TEST 5: No sells → both scores = 0
// ═══════════════════════════════════════════════════════════

(function test_no_sells_zero_score() {
  const tradeLog = [
    makeBuyTrade(100),
    makeBuyTrade(120),
  ];
  const portfolioValues = [1000, 1100];
  const cashValues = [0, 0];

  const result = context.sellMetrics(tradeLog, portfolioValues, cashValues);

  assert.strictEqual(result.buy_quality_score, 0,
    `buy_quality should be 0 with no sells, got ${result.buy_quality_score}`);
  assert.strictEqual(result.sell_quality_score, 0,
    `sell_quality should be 0 with no sells, got ${result.sell_quality_score}`);

  console.log('PASS test_no_sells_zero_score');
})();

// ═══════════════════════════════════════════════════════════
// TEST 6: Flat price (zero amplitude) → both scores = 0
// ═══════════════════════════════════════════════════════════

(function test_flat_price_zero_amplitude() {
  const tradeLog = [
    makeBuyTrade(100),
    makeSellTrade(100, [makeSlice(100, 10, 100, 100, 5)]),  // 5 price points but zero amplitude
  ];
  const portfolioValues = [1000, 1000];
  const cashValues = [0, 1000];

  const result = context.sellMetrics(tradeLog, portfolioValues, cashValues);

  assert.strictEqual(result.buy_quality_score, 0,
    `buy_quality should be 0 with flat price, got ${result.buy_quality_score}`);
  assert.strictEqual(result.sell_quality_score, 0,
    `sell_quality should be 0 with flat price, got ${result.sell_quality_score}`);

  console.log('PASS test_flat_price_zero_amplitude');
})();

// ═══════════════════════════════════════════════════════════
// TEST 7: Narrow holding period (<3 price points) → skipped → both 0
// ═══════════════════════════════════════════════════════════

(function test_narrow_holding_period_skipped() {
  // Only 2 price points in holding period → unreliable score → excluded
  const tradeLog = [
    makeBuyTrade(100),
    makeSellTrade(200, [makeSlice(100, 10, 100, 200, 2)]),
  ];
  const portfolioValues = [1000, 2000];
  const cashValues = [0, 2000];

  const result = context.sellMetrics(tradeLog, portfolioValues, cashValues);

  assert.strictEqual(result.buy_quality_score, 0,
    `buy_quality should be 0 (narrow period skipped), got ${result.buy_quality_score}`);
  assert.strictEqual(result.sell_quality_score, 0,
    `sell_quality should be 0 (narrow period skipped), got ${result.sell_quality_score}`);

  console.log('PASS test_narrow_holding_period_skipped');
})();

// ═══════════════════════════════════════════════════════════
// TEST 8: Multiple sell trades – average across trades
// ═══════════════════════════════════════════════════════════

(function test_multiple_sell_trades_averaged_enough_points() {
  const tradeLog = [
    makeBuyTrade(100),
    makeBuyTrade(120),
    // Trade 1: sell at 190, bought at 100. period [100,200]. buyQ=1.0, sellQ=0.9
    makeSellTrade(190, [makeSlice(100, 5, 100, 200)]),
    // Trade 2: sell at 180, bought at 120. period [100,200]. buyQ=0.8, sellQ=0.8
    makeSellTrade(180, [makeSlice(120, 8, 100, 200)]),
  ];
  const portfolioValues = [1000, 1100, 1500, 1600];
  const cashValues = [0, 0, 500, 1800];

  const result = context.sellMetrics(tradeLog, portfolioValues, cashValues);

  // Trade 1: buyQ=1.0 (100), sellQ=0.9 (90)
  // Trade 2: buyQ=0.8 (80), sellQ=0.8 (80)
  // Average buyQ = (100 + 80) / 2 = 90
  // Average sellQ = (90 + 80) / 2 = 85
  assert.ok(Math.abs(result.buy_quality_score - 90) < 0.01,
    `avg buy_quality across trades should be ~90, got ${result.buy_quality_score}`);
  assert.ok(Math.abs(result.sell_quality_score - 85) < 0.01,
    `avg sell_quality across trades should be ~85, got ${result.sell_quality_score}`);

  console.log('PASS test_multiple_sell_trades_averaged');
})();

console.log('\nAll sell quality metrics tests passed!');
