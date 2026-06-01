/**
 * Test: LEAPS trade chart Plotly traces include hover text on every point.
 * Run: node test_leaps_ga_plotly_tooltip.js
 */

// Simulate the trace-building logic (extracted from drawLeapsTradePlotly)
function buildTradeTraces(trades) {
  const traces = [];
  const entryX = [], entryY = [], entryText = [];
  const sellX = [], sellY = [], sellText = [], sellSizes = [];

  for (const tr of trades) {
    const series = tr.price_series || [];

    if (series.length) {
      const px = series.map(pt => pt.date);
      const py = series.map(pt => pt.price);
      const hoverText = series.map(pt => `${pt.date}<br>价格: ${pt.price.toFixed(2)}` +
        (pt.bollinger_lower != null ? `<br>布林下轨: ${pt.bollinger_lower.toFixed(2)}` : ''));
      
      traces.push({
        x: px, y: py, type: 'scatter', mode: 'lines',
        name: tr.symbol + ' 价格',
        text: hoverText, hoverinfo: 'text',
        line: { color: '#6b7280', width: 1.5 },
        showlegend: false,
      });

      // Bollinger band with hover
      const bbX = [], bbY = [], bbText = [];
      for (const pt of series) {
        if (pt.bollinger_lower != null) {
          bbX.push(pt.date);
          bbY.push(pt.bollinger_lower);
          bbText.push(`${pt.date}<br>布林下轨: ${pt.bollinger_lower.toFixed(2)}`);
        }
      }
      if (bbX.length) {
        traces.push({
          x: bbX, y: bbY, type: 'scatter', mode: 'lines',
          name: tr.symbol + ' 布林下轨',
          text: bbText, hoverinfo: 'text',
          line: { color: '#3b82f6', width: 1, dash: 'dash' },
          showlegend: false,
        });
      }
    }

    entryX.push(tr.entry_date);
    entryY.push(tr.entry_price);
    entryText.push(tr.symbol + ' 买入<br>回撤: ' + tr.drawdown_pct.toFixed(1) + '%<br>价格: ' + tr.entry_price.toFixed(2));

    for (const se of tr.sell_events || []) {
      sellX.push(se.date);
      sellY.push(se.price);
      sellText.push('卖' + se.pct_sold + '% ROI: +' + Math.round(se.roi_pct) + '%<br>价格: ' + se.price.toFixed(2));
      sellSizes.push(Math.max(6, (se.pct_sold / 50) * 14));
    }
  }

  traces.push({
    x: entryX, y: entryY, type: 'scatter', mode: 'markers',
    name: '买入点',
    marker: { color: '#059669', size: 12, symbol: 'circle', line: { color: '#fff', width: 2 } },
    text: entryText, hoverinfo: 'text',
  });

  traces.push({
    x: sellX, y: sellY, type: 'scatter', mode: 'markers',
    name: '卖出点',
    marker: { color: '#dc2626', size: sellSizes, symbol: 'circle', line: { color: '#fff', width: 2 } },
    text: sellText, hoverinfo: 'text',
  });

  return traces;
}

// ── Tests ──
let passed = 0, failed = 0;
function assert(cond, msg) { if (cond) passed++; else { failed++; console.error('FAIL:', msg); } }

const sampleTrades = [{
  symbol: 'AAPL',
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
}];

const traces = buildTradeTraces(sampleTrades);

// Test 1: Price line trace has hover text for every point
const priceTrace = traces.find(t => t.name && t.name.includes('价格'));
assert(priceTrace != null, 'Price line trace exists');
assert(priceTrace.text.length === priceTrace.x.length, 'Price hover text matches point count');
assert(priceTrace.hoverinfo === 'text', 'Price trace uses text hoverinfo');
// Check first hover text contains date and price
assert(priceTrace.text[0].includes('2024-01-15'), 'Hover text contains date');
assert(priceTrace.text[0].includes('180.00'), 'Hover text contains price');
assert(priceTrace.text[0].includes('布林下轨'), 'Hover text contains bollinger when available');

// Test 2: Bollinger trace has hover text
const bbTrace = traces.find(t => t.name && t.name.includes('布林'));
assert(bbTrace != null, 'Bollinger trace exists');
assert(bbTrace.text.length === bbTrace.x.length, 'BB hover text matches point count');
assert(bbTrace.hoverinfo === 'text', 'BB trace uses text hoverinfo');

// Test 3: Entry markers have hover text
const entryTrace = traces.find(t => t.name === '买入点');
assert(entryTrace != null, 'Entry trace exists');
assert(entryTrace.text.length === entryTrace.x.length, 'Entry hover text count matches');
assert(entryTrace.text[0].includes('买入'), 'Entry hover has 买入');
assert(entryTrace.text[0].includes('22'), 'Entry hover has drawdown');

// Test 4: Sell markers have hover text
const sellTrace = traces.find(t => t.name === '卖出点');
assert(sellTrace != null, 'Sell trace exists');
assert(sellTrace.text.length === sellTrace.x.length, 'Sell hover text count matches');
assert(sellTrace.text[0].includes('50%'), 'Sell hover has pct_sold');
assert(sellTrace.text[0].includes('80%'), 'Sell hover has ROI');

console.log(`\n${passed} passed, ${failed} failed`);
process.exit(failed > 0 ? 1 : 0);
