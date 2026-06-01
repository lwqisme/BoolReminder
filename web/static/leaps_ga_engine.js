/**
 * LEAPS GA Engine — pure JS functions ported from drawdown/leaps_option_ga.py.
 * Runs in Web Worker or main thread. No DOM dependencies.
 */

// ── Math helpers ──────────────────────────────────────────────────────────

function normCdf(x) {
  if (x < -8) return 0;
  if (x > 8) return 1;
  // Use native Math.erf when available (fast C implementation in V8)
  if (typeof Math.erf === 'function') {
    return 0.5 * (1 + Math.erf(x / Math.SQRT2));
  }
  // Fallback: Abramowitz & Stegun approximation
  return 0.5 * (1 + _erf(x / Math.SQRT2));
}

// Abramowitz & Stegun 7.1.26 approximation for error function
function _erf(x) {
  const sign = x >= 0 ? 1 : -1;
  x = Math.abs(x);
  const a1 = 0.254829592, a2 = -0.284496736, a3 = 1.421413741;
  const a4 = -1.453152027, a5 = 1.061405429;
  const p = 0.3275911;
  const t = 1 / (1 + p * x);
  const y = 1 - (((((a5 * t + a4) * t) + a3) * t + a2) * t + a1) * t * Math.exp(-x * x);
  return sign * y;
}

function bsCallPrice(S, K, t, r = 0.05, sigma = 0.40) {
  if (t <= 0) return Math.max(0, S - K);
  const d1 = (Math.log(S / K) + (r + sigma * sigma / 2) * t) / (sigma * Math.sqrt(t));
  const d2 = d1 - sigma * Math.sqrt(t);
  return S * normCdf(d1) - K * Math.exp(-r * t) * normCdf(d2);
}

// ── Option pricing ────────────────────────────────────────────────────────

function estimateOptionDelta(stockPrice, strike, dte, r = 0.05, sigma = 0.40) {
  if (dte <= 0) return stockPrice > strike ? 1 : 0;
  const t = dte / 365;
  if (stockPrice <= 0 || strike <= 0 || t <= 0) return 0;
  const d1 = (Math.log(stockPrice / strike) + (r + sigma * sigma / 2) * t) / (sigma * Math.sqrt(t));
  return Math.max(0, Math.min(1, normCdf(d1)));
}

function proxyOptionRoi(entryPrice, exitPrice, entryDate, exitDate, expiration, strikePrice, r = 0.05, sigma = 0.40) {
  // Use pre-parsed timestamps when possible, fall back to string parsing
  const dteEntry = typeof entryDate === 'number'
    ? Math.max(1, (typeof expiration === 'number' ? expiration : new Date(expiration).getTime()) - entryDate) / 86400000
    : Math.max(1, (new Date(expiration) - new Date(entryDate)) / 86400000);
  const dteExit = typeof exitDate === 'number'
    ? Math.max(1, (typeof expiration === 'number' ? expiration : new Date(expiration).getTime()) - exitDate) / 86400000
    : Math.max(1, (new Date(expiration) - new Date(exitDate)) / 86400000);
  const tEntry = dteEntry / 365;
  const tExit = dteExit / 365;
  const optEntry = bsCallPrice(entryPrice, strikePrice, tEntry, r, sigma);
  const optExit = bsCallPrice(exitPrice, strikePrice, tExit, r, sigma);
  if (optEntry <= 0) return 0;
  return (optExit / optEntry - 1) * 100;
}

// ── Technical indicators ──────────────────────────────────────────────────

function rolling120dHigh(prices) {
  const result = [];
  const window = [];
  for (let i = 0; i < prices.length; i++) {
    window.push(prices[i][1]);
    if (i >= 120) window.shift();
    if (i >= 119) {
      result.push([prices[i][0], Math.max(...window)]);
    } else {
      result.push([prices[i][0], null]);
    }
  }
  return result;
}

function bollingerLowerBand(prices, period = 22, stdMult = 2.0) {
  const result = [];
  const window = [];
  for (let i = 0; i < prices.length; i++) {
    window.push(prices[i][1]);
    if (i >= period) window.shift();
    if (i >= period - 1) {
      const mean = window.reduce((a, b) => a + b, 0) / window.length;
      const variance = window.reduce((s, v) => s + (v - mean) ** 2, 0) / window.length;
      const std = Math.sqrt(variance);
      result.push({ date: prices[i][0], ma: mean, band: mean - stdMult * std });
    } else {
      result.push({ date: prices[i][0], ma: null, band: null });
    }
  }
  return result;
}

// ── Entry detection ───────────────────────────────────────────────────────

function detectLeapsEntries(prices, drawdownThresholdPct = 20, entryMode = 'both') {
  if (prices.length < 122) return [];

  const highs = rolling120dHigh(prices);
  const bbData = bollingerLowerBand(prices, 22, 2.0);
  const entries = [];

  for (let i = 121; i < prices.length; i++) {
    const [d, p] = prices[i];
    const high = highs[i][1];
    const { ma, band } = bbData[i];
    if (high == null || ma == null || band == null || high <= 0 || ma <= 0) continue;

    const drawdownPct = (high - p) / high * 100;
    if (drawdownPct < drawdownThresholdPct) continue;

    const maMinusBand = ma - band;
    const bollingerScore = maMinusBand <= 0 ? (p <= band ? 1 : 0) : (ma - p) / maMinusBand;

    const isTouch = bollingerScore >= 1;
    let isBounce = false;
    if (i > 0) {
      const prev = bbData[i - 1];
      if (prev.ma != null && prev.band != null) {
        const prevMaMinusBand = prev.ma - prev.band;
        if (prevMaMinusBand > 0) {
          const prevScore = (prev.ma - prices[i - 1][1]) / prevMaMinusBand;
          isBounce = prevScore >= 1 && bollingerScore < 1;
        }
      }
    }

    if (entryMode === 'touch' && !isTouch) continue;
    if (entryMode === 'bounce' && !isBounce) continue;
    if (entryMode === 'both' && !(isTouch || isBounce)) continue;

    const ddScore = Math.min(drawdownPct / 40, 1);
    const bbScore = Math.min(bollingerScore / 2, 1);
    const composite = (ddScore + bbScore) / 2;

    entries.push({
      date: d, price: p,
      drawdown_pct: Math.round(drawdownPct * 100) / 100,
      bollinger_score: Math.round(bollingerScore * 10000) / 10000,
      composite_score: Math.round(composite * 10000) / 10000,
    });
  }
  return entries;
}

// ── Sell ladder ───────────────────────────────────────────────────────────

function computeSellLadder(entry, prices, stages, expirationDays = 190, strikePrice = null, r = 0.05, sigma = 0.40) {
  if (strikePrice == null) strikePrice = entry.price * 1.1;

  // Use timestamps for fast date comparison
  const entryTs = new Date(entry.date).getTime();
  const expTs = entryTs + expirationDays * 86400000;
  const cutoffTs = expTs - 60 * 86400000;

  // Build price points with timestamps
  const pricePoints = prices.map(([d, p]) => ({ ts: new Date(d).getTime(), date: d, price: p }));

  const effectiveStages = [...stages];
  while (effectiveStages.length < 3) effectiveStages.push([9999, 0, 100]);

  const sellEvents = [];
  let remainingPct = 100;
  const stageTriggered = effectiveStages.map(() => false);

  // Iterate only over actual price data points (not every calendar day!)
  for (const pt of pricePoints) {
    if (pt.ts <= entryTs) continue;

    // Check hard cutoff
    if (pt.ts > cutoffTs) {
      const roi = proxyOptionRoi(entry.price, pt.price, entryTs, pt.ts, expTs, strikePrice, r, sigma);
      sellEvents.push({ date: pt.date, price: pt.price, pct_sold: remainingPct, roi_pct: Math.round(roi * 100) / 100 });
      break;
    }

    const holdDays = Math.round((pt.ts - entryTs) / 86400000);
    const roi = proxyOptionRoi(entry.price, pt.price, entryTs, pt.ts, expTs, strikePrice, r, sigma);

    for (let si = 0; si < effectiveStages.length; si++) {
      if (stageTriggered[si]) continue;
      const [minHold, profitThreshold, sellFraction] = effectiveStages[si];
      if (holdDays < minHold) continue;
      if (roi < profitThreshold) continue;
      if (remainingPct <= 0) break;

      const sellAmount = Math.min(sellFraction, remainingPct);
      remainingPct -= sellAmount;
      stageTriggered[si] = true;
      sellEvents.push({ date: pt.date, price: pt.price, pct_sold: Math.round(sellAmount * 100) / 100, roi_pct: Math.round(roi * 100) / 100 });

      if (remainingPct <= 0) break;
    }

    if (remainingPct <= 0) break;
  }

  // Force-sell if expired without triggering
  let expired = remainingPct > 0;
  if (expired && pricePoints.length) {
    const lastPt = pricePoints[pricePoints.length - 1];
    const roi = proxyOptionRoi(entry.price, lastPt.price, entryTs, cutoffTs, expTs, strikePrice, r, sigma);
    sellEvents.push({ date: new Date(cutoffTs).toISOString().slice(0, 10), price: lastPt.price, pct_sold: remainingPct, roi_pct: Math.round(roi * 100) / 100 });
  }

  let totalRoi = 0;
  if (sellEvents.length) {
    let weightSum = 0;
    for (const se of sellEvents) {
      totalRoi += se.roi_pct * (se.pct_sold / 100);
      weightSum += se.pct_sold / 100;
    }
    totalRoi /= weightSum;
  }

  return {
    entry,
    sell_events: sellEvents,
    expired,
    total_roi_pct: Math.round(totalRoi * 100) / 100,
  };
}

// ── GA Individual ─────────────────────────────────────────────────────────

function leapsIndividualKey(dd, mode, s1d, s1p, s1s, s2d, s2p, s2s) {
  return `dd${dd}__${mode}__s1d${s1d}_p${s1p}_s${s1s}__s2d${s2d}_p${s2p}_s${s2s}`;
}

function makeIndividual(dd, mode, s1d, s1p, s1s, s2d, s2p, s2s) {
  return {
    drawdown_threshold_pct: dd, entry_mode: mode,
    stage1_days: s1d, stage1_profit: s1p, stage1_sell: s1s,
    stage2_days: s2d, stage2_profit: s2p, stage2_sell: s2s,
    key: leapsIndividualKey(dd, mode, s1d, s1p, s1s, s2d, s2p, s2s),
    toStages() { return [[this.stage1_days, this.stage1_profit, this.stage1_sell], [this.stage2_days, this.stage2_profit, this.stage2_sell]]; },
  };
}

// ── GA operators ──────────────────────────────────────────────────────────

const ENTRY_MODES = ['touch', 'bounce', 'both'];

function enforceDayOrder(d1, d2, ranges) {
  if (d1 >= d2) {
    d2 = d1 + Math.floor(Math.random() * 21) + 10;
    if (d2 > ranges.stage2_days[1]) {
      d2 = ranges.stage2_days[1];
      d1 = d2 - Math.floor(Math.random() * 11) - 10;
    }
  }
  return [d1, d2];
}

function enforceProfitOrder(p1, p2, ranges) {
  if (p1 <= p2) {
    p1 = p2 + Math.random() * 20 + 10;
    if (p1 > ranges.stage1_profit[1]) {
      p1 = ranges.stage1_profit[1];
      p2 = p1 - Math.random() * 20 - 10;
    }
  }
  return [p1, p2];
}

function ddOptions(ranges) {
  const [lo, hi] = ranges.drawdown_threshold_pct;
  const opts = [];
  for (let v = lo; v <= hi + 0.01; v += 5) opts.push(Math.round(v * 10) / 10);
  return opts.length ? opts : [lo];
}

function randomIndividual(ranges) {
  const dd = ddOptions(ranges)[Math.floor(Math.random() * ddOptions(ranges).length)];
  const mode = ENTRY_MODES[Math.floor(Math.random() * ENTRY_MODES.length)];
  let s1d = Math.floor(Math.random() * (ranges.stage1_days[1] - ranges.stage1_days[0] + 1)) + ranges.stage1_days[0];
  let s2d = Math.floor(Math.random() * (ranges.stage2_days[1] - ranges.stage2_days[0] + 1)) + ranges.stage2_days[0];
  let s1p = Math.round((Math.random() * (ranges.stage1_profit[1] - ranges.stage1_profit[0]) + ranges.stage1_profit[0]));
  let s2p = Math.round((Math.random() * (ranges.stage2_profit[1] - ranges.stage2_profit[0]) + ranges.stage2_profit[0]));
  let s1s = Math.round((Math.random() * (ranges.stage1_sell[1] - ranges.stage1_sell[0]) + ranges.stage1_sell[0]));
  let s2s = Math.round((Math.random() * (ranges.stage2_sell[1] - ranges.stage2_sell[0]) + ranges.stage2_sell[0]));
  [s1d, s2d] = enforceDayOrder(s1d, s2d, ranges);
  [s1p, s2p] = enforceProfitOrder(s1p, s2p, ranges);
  return makeIndividual(dd, mode, s1d, s1p, s1s, s2d, s2p, s2s);
}

function leapsCrossover(p1, p2, ranges) {
  const pick = (a, b) => Math.random() < 0.5 ? a : b;
  let s1d = pick(p1.stage1_days, p2.stage1_days);
  let s2d = pick(p1.stage2_days, p2.stage2_days);
  let s1p = pick(p1.stage1_profit, p2.stage1_profit);
  let s2p = pick(p1.stage2_profit, p2.stage2_profit);
  [s1d, s2d] = enforceDayOrder(s1d, s2d, ranges);
  [s1p, s2p] = enforceProfitOrder(s1p, s2p, ranges);
  return makeIndividual(
    pick(p1.drawdown_threshold_pct, p2.drawdown_threshold_pct),
    pick(p1.entry_mode, p2.entry_mode),
    s1d, s1p,
    pick(p1.stage1_sell, p2.stage1_sell),
    s2d, s2p,
    pick(p1.stage2_sell, p2.stage2_sell),
  );
}

function leapsMutate(ind, mutationRate, ranges) {
  let dd = ind.drawdown_threshold_pct;
  let mode = ind.entry_mode;
  let s1d = ind.stage1_days, s1p = ind.stage1_profit, s1s = ind.stage1_sell;
  let s2d = ind.stage2_days, s2p = ind.stage2_profit, s2s = ind.stage2_sell;

  if (Math.random() < mutationRate) dd = ddOptions(ranges)[Math.floor(Math.random() * ddOptions(ranges).length)];
  if (Math.random() < mutationRate) mode = ENTRY_MODES[Math.floor(Math.random() * ENTRY_MODES.length)];
  if (Math.random() < mutationRate) s1d = Math.floor(Math.random() * (ranges.stage1_days[1] - ranges.stage1_days[0] + 1)) + ranges.stage1_days[0];
  if (Math.random() < mutationRate) s1p = Math.round(Math.random() * (ranges.stage1_profit[1] - ranges.stage1_profit[0]) + ranges.stage1_profit[0]);
  if (Math.random() < mutationRate) s1s = Math.round(Math.random() * (ranges.stage1_sell[1] - ranges.stage1_sell[0]) + ranges.stage1_sell[0]);
  if (Math.random() < mutationRate) s2d = Math.floor(Math.random() * (ranges.stage2_days[1] - ranges.stage2_days[0] + 1)) + ranges.stage2_days[0];
  if (Math.random() < mutationRate) s2p = Math.round(Math.random() * (ranges.stage2_profit[1] - ranges.stage2_profit[0]) + ranges.stage2_profit[0]);
  if (Math.random() < mutationRate) s2s = Math.round(Math.random() * (ranges.stage2_sell[1] - ranges.stage2_sell[0]) + ranges.stage2_sell[0]);

  [s1d, s2d] = enforceDayOrder(s1d, s2d, ranges);
  [s1p, s2p] = enforceProfitOrder(s1p, s2p, ranges);
  return makeIndividual(dd, mode, s1d, s1p, s1s, s2d, s2p, s2s);
}

// ── Fitness ───────────────────────────────────────────────────────────────

function leapsFitnessFn(individual, priceSeriesBySymbol) {
  const allRois = [];
  const allDates = [];

  for (const [symbol, prices] of Object.entries(priceSeriesBySymbol)) {
    const entries = detectLeapsEntries(prices, individual.drawdown_threshold_pct, individual.entry_mode);
    const stages = individual.toStages();
    for (const entry of entries) {
      const trade = computeSellLadder(entry, prices, stages, 190, entry.price * 1.1);
      allRois.push(trade.total_roi_pct);
      allDates.push(entry.date);
    }
  }

  if (!allRois.length) return 0;

  const totalRoi = allRois.reduce((a, b) => a + b, 0);
  const numTrades = allRois.length;
  allDates.sort();
  const years = Math.max((new Date(allDates[allDates.length - 1]) - new Date(allDates[0])) / (365 * 86400000), 0.5);
  const tradesPerYear = numTrades / years;
  const densityBonus = Math.min(tradesPerYear / 3, 2);
  const annualizedRoi = totalRoi / years;
  return annualizedRoi * densityBonus;
}

// ── Evolution ─────────────────────────────────────────────────────────────

function tournamentSelect(population, fitnesses, tournamentSize) {
  const indices = [];
  while (indices.length < Math.min(tournamentSize, population.length)) {
    const idx = Math.floor(Math.random() * population.length);
    if (!indices.includes(idx)) indices.push(idx);
  }
  let bestIdx = indices[0];
  for (const idx of indices) {
    if (fitnesses[idx] > fitnesses[bestIdx]) bestIdx = idx;
  }
  return population[bestIdx];
}

const DEFAULT_RANGES = {
  drawdown_threshold_pct: [10, 30],
  stage1_days: [10, 30], stage2_days: [30, 90],
  stage1_profit: [60, 120], stage2_profit: [40, 100],
  stage1_sell: [30, 70], stage2_sell: [30, 70],
};

function mergeRanges(custom) {
  // deep clone defaults, then override with custom
  return custom || JSON.parse(JSON.stringify(DEFAULT_RANGES));
}

// Make engine exportable for both Node.js tests and browser worker
const leapsGaEngine = {
  estimateOptionDelta,
  proxyOptionRoi,
  rolling120dHigh,
  bollingerLowerBand,
  detectLeapsEntries,
  computeSellLadder,
  leapsIndividualKey,
  makeIndividual,
  leapsCrossover,
  leapsMutate,
  leapsFitnessFn,
  tournamentsSelect: tournamentSelect,
  randomIndividual,
  ddOptions,
  DEFAULT_RANGES,
  mergeRanges,
  ENTRY_MODES,
};

// Export for Node.js testing
if (typeof module !== 'undefined' && module.exports) {
  module.exports = leapsGaEngine;
}
