let paused = false;
let cancelled = false;

self.onmessage = (event) => {
  const message = event.data || {};
  if (message.type === 'pause') paused = true;
  if (message.type === 'resume') paused = false;
  if (message.type === 'cancel') cancelled = true;
  if (message.type === 'start') {
    run(message.packet || {}, Number(message.worker_index || 0))
      .catch((error) => {
        if (error && error.message === '__cancelled__') {
          postMessage({ type: 'cancelled', worker_index: Number(message.worker_index || 0) });
        } else {
          postMessage({ type: 'error', worker_index: Number(message.worker_index || 0), message: error.message || String(error) });
        }
      });
  }
};

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function gate(workerIndex) {
  if (cancelled) throw new Error('__cancelled__');
  while (paused) {
    postMessage({ type: 'progress', worker_index: workerIndex, stage: 'paused', paused: true, message: 'Parameter Lab worker paused.' });
    await sleep(160);
    if (cancelled) throw new Error('__cancelled__');
  }
}

function num(value, fallback = 0) {
  const n = Number(value);
  return Number.isFinite(n) ? n : fallback;
}

function pct(value) {
  return value * 100;
}

function clamp(value, lo, hi) {
  return Math.min(hi, Math.max(lo, value));
}

function avg(values) {
  return values.length ? values.reduce((a, b) => a + Number(b || 0), 0) / values.length : 0;
}

function priceUsd(symbol, price, inputs) {
  return String(symbol).endsWith('.HK') ? price * num(inputs.hkd_to_usd, 0.128) : price;
}

function drawdownPct(point, inputs) {
  return Math.abs((inputs.drawdown_basis === 'rolling_120' ? point.drawdown_120 : point.drawdown_ath) * 100);
}

function buildTranches(inputs, strategy) {
  if (['weekly_dca', 'salary_flow_dca', 'core_dip_dca'].includes(strategy)) return [{ threshold_pct: 0, allocation_pct: 0 }];
  const maxDd = Math.max(0.0001, num(inputs.max_drawdown_pct, 50));
  const step = Math.max(0.0001, num(inputs.step_pct, 5));
  if (strategy === 'pyramid_3') {
    return [
      { threshold_pct: maxDd * 0.2, allocation_pct: 20 },
      { threshold_pct: maxDd * 0.5, allocation_pct: 30 },
      { threshold_pct: maxDd, allocation_pct: 50 }
    ];
  }
  const thresholds = [];
  const count = Math.floor(maxDd / step);
  for (let i = 1; i <= count; i += 1) thresholds.push(step * i);
  if (!thresholds.length || Math.abs(thresholds[thresholds.length - 1] - maxDd) > 1e-9) thresholds.push(maxDd);
  if (strategy === 'equal_slice') return thresholds.map((threshold) => ({ threshold_pct: threshold, allocation_pct: num(inputs.equal_slice_allocation_pct, 5) }));
  const weightSum = thresholds.reduce((sum, _value, index) => sum + index + 1, 0);
  return thresholds.map((threshold, index) => ({ threshold_pct: threshold, allocation_pct: (index + 1) / weightSum * 100 }));
}

function monthlyContributionDays(days) {
  const seen = new Set();
  const result = [];
  for (const day of days) {
    const ym = day.slice(0, 7);
    if (!seen.has(ym)) {
      seen.add(ym);
      result.push(day);
    }
  }
  return new Set(result.slice(1));
}

function weeklyDcaDays(points) {
  const result = new Set();
  const seen = new Set();
  for (const point of points) {
    const d = new Date(point.date + 'T00:00:00Z');
    const tmp = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth(), d.getUTCDate()));
    const day = tmp.getUTCDay() || 7;
    tmp.setUTCDate(tmp.getUTCDate() + 4 - day);
    const yearStart = new Date(Date.UTC(tmp.getUTCFullYear(), 0, 1));
    const week = Math.ceil((((tmp - yearStart) / 86400000) + 1) / 7);
    const key = tmp.getUTCFullYear() + '-' + week;
    if (!seen.has(key)) {
      seen.add(key);
      result.add(point.date);
    }
  }
  return result;
}

function maxDrawdown(values) {
  let peak = -Infinity;
  let maxDd = 0;
  for (const value of values) {
    peak = Math.max(peak, value);
    if (peak > 0) maxDd = Math.min(maxDd, value / peak - 1);
  }
  return pct(maxDd);
}

function avgCost(state) {
  const lots = state.lots.filter((lot) => lot.remaining_shares > 0);
  const shares = lots.reduce((sum, lot) => sum + lot.remaining_shares, 0);
  return shares > 0 ? lots.reduce((sum, lot) => sum + lot.remaining_shares * lot.buy_price_usd, 0) / shares : 0;
}

function avgBuyDrawdown(state) {
  const lots = state.lots.filter((lot) => lot.remaining_shares > 0);
  const shares = lots.reduce((sum, lot) => sum + lot.remaining_shares, 0);
  return shares > 0 ? lots.reduce((sum, lot) => sum + lot.remaining_shares * lot.buy_drawdown_pct, 0) / shares : 0;
}

function sellableShares(state, requested, inputs) {
  const reserve = state.max_shares * num(inputs.reserve_position_pct, 25) / 100;
  return Math.min(Math.max(0, requested), Math.max(0, state.shares - reserve));
}

function reduceLotsFifo(state, shares) {
  let remaining = shares;
  for (const lot of state.lots) {
    if (remaining <= 0) break;
    const sold = Math.min(lot.remaining_shares, remaining);
    lot.remaining_shares -= sold;
    remaining -= sold;
  }
}

function recordBuy(state, point, inputs, tradeLog, buyStrategy, sellStrategy, grossAmount, drawdown, extra = {}) {
  if (grossAmount <= 0) return false;
  const fee = Math.min(num(inputs.trade_fee, 0.35), grossAmount);
  const net = grossAmount - fee;
  const px = priceUsd(state.symbol, point.close, inputs);
  const shares = net > 0 && px > 0 ? net / px : 0;
  state.cash -= grossAmount;
  state.shares += shares;
  state.invested += grossAmount;
  state.fees += fee;
  state.trades += 1;
  state.buy_trades += 1;
  state.max_shares = Math.max(state.max_shares, state.shares);
  state.last_value = state.shares * px;
  state.lots.push({
    threshold_pct: num(extra.threshold_pct),
    buy_drawdown_pct: drawdown,
    buy_price_usd: px,
    initial_shares: shares,
    remaining_shares: shares,
    first_grid_sell_done: false,
    second_grid_sell_done: false,
    repair_sell_marks: {}
  });
  const rearmed = rearmAfterDcaBuy(state, drawdown, inputs, sellStrategy);
  tradeLog.push({
    action: 'buy',
    date: point.date,
    symbol: state.symbol,
    buy_strategy: buyStrategy,
    sell_strategy: sellStrategy,
    drawdown_pct: drawdown,
    gross_amount: grossAmount,
    fee,
    net_amount: net,
    shares,
    sell_cycle_rearmed: rearmed,
    ...extra
  });
  return true;
}

function rearmAfterDcaBuy(state, drawdown, inputs, sellStrategy) {
  if (!['repair_step', 'grid_rebound', 'cost_deleverage'].includes(sellStrategy)) return false;
  if (!Object.keys(state.sell_marks).length) return false;
  if (drawdown + 1e-9 < Math.min(Math.max(0, num(inputs.dca_rearm_drawdown_pct)), num(inputs.max_drawdown_pct))) return false;
  state.sell_marks = {};
  return true;
}

function salaryMultiplier(drawdown) {
  return drawdown >= 30 ? 4 : drawdown >= 20 ? 3 : drawdown >= 10 ? 2 : drawdown >= 5 ? 1.4 : 1;
}

function salaryReserve(drawdown) {
  return drawdown >= 30 ? 0 : drawdown >= 20 ? 0.03 : drawdown >= 10 ? 0.05 : 0.08;
}

function salarySweep(drawdown) {
  return drawdown >= 30 ? 0.9 : drawdown >= 20 ? 0.7 : drawdown >= 10 ? 0.5 : drawdown >= 5 ? 0.35 : 0.2;
}

function coreBoost(drawdown, inputs) {
  const start = Math.max(0, num(inputs.core_dip_start_drawdown_pct));
  const full = Math.max(start, num(inputs.core_dip_full_drawdown_pct));
  if (drawdown <= start) return 0;
  if (drawdown >= full || full <= start) return 1;
  return (drawdown - start) / (full - start);
}

function coreTimingAllowsBuy(point, recentPoints, drawdown, pendingDays, isInitial, inputs) {
  if (!inputs.core_dip_timing_enabled) return { allowed: true, reason: 'disabled' };
  if (isInitial) return { allowed: true, reason: 'initial_core' };
  if (drawdown >= num(inputs.core_dip_start_drawdown_pct)) return { allowed: true, reason: 'drawdown_reached' };
  const maxDelay = Math.max(0, Math.floor(num(inputs.core_dip_timing_max_delay_days)));
  if (maxDelay <= 0 || pendingDays >= maxDelay) return { allowed: true, reason: 'delay_expired' };
  const closes = (recentPoints || []).map((item) => num(item.close)).filter((value) => value > 0);
  if (closes.length < 2) return { allowed: true, reason: 'insufficient_history' };
  const previous = closes[closes.length - 2];
  const dayChange = previous > 0 ? (num(point.close) / previous - 1) * 100 : 0;
  const recentLow = Math.min(...closes);
  const distanceFromLow = recentLow > 0 ? (num(point.close) / recentLow - 1) * 100 : 0;
  if (dayChange <= 0) return { allowed: true, reason: 'down_day' };
  if (distanceFromLow <= num(inputs.core_dip_timing_near_low_pct)) return { allowed: true, reason: 'near_recent_low' };
  if (dayChange >= num(inputs.core_dip_timing_rise_threshold_pct)) return { allowed: false, reason: 'defer_after_rise' };
  return { allowed: true, reason: 'normal' };
}

function executeDca(state, point, dcaDays, inputs, tradeLog, strategy, sellStrategy) {
  const drawdown = drawdownPct(point, inputs);
  if (strategy === 'weekly_dca') {
    const gross = Math.min(state.dca_pending_cash, state.cash);
    if (gross <= 0) return false;
    state.dca_pending_cash = Math.max(0, state.dca_pending_cash - gross);
    return recordBuy(state, point, inputs, tradeLog, strategy, sellStrategy, gross, drawdown);
  }
  const isDcaDay = dcaDays.has(point.date);
  if (!isDcaDay && !(strategy === 'core_dip_dca' && state.core_dip_pending_cash > 0)) return false;
  const monthlyAmount = num(inputs.monthly_contribution) * state.weight / 100;
  if (strategy === 'salary_flow_dca') {
    if (!isDcaDay || num(inputs.monthly_contribution) <= 0) return false;
    const base = monthlyAmount / 4 * salaryMultiplier(drawdown);
    const reserve = state.budget * salaryReserve(drawdown);
    const available = Math.max(0, state.cash - reserve);
    const extra = Math.max(0, available - base);
    return recordBuy(state, point, inputs, tradeLog, strategy, sellStrategy, Math.min(base + extra * salarySweep(drawdown), available), drawdown);
  }
  const scheduled = monthlyAmount / 4;
  const boost = coreBoost(drawdown, inputs);
  const coreRatio = clamp(num(inputs.core_dip_weekly_core_pct) / 100, 0, 1);
  const newCore = isDcaDay && num(inputs.monthly_contribution) > 0 ? scheduled * coreRatio : 0;
  const dip = isDcaDay && num(inputs.monthly_contribution) > 0 ? scheduled * Math.max(0, 1 - coreRatio) * boost : 0;
  let timing = { allowed: true, reason: 'disabled' };
  let core = newCore;
  if (inputs.core_dip_timing_enabled) {
    state.core_dip_pending_cash += newCore;
    if (state.core_dip_pending_cash > 0) state.core_dip_pending_days = state.core_dip_pending_days ? state.core_dip_pending_days + 1 : 1;
    timing = coreTimingAllowsBuy(point, state.recent_points || [point], drawdown, state.core_dip_pending_days || 0, !state.buy_trades, inputs);
    core = timing.allowed ? state.core_dip_pending_cash : 0;
  }
  const reserve = state.budget * Math.max(0.01, clamp(num(inputs.core_dip_cash_reserve_pct) / 100, 0, 1) * (1 - boost * 0.85));
  const available = Math.max(0, state.cash - reserve);
  let extra = Math.max(0, available - core - dip);
  let initialCore = 0;
  if (!state.buy_trades) {
    initialCore = Math.min(extra, num(inputs.initial_cash) * state.weight / 100 * clamp(num(inputs.core_dip_initial_core_pct) / 100, 0, 1));
    extra = Math.max(0, extra - initialCore);
  }
  const sweep = (timing.allowed || boost > 0 || initialCore > 0) ? extra * (boost <= 0 ? 0.12 : Math.min(0.9, 0.25 + boost * 0.65)) : 0;
  const gross = Math.min(core + dip + initialCore + sweep, available);
  const bought = recordBuy(state, point, inputs, tradeLog, strategy, sellStrategy, gross, drawdown, {
    core_amount: core,
    new_core_amount: newCore,
    pending_core_amount: 0,
    timing_reason: timing.reason,
    dip_amount: dip,
    initial_core_amount: initialCore
  });
  if (bought && inputs.core_dip_timing_enabled && core > 0) {
    state.core_dip_pending_cash = Math.max(0, state.core_dip_pending_cash - Math.min(core, gross));
    if (state.core_dip_pending_cash <= 1e-9) {
      state.core_dip_pending_cash = 0;
      state.core_dip_pending_days = 0;
    }
    const last = tradeLog[tradeLog.length - 1];
    if (last) last.pending_core_amount = state.core_dip_pending_cash;
  }
  return bought;
}

function executeTranches(state, point, tranches, executed, inputs, tradeLog, buyStrategy, sellStrategy) {
  const drawdown = drawdownPct(point, inputs);
  let bought = false;
  for (const tranche of tranches) {
    const key = String(Math.round(tranche.threshold_pct * 1e8) / 1e8);
    if (drawdown + 1e-9 < tranche.threshold_pct) continue;
    const target = state.budget * tranche.allocation_pct / 100;
    const already = executed[key] || 0;
    if (buyStrategy === 'pyramid_3' && already > 0) continue;
    const gross = buyStrategy === 'pyramid_3'
      ? Math.min(target, state.cash)
      : Math.min(Math.max(0, target - already), state.cash);
    if (gross <= 0) {
      executed[key] = buyStrategy === 'pyramid_3' ? 1 : Math.max(already, target);
      continue;
    }
    if (Object.keys(state.sell_marks).length) state.sell_marks = {};
    bought = recordBuy(state, point, inputs, tradeLog, buyStrategy, sellStrategy, gross, drawdown, {
      threshold_pct: tranche.threshold_pct,
      allocation_pct: tranche.allocation_pct
    }) || bought;
    executed[key] = buyStrategy === 'pyramid_3' ? 1 : already + gross;
  }
  return bought;
}

function recordSell(state, point, shares, inputs, tradeLog, sellStrategy, trigger, costBasis = null) {
  const px = priceUsd(state.symbol, point.close, inputs);
  const gross = shares * px;
  if (gross <= 0) return false;
  const basis = costBasis ?? avgCost(state) * shares;
  const fee = Math.min(num(inputs.trade_fee, 0.35), gross);
  state.cash += gross - fee;
  state.shares -= shares;
  if (state.shares < 1e-10) state.shares = 0;
  state.fees += fee;
  state.trades += 1;
  state.sell_trades += 1;
  state.sold_gross += gross;
  state.last_value = state.shares * px;
  tradeLog.push({
    action: 'sell',
    date: point.date,
    symbol: state.symbol,
    sell_strategy: sellStrategy,
    trigger_value: trigger,
    drawdown_pct: drawdownPct(point, inputs),
    gross_amount: gross,
    fee,
    net_amount: gross - fee,
    shares,
    estimated_profit: basis > 0 ? gross - basis : 0,
    estimated_profit_pct: basis > 0 ? pct(gross / basis - 1) : 0
  });
  return true;
}

function markBuyRearmAfterPositionSell(state, point, inputs) {
  const drawdown = drawdownPct(point, inputs);
  const rearm = Math.min(Math.max(0, num(inputs.dca_rearm_drawdown_pct)), num(inputs.max_drawdown_pct));
  state.buy_rearm_drawdown_pct = Math.min(num(inputs.max_drawdown_pct), drawdown + rearm);
}

function sellShares(state, point, requested, inputs, tradeLog, sellStrategy, trigger, minGross = 0) {
  const shares = sellableShares(state, requested, inputs);
  if (shares <= 0 || shares * priceUsd(state.symbol, point.close, inputs) + 1e-9 < minGross) return false;
  const basis = avgCost(state) * shares;
  reduceLotsFifo(state, shares);
  const sold = recordSell(state, point, shares, inputs, tradeLog, sellStrategy, trigger, basis);
  if (sold) markBuyRearmAfterPositionSell(state, point, inputs);
  return sold;
}

function executeSells(state, point, inputs, buyStrategy, sellStrategy, tradeLog, tradeIndex) {
  if (sellStrategy === 'none' || state.shares <= 0) return;
  const current = priceUsd(state.symbol, point.close, inputs);
  if (sellStrategy === 'repair_step') {
    if (num(inputs.repair_sell_cooldown_days) > 0 && state.last_repair_sell_trade_index !== null && tradeIndex - state.last_repair_sell_trade_index < num(inputs.repair_sell_cooldown_days)) return;
    const cost = avgCost(state);
    if (cost <= 0 || current < cost * (1 + num(inputs.sell_min_profit_pct) / 100)) return;
    const drawdown = drawdownPct(point, inputs);
    const avgBuy = avgBuyDrawdown(state);
    for (const [mark, threshold] of [['repair_50', avgBuy * 0.5], ['repair_20', avgBuy * 0.2], ['repair_ath', 0.5]]) {
      if (state.sell_marks[mark] || drawdown > threshold + 1e-9) continue;
      if (sellShares(state, point, state.shares * num(inputs.repair_stage_sell_pct) / 100, inputs, tradeLog, sellStrategy, threshold)) {
        state.sell_marks[mark] = true;
        state.last_repair_sell_trade_index = tradeIndex;
        return;
      }
    }
  } else if (sellStrategy === 'grid_rebound') {
    const cost = avgCost(state);
    if (cost <= 0 || current < cost * (1 + num(inputs.sell_min_profit_pct) / 100)) return;
    const drawdown = drawdownPct(point, inputs);
    const avgBuy = avgBuyDrawdown(state);
    for (const [mark, threshold, sellPct] of [
      ['grid_1', Math.max(0, avgBuy - num(inputs.grid_rebound_step_pct)), num(inputs.grid_first_sell_pct)],
      ['grid_2', Math.max(0, avgBuy - num(inputs.grid_rebound_step_pct) * 2), num(inputs.grid_second_sell_pct)]
    ]) {
      if (state.sell_marks[mark] || drawdown > threshold + 1e-9) continue;
      if (sellShares(state, point, state.shares * sellPct / 100, inputs, tradeLog, sellStrategy, threshold, num(inputs.grid_min_sell_amount))) {
        state.sell_marks[mark] = true;
        return;
      }
    }
  } else if (sellStrategy === 'cost_deleverage') {
    if (num(inputs.cost_deleverage_cooldown_days) > 0 && state.last_cost_deleverage_sell_trade_index !== null && tradeIndex - state.last_cost_deleverage_sell_trade_index < num(inputs.cost_deleverage_cooldown_days)) return;
    const cost = avgCost(state);
    if (cost <= 0) return;
    const profit = current / cost * 100 - 100;
    for (const [mark, threshold, sellPct] of [
      ['cost_1', num(inputs.cost_first_profit_pct), num(inputs.cost_first_sell_pct)],
      ['cost_2', num(inputs.cost_second_profit_pct), num(inputs.cost_second_sell_pct)],
      ['cost_3', num(inputs.cost_third_profit_pct), num(inputs.cost_third_sell_pct)]
    ]) {
      if (state.sell_marks[mark] || profit < Math.max(threshold, num(inputs.sell_min_profit_pct))) continue;
      if (sellShares(state, point, state.shares * sellPct / 100, inputs, tradeLog, sellStrategy, threshold, num(inputs.cost_min_sell_amount))) {
        state.sell_marks[mark] = true;
        state.last_cost_deleverage_sell_trade_index = tradeIndex;
        return;
      }
    }
  }
}

function candidateInputs(base, candidate) {
  return {
    ...base,
    step_pct: candidate.step_pct ?? base.step_pct,
    equal_slice_allocation_pct: candidate.equal_slice_allocation_pct ?? base.equal_slice_allocation_pct,
    core_dip_initial_core_pct: candidate.core_dip_initial_core_pct ?? base.core_dip_initial_core_pct,
    core_dip_weekly_core_pct: candidate.core_dip_weekly_core_pct ?? base.core_dip_weekly_core_pct,
    core_dip_cash_reserve_pct: candidate.core_dip_cash_reserve_pct ?? base.core_dip_cash_reserve_pct,
    core_dip_start_drawdown_pct: candidate.core_dip_start_drawdown_pct ?? base.core_dip_start_drawdown_pct,
    core_dip_full_drawdown_pct: candidate.core_dip_full_drawdown_pct ?? base.core_dip_full_drawdown_pct,
    core_dip_timing_enabled: candidate.core_dip_timing_enabled ?? base.core_dip_timing_enabled,
    core_dip_timing_max_delay_days: candidate.core_dip_timing_max_delay_days ?? base.core_dip_timing_max_delay_days,
    core_dip_timing_rise_threshold_pct: candidate.core_dip_timing_rise_threshold_pct ?? base.core_dip_timing_rise_threshold_pct,
    core_dip_timing_near_low_pct: candidate.core_dip_timing_near_low_pct ?? base.core_dip_timing_near_low_pct,
    sell_min_profit_pct: candidate.sell_min_profit_pct ?? base.sell_min_profit_pct,
    repair_sell_cooldown_days: candidate.repair_sell_cooldown_days ?? base.repair_sell_cooldown_days,
    repair_stage_sell_pct: candidate.repair_stage_sell_pct ?? base.repair_stage_sell_pct,
    dca_rearm_drawdown_pct: candidate.dca_rearm_drawdown_pct ?? base.dca_rearm_drawdown_pct,
    grid_rebound_step_pct: candidate.grid_rebound_step_pct ?? base.grid_rebound_step_pct,
    grid_first_sell_pct: candidate.grid_first_sell_pct ?? base.grid_first_sell_pct,
    grid_second_sell_pct: candidate.grid_second_sell_pct ?? base.grid_second_sell_pct,
    grid_min_sell_amount: candidate.grid_min_sell_amount ?? base.grid_min_sell_amount,
    cost_first_profit_pct: candidate.cost_first_profit_pct ?? base.cost_first_profit_pct,
    cost_second_profit_pct: candidate.cost_second_profit_pct ?? base.cost_second_profit_pct,
    cost_third_profit_pct: candidate.cost_third_profit_pct ?? base.cost_third_profit_pct,
    cost_first_sell_pct: candidate.cost_first_sell_pct ?? base.cost_first_sell_pct,
    cost_second_sell_pct: candidate.cost_second_sell_pct ?? base.cost_second_sell_pct,
    cost_third_sell_pct: candidate.cost_third_sell_pct ?? base.cost_third_sell_pct,
    cost_deleverage_cooldown_days: candidate.cost_deleverage_cooldown_days ?? base.cost_deleverage_cooldown_days,
    cost_min_sell_amount: candidate.cost_min_sell_amount ?? base.cost_min_sell_amount
  };
}

function targetMaxDrawdown(targets, symbol, inputs) {
  const target = targets.find((item) => item.symbol === symbol);
  return target && target.max_drawdown_pct != null ? num(target.max_drawdown_pct) : num(inputs.max_drawdown_pct);
}

function sellMetrics(tradeLog, portfolioValues, cashValues) {
  const buys = tradeLog.filter((trade) => trade.action === 'buy');
  const sells = tradeLog.filter((trade) => trade.action === 'sell');
  const avgBuy = avg(buys.map((trade) => trade.drawdown_pct));
  const avgSell = avg(sells.map((trade) => trade.drawdown_pct));
  const avgProfit = avg(sells.map((trade) => trade.estimated_profit_pct));
  const totalSellCash = sells.reduce((sum, trade) => sum + num(trade.net_amount), 0);
  let sellPool = 0;
  let reused = 0;
  for (const trade of tradeLog) {
    if (trade.action === 'sell') sellPool += num(trade.net_amount);
    if (trade.action === 'buy' && sellPool > 0) {
      const amount = Math.min(sellPool, num(trade.gross_amount));
      reused += amount;
      sellPool -= amount;
    }
  }
  const cashReuse = totalSellCash > 0 ? pct(reused / totalSellCash) : 0;
  const avgCash = avg(cashValues.map((cash, index) => portfolioValues[index] > 0 ? pct(cash / portfolioValues[index]) : 0));
  const sellQuality = sells.length
    ? (
      clamp(avgProfit / 35, 0, 1) * 0.35
      + clamp((30 - avgSell) / 30, 0, 1) * 0.30
      + clamp(cashReuse / 100, 0, 1) * 0.20
      + clamp((65 - avgCash) / 65, 0, 1) * 0.15
    ) * 100
    : 0;
  return {
    avg_buy_drawdown_pct: avgBuy,
    avg_sell_drawdown_pct: avgSell,
    avg_sell_profit_pct: avgProfit,
    cash_reuse_pct: cashReuse,
    avg_cash_pct: avgCash,
    sell_quality_score: sellQuality
  };
}

function simulate(task, baseInputs, candidate) {
  const inputs = candidateInputs(baseInputs, candidate);
  const strategy = candidate.buy_strategy;
  const sellStrategy = candidate.sell_strategy;
  const pointByDay = {};
  const allDaySet = new Set();
  const dcaDays = {};
  const tradingIndex = {};
  for (const [symbol, points] of Object.entries(task.price_points)) {
    pointByDay[symbol] = {};
    tradingIndex[symbol] = {};
    points.forEach((point, index) => {
      pointByDay[symbol][point.date] = point;
      tradingIndex[symbol][point.date] = index;
      allDaySet.add(point.date);
    });
    if (['salary_flow_dca', 'core_dip_dca'].includes(strategy)) dcaDays[symbol] = weeklyDcaDays(points);
  }
  const allDays = [...allDaySet].sort();
  const contribDays = monthlyContributionDays(allDays);
  const states = {};
  for (const target of task.targets) {
    const budget = num(inputs.initial_cash) * num(target.weight) / 100;
    states[target.symbol] = {
      symbol: target.symbol,
      weight: num(target.weight),
      budget,
      cash: budget,
      shares: 0,
      invested: 0,
      fees: 0,
      trades: 0,
      buy_trades: 0,
      sell_trades: 0,
      sold_gross: 0,
      max_shares: 0,
      last_value: 0,
      lots: [],
      sell_marks: {},
      last_repair_sell_trade_index: null,
      last_cost_deleverage_sell_trade_index: null,
      dca_pending_cash: strategy === 'weekly_dca' ? budget : 0,
      core_dip_pending_cash: 0,
      core_dip_pending_days: 0,
      recent_points: [],
      buy_rearm_drawdown_pct: null
    };
  }
  const executed = Object.fromEntries(task.targets.map((target) => [target.symbol, {}]));
  let contributionCount = 0;
  let totalMonthlyContributions = 0;
  const portfolioValues = [];
  const cashValues = [];
  const tradeLog = [];
  for (const day of allDays) {
    if (num(inputs.monthly_contribution) > 0 && contribDays.has(day)) {
      contributionCount += 1;
      for (const target of task.targets) {
        const contribution = num(inputs.monthly_contribution) * num(target.weight) / 100;
        const state = states[target.symbol];
        state.cash += contribution;
        state.budget += contribution;
        if (strategy === 'weekly_dca') state.dca_pending_cash += contribution;
        totalMonthlyContributions += contribution;
      }
    }
    for (const [symbol, dayPoints] of Object.entries(pointByDay)) {
      const point = dayPoints[day];
      if (!point) continue;
      const state = states[symbol];
      state.recent_points = (state.recent_points || []).concat([point]).slice(-5);
      state.last_value = state.shares * priceUsd(symbol, point.close, inputs);
      const tradeIndex = tradingIndex[symbol][day];
      let bought = false;
      if (['weekly_dca', 'salary_flow_dca', 'core_dip_dca'].includes(strategy)) {
        bought = executeDca(state, point, dcaDays[symbol] || new Set(), inputs, tradeLog, strategy, sellStrategy);
      } else {
        if (Object.keys(executed[symbol]).length && drawdownPct(point, inputs) <= 0.5) executed[symbol] = {};
        if (
          Object.keys(executed[symbol]).length
          && state.buy_rearm_drawdown_pct !== null
          && state.buy_rearm_drawdown_pct !== undefined
          && drawdownPct(point, inputs) + 1e-9 >= state.buy_rearm_drawdown_pct
        ) {
          executed[symbol] = {};
          state.buy_rearm_drawdown_pct = null;
        }
        const tranches = buildTranches({ ...inputs, max_drawdown_pct: targetMaxDrawdown(task.targets, symbol, inputs) }, strategy);
        bought = executeTranches(state, point, tranches, executed[symbol], inputs, tradeLog, strategy, sellStrategy);
      }
      if (!bought) executeSells(state, point, inputs, strategy, sellStrategy, tradeLog, tradeIndex);
    }
    portfolioValues.push(Object.values(states).reduce((sum, state) => sum + state.cash + state.last_value, 0));
    cashValues.push(Object.values(states).reduce((sum, state) => sum + state.cash, 0));
  }
  const finalValue = portfolioValues[portfolioValues.length - 1] || 0;
  const totalContributed = num(inputs.initial_cash) + totalMonthlyContributions;
  const metrics = sellMetrics(tradeLog, portfolioValues, cashValues);
  return {
    return_pct: totalContributed > 0 ? pct(finalValue / totalContributed - 1) : 0,
    max_drawdown_pct: maxDrawdown(portfolioValues),
    trade_count: Object.values(states).reduce((sum, state) => sum + state.trades, 0),
    contribution_count: contributionCount,
    ...metrics
  };
}

async function run(packet, workerIndex) {
  const candidates = packet.candidate_pool || [];
  const tasks = packet.tasks || [];
  const inputs = packet.inputs || {};
  const started = performance.now();
  const rows = [];
  const total = Math.max(1, candidates.length * tasks.length);
  let completed = 0;
  for (const candidate of candidates) {
    await gate(workerIndex);
    const observations = [];
    for (const task of tasks) {
      await gate(workerIndex);
      const metrics = simulate(task, inputs, candidate);
      observations.push({
        candidate_key: candidate.key,
        combination_key: candidate.combination_key || candidate.key,
        topic_key: task.key,
        task_key: task.key,
        portfolio_key: task.portfolio_key,
        portfolio_label: task.portfolio_label,
        period_key: task.period_key,
        period_label: task.period_label,
        start: task.start,
        end: task.end,
        ...metrics
      });
      completed += 1;
      if (completed % 10 === 0 || completed === total) {
        postMessage({
          type: 'progress',
          worker_index: workerIndex,
          stage: 'simulate',
          completed_simulations: completed,
          total_simulations: total,
          message: `${completed} / ${total}`
        });
      }
    }
    rows.push({ candidate, observations });
  }
  postMessage({
    type: 'done',
    worker_index: workerIndex,
    rows,
    completed_simulations: completed,
    chunk_size: candidates.length,
    elapsed_ms: performance.now() - started
  });
}
