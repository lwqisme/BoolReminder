/**
 * Tests for LEAPS GA engine (Node.js).
 * Run: node test_leaps_ga_engine.js
 */
const leaps = require('./web/static/leaps_ga_engine.js');

let passed = 0, failed = 0;

function assert(cond, msg) {
  if (cond) { passed++; }
  else { failed++; console.error('FAIL:', msg); }
}

function assertGreater(a, b, msg) { assert(a > b, msg + ` (${a} > ${b})`); }
function assertLess(a, b, msg) { assert(a < b, msg + ` (${a} < ${b})`); }
function assertEqual(a, b, msg) { assert(a === b, msg + ` (${a} === ${b})`); }
function assertAlmostEqual(a, b, delta, msg) { assert(Math.abs(a - b) <= delta, msg + ` (${a} ≈ ${b})`); }

// ── Option Delta Tests ────────────────────────────────────────────────────

function testDelta() {
  const d = leaps.estimateOptionDelta(100, 100, 250);
  assertGreater(d, 0.60, 'ATM delta > 0.6');
  assertLess(d, 0.85, 'ATM delta < 0.85');

  const atm = leaps.estimateOptionDelta(100, 100, 250);
  const otm = leaps.estimateOptionDelta(100, 110, 250);
  assertGreater(atm, otm, 'ATM delta > OTM delta');

  const d250 = leaps.estimateOptionDelta(110, 100, 250);
  const d30 = leaps.estimateOptionDelta(110, 100, 30);
  assertGreater(d30, d250, 'ITM shorter DTE > longer DTE');

  const deepOtm = leaps.estimateOptionDelta(50, 100, 250);
  assertGreater(deepOtm, 0, 'Deep OTM delta > 0');
  assertLess(deepOtm, 0.4, 'Deep OTM delta < 0.4');
}

// ── Proxy ROI Tests ───────────────────────────────────────────────────────

function testProxyRoi() {
  const roi = leaps.proxyOptionRoi(100, 120, '2025-01-15', '2025-04-15', '2025-10-15', 110);
  assertGreater(roi, 30, 'Stock +20% -> option ROI > 30%');
  assertLess(roi, 200, 'Stock +20% -> option ROI < 200%');

  const roiDown = leaps.proxyOptionRoi(100, 90, '2025-01-15', '2025-04-15', '2025-10-15', 110);
  assertLess(roiDown, -5, 'Stock -10% -> option ROI < -5%');

  const roi30 = leaps.proxyOptionRoi(100, 120, '2025-01-15', '2025-02-15', '2025-10-15', 110);
  const roi180 = leaps.proxyOptionRoi(100, 120, '2025-01-15', '2025-07-15', '2025-10-15', 110);
  assertGreater(roi30, roi180, 'Shorter hold > longer hold (theta)');
}

// ── Rolling 120-day High ──────────────────────────────────────────────────

function makePrices(values, startDate) {
  const d = new Date(startDate);
  return values.map((v, i) => {
    const date = new Date(d);
    date.setDate(date.getDate() + i);
    return [date.toISOString().slice(0, 10), v];
  });
}

function testRollingHigh() {
  const prices = makePrices(Array(119).fill(100), '2025-01-01');
  const highs = leaps.rolling120dHigh(prices);
  assertEqual(highs.length, 119, '119 prices -> 119 highs');
  for (const [, h] of highs) assertEqual(h, null, 'First 119 highs = null');

  const prices2 = makePrices([...Array(100).fill(100), 150, ...Array(50).fill(100)], '2025-01-01');
  const highs2 = leaps.rolling120dHigh(prices2);
  assert(highs2[120][1] >= 150, 'Day 120 high >= 150');
}

// ── Bollinger Band ────────────────────────────────────────────────────────

function testBollinger() {
  const prices = makePrices(Array(30).fill(100), '2025-01-01');
  const bands = leaps.bollingerLowerBand(prices);
  for (let i = 21; i < bands.length; i++) {
    assertAlmostEqual(bands[i].band, 100, 0.01, 'Flat price band ≈ 100');
  }

  const prices2 = makePrices(Array(10).fill(100), '2025-01-01');
  const bands2 = leaps.bollingerLowerBand(prices2);
  for (const b of bands2) assertEqual(b.band, null, 'Short series band = null');

  // Oscillating: band < MA
  const osc = [];
  for (let i = 0; i < 15; i++) { osc.push(100); osc.push(105); }
  const prices3 = makePrices(osc, '2025-01-01');
  const bands3 = leaps.bollingerLowerBand(prices3);
  assertLess(bands3[bands3.length - 1].band, 102.5, 'Volatile band < MA');
}

// ── Entry Detection ───────────────────────────────────────────────────────

function testEntryDetection() {
  // Short series
  assertEqual(leaps.detectLeapsEntries(makePrices(Array(50).fill(100), '2025-01-01'), 20, 'touch').length, 0, 'Short series = 0 entries');

  // Touch entry
  const vals = [...Array(122).fill(100), 95, 90, 87, 84, 81, 80, 78];
  const prices = makePrices(vals, '2025-01-01');
  const entries = leaps.detectLeapsEntries(prices, 10, 'touch');
  assertGreater(entries.length, 0, 'Touch finds entries');
  assertGreater(Math.max(...entries.map(e => e.drawdown_pct)), 15, 'Max drawdown >= 15%');
  for (const e of entries) assert(e.bollinger_score >= 1, 'Touch entry bollinger_score >= 1');

  // Drawdown below threshold
  const vals2 = [...Array(122).fill(100), 95, 95, 95, 95, 95, 95];
  assertEqual(leaps.detectLeapsEntries(makePrices(vals2, '2025-01-01'), 20, 'touch').length, 0, 'Low drawdown = 0 entries');

  // Bounce mode
  const vals3 = [...Array(122).fill(100), ...Array(5).fill(85), 90, 93, 95];
  const bounceEntries = leaps.detectLeapsEntries(makePrices(vals3, '2025-01-01'), 10, 'bounce');
  for (const e of bounceEntries) assertLess(e.bollinger_score, 1, 'Bounce entry score < 1');

  // Both mode >= touch
  const vals4 = [...Array(122).fill(100), ...Array(5).fill(85), 90, 93, 95, 98];
  const p4 = makePrices(vals4, '2025-01-01');
  const touchE = leaps.detectLeapsEntries(p4, 10, 'touch');
  const bothE = leaps.detectLeapsEntries(p4, 10, 'both');
  assert(bothE.length >= touchE.length, 'Both >= touch entries');
}

// ── Sell Ladder ───────────────────────────────────────────────────────────

function testSellLadder() {
  const prices = makePrices([...Array(122).fill(100), 100, ...Array(200).fill(200)], '2025-01-01');
  const entry = { date: '2025-05-03', price: 100, drawdown_pct: 20, bollinger_score: 1.2, composite_score: 0.6 };

  // No sell before hold period
  const trade1 = leaps.computeSellLadder(entry, prices, [[10, 50, 100]], 190, 110);
  if (trade1.sell_events.length) {
    const holdDays = Math.round((new Date(trade1.sell_events[0].date) - new Date(entry.date)) / 86400000);
    assert(holdDays >= 10, 'No sell before hold period');
  }

  // Single stage sells all
  const vals2 = [...Array(122).fill(100), 100, ...Array(14).fill(100), 150, ...Array(200).fill(150)];
  const prices2 = makePrices(vals2, '2025-01-01');
  const entry2 = { date: '2025-05-03', price: 100, drawdown_pct: 20, bollinger_score: 1.2, composite_score: 0.6 };
  const trade2 = leaps.computeSellLadder(entry2, prices2, [[10, 30, 100]], 190, 110);
  assertGreater(trade2.sell_events.length, 0, 'Profit trigger -> sell');
  assertGreater(trade2.total_roi_pct, 0, 'Positive total ROI');

  // Expiration force sell
  const flatPrices = makePrices(Array(400).fill(100), '2025-01-01');
  const trade3 = leaps.computeSellLadder(entry, flatPrices, [[20, 50, 100]], 190, 110);
  assert(trade3.expired, 'Flat price -> expired');
  assertGreater(trade3.sell_events.length, 0, 'Expired has sell event');

  // Two-stage
  const vals4 = [...Array(122).fill(100), 100, ...[...Array(100).keys()].map(i => 100 + i)];
  const prices4 = makePrices(vals4, '2025-01-01');
  const trade4 = leaps.computeSellLadder(entry2, prices4, [[5, 10, 50], [10, 20, 100]], 190, 110);
  assert(trade4.sell_events.length >= 2, 'Two stage -> >= 2 sells');
  assertAlmostEqual(trade4.sell_events[0].pct_sold, 50, 1, 'First sell ~50%');
}

// ── Individual Key ────────────────────────────────────────────────────────

function testIndividualKey() {
  const k1 = leaps.leapsIndividualKey(20, 'touch', 15, 80, 50, 60, 60, 50);
  const k2 = leaps.leapsIndividualKey(20, 'touch', 15, 80, 50, 60, 60, 50);
  assertEqual(k1, k2, 'Key deterministic');

  const k3 = leaps.leapsIndividualKey(25, 'touch', 15, 80, 50, 60, 60, 50);
  assert(k1 !== k3, 'Different params -> different key');
}

// ── Crossover & Mutation ──────────────────────────────────────────────────

function testCrossover() {
  const ranges = leaps.DEFAULT_RANGES;
  const p1 = leaps.makeIndividual(20, 'touch', 10, 100, 50, 50, 60, 50);
  const p2 = leaps.makeIndividual(25, 'bounce', 30, 70, 40, 80, 40, 70);
  const child = leaps.leapsCrossover(p1, p2, ranges);
  assertLess(child.stage1_days, child.stage2_days, 'Crossover: s1d < s2d');
  assertGreater(child.stage1_profit, child.stage2_profit, 'Crossover: s1p > s2p');
}

function testMutation() {
  const ranges = leaps.DEFAULT_RANGES;
  const ind = leaps.makeIndividual(20, 'touch', 20, 80, 50, 60, 60, 50);
  const mutant = leaps.leapsMutate(ind, 1.0, ranges);  // 100% mutation
  assertLess(mutant.stage1_days, mutant.stage2_days, 'Mutation: s1d < s2d');
  assertGreater(mutant.stage1_profit, mutant.stage2_profit, 'Mutation: s1p > s2p');
}

// ── Fitness ───────────────────────────────────────────────────────────────

function testFitness() {
  const ind = leaps.makeIndividual(15, 'touch', 5, 10, 100, 60, 60, 100);
  assertEqual(leaps.leapsFitnessFn(ind, {}), 0, 'No data -> fitness 0');

  // Positive returns
  const vals = [...Array(122).fill(100), 95, 90, 87, 85, 83, 80, 78, 85, 90, 95, 100, 110, 120, ...Array(200).fill(120)];
  const prices = makePrices(vals, '2024-01-01');
  const fit = leaps.leapsFitnessFn(ind, { TEST: prices });
  assertGreater(fit, 0, 'Positive trades -> positive fitness');
}

// ── Run all ───────────────────────────────────────────────────────────────

testDelta();
testProxyRoi();
testRollingHigh();
testBollinger();
testEntryDetection();
testSellLadder();
testIndividualKey();
testCrossover();
testMutation();
testFitness();

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
