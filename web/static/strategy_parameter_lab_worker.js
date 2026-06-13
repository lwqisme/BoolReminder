if (typeof importScripts !== 'undefined') {
  var engineV = '';
  try {
    engineV = new URL(self.location.href).searchParams.get('engine_v') || '';
  } catch(e) {}
  importScripts('/static/leaps_ga_engine.js?v=' + (engineV || '1'));
}

let paused = false;
let cancelled = false;
let activeRunId = '';
let workerState = null;

function diagnosticLog(event, fields = {}) {
  console.info('[parameter-lab-worker]', { event, ...fields });
}

function diagnosticError(event, fields = {}) {
  console.error('[parameter-lab-worker]', { event, ...fields });
}

self.onmessage = (event) => {
  const message = event.data || {};
  const messageRunId = message.run_id || (message.packet || {}).run_id || activeRunId || '';

  if (message.type === 'leaps_ga') {
    handleLeapsGa(message).catch((error) => {
      postMessage({ type: 'leaps_ga_error', run_id: messageRunId, message: error.message, stack: error.stack });
    });
    return;
  }

  if (message.type === 'pause') {
    paused = true;
    diagnosticLog('pause', { run_id: messageRunId, worker_index: Number(message.worker_index || 0) });
  }
  if (message.type === 'resume') {
    paused = false;
    diagnosticLog('resume', { run_id: messageRunId, worker_index: Number(message.worker_index || 0) });
  }
  if (message.type === 'cancel') {
    cancelled = true;
    diagnosticLog('cancel', { run_id: messageRunId, worker_index: Number(message.worker_index || 0) });
  }
  if (message.type === 'start') {
    paused = false;
    cancelled = false;
    const startRunId = messageRunId;
    activeRunId = startRunId;
    initRun(message.packet || {}, Number(message.worker_index || 0), startRunId, Number(message.total_simulations || 0))
      .catch((error) => {
        postRunError(error, startRunId, Number(message.worker_index || 0));
      });
  }
  if (message.type === 'batch') {
    const batchRunId = messageRunId;
    processBatch(message, Number(message.worker_index || 0), batchRunId)
      .catch((error) => {
        postRunError(error, batchRunId, Number(message.worker_index || 0));
      });
  }
  if (message.type === 'finish') {
    const finishRunId = messageRunId;
    finishRun(Number(message.worker_index || 0), finishRunId)
      .catch((error) => {
        postRunError(error, finishRunId, Number(message.worker_index || 0));
      });
  }
};

function postRunError(error, runId, workerIndex) {
  if (error && error.message === '__cancelled__') {
    postMessage({ type: 'cancelled', run_id: runId, worker_index: workerIndex });
    return;
  }
  const context = error && error.__parameter_lab_context ? error.__parameter_lab_context : {};
  postMessage({
    type: 'error',
    run_id: runId,
    worker_index: workerIndex,
    stage: context.stage || 'run_error',
    candidate_index: context.candidate_index,
    candidate_key: context.candidate_key,
    task_index: context.task_index,
    task_key: context.task_key,
    batch_id: context.batch_id,
    elapsed_ms: context.elapsed_ms,
    completed_simulations: context.completed_simulations,
    total_simulations: context.total_simulations,
    message: error && error.message ? error.message : String(error),
    stack: error && error.stack ? String(error.stack) : '',
    context
  });
}

const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

async function gate(workerIndex, runId) {
  if (cancelled) throw new Error('__cancelled__');
  while (paused) {
    postMessage({
      type: 'progress',
      run_id: runId,
      worker_index: workerIndex,
      stage: 'paused',
      paused: true,
      message: 'Parameter Lab worker paused.'
    });
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

function safeRatio(numerator, denominator) {
  const den = Number(denominator || 0);
  return den > 0 ? Number(numerator || 0) / den : 0;
}

function avg(values) {
  return values.length ? values.reduce((a, b) => a + Number(b || 0), 0) / values.length : 0;
}

const BUY_PARAMETER_FIELDS = [
  'step_pct',
  'equal_slice_allocation_pct',
  'core_dip_initial_core_pct',
  'core_dip_weekly_core_pct',
  'core_dip_cash_reserve_pct',
  'core_dip_start_drawdown_pct',
  'core_dip_full_drawdown_pct',
  'core_dip_timing_enabled',
  'core_dip_timing_max_delay_days',
  'core_dip_timing_rise_threshold_pct',
  'core_dip_timing_near_low_pct',
  'max_drawdown_pct'
];

const SELL_PARAMETER_FIELDS = [
  'sell_min_profit_pct',
  'repair_sell_cooldown_days',
  'repair_stage_sell_pct',
  'grid_rebound_step_pct',
  'grid_sell_pct',
  'grid_first_sell_pct',
  'grid_second_sell_pct',
  'grid_min_sell_amount',
  'grid_rebound_cycle_reset',
  'cost_first_profit_pct',
  'cost_second_profit_pct',
  'cost_third_profit_pct',
  'cost_first_sell_pct',
  'cost_second_sell_pct',
  'cost_third_sell_pct',
  'cost_deleverage_cooldown_days',
  'sell_allow_same_day_sell',
  'cost_min_sell_amount',
  'dca_rearm_drawdown_pct',
  'buy_rearm_mode',
  'sell_stage_rearm_drawdown_pct'
];

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

function rebuildPricePoints(dates, closes, start, end, warmupDays) {
  const startKey = String(start || '');
  const endKey = String(end || '');
  const warmup = Math.max(0, Number(warmupDays) || 0);

  // Compute warmup start date for accurate ATH tracking.
  let warmupStartKey = startKey;
  if (warmup > 0 && startKey) {
    const d = new Date(startKey + 'T00:00:00');
    if (!isNaN(d.getTime())) {
      d.setDate(d.getDate() - warmup);
      warmupStartKey = d.toISOString().slice(0, 10);
    }
  }

  // First pass: collect all points (warmup + window) for ATH calculation.
  const allRows = [];
  for (let index = 0; index < dates.length; index += 1) {
    const day = String(dates[index] || '');
    if (!day) continue;
    if (warmupStartKey && day < warmupStartKey) continue;
    if (endKey && day > endKey) continue;
    const close = num(closes[index]);
    if (close <= 0) continue;
    allRows.push({ date: day, close });
  }

  // Compute ATH and 120-day drawdown across all points (including warmup).
  let rollingPeak = -Infinity;
  for (let index = 0; index < allRows.length; index += 1) {
    const point = allRows[index];
    rollingPeak = Math.max(rollingPeak, point.close);
    let windowPeak = -Infinity;
    const windowStart = Math.max(0, index - 119);
    for (let windowIndex = windowStart; windowIndex <= index; windowIndex += 1) {
      windowPeak = Math.max(windowPeak, allRows[windowIndex].close);
    }
    point.rolling_peak = rollingPeak;
    point.drawdown_ath = rollingPeak > 0 ? point.close / rollingPeak - 1 : 0;
    point.rolling_120_peak = windowPeak;
    point.drawdown_120 = windowPeak > 0 ? point.close / windowPeak - 1 : 0;
  }

  // Return only window points (>= startKey).
  return allRows.filter(function (r) { return r.date >= startKey; });
}

function inflateTask(task, marketData) {
  if (task.price_points && typeof task.price_points === 'object') return task;
  const symbols = marketData && marketData.symbols ? marketData.symbols : {};
  const taskSymbols = Array.isArray(task.symbols) && task.symbols.length
    ? task.symbols
    : (task.targets || []).map((target) => target.symbol).filter(Boolean);
  const pricePoints = {};
  for (const symbol of taskSymbols) {
    const series = symbols[symbol] || {};
    const dates = Array.isArray(series.dates) ? series.dates : [];
    const closes = Array.isArray(series.closes) ? series.closes : [];
    pricePoints[symbol] = rebuildPricePoints(dates, closes, task.start, task.end, 365);
  }
  return { ...task, price_points: pricePoints };
}

function buildTaskContext(task) {
  const pointByDay = {};
  const allDaySet = new Set();
  const dcaDays = {};
  const tradingIndex = {};
  for (const [symbol, points] of Object.entries(task.price_points || {})) {
    pointByDay[symbol] = {};
    tradingIndex[symbol] = {};
    points.forEach((point, index) => {
      pointByDay[symbol][point.date] = point;
      tradingIndex[symbol][point.date] = index;
      allDaySet.add(point.date);
    });
    dcaDays[symbol] = weeklyDcaDays(points);
  }
  const allDays = [...allDaySet].sort();
  return {
    ...task,
    allDays,
    contribDays: monthlyContributionDays(allDays),
    pointByDay,
    dcaDays,
    tradingIndex
  };
}

function buildTaskContexts(packet) {
  const marketData = packet.market_data || {};
  return (packet.tasks || [])
    .map((task) => inflateTask(task, marketData))
    .map((task) => buildTaskContext(task));
}

function schemaIndexMap(schema) {
  const map = {};
  (Array.isArray(schema) ? schema : []).forEach((field, index) => {
    map[String(field)] = index;
  });
  return map;
}

function inflateVariant(row, schema, fields) {
  const indexMap = schemaIndexMap(schema);
  const variant = {};
  fields.forEach((field) => {
    const index = indexMap[field];
    if (index === undefined) return;
    variant[field] = row[index];
  });
  return variant;
}

function formatCompact(value) {
  const n = Number(value || 0);
  if (!Number.isFinite(n)) return '0';
  return Number.isInteger(n) ? String(n) : String(Number(n.toPrecision(6))).replace(/\.0+$/, '');
}

function buildCandidateKey(buyStrategy, sellStrategy, buyParams, sellParams) {
  const parts = [buyStrategy];
  if (buyParams.step_pct !== null && buyParams.step_pct !== undefined) parts.push(`step${formatCompact(buyParams.step_pct)}`);
  if (buyParams.equal_slice_allocation_pct !== null && buyParams.equal_slice_allocation_pct !== undefined) parts.push(`alloc${formatCompact(buyParams.equal_slice_allocation_pct)}`);
  if (buyParams.core_dip_initial_core_pct !== null && buyParams.core_dip_initial_core_pct !== undefined) {
    parts.push(`ci${formatCompact(buyParams.core_dip_initial_core_pct)}`);
    parts.push(`cw${formatCompact(buyParams.core_dip_weekly_core_pct)}`);
    parts.push(`cr${formatCompact(buyParams.core_dip_cash_reserve_pct)}`);
    parts.push(`csd${formatCompact(buyParams.core_dip_start_drawdown_pct)}`);
    parts.push(`cfd${formatCompact(buyParams.core_dip_full_drawdown_pct)}`);
    if (buyParams.core_dip_timing_enabled) {
      parts.push(`ctd${formatCompact(Math.trunc(Number(buyParams.core_dip_timing_max_delay_days || 0)))}`);
      parts.push(`ctr${formatCompact(buyParams.core_dip_timing_rise_threshold_pct)}`);
      parts.push(`ctl${formatCompact(buyParams.core_dip_timing_near_low_pct)}`);
    }
  }
  parts.push(sellStrategy);
  if (sellStrategy === 'repair_step') {
    parts.push(`p${formatCompact(sellParams.sell_min_profit_pct)}`);
    parts.push(`c${formatCompact(Math.trunc(Number(sellParams.repair_sell_cooldown_days || 0)))}`);
    parts.push(`s${formatCompact(sellParams.repair_stage_sell_pct)}`);
  }
  if (sellStrategy === 'grid_rebound' || sellStrategy === 'price_rise_grid') {
    parts.push(`g${formatCompact(sellParams.grid_rebound_step_pct)}`);
    parts.push(`gsell${formatCompact(sellParams.grid_sell_pct ?? sellParams.grid_second_sell_pct)}`);
    parts.push(`gmin${formatCompact(sellParams.grid_min_sell_amount)}`);
    if (Number(sellParams.grid_rebound_cycle_reset)) parts.push(`greset${formatCompact(sellParams.grid_rebound_cycle_reset)}`);
  }
  if (sellStrategy === 'cost_deleverage') {
    const profits = [sellParams.cost_first_profit_pct, sellParams.cost_second_profit_pct, sellParams.cost_third_profit_pct];
    const sells = [sellParams.cost_first_sell_pct, sellParams.cost_second_sell_pct, sellParams.cost_third_sell_pct];
    parts.push(`cp${profits.map(formatCompact).join('-')}`);
    parts.push(`cs${sells.map(formatCompact).join('-')}`);
    parts.push(`cc${formatCompact(Math.trunc(Number(sellParams.cost_deleverage_cooldown_days || 0)))}`);
    parts.push(`cmin${formatCompact(sellParams.cost_min_sell_amount)}`);
  }
  if (sellStrategy !== 'none' && sellParams.sell_allow_same_day_sell) parts.push('same1');
  if (sellParams.dca_rearm_drawdown_pct !== null && sellParams.dca_rearm_drawdown_pct !== undefined) parts.push(`rearm${formatCompact(sellParams.dca_rearm_drawdown_pct)}`);
  if (sellParams.buy_rearm_mode === 'restart_from_rearm') parts.push('rearmmode_restart');
  if (sellParams.sell_stage_rearm_drawdown_pct !== null && sellParams.sell_stage_rearm_drawdown_pct !== undefined) parts.push(`sellrearm${formatCompact(sellParams.sell_stage_rearm_drawdown_pct)}`);
  return parts.join('__');
}

function gridSellPct(params) {
  if (!params) return 0;
  if (params.grid_sell_pct !== null && params.grid_sell_pct !== undefined) return params.grid_sell_pct;
  return params.grid_second_sell_pct;
}

function buildBuyLabel(strategyKey, params, labels = {}) {
  const label = labels[strategyKey] || strategyKey;
  const bits = [];
  if (params.step_pct !== null && params.step_pct !== undefined) bits.push(`步长 ${formatCompact(params.step_pct)}%`);
  if (params.equal_slice_allocation_pct !== null && params.equal_slice_allocation_pct !== undefined) bits.push(`每步 ${formatCompact(params.equal_slice_allocation_pct)}%`);
  if (params.core_dip_initial_core_pct !== null && params.core_dip_initial_core_pct !== undefined) {
    bits.push(`初始 ${formatCompact(params.core_dip_initial_core_pct)}%`);
    bits.push(`周投 ${formatCompact(params.core_dip_weekly_core_pct)}%`);
    bits.push(`现金垫 ${formatCompact(params.core_dip_cash_reserve_pct)}%`);
    bits.push(`加仓 ${formatCompact(params.core_dip_start_drawdown_pct)}-${formatCompact(params.core_dip_full_drawdown_pct)}%`);
    if (params.core_dip_timing_enabled) {
      bits.push(`买点优化 延迟${formatCompact(Math.trunc(num(params.core_dip_timing_max_delay_days)))}日 大涨${formatCompact(params.core_dip_timing_rise_threshold_pct)}% 近低${formatCompact(params.core_dip_timing_near_low_pct)}%`);
    } else {
      bits.push('买点优化 关闭');
    }
  }
  return bits.length ? `${label} (${bits.join(' / ')})` : label;
}

function buildSellLabel(strategyKey, params, labels = {}, baseInputs = {}) {
  let label;
  if (strategyKey === 'repair_step') {
    label = `阶梯修复 ${formatCompact(params.sell_min_profit_pct)}%盈利 ${formatCompact(Math.trunc(num(params.repair_sell_cooldown_days)))}日冷却 ${formatCompact(params.repair_stage_sell_pct)}%单档`;
  } else if (strategyKey === 'grid_rebound') {
    label = `网格回弹 ${formatCompact(params.grid_rebound_step_pct)}%步长 每档${formatCompact(gridSellPct(params))}%卖出 ${formatCompact(params.sell_min_profit_pct ?? baseInputs.sell_min_profit_pct)}%最小盈利`;
    if (num(params.grid_rebound_cycle_reset)) label += ` 周期重启${Math.trunc(num(params.grid_rebound_cycle_reset))}`;
  } else if (strategyKey === 'price_rise_grid') {
    label = `价格上涨网格 ${formatCompact(params.grid_rebound_step_pct)}%步长 每档${formatCompact(gridSellPct(params))}%卖出 ${formatCompact(params.sell_min_profit_pct ?? baseInputs.sell_min_profit_pct)}%最小盈利`;
  } else if (strategyKey === 'cost_deleverage') {
    const profits = [
      params.cost_first_profit_pct,
      params.cost_second_profit_pct,
      params.cost_third_profit_pct
    ];
    const sells = [
      params.cost_first_sell_pct,
      params.cost_second_sell_pct,
      params.cost_third_sell_pct
    ];
    label = `成本去杠杆 盈利档 ${profits.map((value) => `${formatCompact(value)}%`).join('/')} × 减仓 ${sells.map((value) => `${formatCompact(value)}%`).join('/')} 冷却 ${formatCompact(Math.trunc(num(params.cost_deleverage_cooldown_days)))}日`;
  } else {
    label = labels[strategyKey] || strategyKey;
  }
  if (strategyKey !== 'none' && params.sell_allow_same_day_sell) label = `${label} / 买入日可卖`;
  if (params.dca_rearm_drawdown_pct !== null && params.dca_rearm_drawdown_pct !== undefined) {
    label = `${label} / 买档重启 ${formatCompact(params.dca_rearm_drawdown_pct)}%回撤`;
  }
  if (params.buy_rearm_mode === 'restart_from_rearm') label = `${label} / 重启后从首档`;
  const rearmMode = params.sell_stage_rearm_mode || baseInputs.sell_stage_rearm_mode;
  const hasPct = params.sell_stage_rearm_drawdown_pct !== null && params.sell_stage_rearm_drawdown_pct !== undefined;
  if (rearmMode === 'drop_from_last_sell') {
    if (hasPct) label = `${label} / 卖档重启 距上次卖出跳水 ${formatCompact(params.sell_stage_rearm_drawdown_pct)}%`;
    else label = `${label} / 卖档重启 距上次卖出跳水 (退化为买档重启阈值)`;
  } else if (hasPct) {
    label = `${label} / 卖档重启 ATH回撤 ${formatCompact(params.sell_stage_rearm_drawdown_pct)}%`;
  }
  return label;
}

function inflateCandidate(packet, candidateRow) {
  const candidateSchema = schemaIndexMap(packet.candidate_schema || []);
  const buyVariants = Array.isArray(packet.buy_variants) ? packet.buy_variants : [];
  const sellVariants = Array.isArray(packet.sell_variants) ? packet.sell_variants : [];
  const buySchema = packet.buy_variant_schema || [];
  const sellSchema = packet.sell_variant_schema || [];
  const buyVariantIndex = candidateRow[candidateSchema.buy_variant_id];
  const sellVariantIndex = candidateRow[candidateSchema.sell_variant_id];
  const buyRow = buyVariants[buyVariantIndex] || [];
  const sellRow = sellVariants[sellVariantIndex] || [];
  const buyVariant = inflateVariant(buyRow, buySchema, BUY_PARAMETER_FIELDS);
  const sellVariant = inflateVariant(sellRow, sellSchema, SELL_PARAMETER_FIELDS);
  const candidateId = candidateRow[candidateSchema.candidate_id];
  const buyVariantKey = buyRow[1] || `buy:${buyVariantIndex}`;
  const sellVariantKey = sellRow[1] || `sell:${sellVariantIndex}`;
  const buyStrategy = buyRow[2] || '';
  const sellStrategy = sellRow[2] || '';
  const registry = packet.registry || {};
  const labels = registry.labels || {};
  const buyLabels = labels.buy || registry.buy_strategy_labels || {};
  const sellLabels = labels.sell || registry.sell_strategy_labels || {};
  const buyLabel = buildBuyLabel(buyStrategy, buyVariant, buyLabels);
  const sellLabel = buildSellLabel(sellStrategy, sellVariant, sellLabels, packet.inputs || {});
  const candidate = {
    key: candidateRow[candidateSchema.candidate_key] || `${buyStrategy}__${sellStrategy}__id${candidateId}`,
    candidate_id: candidateId,
    combination_key: `${buyVariantKey}__${sellVariantKey}`,
    label: `${buyLabel} / ${sellLabel}`,
    buy_strategy: buyStrategy,
    sell_strategy: sellStrategy,
    buy_variant_key: buyVariantKey,
    sell_variant_key: sellVariantKey,
    strategy_definition_version: packet.registry?.version || '',
    ...buyVariant,
    ...sellVariant
  };
  return candidate;
}

function inflateCandidateBatch(packet, rows) {
  return (Array.isArray(rows) ? rows : []).map((row) => inflateCandidate(packet, row));
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

function holdingPeriodPrices(state, buyDate, sellDate, inputs) {
  return (state.price_points || [])
    .filter((point) => point.date >= buyDate && point.date <= sellDate)
    .map((point) => priceUsd(state.symbol, num(point.close), inputs))
    .filter((price) => price > 0);
}

function sliceEfficiency(state, point, inputs, lot, shares) {
  const sellPrice = priceUsd(state.symbol, num(point.close), inputs);
  const prices = holdingPeriodPrices(state, lot.buy_date || point.date, point.date, inputs);
  const hadIntermediatePoints = prices.length > 0;
  if (!prices.length) prices.push(num(lot.buy_price_usd), sellPrice);
  const high = Math.max(...prices);
  const low = Math.min(...prices);
  const spread = sellPrice - num(lot.buy_price_usd);
  const amplitude = high - low;
  const upside = high - num(lot.buy_price_usd);
  return {
    buy_date: lot.buy_date || point.date,
    buy_price_usd: num(lot.buy_price_usd),
    shares,
    holding_period_high_usd: high,
    holding_period_low_usd: low,
    holding_period_price_point_count: prices.length,
    holding_period_had_intermediate_points: hadIntermediatePoints,
    price_spread_efficiency: amplitude > 1e-9 ? spread / amplitude : 0,
    sell_timing_efficiency: upside > 1e-9 ? spread / upside : 0
  };
}

function sellQualityLotSlices(state, point, inputs, shares, lot = null) {
  if (shares <= 0) return [];
  if (lot) return [sliceEfficiency(state, point, inputs, lot, shares)];
  let remaining = shares;
  const slices = [];
  for (const item of state.lots) {
    if (remaining <= 0) break;
    if (num(item.remaining_shares) <= 0) continue;
    const sold = Math.min(num(item.remaining_shares), remaining);
    slices.push(sliceEfficiency(state, point, inputs, item, sold));
    remaining -= sold;
  }
  return slices;
}

function weightedSliceMetric(slices, field) {
  const total = slices.reduce((sum, item) => sum + num(item.shares), 0);
  return total > 0 ? slices.reduce((sum, item) => sum + num(item[field]) * num(item.shares), 0) / total : 0;
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
  const recent = Array.isArray(state.recent_points) ? state.recent_points : [];
  const previousPoint = recent.length >= 2 ? recent[recent.length - 2] : null;
  const previousClose = previousPoint ? num(previousPoint.close) : 0;
  const dayChangePct = previousClose > 0 ? pct(num(point.close) / previousClose - 1) : 0;
  const positionValue = state.last_value;
  const stateValue = state.cash + positionValue;
  state.lots.push({
    threshold_pct: num(extra.threshold_pct),
    buy_drawdown_pct: drawdown,
    buy_price_usd: px,
    buy_date: point.date,
    initial_shares: shares,
    remaining_shares: shares,
    first_grid_sell_done: false,
    second_grid_sell_done: false,
    repair_sell_marks: {}
  });
  const rearmed = rearmAfterDcaBuy(state, drawdown, inputs, sellStrategy, px);
  tradeLog.push({
    action: 'buy',
    date: point.date,
    symbol: state.symbol,
    price: point.close,
    buy_strategy: buyStrategy,
    sell_strategy: sellStrategy,
    drawdown_pct: drawdown,
    gross_amount: grossAmount,
    fee,
    net_amount: net,
    shares,
    cash_after: state.cash,
    cash_pct_after: pct(safeRatio(state.cash, stateValue)),
    position_value_after: positionValue,
    day_change_pct: dayChangePct,
    sell_cycle_rearmed: rearmed,
    ...extra
  });
  return true;
}

function rearmAfterDcaBuy(state, drawdown, inputs, sellStrategy, currentPrice) {
  if (!['repair_step', 'grid_rebound', 'price_rise_grid', 'cost_deleverage'].includes(sellStrategy)) return false;
  if (!Object.keys(state.sell_marks).length) return false;
  const mode = inputs.sell_stage_rearm_mode || 'legacy';
  // Mirror Python `sell_stage_rearm_drawdown_pct(inputs)`: when the raw
  // sell-stage threshold is null OR <= dca threshold, fall back to dca
  // (parity bug found 2026-06-13 — JS only handled the null case via `??`,
  // letting drop_from_last_sell trigger at smaller drops than Python did).
  const dcaThresh = Math.max(0, num(inputs.dca_rearm_drawdown_pct));
  const rawThreshold = inputs.sell_stage_rearm_drawdown_pct;
  const effective = (rawThreshold == null || Number(rawThreshold) <= dcaThresh) ? dcaThresh : Number(rawThreshold);
  const threshold = Math.min(Math.max(0, effective), num(inputs.max_drawdown_pct));
  if (mode === 'drop_from_last_sell') {
    const lastSell = state.last_position_sell_price;
    if (lastSell == null || lastSell <= 0 || currentPrice == null) return false;
    const dropPct = (lastSell - Number(currentPrice)) / lastSell * 100;
    if (dropPct + 1e-9 < threshold) return false;
    state.sell_marks = {};
    state.last_position_sell_price = null;
    if (sellStrategy === 'grid_rebound') {
      state.grid_rebound_cycle_anchor_drawdown_pct = null;
      state.grid_rebound_last_sell_drawdown_pct = null;
    }
    return true;
  }
  // legacy: ATH-relative drawdown threshold
  if (drawdown + 1e-9 < threshold) return false;
  state.sell_marks = {};
  if (sellStrategy === 'grid_rebound') {
    state.grid_rebound_cycle_anchor_drawdown_pct = null;
    state.grid_rebound_last_sell_drawdown_pct = null;
  }
  // ADR-0004: the cycle anchor (price_rise_grid_anchor_price / cost_deleverage_cycle_anchor_price)
  // survives a sell-stage rearm. Resetting it re-seeded from the diluted average cost, which made
  // deep-drawdown buys instantly re-trigger same-day sells at the buy price.
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
  const anchor = Math.max(0, num(state.buy_rearm_anchor_drawdown_pct, 0));
  let bought = false;
  for (const tranche of tranches) {
    const key = String(Math.round(tranche.threshold_pct * 1e8) / 1e8);
    const effectiveThreshold = anchor + num(tranche.threshold_pct);
    if (drawdown + 1e-9 < effectiveThreshold) continue;
    const currentTotal = state.cash + state.shares * priceUsd(state.symbol, point.close, inputs);
    const target = currentTotal * tranche.allocation_pct / 100;
    const already = executed[key] || 0;
    // Fire each tranche once per buy cycle. Without this, target is recomputed on the live
    // currentTotal every day, so as price recovers from the dip the engine keeps topping up
    // already-funded tranches with tiny (target - already) micro-buys — each paying the fixed
    // fee. A tranche is "done" once funded; it only re-arms after a drawdown reset/rearm (which
    // clears `executed` and re-marks via markConsumedTranchesFromPosition).
    if (already > 0) continue;
    const gross = buyStrategy === 'pyramid_3'
      ? Math.min(target, state.cash)
      : Math.min(Math.max(0, target - already), state.cash);
    if (gross <= 0) {
      executed[key] = buyStrategy === 'pyramid_3' ? 1 : Math.max(already, target);
      continue;
    }
    bought = recordBuy(state, point, inputs, tradeLog, buyStrategy, sellStrategy, gross, drawdown, {
      threshold_pct: effectiveThreshold,
      base_threshold_pct: tranche.threshold_pct,
      buy_rearm_anchor_drawdown_pct: anchor || null,
      allocation_pct: tranche.allocation_pct
    }) || bought;
    executed[key] = buyStrategy === 'pyramid_3' ? 1 : already + gross;
  }
  return bought;
}

function markConsumedTranchesFromPosition(state, tranches, executed, strategy) {
  // Mark tranches as consumed based on current invested ratio (cumulative allocation).
  // Mirrors Python _mark_consumed_tranches_from_position.
  if (state.shares <= 0 || !tranches || !tranches.length) return;
  // state.last_value is refreshed each day before this runs (mirrors Python's state.last_value).
  // NOTE: state.last_price is never assigned in the JS engine — do not use it here.
  const marketValue = num(state.last_value, 0);
  const total = state.cash + marketValue;
  if (total <= 0) return;
  const investedRatio = marketValue / total;
  var cumulative = 0;
  var sorted = tranches.slice().sort(function (a, b) { return num(a.threshold_pct) - num(b.threshold_pct); });
  for (var i = 0; i < sorted.length; i++) {
    var t = sorted[i];
    var key = String(Math.round(num(t.threshold_pct) * 1e8) / 1e8);
    cumulative += num(t.allocation_pct) / 100;
    if (investedRatio >= cumulative - 1e-9) {
      if (strategy === 'pyramid_3') {
        executed[key] = 1;
      } else {
        executed[key] = total * num(t.allocation_pct) / 100;
      }
    }
  }
}

function recordSell(state, point, shares, inputs, tradeLog, sellStrategy, trigger, costBasis = null, stage = null, soldLotSlices = null) {
  const px = priceUsd(state.symbol, point.close, inputs);
  const gross = shares * px;
  if (gross <= 0) return false;
  const slices = soldLotSlices || sellQualityLotSlices(state, point, inputs, shares, null);
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
  const event = {
    action: 'sell',
    date: point.date,
    symbol: state.symbol,
    price: point.close,
    sell_strategy: sellStrategy,
    trigger_value: trigger,
    drawdown_pct: drawdownPct(point, inputs),
    gross_amount: gross,
    fee,
    net_amount: gross - fee,
    shares,
    cash_after: state.cash,
    cash_pct_after: pct(safeRatio(state.cash, state.cash + state.last_value)),
    position_value_after: state.last_value,
    estimated_profit: basis > 0 ? gross - basis : 0,
    estimated_profit_pct: basis > 0 ? pct(gross / basis - 1) : 0,
    price_spread_efficiency: weightedSliceMetric(slices, 'price_spread_efficiency'),
    sell_timing_efficiency: weightedSliceMetric(slices, 'sell_timing_efficiency'),
    sold_lot_slices: slices
  };
  if (stage) event.stage = stage;
  tradeLog.push(event);
  return true;
}

function markBuyRearmAfterPositionSell(state, point, inputs) {
  const drawdown = drawdownPct(point, inputs);
  const rearm = Math.min(Math.max(0, num(inputs.dca_rearm_drawdown_pct)), num(inputs.max_drawdown_pct));
  state.buy_rearm_drawdown_pct = Math.min(num(inputs.max_drawdown_pct), drawdown + rearm);
}

function sellShares(state, point, requested, inputs, tradeLog, sellStrategy, trigger, minGross = 0, stage = null) {
  const shares = sellableShares(state, requested, inputs);
  if (shares <= 0 || shares * priceUsd(state.symbol, point.close, inputs) + 1e-9 < minGross) return false;
  const basis = avgCost(state) * shares;
  const soldLotSlices = sellQualityLotSlices(state, point, inputs, shares, null);
  reduceLotsFifo(state, shares);
  const sold = recordSell(state, point, shares, inputs, tradeLog, sellStrategy, trigger, basis, stage, soldLotSlices);
  if (sold) {
    state.last_position_sell_price = priceUsd(state.symbol, point.close, inputs);
    markBuyRearmAfterPositionSell(state, point, inputs);
  }
  return sold;
}

function costDeleverageCycleAnchor(state) {
  const anchor = num(state.cost_deleverage_cycle_anchor_price, 0);
  if (anchor > 0) return anchor;
  const cost = avgCost(state);
  if (cost > 0) state.cost_deleverage_cycle_anchor_price = cost;
  return cost;
}

function gridReboundCycleAnchor(state) {
  if (state.grid_rebound_cycle_anchor_drawdown_pct !== null && state.grid_rebound_cycle_anchor_drawdown_pct !== undefined) {
    return num(state.grid_rebound_cycle_anchor_drawdown_pct);
  }
  const anchor = avgBuyDrawdown(state);
  state.grid_rebound_cycle_anchor_drawdown_pct = anchor;
  return anchor;
}

function gridReboundStages(anchor, inputs) {
  if (anchor <= 0) return [];
  const step = Math.max(num(inputs.grid_rebound_step_pct), 1e-9);
  const stages = [];
  let stageIndex = 1;
  while (true) {
    const threshold = Math.max(0, anchor - step * stageIndex);
    const sellPct = num(inputs.grid_sell_pct);
    stages.push([`grid_${stageIndex}`, threshold, sellPct]);
    if (threshold <= 0) break;
    stageIndex += 1;
  }
  return stages;
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
      if (sellShares(state, point, state.shares * num(inputs.repair_stage_sell_pct) / 100, inputs, tradeLog, sellStrategy, threshold, 0, mark)) {
        state.sell_marks[mark] = true;
        state.last_repair_sell_trade_index = tradeIndex;
        return;
      }
    }
  } else if (sellStrategy === 'grid_rebound') {
    const cost = avgCost(state);
    if (cost <= 0 || current < cost * (1 + num(inputs.sell_min_profit_pct) / 100)) return;
    const drawdown = drawdownPct(point, inputs);
    const step = num(inputs.grid_rebound_step_pct);

    const deepestBuy = Math.max(...state.lots.filter((lot) => lot.remaining_shares > 0).map((lot) => num(lot.buy_drawdown_pct)), 0);
    if (state.grid_rebound_last_sell_drawdown_pct === null || state.grid_rebound_last_sell_drawdown_pct === undefined) {
      state.grid_rebound_last_sell_drawdown_pct = deepestBuy;
    }

    const mark = state.grid_rebound_last_sell_drawdown_pct;
    if (mark <= 0) return;
    const targetDrawdown = mark - step;
    if (targetDrawdown < 0 && drawdown <= 1e-9) {
      // Rebounded to ATH or higher, allow sell
    } else if (drawdown > targetDrawdown + 1e-9) {
      return;
    }

    const sellPct = num(inputs.grid_sell_pct);
    const stage = `grid_rebound_${drawdown.toFixed(2)}`;
    if (sellShares(state, point, state.shares * sellPct / 100, inputs, tradeLog, sellStrategy, drawdown, num(inputs.grid_min_sell_amount), stage)) {
      state.sell_marks[stage] = true;
      state.grid_rebound_last_sell_drawdown_pct = drawdown;
    }
  } else if (sellStrategy === 'price_rise_grid') {
    const cost = avgCost(state);
    if (cost <= 0 || current < cost * (1 + num(inputs.sell_min_profit_pct) / 100)) return;
    if (state.price_rise_grid_anchor_price === null || state.price_rise_grid_anchor_price === undefined) {
      state.price_rise_grid_anchor_price = cost;
    }
    const anchor = state.price_rise_grid_anchor_price;
    const step = num(inputs.grid_rebound_step_pct);
    if (current < anchor * (1 + step / 100) - 1e-9) return;
    const sellPct = num(inputs.grid_sell_pct);
    const stage = `price_rise_${current.toFixed(2)}`;
    if (sellShares(state, point, state.shares * sellPct / 100, inputs, tradeLog, sellStrategy, current, num(inputs.grid_min_sell_amount), stage)) {
      state.sell_marks[stage] = true;
      state.price_rise_grid_anchor_price = current;
    }
  } else if (sellStrategy === 'cost_deleverage') {
    if (num(inputs.cost_deleverage_cooldown_days) > 0 && state.last_cost_deleverage_sell_trade_index !== null && tradeIndex - state.last_cost_deleverage_sell_trade_index < num(inputs.cost_deleverage_cooldown_days)) return;
    // ADR-0004: stage thresholds measure against the cycle anchor; sell_min_profit_pct stays a
    // separate cost-based no-loss gate.
    const cost = avgCost(state);
    if (cost <= 0 || current < cost * (1 + num(inputs.sell_min_profit_pct) / 100)) return;
    const anchor = costDeleverageCycleAnchor(state);
    if (anchor <= 0) return;
    const profit = current / anchor * 100 - 100;
    for (const [mark, threshold, sellPct] of [
      ['cost_1', num(inputs.cost_first_profit_pct), num(inputs.cost_first_sell_pct)],
      ['cost_2', num(inputs.cost_second_profit_pct), num(inputs.cost_second_sell_pct)],
      ['cost_3', num(inputs.cost_third_profit_pct), num(inputs.cost_third_sell_pct)]
    ]) {
      if (state.sell_marks[mark] || profit < threshold) continue;
      if (sellShares(state, point, state.shares * sellPct / 100, inputs, tradeLog, sellStrategy, threshold, num(inputs.cost_min_sell_amount), mark)) {
        state.sell_marks[mark] = true;
        if (mark === 'cost_3') {
          state.sell_marks = {};
          state.cost_deleverage_cycle_anchor_price = state.shares > 0 ? current : null;
        }
        state.last_cost_deleverage_sell_trade_index = tradeIndex;
        return;
      }
    }
  }
}

function candidateInputs(base, candidate) {
  const sellStageRearm = Object.prototype.hasOwnProperty.call(candidate, 'sell_stage_rearm_drawdown_pct')
    ? candidate.sell_stage_rearm_drawdown_pct
    : base.sell_stage_rearm_drawdown_pct;
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
    sell_stage_rearm_drawdown_pct: sellStageRearm,
    grid_rebound_step_pct: candidate.grid_rebound_step_pct ?? base.grid_rebound_step_pct,
    grid_sell_pct: candidate.grid_sell_pct ?? candidate.grid_second_sell_pct ?? base.grid_sell_pct ?? base.grid_second_sell_pct,
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
    sell_allow_same_day_sell: candidate.sell_allow_same_day_sell ?? base.sell_allow_same_day_sell,
    buy_rearm_mode: candidate.buy_rearm_mode ?? base.buy_rearm_mode ?? 'cumulative',
    cost_min_sell_amount: candidate.cost_min_sell_amount ?? base.cost_min_sell_amount
  };
}

function targetMaxDrawdown(targets, symbol, inputs) {
  const target = targets.find((item) => item.symbol === symbol);
  return target && target.max_drawdown_pct != null ? num(target.max_drawdown_pct) : num(inputs.max_drawdown_pct);
}

function sellMetrics(tradeLog, portfolioValues, cashValues, globalBounds) {
  const buys = tradeLog.filter((trade) => trade.action === 'buy');
  const sells = tradeLog.filter((trade) => trade.action === 'sell');
  const avgBuy = avg(buys.map((trade) => trade.drawdown_pct));
  const avgSell = avg(sells.map((trade) => trade.drawdown_pct));
  const avgProfit = avg(sells.map((trade) => trade.estimated_profit_pct));
  const avgSpreadEfficiency = avg(sells.map((trade) => trade.price_spread_efficiency));
  const avgSellTimingEfficiency = avg(sells.map((trade) => trade.sell_timing_efficiency));

  // buy_quality = (global_high - buy_price) / (global_high - global_low), weighted by shares
  // sell_quality = (sell_price - global_low) / (global_high - global_low), weighted by shares
  // If globalBounds provided, use task-wide price range per symbol. Otherwise fall back to per-slice period.
  // Averaged across all sell trades. Only completed (sold) lots contribute.
  // Require ≥3 holding-period price points (≥1 intermediate) for reliable scores.
  const MIN_PRICE_POINTS = 3;
  let skipsNoSlices = 0, skipsNarrowPeriod = 0, skipsNoAmplitude = 0, tradesScored = 0;
  let buyQuality = 0;
  let sellQuality = 0;
  if (sells.length) {
    const buyQualities = [];
    const sellQualities = [];
    for (const trade of sells) {
      const slices = trade.sold_lot_slices || [];
      if (!slices.length) { skipsNoSlices++; continue; }

      // Resolve bounds: globalBounds takes priority, fall back to per-slice period
      const symBounds = globalBounds && globalBounds[trade.symbol];
      const globalHigh = symBounds ? symBounds.high : null;
      const globalLow = symBounds ? symBounds.low : null;
      const useGlobal = globalHigh != null && globalLow != null && globalHigh - globalLow > 1e-9;

      let totalShares = 0;
      for (const sl of slices) totalShares += sl.shares || 0;
      if (totalShares <= 0) continue;

      // Check if ANY slice has enough price points for reliable scoring
      const minPtCount = Math.min(...slices.map(sl => sl.holding_period_price_point_count || 0));
      if (minPtCount < MIN_PRICE_POINTS) { skipsNarrowPeriod++; continue; }

      let tradeBuyQ = 0, tradeSellQ = 0, sharesWithAmplitude = 0;
      for (const sl of slices) {
        const shares = sl.shares || 0;
        const high = useGlobal ? globalHigh : (sl.holding_period_high_usd || 0);
        const low = useGlobal ? globalLow : (sl.holding_period_low_usd || 0);
        const amplitude = high - low;
        if (amplitude <= 1e-9) { skipsNoAmplitude++; continue; }
        const bq = clamp((high - (sl.buy_price_usd || 0)) / amplitude, 0, 1);
        const sq = clamp(((trade.price || 0) - low) / amplitude, 0, 1);
        tradeBuyQ += bq * shares;
        tradeSellQ += sq * shares;
        sharesWithAmplitude += shares;
      }
      if (sharesWithAmplitude > 0) {
        buyQualities.push(tradeBuyQ / sharesWithAmplitude);
        sellQualities.push(tradeSellQ / sharesWithAmplitude);
        tradesScored++;
      }
    }
    if (buyQualities.length) {
      buyQuality = buyQualities.reduce((s, v) => s + v, 0) / buyQualities.length * 100;
      sellQuality = sellQualities.reduce((s, v) => s + v, 0) / sellQualities.length * 100;
    }
  }
  if (typeof console !== 'undefined' && console.info) {
    const mode = globalBounds ? 'global' : 'period';
    console.info('[sellMetrics] mode=' + mode +
      ' sells=' + sells.length +
      ' scored=' + tradesScored +
      ' skipped(noSlices=' + skipsNoSlices + ' narrow=' + skipsNarrowPeriod + ' zeroAmp=' + skipsNoAmplitude + ')' +
      ' buyQ=' + Number(buyQuality).toFixed(1) +
      ' sellQ=' + Number(sellQuality).toFixed(1));
  }

  // Cash reuse / idle — kept as raw display metrics, no longer composite into sell_quality
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

  return {
    avg_buy_drawdown_pct: avgBuy,
    avg_sell_drawdown_pct: avgSell,
    avg_sell_profit_pct: avgProfit,
    avg_price_spread_efficiency: avgSpreadEfficiency,
    avg_sell_timing_efficiency: avgSellTimingEfficiency,
    buy_quality_score: buyQuality,
    cash_reuse_pct: cashReuse,
    avg_cash_pct: avgCash,
    sell_quality_score: sellQuality
  };
}

function leapsSignalSettings(inputs) {
  return {
    low_cash_threshold_pct: clamp(num(inputs.leaps_low_cash_threshold_pct, 12), 0, 100),
    min_drawdown_pct: Math.max(0, num(inputs.leaps_min_drawdown_pct, 12)),
    premium_budget_cap: Math.max(0, num(inputs.leaps_premium_budget_cap, 1000)),
    target_dte_label: String(inputs.leaps_target_dte_label || '18-24M')
  };
}

function gradeLeapsSignal(score) {
  if (score >= 78) return '高';
  if (score >= 55) return '中';
  if (score >= 32) return '低';
  return '无';
}

function parseTradeDateMs(value) {
  const match = /^(\d{4})-(\d{2})-(\d{2})/.exec(String(value || ''));
  if (!match) return NaN;
  return Date.UTC(Number(match[1]), Number(match[2]) - 1, Number(match[3]));
}

function naturalDayDiff(startDate, endDate) {
  const start = parseTradeDateMs(startDate);
  const end = parseTradeDateMs(endDate);
  if (!Number.isFinite(start) || !Number.isFinite(end) || end <= start) return null;
  return Math.round((end - start) / 86400000);
}

function stockPriceFromTrade(trade) {
  const direct = num(trade?.price, NaN);
  if (Number.isFinite(direct) && direct > 0) return direct;
  const gross = num(trade?.gross_amount, NaN);
  const shares = num(trade?.shares, NaN);
  return Number.isFinite(gross) && Number.isFinite(shares) && shares > 0 ? gross / shares : null;
}

function stockPointsForSignal(task, signalOrTrade) {
  const symbol = signalOrTrade?.symbol;
  const points = symbol ? task?.price_points?.[symbol] : null;
  return Array.isArray(points) ? points : [];
}

function latestStockPoint(points) {
  const point = Array.isArray(points) && points.length ? points[points.length - 1] : null;
  return point ? { date: point.date, close: num(point.close) } : null;
}

function pointIndexBeforeDate(points, entryDate) {
  if (!Array.isArray(points) || !points.length || !entryDate) return -1;
  let low = 0;
  let high = points.length - 1;
  let result = -1;
  while (low <= high) {
    const mid = Math.floor((low + high) / 2);
    if (String(points[mid]?.date || '') < entryDate) {
      result = mid;
      low = mid + 1;
    } else {
      high = mid - 1;
    }
  }
  return result;
}

function realizedVolatilityPct(points, entryDate, entryIndex = null) {
  const lastHistoryIndex = Number.isFinite(Number(entryIndex))
    ? Number(entryIndex) - 1
    : pointIndexBeforeDate(points, String(entryDate || ''));
  if (!Array.isArray(points) || lastHistoryIndex < 1) return 60;
  const start = Math.max(0, lastHistoryIndex - 60);
  const returns = [];
  for (let index = start + 1; index <= lastHistoryIndex; index += 1) {
    const previous = num(points[index - 1]?.close);
    const current = num(points[index]?.close);
    if (previous > 0 && current > 0) returns.push(Math.log(current / previous));
  }
  if (returns.length < 20) return 60;
  const mean = avg(returns);
  const variance = returns.reduce((sum, value) => sum + Math.pow(value - mean, 2), 0) / Math.max(1, returns.length - 1);
  const annualized = Math.sqrt(variance) * Math.sqrt(252) * 100;
  return clamp(annualized, 15, 120);
}

function stockSignalMark(trade, nextStockSell, task) {
  const buyPrice = stockPriceFromTrade(trade);
  const points = stockPointsForSignal(task, trade);
  const symbol = trade?.symbol;
  const entryIndex = task?.tradingIndex?.[symbol]?.[trade?.date];
  const sellPrice = nextStockSell ? stockPriceFromTrade(nextStockSell) : null;
  const latestPoint = latestStockPoint(points);
  const markDate = nextStockSell?.date || latestPoint?.date || '';
  const markPrice = nextStockSell ? sellPrice : (latestPoint ? latestPoint.close : null);
  return {
    stock_mark_date: markDate,
    stock_mark_price: markPrice,
    stock_return_pct: buyPrice && markPrice ? pct(markPrice / buyPrice - 1) : null,
    realized_volatility_pct: realizedVolatilityPct(points, trade?.date, entryIndex)
  };
}

function buildSellQueuesBySymbol(tradeLog) {
  const queues = {};
  for (const trade of Array.isArray(tradeLog) ? tradeLog : []) {
    if (!trade || trade.action !== 'sell' || !trade.symbol) continue;
    if (!queues[trade.symbol]) queues[trade.symbol] = [];
    queues[trade.symbol].push(trade);
  }
  Object.values(queues).forEach((items) => items.sort((a, b) => String(a.date || '').localeCompare(String(b.date || ''))));
  return queues;
}

function findNextStockSellFromQueues(sellQueues, sellIndexes, buyTrade) {
  const buyDate = parseTradeDateMs(buyTrade?.date);
  if (!Number.isFinite(buyDate)) return null;
  const symbol = buyTrade?.symbol;
  const queue = sellQueues?.[symbol] || [];
  let index = sellIndexes[symbol] || 0;
  while (index < queue.length) {
    const sellDate = parseTradeDateMs(queue[index]?.date);
    if (Number.isFinite(sellDate) && sellDate > buyDate) break;
    index += 1;
  }
  sellIndexes[symbol] = index;
  return queue[index] || null;
}

function findNextStockSell(tradeLog, buyTrade) {
  return findNextStockSellFromQueues(buildSellQueuesBySymbol(tradeLog), {}, buyTrade);
}

function scoreLeapsBuySignal(trade, settings, nextStockSell = null, task = null) {
  if (!trade || trade.action !== 'buy') return null;
  const drawdown = Math.max(0, num(trade.drawdown_pct));
  const cashPct = clamp(num(trade.cash_pct_after, 100), 0, 100);
  const dayChange = num(trade.day_change_pct);
  const gross = Math.max(0, num(trade.gross_amount));
  const pendingCore = Math.max(0, num(trade.pending_core_amount));
  const drawdownScore = clamp(safeRatio(drawdown, Math.max(settings.min_drawdown_pct, 1)) * 34, 0, 34);
  const cashScore = clamp(safeRatio(settings.low_cash_threshold_pct - cashPct, Math.max(settings.low_cash_threshold_pct, 1)) * 30, 0, 30);
  const buyScore = gross > 0 ? 20 : 0;
  const pendingScore = pendingCore > 0 ? 8 : 0;
  const notChasingScore = dayChange <= 1.8 ? 8 : dayChange <= 3.5 ? 3 : -12;
  const score = clamp(drawdownScore + cashScore + buyScore + pendingScore + notChasingScore, 0, 100);
  const grade = gradeLeapsSignal(score);
  const reasons = [];
  if (cashPct <= settings.low_cash_threshold_pct) reasons.push('低现金');
  if (drawdown >= settings.min_drawdown_pct) reasons.push('回撤达标');
  if (gross > 0) reasons.push('股票策略买入');
  if (pendingCore > 0) reasons.push('仍有待买现金');
  if (dayChange > 3.5) reasons.push('追高日降级');
  return {
    date: trade.date,
    symbol: trade.symbol,
    grade,
    score,
    drawdown_pct: drawdown,
    cash_pct_after: cashPct,
    buy_amount: gross,
    stock_buy_price: stockPriceFromTrade(trade),
    day_change_pct: dayChange,
    premium_budget_cap: Math.min(settings.premium_budget_cap, Math.max(0, gross * 0.35)),
    target_dte_label: settings.target_dte_label,
    next_stock_sell_date: nextStockSell?.date || '',
    stock_sell_date: nextStockSell?.date || '',
    stock_sell_risk: nextStockSell?.date ? '' : 'no_stock_sell',
    stock_sell_risk_label: nextStockSell?.date ? '' : '无正股卖点',
    stock_sell_price: nextStockSell ? stockPriceFromTrade(nextStockSell) : null,
    stock_holding_days: nextStockSell ? naturalDayDiff(trade.date, nextStockSell.date) : null,
    ...stockSignalMark(trade, nextStockSell, task),
    reasons
  };
}

function summarizeLeapsSignals(tradeLog, inputs, includeDetails = false, task = null) {
  const settings = leapsSignalSettings(inputs);
  const sellQueues = buildSellQueuesBySymbol(tradeLog);
  const sellIndexes = {};
  const signals = (Array.isArray(tradeLog) ? tradeLog : [])
    .filter((trade) => trade.action === 'buy')
    .map((trade) => scoreLeapsBuySignal(trade, settings, findNextStockSellFromQueues(sellQueues, sellIndexes, trade), task))
    .filter((signal) => signal && signal.grade !== '无')
    .sort((a, b) => {
      const scoreDiff = Number(b.score || 0) - Number(a.score || 0);
      if (Math.abs(scoreDiff) > 1e-9) return scoreDiff;
      return String(a.date || '').localeCompare(String(b.date || ''));
    });
  const best = signals[0] || null;
  const gradeCounts = signals.reduce((counts, signal) => {
    const grade = signal.grade || '无';
    if (grade !== '无') counts[grade] = (counts[grade] || 0) + 1;
    return counts;
  }, { 高: 0, 中: 0, 低: 0 });
  const summary = {
    grade: best ? best.grade : '无',
    score: best ? best.score : 0,
    best_date: best ? best.date : '',
    trigger_count: signals.length,
    grade_counts: gradeCounts,
    low_cash_threshold_pct: settings.low_cash_threshold_pct,
    min_drawdown_pct: settings.min_drawdown_pct,
    premium_budget_cap: settings.premium_budget_cap,
    target_dte_label: settings.target_dte_label,
    top_signals: signals.slice(0, 5)
  };
  if (includeDetails) summary.all_signals = signals;
  return summary;
}

function simulate(task, baseInputs, candidate) {
  const inputs = candidateInputs(baseInputs, candidate);
  const strategy = candidate.buy_strategy;
  const sellStrategy = candidate.sell_strategy;
  const pointByDay = task.pointByDay || {};
  const allDays = task.allDays || [];
  const contribDays = task.contribDays || new Set();
  const dcaDays = task.dcaDays || {};
  const tradingIndex = task.tradingIndex || {};
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
      grid_rebound_cycle_anchor_drawdown_pct: null,
      grid_rebound_last_sell_drawdown_pct: null,
      price_rise_grid_anchor_price: null,
      cost_deleverage_cycle_anchor_price: null,
      last_position_sell_price: null,
      last_repair_sell_trade_index: null,
      last_cost_deleverage_sell_trade_index: null,
      dca_pending_cash: strategy === 'weekly_dca' ? budget : 0,
      core_dip_pending_cash: 0,
      core_dip_pending_days: 0,
      recent_points: [],
      price_points: task.price_points?.[target.symbol] || [],
      buy_rearm_drawdown_pct: null,
      buy_rearm_anchor_drawdown_pct: null
    };
  }
  const executed = Object.fromEntries(task.targets.map((target) => [target.symbol, {}]));
  const tranchesBySymbol = Object.fromEntries(
    task.targets.map((target) => [
      target.symbol,
      buildTranches({ ...inputs, max_drawdown_pct: targetMaxDrawdown(task.targets, target.symbol, inputs) }, strategy)
    ])
  );
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
        if (drawdownPct(point, inputs) <= 0.5) {
          executed[symbol] = {};
          state.buy_rearm_anchor_drawdown_pct = null;
          state.buy_rearm_drawdown_pct = null;
        }
        if (
          Object.keys(executed[symbol]).length
          && state.buy_rearm_drawdown_pct !== null
          && state.buy_rearm_drawdown_pct !== undefined
          && drawdownPct(point, inputs) + 1e-9 >= state.buy_rearm_drawdown_pct
        ) {
          executed[symbol] = {};
          state.buy_rearm_anchor_drawdown_pct = inputs.buy_rearm_mode === 'restart_from_rearm' ? drawdownPct(point, inputs) : null;
          state.buy_rearm_drawdown_pct = null;
        }
        const tranches = tranchesBySymbol[symbol] || [];
        // Re-mark consumed tranches after any rearm so the engine respects current position.
        if (!Object.keys(executed[symbol]).length) {
          markConsumedTranchesFromPosition(state, tranches, executed[symbol], strategy);
        }
        bought = executeTranches(state, point, tranches, executed[symbol], inputs, tradeLog, strategy, sellStrategy);
      }
      if (!bought || (bought && sellStrategy !== 'none' && Boolean(inputs.sell_allow_same_day_sell))) {
        executeSells(state, point, inputs, strategy, sellStrategy, tradeLog, tradeIndex);
      }
    }
    portfolioValues.push(Object.values(states).reduce((sum, state) => sum + state.cash + state.last_value, 0));
    cashValues.push(Object.values(states).reduce((sum, state) => sum + state.cash, 0));
  }
  const finalValue = portfolioValues[portfolioValues.length - 1] || 0;
  const totalContributed = num(inputs.initial_cash) + totalMonthlyContributions;
  // Global price bounds per symbol from full task price range
  const globalBounds = {};
  if (task.price_points) {
    for (const [symbol, points] of Object.entries(task.price_points)) {
      if (!Array.isArray(points) || !points.length) continue;
      let high = -Infinity, low = Infinity;
      for (const pt of points) {
        const price = num(pt.close);
        if (price > high) high = price;
        if (price < low) low = price;
      }
      if (high - low > 1e-9) globalBounds[symbol] = { high, low };
    }
  }
  const metrics = sellMetrics(tradeLog, portfolioValues, cashValues, globalBounds);
  const result = {
    return_pct: totalContributed > 0 ? pct(finalValue / totalContributed - 1) : 0,
    max_drawdown_pct: maxDrawdown(portfolioValues),
    trade_count: Object.values(states).reduce((sum, state) => sum + state.trades, 0),
    contribution_count: contributionCount,
    leaps_signal: summarizeLeapsSignals(tradeLog, inputs, Boolean(workerState?.include_leaps_signal_details), task),
    ...metrics
  };
  if (workerState?.include_trades) result.trade_log = tradeLog;
  if (workerState?.include_trades) {
    // [GA-DIAG] Capture full candidate object so the trade-detail UI can show what was actually executed.
    const candidateForDiag = {};
    for (const key of Object.keys(candidate || {})) {
      const v = candidate[key];
      if (typeof v === 'object' && v !== null) continue;
      candidateForDiag[key] = v;
    }
    result.candidate_parameters = candidateForDiag;
    result.inputs_snapshot = {
      buy_strategy: candidate.buy_strategy,
      sell_strategy: candidate.sell_strategy,
      candidate_key: candidate.key,
      step_pct: inputs.step_pct,
      equal_slice_allocation_pct: inputs.equal_slice_allocation_pct,
      grid_rebound_step_pct: inputs.grid_rebound_step_pct,
      grid_sell_pct: inputs.grid_sell_pct,
      sell_min_profit_pct: inputs.sell_min_profit_pct,
      sell_allow_same_day_sell: inputs.sell_allow_same_day_sell,
      grid_min_sell_amount: inputs.grid_min_sell_amount,
      max_drawdown_pct: inputs.max_drawdown_pct,
      dca_rearm_drawdown_pct: inputs.dca_rearm_drawdown_pct,
      buy_rearm_mode: inputs.buy_rearm_mode,
      sell_stage_rearm_drawdown_pct: inputs.sell_stage_rearm_drawdown_pct,
      reserve_position_pct: inputs.reserve_position_pct,
      drawdown_basis: inputs.drawdown_basis,
      initial_cash: inputs.initial_cash,
      monthly_contribution: inputs.monthly_contribution,
      task_start: task.start,
      task_end: task.end,
      first_portfolio_value: portfolioValues[0],
      last_portfolio_value: portfolioValues[portfolioValues.length - 1],
      market_data_hash: (workerState?.packet?.market_data?.hash) || '',
      price_point_count: (task.price_points && task.price_points[Object.keys(task.price_points || {})[0]] || []).length,
      price_points_hash: (() => {
        const sym = Object.keys(task.price_points || {})[0];
        const pts = sym ? task.price_points[sym] : [];
        if (!pts.length) return '';
        const sample = pts.slice(0, 5).map(p => `${p.date}:${Number(p.close).toFixed(2)}:${Number(p.drawdown_120).toFixed(6)}`).join('|');
        const tailSample = pts.length > 5 ? '|...|' + pts.slice(-3).map(p => `${p.date}:${Number(p.close).toFixed(2)}:${Number(p.drawdown_120).toFixed(6)}`).join('|') : '';
        return `${pts.length}pts:${sample}${tailSample}`;
      })()
    };
  }
  if (workerState?.include_series) {
    result.series = {
      dates: allDays.slice(),
      portfolio_values: portfolioValues,
      cash_values: cashValues
    };
  }
  return result;
}

async function initRun(packet, workerIndex, runId, declaredTotal) {
  runId = runId || packet.run_id || '';
  const started = performance.now();
  const taskContexts = buildTaskContexts(packet);
  const diagnostics = packet.diagnostics || {};
  workerState = {
    runId,
    workerIndex,
    inputs: packet.inputs || {},
    packet,
    taskContexts,
    diagnostics,
    include_trades: Boolean(packet.include_trades),
    include_series: Boolean(packet.include_series),
    include_leaps_signal_details: Boolean(packet.include_leaps_signal_details),
    started,
    completed_simulations: 0,
    total_simulations: Math.max(1, Number(declaredTotal || 0)),
    last_progress_at: 0
  };
  diagnosticLog('start', {
    run_id: runId,
    worker_index: workerIndex,
    task_count: taskContexts.length,
    total_simulations: workerState.total_simulations,
    payload_schema: packet.payload_schema || ''
  });
  postMessage({
    type: 'start',
    run_id: runId,
    worker_index: workerIndex,
    task_count: taskContexts.length,
    total_simulations: workerState.total_simulations
  });
  postMessage({
    type: 'ready',
    run_id: runId,
    worker_index: workerIndex,
    task_count: taskContexts.length,
    completed_simulations: 0,
    elapsed_ms: performance.now() - started
  });
}

async function processBatch(message, workerIndex, runId) {
  if (!workerState || workerState.runId !== runId) {
    throw new Error('Worker 尚未初始化。');
  }
  if (workerState.busy) {
    throw new Error('Worker 正在处理上一批候选。');
  }
  workerState.busy = true;
  const candidateRows = Array.isArray(message.candidate_rows) ? message.candidate_rows : [];
  const candidates = inflateCandidateBatch(workerState.packet || {}, candidateRows);
  const batchId = message.batch_id || '';
  const taskContexts = workerState.taskContexts || [];
  const diagnostics = workerState.diagnostics || {};
  const verboseSimulationLogs = Boolean(diagnostics.verbose_simulation_logs);
  const progressEvery = Math.max(1, Number(diagnostics.progress_every || 50));
  const progressMinMs = Math.max(0, Number(diagnostics.progress_min_ms || 500));
  const started = performance.now();
  const rows = [];
  const batchTotal = Math.max(1, candidates.length * taskContexts.length);
  const slowSimulationMs = Number(diagnostics.slow_simulation_ms || 3000);
  let batchCompleted = 0;
  let simulateElapsedMsSum = 0;
  let simulateElapsedMsMax = 0;
  let slowSimulationCount = 0;
  postMessage({
    type: 'batch_start',
    run_id: runId,
    worker_index: workerIndex,
    batch_id: batchId,
    candidate_count: candidates.length,
    task_count: taskContexts.length,
    batch_total_simulations: batchTotal,
    completed_simulations: workerState.completed_simulations,
    total_simulations: workerState.total_simulations
  });
  try {
    for (let candidateIndex = 0; candidateIndex < candidates.length; candidateIndex += 1) {
      const candidate = candidates[candidateIndex];
      await gate(workerIndex, runId);
      const observations = [];
      for (let taskIndex = 0; taskIndex < taskContexts.length; taskIndex += 1) {
        const task = taskContexts[taskIndex];
        await gate(workerIndex, runId);
        const simulateStarted = performance.now();
        if (verboseSimulationLogs) {
          diagnosticLog('simulate_start', {
            run_id: runId,
            worker_index: workerIndex,
            batch_id: batchId,
            candidate_index: candidateIndex,
            candidate_key: candidate.key,
            task_index: taskIndex,
            task_key: task.key
          });
        }
        let metrics;
        let verifyResult = null;
        try {
          metrics = simulate(task, workerState.inputs, candidate);
          if (workerState.include_trades) {
            const run2 = simulate(task, workerState.inputs, candidate);
            const run3 = simulate(task, workerState.inputs, candidate);
            const sameR2 = Math.abs(Number(metrics.return_pct || 0) - Number(run2.return_pct || 0)) < 0.001
              && Math.abs(Number(metrics.max_drawdown_pct || 0) - Number(run2.max_drawdown_pct || 0)) < 0.001
              && Number(metrics.trade_count || 0) === Number(run2.trade_count || 0);
            const sameR3 = Math.abs(Number(metrics.return_pct || 0) - Number(run3.return_pct || 0)) < 0.001
              && Math.abs(Number(metrics.max_drawdown_pct || 0) - Number(run3.max_drawdown_pct || 0)) < 0.001
              && Number(metrics.trade_count || 0) === Number(run3.trade_count || 0);
            verifyResult = {
              deterministic: sameR2 && sameR3,
              run1: { ret: metrics.return_pct, dd: metrics.max_drawdown_pct, trades: metrics.trade_count },
              run2: { ret: run2.return_pct, dd: run2.max_drawdown_pct, trades: run2.trade_count },
              run3: { ret: run3.return_pct, dd: run3.max_drawdown_pct, trades: run3.trade_count }
            };
            diagnosticLog('verify_runs', verifyResult);
          }
        } catch (error) {
          const context = {
            stage: 'simulate_error',
            run_id: runId,
            worker_index: workerIndex,
            batch_id: batchId,
            candidate_index: candidateIndex,
            candidate_key: candidate.key,
            task_index: taskIndex,
            task_key: task.key,
            elapsed_ms: performance.now() - simulateStarted,
            completed_simulations: workerState.completed_simulations,
            total_simulations: workerState.total_simulations,
            message: error && error.message ? error.message : String(error),
            stack: error && error.stack ? String(error.stack) : ''
          };
          diagnosticError('simulate_error', context);
          if (error && typeof error === 'object') error.__parameter_lab_context = context;
          throw error;
        }
        const obs = {
          candidate_id: candidate.candidate_id,
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
        };
        if (workerState.include_trades && metrics.trade_log) {
          obs.trade_log = metrics.trade_log;
          // [GA-DIAG] Pass through diagnostic fields used by renderCellTradeRows banner.
          if (metrics.inputs_snapshot) obs.inputs_snapshot = metrics.inputs_snapshot;
          if (metrics.candidate_parameters) obs.candidate_parameters = metrics.candidate_parameters;
          obs.buy_strategy = candidate.buy_strategy;
          obs.sell_strategy = candidate.sell_strategy;
          obs.label = candidate.label;
        }
        if (verifyResult) {
          obs._verify = verifyResult;
        }
        observations.push(obs);
        batchCompleted += 1;
        workerState.completed_simulations += 1;
        const simulateElapsed = performance.now() - simulateStarted;
        simulateElapsedMsSum += simulateElapsed;
        simulateElapsedMsMax = Math.max(simulateElapsedMsMax, simulateElapsed);
        if (simulateElapsed >= slowSimulationMs) slowSimulationCount += 1;
        if (verboseSimulationLogs) {
          diagnosticLog('simulate_done', {
            run_id: runId,
            worker_index: workerIndex,
            batch_id: batchId,
            candidate_index: candidateIndex,
            candidate_key: candidate.key,
            task_index: taskIndex,
            task_key: task.key,
            elapsed_ms: simulateElapsed,
            completed_simulations: workerState.completed_simulations,
            total_simulations: workerState.total_simulations
          });
        } else if (simulateElapsed >= slowSimulationMs) {
          diagnosticLog('simulate_slow', {
            run_id: runId,
            worker_index: workerIndex,
            batch_id: batchId,
            candidate_index: candidateIndex,
            candidate_key: candidate.key,
            task_index: taskIndex,
            task_key: task.key,
            elapsed_ms: simulateElapsed,
            completed_simulations: workerState.completed_simulations,
            total_simulations: workerState.total_simulations
          });
        }
        const now = performance.now();
        if (
          batchCompleted === batchTotal
          || (workerState.completed_simulations % progressEvery === 0 && now - workerState.last_progress_at >= progressMinMs)
        ) {
          workerState.last_progress_at = now;
          postMessage({
            type: 'progress',
            run_id: runId,
            worker_index: workerIndex,
            batch_id: batchId,
            stage: 'simulate',
            completed_simulations: workerState.completed_simulations,
            batch_completed_simulations: batchCompleted,
            batch_total_simulations: batchTotal,
            total_simulations: workerState.total_simulations,
            message: `${batchCompleted} / ${batchTotal}`
          });
        }
      }
      rows.push({ candidate_id: candidate.candidate_id, candidate_key: candidate.key, observations });
    }
    workerState.busy = false;
    postMessage({
      type: 'batch_done',
      run_id: runId,
      worker_index: workerIndex,
      batch_id: batchId,
      rows,
      completed_simulations: workerState.completed_simulations,
      batch_completed_simulations: batchCompleted,
      batch_total_simulations: batchTotal,
      simulate_elapsed_ms_sum: simulateElapsedMsSum,
      simulate_elapsed_ms_max: simulateElapsedMsMax,
      slow_simulation_count: slowSimulationCount,
      chunk_size: candidates.length,
      elapsed_ms: performance.now() - started
    });
  } catch (error) {
    workerState.busy = false;
    throw error;
  }
}

async function finishRun(workerIndex, runId) {
  if (!workerState || workerState.runId !== runId) return;
  await gate(workerIndex, runId);
  postMessage({
    type: 'done',
    run_id: runId,
    worker_index: workerIndex,
    completed_simulations: workerState.completed_simulations,
    elapsed_ms: performance.now() - workerState.started
  });
}

// ── LEAPS GA ─────────────────────────────────────────────────────────

async function handleLeapsGa(message) {
  const tTotal = performance.now();
  const { packet, run_id } = message;
  const { priceSeriesBySymbol, config, paramRanges } = packet || {};

  if (!priceSeriesBySymbol || !Object.keys(priceSeriesBySymbol).length) {
    postMessage({ type: 'leaps_ga_error', run_id, message: 'No price data' });
    return;
  }

  // Log data size
  let totalPoints = 0;
  for (const pts of Object.values(priceSeriesBySymbol)) totalPoints += pts.length;
  console.log('[leaps-ga] Data:', Object.keys(priceSeriesBySymbol).length, 'symbols,', totalPoints, 'points');

  const cfg = config || {};
  const popSize = cfg.population_size || 30;
  const generations = cfg.generations || 15;
  const mutationRate = cfg.mutation_rate || 0.15;
  const crossoverRate = cfg.crossover_rate || 0.80;
  const elitismCount = cfg.elitism_count || 3;
  const tournamentSize = cfg.tournament_size || 4;
  const seed = cfg.seed;
  const capitalMode = cfg.capital_mode || 'fixed';
  const totalCapital = cfg.total_capital || 10000;

  const ranges = leapsGaEngine.mergeRanges(paramRanges);
  const minEntryDate = packet.start || null;

  // Pre-parse dates once: [dateStr, price] -> [ts, price, dateStr]
  const parsed = {};
  for (const [sym, pts] of Object.entries(priceSeriesBySymbol)) {
    parsed[sym] = pts.map(([d, p]) => [new Date(d).getTime(), p, d]);
  }

  // Use native Math.random (seeded PRNG is unreliable cross-browser)
  // Seed is stored in config for reproducibility metadata only

  const seenKeys = new Set();
  const population = [];
  while (population.length < popSize) {
    const ind = leapsGaEngine.randomIndividual(ranges);
    if (!seenKeys.has(ind.key)) { seenKeys.add(ind.key); population.push(ind); }
  }

  const fit0Start = performance.now();
  let fitnesses = population.map(ind => leapsGaEngine.leapsFitnessFn(ind, parsed, capitalMode, totalCapital, minEntryDate));
  console.log('[leaps-ga] init_pop:', population.length, 'first_fitness:', Math.round(performance.now() - fit0Start) + 'ms');
  const snapshots = [];
  let bestIndividual = population[0];
  let bestFitness = fitnesses[0];
  const allEvaluated = new Map();

  for (let gen = 0; gen < generations; gen++) {
    if (paused) { while (paused && !cancelled) { await sleep(200); } }
    if (cancelled) { postMessage({ type: 'leaps_ga_cancelled', run_id }); return; }

    const ranked = population.map((ind, i) => [ind, fitnesses[i]]).sort((a, b) => b[1] - a[1]);
    population.length = 0; fitnesses.length = 0;
    for (const [ind, fit] of ranked) { population.push(ind); fitnesses.push(fit); }

    for (const [ind, fit] of ranked) {
      const existing = allEvaluated.get(ind.key);
      if (!existing || fit > existing[1]) allEvaluated.set(ind.key, [ind, fit]);
    }

    const genBest = fitnesses[0];
    const genAvg = fitnesses.reduce((a, b) => a + b, 0) / fitnesses.length;
    const genWorst = fitnesses[fitnesses.length - 1];
    if (genBest > bestFitness) { bestFitness = genBest; bestIndividual = population[0]; }

    snapshots.push({
      generation: gen + 1, best_fitness: genBest, avg_fitness: genAvg,
      worst_fitness: genWorst, best_key: bestIndividual.key,
      best_params: {
        drawdown_threshold_pct: bestIndividual.drawdown_threshold_pct,
        entry_mode: bestIndividual.entry_mode,
        stage1_days: bestIndividual.stage1_days, stage1_profit: bestIndividual.stage1_profit,
        stage1_sell: bestIndividual.stage1_sell, stage2_days: bestIndividual.stage2_days,
        stage2_profit: bestIndividual.stage2_profit, stage2_sell: bestIndividual.stage2_sell,
        position_pct: bestIndividual.position_pct, cooldown_days: bestIndividual.cooldown_days,
      },
    });

    postMessage({ type: 'leaps_ga_progress', run_id, generation: gen + 1, total_generations: generations, best_fitness: genBest, avg_fitness: genAvg });

    const breedStart = performance.now();
    const elites = population.slice(0, elitismCount);
    const nextPop = [...elites];
    while (nextPop.length < popSize) {
      const p1 = leapsGaEngine.tournamentsSelect(population, fitnesses, tournamentSize);
      const p2 = leapsGaEngine.tournamentsSelect(population, fitnesses, tournamentSize);
      let child = Math.random() < crossoverRate ? leapsGaEngine.leapsCrossover(p1, p2, ranges) : p1;
      child = leapsGaEngine.leapsMutate(child, mutationRate, ranges);
      nextPop.push(child);
    }
    population.length = 0;
    for (const ind of nextPop.slice(0, popSize)) population.push(ind);
    const breedMs = Math.round(performance.now() - breedStart);

    const fitStart = performance.now();
    fitnesses = population.map(ind => leapsGaEngine.leapsFitnessFn(ind, parsed, capitalMode, totalCapital, minEntryDate));
    const fitMs = Math.round(performance.now() - fitStart);
    console.log('[leaps-ga] gen', gen + 1, 'breed:', breedMs + 'ms', 'fit:', fitMs + 'ms', 'best:', genBest.toFixed(1));
  }

  const rankedFinal = [...allEvaluated.entries()].sort((a, b) => b[1][1] - a[1][1]);
  const finalPop = [];
  for (let rank = 0; rank < rankedFinal.length; rank++) {
    const [key, [ind, fit]] = rankedFinal[rank];
    const totalRoi = leapsGaEngine.leapsTotalRoi(ind, parsed, capitalMode, totalCapital, minEntryDate);
    const row = {
      rank: rank + 1, key, fitness: fit,
      total_roi: totalRoi,
      drawdown_threshold_pct: ind.drawdown_threshold_pct, entry_mode: ind.entry_mode,
      stage1_days: ind.stage1_days, stage1_profit: ind.stage1_profit, stage1_sell: ind.stage1_sell,
      stage2_days: ind.stage2_days, stage2_profit: ind.stage2_profit, stage2_sell: ind.stage2_sell,
      position_pct: ind.position_pct, cooldown_days: ind.cooldown_days,
    };
    // Add capital-mode-specific metrics
    if (capitalMode === 'fixed') {
      const capResult = leapsGaEngine._leapsEvalFixedCapital(ind, parsed, totalCapital, minEntryDate);
      row.final_equity = capResult.final_equity;
      row.cagr = capResult.cagr;
      row.max_drawdown_pct = capResult.max_drawdown_pct;
      row.trade_count = capResult.trade_count;
    } else {
      const unlResult = leapsGaEngine._leapsEvalUnlimited(ind, parsed, minEntryDate);
      row.annualized_geo = unlResult.annualized_geo;
      row.trade_count = unlResult.trade_count;
      const tc = unlResult.total_opt_cost || 0;
      const tr = unlResult.total_opt_revenue || 0;
      row.input_output_ratio = tc > 0 ? Math.round(tr / tc * 1e4) / 1e4 : 0;
    }
    if (rank < 10) {
      row.trade_details = collectLeapsTradeDetails(ind, parsed, {
        executedOnly: capitalMode === 'fixed',
        totalCapital: totalCapital,
        minEntryDate: minEntryDate,
      });
    }
    finalPop.push(row);
  }

  // Compute filtered entries for the best individual
  var filteredEntries = [];
  if (bestIndividual) {
    var bestStages = bestIndividual.toStages();
    var minHold = bestStages.length ? Math.min.apply(null, bestStages.map(function(s) { return s[0]; })) : 0;
    for (var sym in parsed) {
      var prices = parsed[sym];
      var maxDate = new Date(Math.max.apply(null, prices.map(function(p) { return p[0]; })));
      var entries = leapsGaEngine.detectLeapsEntries(prices, bestIndividual.drawdown_threshold_pct, bestIndividual.entry_mode, minEntryDate || null);
      for (var ei = 0; ei < entries.length; ei++) {
        var e = entries[ei];
        var minHoldDate = new Date(new Date(e.date).getTime() + minHold * 86400000);
        if (minHoldDate > maxDate) {
          filteredEntries.push({
            symbol: sym,
            date: e.date,
            price: Math.round(e.price * 100) / 100,
            drawdown_pct: Math.round(e.drawdown_pct * 10) / 10,
            days_to_end: Math.round((maxDate.getTime() - new Date(e.date).getTime()) / 86400000),
            min_hold_days: minHold,
            reason: '数据不足：距末尾仅' + Math.round((maxDate.getTime() - new Date(e.date).getTime()) / 86400000) + '天，需≥' + minHold + '天',
          });
        }
      }
    }
  }

  postMessage({
    type: 'leaps_ga_done', run_id,
    result: { snapshots, best: finalPop[0] || null, final_population: finalPop, total_evaluated: allEvaluated.size, filtered_entries: filteredEntries,
      config: { population_size: popSize, generations, mutation_rate: mutationRate, crossover_rate: crossoverRate, elitism_count: elitismCount, tournament_size: tournamentSize, seed, capital_mode: capitalMode, total_capital: totalCapital } },
  });
}

// Unified trade detail collector: handles both fixed capital (executedOnly) and unlimited modes
function collectLeapsTradeDetails(individual, parsedPriceData, opts) {
  const { executedOnly, totalCapital, minEntryDate } = opts || {};
  let tradeList;
  if (executedOnly) {
    const capResult = leapsGaEngine._leapsEvalFixedCapital(individual, parsedPriceData, totalCapital || 10000, minEntryDate || null);
    tradeList = capResult.executed_trades || [];
  } else {
    tradeList = [];
    for (const [symbol, prices] of Object.entries(parsedPriceData)) {
      const entries = leapsGaEngine.detectLeapsEntries(prices, individual.drawdown_threshold_pct, individual.entry_mode, minEntryDate || null);
      const stages = individual.toStages();
      // Filter entries that can't reach minimum hold days before data ends
      const maxPriceDate = new Date(Math.max(...prices.map(p => p[0])));
      const minHoldDays = stages.length ? Math.min(...stages.map(s => s[0])) : 0;
      for (const entry of entries) {
        const minHoldDate = new Date(new Date(entry.date).getTime() + minHoldDays * 86400000);
        if (minHoldDate > maxPriceDate) continue;
        const trade = leapsGaEngine.computeSellLadder(entry, prices, stages, 190, entry.price * 1.1);
        trade.symbol = symbol;
        tradeList.push(trade);
      }
    }
  }

  const trades = [];
  for (const [symbol, prices] of Object.entries(parsedPriceData)) {
    const bbFull = leapsGaEngine.bollingerLowerBand(prices, 22, 2.0);
    const bbByDate = {};
    for (const b of bbFull) bbByDate[b.date] = b.band;
    for (const trade of tradeList) {
      if (trade.symbol !== symbol) continue;
      const entry = trade.entry;
      const allDates = [entry.date, ...trade.sell_events.map(se => se.date)].sort();
      const ss = new Date(allDates[0]); ss.setDate(ss.getDate() - 60);
      const se = new Date(allDates[allDates.length - 1]); se.setDate(se.getDate() + 30);
      const ssTs = ss.getTime(), seTs = se.getTime();
      const ps = [];
      for (const [ts, p, d] of prices) {
        if (ts >= ssTs && ts <= seTs) {
          const pt = { date: d, price: p };
          if (bbByDate[d] != null) pt.bollinger_lower = bbByDate[d];
          ps.push(pt);
        }
      }
      trades.push({ symbol, entry_date: entry.date, entry_price: entry.price, drawdown_pct: entry.drawdown_pct, bollinger_score: entry.bollinger_score, composite_score: entry.composite_score, sell_events: trade.sell_events, expired: trade.expired, total_roi_pct: trade.total_roi_pct, open_pct: trade.open_pct || 0, unrealized_roi_pct: trade.unrealized_roi_pct || 0, price_series: ps });
    }
  }
  return trades;
}
