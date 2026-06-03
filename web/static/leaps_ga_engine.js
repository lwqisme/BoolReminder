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
  // prices: [timestamp, price, dateString]
  const result = [];
  const window = [];
  for (let i = 0; i < prices.length; i++) {
    window.push(prices[i][1]);
    if (i >= 120) window.shift();
    if (i >= 119) {
      result.push([prices[i][2], Math.max(...window)]);
    } else {
      result.push([prices[i][2], null]);
    }
  }
  return result;
}

function bollingerLowerBand(prices, period = 22, stdMult = 2.0) {
  // prices: [timestamp, price, dateString]
  const result = [];
  const window = [];
  for (let i = 0; i < prices.length; i++) {
    window.push(prices[i][1]);
    if (i >= period) window.shift();
    if (i >= period - 1) {
      const mean = window.reduce((a, b) => a + b, 0) / window.length;
      const variance = window.reduce((s, v) => s + (v - mean) ** 2, 0) / window.length;
      const std = Math.sqrt(variance);
      result.push({ date: prices[i][2], ma: mean, band: mean - stdMult * std });
    } else {
      result.push({ date: prices[i][2], ma: null, band: null });
    }
  }
  return result;
}

// ── Entry detection ───────────────────────────────────────────────────────

function detectLeapsEntries(prices, drawdownThresholdPct = 20, entryMode = 'both', minEntryDate = null) {
  if (prices.length < 122) return [];

  const highs = rolling120dHigh(prices);
  const bbData = bollingerLowerBand(prices, 22, 2.0);
  const entries = [];

  for (let i = 121; i < prices.length; i++) {
    const [ts, p, d] = prices[i];
    if (minEntryDate && d < minEntryDate) continue;
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

function computeSellLadder(entry, prices, stages, expirationDays = 190, strikePrice = null, r = 0.05, sigma = 0.40, tradeOverrides = null) {
  if (strikePrice == null) strikePrice = entry.price * 1.1;

  // Use timestamps for fast date comparison (prices are [ts, price, dateStr])
  const entryTs = new Date(entry.date).getTime();
  const expTs = entryTs + expirationDays * 86400000;
  const cutoffTs = expTs - 60 * 86400000;

  // Build price points with timestamps (already pre-parsed: [ts, price, dateStr])
  const pricePoints = prices.map(([ts, p, d]) => ({ ts, date: d, price: p }));

  const effectiveStages = [...stages];
  while (effectiveStages.length < 3) effectiveStages.push([9999, 0, 100]);

  const sellEvents = [];
  let remainingPct = 100;
  const stageTriggered = effectiveStages.map(() => false);
  // 方案 B: track how much of each stage was actually sold
  const stageSoldPct = effectiveStages.map(() => 0.0);

  // Build override lookup: date → pct sold
  const overrideByDate = {};
  if (tradeOverrides) {
    for (const [d, v] of Object.entries(tradeOverrides)) {
      overrideByDate[d] = Math.min(Number(v), 100);
    }
  }

  // Iterate only over actual price data points (not every calendar day!)
  for (const pt of pricePoints) {
    if (pt.ts <= entryTs) continue;

    // Check hard cutoff
    if (pt.ts > cutoffTs) {
      const roi = proxyOptionRoi(entry.price, pt.price, entryTs, pt.ts, expTs, strikePrice, r, sigma);
      sellEvents.push({ date: pt.date, price: pt.price, pct_sold: remainingPct, roi_pct: Math.round(roi * 100) / 100 });
      remainingPct = 0;
      break;
    }

    const holdDays = Math.round((pt.ts - entryTs) / 86400000);

    // Apply real trade overrides before checking stages (方案 B)
    const overridePct = overrideByDate[pt.date] || 0;
    if (overridePct > 0 && remainingPct > 0) {
      let remainingOverride = Math.min(overridePct, remainingPct);
      for (let si = 0; si < effectiveStages.length; si++) {
        if (remainingOverride <= 0) break;
        const stageTarget = effectiveStages[si][2];
        const stageRemaining = Math.max(0, stageTarget - stageSoldPct[si]);
        if (stageRemaining > 0) {
          const deduct = Math.min(remainingOverride, stageRemaining);
          stageSoldPct[si] += deduct;
          remainingOverride -= deduct;
          remainingPct -= deduct;
          // No sell event — real trade, not a signal
          if (stageSoldPct[si] >= stageTarget - 0.01) {
            stageTriggered[si] = true;
          }
        }
      }
      if (remainingOverride > 0) {
        remainingPct -= remainingOverride;
      }
      if (remainingPct <= 0) break;
      // Fall through to normal stage check — remaining may trigger same day
    }

    const roi = proxyOptionRoi(entry.price, pt.price, entryTs, pt.ts, expTs, strikePrice, r, sigma);

    for (let si = 0; si < effectiveStages.length; si++) {
      if (stageTriggered[si]) continue;
      const [minHold, profitThreshold, sellFraction] = effectiveStages[si];
      if (holdDays < minHold) continue;
      if (roi < profitThreshold) continue;
      if (remainingPct <= 0) break;

      // 方案 B: account for partial execution
      const alreadySold = stageSoldPct[si];
      const toSell = Math.max(0, sellFraction - alreadySold);
      if (toSell <= 0.01) {
        stageTriggered[si] = true;
        continue;
      }

      const sellAmount = Math.min(toSell, remainingPct);
      remainingPct -= sellAmount;
      stageSoldPct[si] += sellAmount;
      if (stageSoldPct[si] >= sellFraction - 0.01) {
        stageTriggered[si] = true;
      }
      sellEvents.push({ date: pt.date, price: pt.price, pct_sold: Math.round(sellAmount * 100) / 100, roi_pct: Math.round(roi * 100) / 100 });

      if (remainingPct <= 0) break;
    }

    if (remainingPct <= 0) break;
  }

  // Force-sell if expired without triggering
  let expired = remainingPct > 0;
  if (expired) {
    // Find price at or nearest to cutoff date
    let cutoffPrice = entry.price;
    let cutoffDate = new Date(cutoffTs).toISOString().slice(0, 10);
    for (const pt of pricePoints) {
      if (pt.ts <= cutoffTs) {
        cutoffPrice = pt.price;
        cutoffDate = pt.date;
      } else {
        break;
      }
    }
    const roi = proxyOptionRoi(entry.price, cutoffPrice, entryTs, cutoffTs, expTs, strikePrice, r, sigma);
    sellEvents.push({ date: cutoffDate, price: cutoffPrice, pct_sold: remainingPct, roi_pct: Math.round(roi * 100) / 100 });
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

function leapsIndividualKey(dd, mode, s1d, s1p, s1s, s2d, s2p, s2s, pos, cd) {
  return `dd${dd}__${mode}__s1d${s1d}_p${s1p}_s${s1s}__s2d${s2d}_p${s2p}_s${s2s}__pos${pos}__cd${cd}`;
}

function makeIndividual(dd, mode, s1d, s1p, s1s, s2d, s2p, s2s, pos, cd) {
  return {
    drawdown_threshold_pct: dd, entry_mode: mode,
    stage1_days: s1d, stage1_profit: s1p, stage1_sell: s1s,
    stage2_days: s2d, stage2_profit: s2p, stage2_sell: s2s,
    position_pct: pos != null ? pos : DEFAULTS.positionPct, cooldown_days: cd != null ? cd : DEFAULTS.cooldownDays,
    key: leapsIndividualKey(dd, mode, s1d, s1p, s1s, s2d, s2p, s2s, pos != null ? pos : DEFAULTS.positionPct, cd != null ? cd : DEFAULTS.cooldownDays),
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
  // Clamp to ranges
  p1 = Math.max(ranges.stage1_profit[0], Math.min(ranges.stage1_profit[1], p1));
  p2 = Math.max(ranges.stage2_profit[0], Math.min(ranges.stage2_profit[1], p2));
  if (p1 <= p2) {
    p1 = Math.min(ranges.stage1_profit[1], p2 + Math.random() * 10 + 5);
    p2 = Math.max(ranges.stage2_profit[0], p1 - Math.random() * 10 - 5);
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
  const pos = Math.round((Math.random() * (ranges.position_pct[1] - ranges.position_pct[0]) + ranges.position_pct[0]) * 10) / 10;
  const cd = Math.floor(Math.random() * (ranges.cooldown_days[1] - ranges.cooldown_days[0] + 1)) + ranges.cooldown_days[0];
  [s1d, s2d] = enforceDayOrder(s1d, s2d, ranges);
  [s1p, s2p] = enforceProfitOrder(s1p, s2p, ranges);
  return makeIndividual(dd, mode, s1d, s1p, s1s, s2d, s2p, s2s, pos, cd);
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
    pick(p1.position_pct, p2.position_pct),
    pick(p1.cooldown_days, p2.cooldown_days),
  );
}

function leapsMutate(ind, mutationRate, ranges) {
  let dd = ind.drawdown_threshold_pct;
  let mode = ind.entry_mode;
  let s1d = ind.stage1_days, s1p = ind.stage1_profit, s1s = ind.stage1_sell;
  let s2d = ind.stage2_days, s2p = ind.stage2_profit, s2s = ind.stage2_sell;
  let pos = ind.position_pct, cd = ind.cooldown_days;

  if (Math.random() < mutationRate) dd = ddOptions(ranges)[Math.floor(Math.random() * ddOptions(ranges).length)];
  if (Math.random() < mutationRate) mode = ENTRY_MODES[Math.floor(Math.random() * ENTRY_MODES.length)];
  if (Math.random() < mutationRate) s1d = Math.floor(Math.random() * (ranges.stage1_days[1] - ranges.stage1_days[0] + 1)) + ranges.stage1_days[0];
  if (Math.random() < mutationRate) s1p = Math.round(Math.random() * (ranges.stage1_profit[1] - ranges.stage1_profit[0]) + ranges.stage1_profit[0]);
  if (Math.random() < mutationRate) s1s = Math.round(Math.random() * (ranges.stage1_sell[1] - ranges.stage1_sell[0]) + ranges.stage1_sell[0]);
  if (Math.random() < mutationRate) s2d = Math.floor(Math.random() * (ranges.stage2_days[1] - ranges.stage2_days[0] + 1)) + ranges.stage2_days[0];
  if (Math.random() < mutationRate) s2p = Math.round(Math.random() * (ranges.stage2_profit[1] - ranges.stage2_profit[0]) + ranges.stage2_profit[0]);
  if (Math.random() < mutationRate) s2s = Math.round(Math.random() * (ranges.stage2_sell[1] - ranges.stage2_sell[0]) + ranges.stage2_sell[0]);
  if (Math.random() < mutationRate) pos = Math.round((Math.random() * (ranges.position_pct[1] - ranges.position_pct[0]) + ranges.position_pct[0]) * 10) / 10;
  if (Math.random() < mutationRate) cd = Math.floor(Math.random() * (ranges.cooldown_days[1] - ranges.cooldown_days[0] + 1)) + ranges.cooldown_days[0];

  [s1d, s2d] = enforceDayOrder(s1d, s2d, ranges);
  [s1p, s2p] = enforceProfitOrder(s1p, s2p, ranges);
  return makeIndividual(dd, mode, s1d, s1p, s1s, s2d, s2p, s2s, pos, cd);
}

// ── Fitness ───────────────────────────────────────────────────────────────

// Shared trade evaluation: returns all trade objects chronologically
function _leapsEvalTrades(individual, priceSeriesBySymbol, minEntryDate = null) {
  const trades = [];

  for (const [symbol, prices] of Object.entries(priceSeriesBySymbol)) {
    const entries = detectLeapsEntries(prices, individual.drawdown_threshold_pct, individual.entry_mode, minEntryDate);
    const stages = individual.toStages();
    // Filter entries that can't reach minimum hold days before data ends
    const maxPriceDate = new Date(Math.max(...prices.map(p => p[0])));
    const minHoldDays = stages.length ? Math.min(...stages.map(s => s[0])) : 0;
    for (const entry of entries) {
      const minHoldDate = new Date(new Date(entry.date).getTime() + minHoldDays * 86400000);
      if (minHoldDate > maxPriceDate) continue;
      const trade = computeSellLadder(entry, prices, stages, 190, entry.price * 1.1);
      trade.symbol = symbol;
      trades.push(trade);
    }
  }
  trades.sort((a, b) => String(a.entry.date).localeCompare(String(b.entry.date)));
  return trades;
}

// Fixed capital simulation with fund tracking, cooldown, and sequential entries
function _leapsEvalFixedCapital(individual, priceSeriesBySymbol, totalCapital, minEntryDate = null) {
  const allTrades = _leapsEvalTrades(individual, priceSeriesBySymbol, minEntryDate);
  if (!allTrades.length) {
    return { final_equity: totalCapital, cagr: 0, total_return_pct: 0, max_drawdown_pct: 0, trade_count: 0, executed_trades: [] };
  }

  const investPerTrade = totalCapital * individual.position_pct / 100;
  let equity = totalCapital;
  const cd = individual.cooldown_days;

  // Collect all date events
  const dateSet = new Set();
  const entriesByDate = {};
  for (const t of allTrades) {
    const ds = t.entry.date;
    dateSet.add(ds);
    if (!entriesByDate[ds]) entriesByDate[ds] = [];
    entriesByDate[ds].push(t);
    for (const se of t.sell_events) dateSet.add(se.date);
  }
  const sortedDates = [...dateSet].sort();

  // State
  let peakEquity = totalCapital;
  let maxDd = 0;
  let cooldownUntil = null;
  const executedTrades = [];
  const sellEventsByDate = {};
  const openPositions = [];

  for (const currentDate of sortedDates) {
    // Process sells
    if (sellEventsByDate[currentDate]) {
      for (const se of sellEventsByDate[currentDate]) {
        const released = se.invested * (se.pct_sold / 100) * (1 + se.roi_pct / 100);
        equity += released;
        se.position.cumulative_sold = (se.position.cumulative_sold || 0) + se.pct_sold;
      }
    }

    // Remove completed positions
    for (let i = openPositions.length - 1; i >= 0; i--) {
      if ((openPositions[i].cumulative_sold || 0) >= 99.9) openPositions.splice(i, 1);
    }

    // Track equity curve
    if (equity > peakEquity) peakEquity = equity;
    const dd = (peakEquity - equity) / peakEquity * 100;
    if (dd > maxDd) maxDd = dd;

    // Process new entries
    if (entriesByDate[currentDate]) {
      for (const trade of entriesByDate[currentDate]) {
        if (cooldownUntil && String(currentDate) <= cooldownUntil) continue;
        if (equity < investPerTrade) continue;
        equity -= investPerTrade;
        const posData = { invested: investPerTrade, cumulative_sold: 0, _trade: trade };
        openPositions.push(posData);
        executedTrades.push(trade);
        for (const se of trade.sell_events) {
          if (!sellEventsByDate[se.date]) sellEventsByDate[se.date] = [];
          sellEventsByDate[se.date].push({ invested: investPerTrade, pct_sold: se.pct_sold, roi_pct: se.roi_pct, position: posData });
        }
        cooldownUntil = _addDays(currentDate, cd);
      }
    }
  }

  // Force-sell remaining using actual option ROI at last available date
  if (openPositions.length) {
    const lastDate = sortedDates[sortedDates.length - 1];
    for (const pos of openPositions) {
      const remaining = 100 - (pos.cumulative_sold || 0);
      if (remaining <= 0.1) continue;
      const trade = pos._trade;
      let recovery = 0.10; // conservative fallback
      if (trade) {
        const entrySig = trade.entry;
        const expTs = new Date(entrySig.date).getTime() + 190 * 86400000;
        const lastRoi = proxyOptionRoi(entrySig.price, entrySig.price, new Date(entrySig.date).getTime(), new Date(lastDate).getTime(), expTs, entrySig.price * 1.1);
        // Use a floor of -95% to avoid extreme negative values from expired OTM options
        recovery = Math.max(0.05, 1 + lastRoi / 100);
      }
      equity += pos.invested * (remaining / 100) * recovery;
    }
  }

  const totalReturn = (equity / totalCapital - 1) * 100;
  const lastDate = sortedDates[sortedDates.length - 1];
  const firstDate = sortedDates[0];
  const years = Math.max((new Date(lastDate) - new Date(firstDate)) / (365 * 86400000), 0.5);
  const cagr = (Math.pow(equity / totalCapital, 1 / years) - 1) * 100;

  return {
    final_equity: Math.round(equity * 100) / 100,
    cagr: Math.round(cagr * 100) / 100,
    total_return_pct: Math.round(totalReturn * 100) / 100,
    max_drawdown_pct: Math.round(maxDd * 100) / 100,
    trade_count: executedTrades.length,
    executed_trades: executedTrades,
  };
}

function _addDays(dateStr, days) {
  const d = new Date(dateStr);
  d.setDate(d.getDate() + days);
  return d.toISOString().slice(0, 10);
}

// Unlimited capital: geometric compounding of all signals
function _leapsEvalUnlimited(individual, priceSeriesBySymbol, minEntryDate = null) {
  const allTrades = _leapsEvalTrades(individual, priceSeriesBySymbol, minEntryDate);
  if (!allTrades.length) {
    return { geo_product: 1, annualized_geo: 0, total_return_pct: 0, trade_count: 0,
      total_opt_cost: 0, total_opt_revenue: 0 };
  }

  let geoProduct = 1;
  let totalOptCost = 0;
  let totalOptRevenue = 0;
  for (const t of allTrades) {
    geoProduct *= (1 + t.total_roi_pct / 100);
    const optEntry = bsCallPrice(t.entry.price, t.entry.price * 1.1, 190 / 365, 0.05, 0.40);
    totalOptCost += optEntry;
    totalOptRevenue += optEntry * (1 + t.total_roi_pct / 100);
  }

  const firstDate = allTrades[0].entry.date;
  const lastDate = allTrades[allTrades.length - 1].entry.date;
  const years = Math.max((new Date(lastDate) - new Date(firstDate)) / (365 * 86400000), 0.5);
  const annualized = (Math.pow(geoProduct, 1 / years) - 1) * 100;
  const totalReturn = (geoProduct - 1) * 100;

  return {
    geo_product: Math.round(geoProduct * 1e6) / 1e6,
    annualized_geo: Math.round(annualized * 100) / 100,
    total_return_pct: Math.round(totalReturn * 100) / 100,
    trade_count: allTrades.length,
    total_opt_cost: Math.round(totalOptCost * 1e4) / 1e4,
    total_opt_revenue: Math.round(totalOptRevenue * 1e4) / 1e4,
  };
}

function leapsFitnessFn(individual, priceSeriesBySymbol, capitalMode, totalCapital, minEntryDate = null) {
  const mode = capitalMode || DEFAULTS.capitalMode;
  const cap = totalCapital || DEFAULTS.totalCapital;
  if (mode === 'unlimited') {
    const result = _leapsEvalUnlimited(individual, priceSeriesBySymbol, minEntryDate);
    return result.geo_product;
  }
  const result = _leapsEvalFixedCapital(individual, priceSeriesBySymbol, cap, minEntryDate);
  return result.final_equity / cap;
}

function leapsTotalRoi(individual, priceSeriesBySymbol, capitalMode, totalCapital, minEntryDate = null) {
  const mode = capitalMode || DEFAULTS.capitalMode;
  const cap = totalCapital || DEFAULTS.totalCapital;
  if (mode === 'unlimited') {
    return _leapsEvalUnlimited(individual, priceSeriesBySymbol, minEntryDate).total_return_pct;
  }
  return _leapsEvalFixedCapital(individual, priceSeriesBySymbol, cap, minEntryDate).total_return_pct;
}

// ── Evolution ─────────────────────────────────────────────────────────────

function tournamentSelect(population, fitnesses, tournamentSize) {
  const n = population.length;
  const k = Math.min(tournamentSize, n);
  let bestIdx = Math.floor(Math.random() * n);
  for (let i = 1; i < k; i++) {
    const idx = Math.floor(Math.random() * n);
    if (fitnesses[idx] > fitnesses[bestIdx]) bestIdx = idx;
  }
  return population[bestIdx];
}

const DEFAULTS = {
  capitalMode: 'fixed',
  totalCapital: 10000,
  positionPct: 20,
  cooldownDays: 5,
};

const DEFAULT_RANGES = {
  drawdown_threshold_pct: [10, 30],
  stage1_days: [10, 30], stage2_days: [30, 90],
  stage1_profit: [60, 120], stage2_profit: [40, 100],
  stage1_sell: [30, 70], stage2_sell: [30, 70],
  position_pct: [5, 50],
  cooldown_days: [1, 30],
};

function mergeRanges(custom) {
  // Deep clone defaults, then override with any provided custom keys
  if (!custom) return JSON.parse(JSON.stringify(DEFAULT_RANGES));
  const merged = JSON.parse(JSON.stringify(DEFAULT_RANGES));
  for (const key of Object.keys(custom)) {
    if (custom[key] !== undefined && custom[key] !== null) {
      merged[key] = custom[key];
    }
  }
  return merged;
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
  leapsTotalRoi,
  _leapsEvalFixedCapital,
  _leapsEvalUnlimited,
  tournamentsSelect: tournamentSelect,
  randomIndividual,
  ddOptions,
  DEFAULT_RANGES,
  DEFAULTS,
  mergeRanges,
  ENTRY_MODES,
};

// Export for Node.js testing
if (typeof module !== 'undefined' && module.exports) {
  module.exports = leapsGaEngine;
}
