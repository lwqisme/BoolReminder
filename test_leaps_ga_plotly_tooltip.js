/**
 * Test: LEAPS trade chart Plotly traces include hover text on every point.
 * Run: node test_leaps_ga_plotly_tooltip.js
 */

// Simulate bollinger computation matching engine (for testing)
function computeBollingerLower(prices, period, stdMult) {
  // prices: [[dateStr, price], ...]
  const result = [];
  const window = [];
  for (let i = 0; i < prices.length; i++) {
    window.push(prices[i][1]);
    if (i >= period) window.shift();
    if (i >= period - 1) {
      const mean = window.reduce((a, b) => a + b, 0) / window.length;
      const variance = window.reduce((s, v) => s + (v - mean) ** 2, 0) / window.length;
      const std = Math.sqrt(variance);
      result.push({ date: prices[i][0], band: mean - stdMult * std });
    } else {
      result.push({ date: prices[i][0], band: null });
    }
  }
  return result;
}

// Simulate the trace-building logic (extracted from drawLeapsTradePlotly)
function buildTradeTraces(trades, fullPriceSeries) {
  const traces = [];
  const entryX = [], entryY = [], entryText = [];
  const sellX = [], sellY = [], sellText = [], sellSizes = [];

  // ── Full price line (single continuous trace) ──
  if (fullPriceSeries && fullPriceSeries.length) {
    const px = fullPriceSeries.map(pt => pt[0]);
    const py = fullPriceSeries.map(pt => pt[1]);
    const hoverText = fullPriceSeries.map(pt => `${pt[0]}<br>价格: ${pt[1].toFixed(2)}`);
    traces.push({
      x: px, y: py, type: 'scatter', mode: 'lines',
      name: 'TSLA.US 价格',
      text: hoverText, hoverinfo: 'text',
      line: { color: '#6b7280', width: 1.5 },
      showlegend: true,
    });

    // Full bollinger lower band (solid, not dashed)
    const bb = computeBollingerLower(fullPriceSeries, 22, 2.0);
    const bbX = [], bbY = [], bbText = [];
    for (const b of bb) {
      if (b.band != null) {
        bbX.push(b.date);
        bbY.push(b.band);
        bbText.push(`${b.date}<br>布林下轨: ${b.band.toFixed(2)}`);
      }
    }
    if (bbX.length) {
      traces.push({
        x: bbX, y: bbY, type: 'scatter', mode: 'lines',
        name: '布林下轨',
        text: bbText, hoverinfo: 'text',
        line: { color: '#3b82f6', width: 1.5 },
        showlegend: true,
      });
    }
  }

  // ── Entry/sell markers from trades ──
  const entryCustomdata = [], sellCustomdata = [];
  for (let t = 0; t < trades.length; t++) {
    const tr = trades[t];
    entryX.push(tr.entry_date);
    entryY.push(tr.entry_price);
    entryText.push(tr.entry_date + '<br>' + tr.symbol + ' 买入<br>回撤: ' + tr.drawdown_pct.toFixed(1) + '%<br>价格: ' + tr.entry_price.toFixed(2));
    entryCustomdata.push(t);

    for (const se of tr.sell_events || []) {
      sellX.push(se.date);
      sellY.push(se.price);
      sellText.push(se.date + '<br>卖' + se.pct_sold + '%<br>价格: ' + se.price.toFixed(2) + '<br>ROI: ' + (se.roi_pct > 0 ? '+' : '') + Math.round(se.roi_pct) + '%');
      sellSizes.push(Math.max(6, (se.pct_sold / 50) * 14));
      sellCustomdata.push(t);
    }
  }

  traces.push({
    x: entryX, y: entryY, type: 'scatter', mode: 'markers',
    name: '买入点',
    marker: { color: '#059669', size: 12, symbol: 'circle', line: { color: '#fff', width: 2 } },
    text: entryText, hoverinfo: 'text',
    customdata: entryCustomdata,
  });

  traces.push({
    x: sellX, y: sellY, type: 'scatter', mode: 'markers',
    name: '卖出点',
    marker: { color: '#dc2626', size: sellSizes, symbol: 'circle', line: { color: '#fff', width: 2 } },
    text: sellText, hoverinfo: 'text',
    customdata: sellCustomdata,
  });

  return traces;
}

// ── Tests ──
let passed = 0, failed = 0;
function assert(cond, msg) { if (cond) passed++; else { failed++; console.error('FAIL:', msg); } }

const sampleFullPriceSeries = [
  ['2024-01-15', 180], ['2024-02-01', 175], ['2024-02-15', 165],
  ['2024-03-01', 155], ['2024-03-15', 150], ['2024-04-01', 170],
  ['2024-04-15', 185], ['2024-05-01', 200], ['2024-05-15', 210],
  ['2024-06-01', 220], ['2024-06-15', 230], ['2024-07-01', 250],
  ['2024-07-15', 240], ['2024-08-01', 235], ['2024-08-15', 245],
  ['2024-09-01', 255], ['2024-09-15', 260], ['2024-10-01', 250],
  ['2024-10-15', 245], ['2024-11-01', 265], ['2024-11-15', 280],
  ['2024-12-01', 290], ['2024-12-15', 300],
];

const sampleTrades = [{
  symbol: 'TSLA.US',
  entry_date: '2024-03-15', entry_price: 150, drawdown_pct: 22,
  price_series: [
    { date: '2024-01-15', price: 180, bollinger_lower: 175 },
    { date: '2024-03-01', price: 155, bollinger_lower: 158 },
    { date: '2024-03-15', price: 150, bollinger_lower: 152 },
    { date: '2024-05-01', price: 200, bollinger_lower: 155 },
  ],
  sell_events: [
    { date: '2024-05-01', price: 200, pct_sold: 50, roi_pct: 80 },
    { date: '2024-07-01', price: 250, pct_sold: 50, roi_pct: 120 },
  ],
}, {
  symbol: 'TSLA.US',
  entry_date: '2024-11-01', entry_price: 265, drawdown_pct: 15,
  price_series: [
    { date: '2024-09-01', price: 255, bollinger_lower: 250 },
    { date: '2024-11-01', price: 265, bollinger_lower: 260 },
    { date: '2024-12-15', price: 300, bollinger_lower: 270 },
  ],
  sell_events: [
    { date: '2024-12-15', price: 300, pct_sold: 100, roi_pct: 50 },
  ],
}];

const traces = buildTradeTraces(sampleTrades, sampleFullPriceSeries);

// Test 1: Single continuous price line (not per-trade slices)
const priceTraces = traces.filter(t => t.name && t.name.includes('价格'));
assert(priceTraces.length === 1, 'Exactly one price trace (full continuous line)');
const priceTrace = priceTraces[0];
assert(priceTrace.x.length === sampleFullPriceSeries.length, 'Price line covers all data points');
assert(priceTrace.text.length === priceTrace.x.length, 'Price hover text matches point count');
assert(priceTrace.hoverinfo === 'text', 'Price trace uses text hoverinfo');
assert(priceTrace.text[0].includes('2024-01-15'), 'Hover text contains date');
assert(priceTrace.text[0].includes('180.00'), 'Hover text contains price');

// Test 2: Single bollinger trace, solid line (NOT dashed)
const bbTraces = traces.filter(t => t.name && t.name.includes('布林'));
assert(bbTraces.length === 1, 'Exactly one bollinger trace');
const bbTrace = bbTraces[0];
assert(!bbTrace.line.dash, 'Bollinger line is solid (no dash property)');
assert(bbTrace.line.width === 1.5, 'Bollinger line width matches price line');
assert(bbTrace.text.length === bbTrace.x.length, 'BB hover text matches point count');
assert(bbTrace.hoverinfo === 'text', 'BB trace uses text hoverinfo');
// First 21 points (period-1) should have no band, so BB starts at index 21
assert(bbTrace.x[0] === '2024-12-01', 'Bollinger starts at index 21 (22-1 warmup)');

// Test 3: Entry markers have hover text
const entryTrace = traces.find(t => t.name === '买入点');
assert(entryTrace != null, 'Entry trace exists');
assert(entryTrace.text.length === entryTrace.x.length, 'Entry hover text count matches');
assert(entryTrace.text[0].includes('买入'), 'Entry hover has 买入');
assert(entryTrace.text[0].includes('2024-03-15'), 'Entry hover has date');
assert(entryTrace.text[0].includes('22'), 'Entry hover has drawdown');

// Test 4: Sell markers have hover text with date
const sellTrace = traces.find(t => t.name === '卖出点');
assert(sellTrace != null, 'Sell trace exists');
assert(sellTrace.text.length === sellTrace.x.length, 'Sell hover text count matches');
assert(sellTrace.text[0].includes('2024-05-01'), 'Sell hover has date');
assert(sellTrace.text[0].includes('50%'), 'Sell hover has pct_sold');
assert(sellTrace.text[0].includes('80%'), 'Sell hover has ROI');

// Test 5: Entry markers have customdata with trade index
assert(Array.isArray(entryTrace.customdata), 'Entry trace has customdata array');
assert(entryTrace.customdata.length === entryTrace.x.length, 'Entry customdata count matches markers');
assert(entryTrace.customdata[0] === 0, 'First entry belongs to trade 0');
assert(entryTrace.customdata[1] === 1, 'Second entry belongs to trade 1');

// Test 6: Sell markers have customdata matching parent trade
assert(Array.isArray(sellTrace.customdata), 'Sell trace has customdata array');
assert(sellTrace.customdata.length === sellTrace.x.length, 'Sell customdata count matches markers');
// Trade 0 has 2 sells -> customdata[0]=0, customdata[1]=0
assert(sellTrace.customdata[0] === 0, 'First sell belongs to trade 0');
assert(sellTrace.customdata[1] === 0, 'Second sell belongs to trade 0');
// Trade 1 has 1 sell -> customdata[2]=1
assert(sellTrace.customdata[2] === 1, 'Third sell belongs to trade 1');

// Test 7: Buy and sell markers for same trade share same customdata index
assert(entryTrace.customdata[0] === sellTrace.customdata[0], 'Trade 0 buy and first sell share same index');

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
