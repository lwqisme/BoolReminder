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
  'core_dip_timing_near_low_pct'
];

const SELL_PARAMETER_FIELDS = [
  'sell_min_profit_pct',
  'repair_sell_cooldown_days',
  'repair_stage_sell_pct',
  'grid_rebound_step_pct',
  'grid_first_sell_pct',
  'grid_second_sell_pct',
  'grid_min_sell_amount',
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

function rebuildPricePoints(dates, closes, start, end) {
  const rows = [];
  const startKey = String(start || '');
  const endKey = String(end || '');
  for (let index = 0; index < dates.length; index += 1) {
    const day = String(dates[index] || '');
    if (!day) continue;
    if (startKey && day < startKey) continue;
    if (endKey && day > endKey) continue;
    const close = num(closes[index]);
    if (close <= 0) continue;
    rows.push({ date: day, close });
  }
  let rollingPeak = -Infinity;
  for (let index = 0; index < rows.length; index += 1) {
    const point = rows[index];
    rollingPeak = Math.max(rollingPeak, point.close);
    let windowPeak = -Infinity;
    const windowStart = Math.max(0, index - 119);
    for (let windowIndex = windowStart; windowIndex <= index; windowIndex += 1) {
      windowPeak = Math.max(windowPeak, rows[windowIndex].close);
    }
    point.rolling_peak = rollingPeak;
    point.drawdown_ath = rollingPeak > 0 ? point.close / rollingPeak - 1 : 0;
    point.rolling_120_peak = windowPeak;
    point.drawdown_120 = windowPeak > 0 ? point.close / windowPeak - 1 : 0;
  }
  return rows;
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
    pricePoints[symbol] = rebuildPricePoints(dates, closes, task.start, task.end);
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
      parts.push(`ctd${formatCompact(Math.trunc(num(buyParams.core_dip_timing_max_delay_days)))}`);
      parts.push(`ctr${formatCompact(buyParams.core_dip_timing_rise_threshold_pct)}`);
      parts.push(`ctl${formatCompact(buyParams.core_dip_timing_near_low_pct)}`);
    }
  }
  parts.push(sellStrategy);
  if (sellStrategy === 'repair_step') {
    parts.push(`p${formatCompact(sellParams.sell_min_profit_pct)}`);
    parts.push(`c${formatCompact(Math.trunc(num(sellParams.repair_sell_cooldown_days)))}`);
    parts.push(`s${formatCompact(sellParams.repair_stage_sell_pct)}`);
  }
  if (sellStrategy === 'grid_rebound') {
    parts.push(`g${formatCompact(sellParams.grid_rebound_step_pct)}`);
    parts.push(`g1${formatCompact(sellParams.grid_first_sell_pct)}`);
    parts.push(`g2${formatCompact(sellParams.grid_second_sell_pct)}`);
    parts.push(`gmin${formatCompact(sellParams.grid_min_sell_amount)}`);
  }
  if (sellStrategy === 'cost_deleverage') {
    const profits = [
      sellParams.cost_first_profit_pct,
      sellParams.cost_second_profit_pct,
      sellParams.cost_third_profit_pct
    ];
    const sells = [
      sellParams.cost_first_sell_pct,
      sellParams.cost_second_sell_pct,
      sellParams.cost_third_sell_pct
    ];
    parts.push(`cp${profits.map(formatCompact).join('-')}`);
    parts.push(`cs${sells.map(formatCompact).join('-')}`);
    parts.push(`cc${formatCompact(Math.trunc(num(sellParams.cost_deleverage_cooldown_days)))}`);
    parts.push(`cmin${formatCompact(sellParams.cost_min_sell_amount)}`);
  }
  if (sellStrategy !== 'none' && sellParams.sell_allow_same_day_sell) parts.push('same1');
  if (sellParams.dca_rearm_drawdown_pct !== null && sellParams.dca_rearm_drawdown_pct !== undefined) {
    parts.push(`rearm${formatCompact(sellParams.dca_rearm_drawdown_pct)}`);
  }
  if (sellParams.sell_stage_rearm_drawdown_pct !== null && sellParams.sell_stage_rearm_drawdown_pct !== undefined) {
    parts.push(`sellrearm${formatCompact(sellParams.sell_stage_rearm_drawdown_pct)}`);
  }
  return parts.join('__');
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

function buildSellLabel(strategyKey, params, labels = {}) {
  let label;
  if (strategyKey === 'repair_step') {
    label = `阶梯修复 ${formatCompact(params.sell_min_profit_pct)}%盈利 ${formatCompact(Math.trunc(num(params.repair_sell_cooldown_days)))}日冷却 ${formatCompact(params.repair_stage_sell_pct)}%单档`;
  } else if (strategyKey === 'grid_rebound') {
    label = `网格回弹 ${formatCompact(params.grid_rebound_step_pct)}%步长 ${formatCompact(params.grid_first_sell_pct)}%+${formatCompact(params.grid_second_sell_pct)}%卖出`;
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
    label = `成本去杠杆 ${profits.map((value) => `${formatCompact(value)}%`).join('/')} 盈利 ${sells.map((value) => `${formatCompact(value)}%`).join('+')} 卖出 ${formatCompact(Math.trunc(num(params.cost_deleverage_cooldown_days)))}日冷却`;
  } else {
    label = labels[strategyKey] || strategyKey;
  }
  if (strategyKey !== 'none' && params.sell_allow_same_day_sell) label = `${label} / 买入日可卖`;
  if (params.dca_rearm_drawdown_pct !== null && params.dca_rearm_drawdown_pct !== undefined) {
    label = `${label} / 卖后重启 ${formatCompact(params.dca_rearm_drawdown_pct)}%回撤`;
  }
  if (params.sell_stage_rearm_drawdown_pct !== null && params.sell_stage_rearm_drawdown_pct !== undefined) {
    label = `${label} / 卖档重启 ${formatCompact(params.sell_stage_rearm_drawdown_pct)}%回撤`;
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
  const sellLabel = buildSellLabel(sellStrategy, sellVariant, sellLabels);
  const candidate = {
    key: buildCandidateKey(buyStrategy, sellStrategy, buyVariant, sellVariant),
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

function rearmAfterDcaBuy(state, drawdown, inputs, sellStrategy) {
  if (!['repair_step', 'grid_rebound', 'cost_deleverage'].includes(sellStrategy)) return false;
  if (!Object.keys(state.sell_marks).length) return false;
  const rawThreshold = inputs.sell_stage_rearm_drawdown_pct ?? inputs.dca_rearm_drawdown_pct;
  if (drawdown + 1e-9 < Math.min(Math.max(0, num(rawThreshold)), num(inputs.max_drawdown_pct))) return false;
  state.sell_marks = {};
  if (sellStrategy === 'grid_rebound') state.grid_rebound_cycle_anchor_drawdown_pct = null;
  if (sellStrategy === 'cost_deleverage') state.cost_deleverage_cycle_anchor_price = null;
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
    const anchor = gridReboundCycleAnchor(state);
    for (const [mark, threshold, sellPct] of [
      ['grid_1', Math.max(0, anchor - num(inputs.grid_rebound_step_pct)), num(inputs.grid_first_sell_pct)],
      ['grid_2', Math.max(0, anchor - num(inputs.grid_rebound_step_pct) * 2), num(inputs.grid_second_sell_pct)]
    ]) {
      if (state.sell_marks[mark] || drawdown > threshold + 1e-9) continue;
      if (sellShares(state, point, state.shares * sellPct / 100, inputs, tradeLog, sellStrategy, threshold, num(inputs.grid_min_sell_amount))) {
        state.sell_marks[mark] = true;
        if (mark === 'grid_2' && drawdown > 1e-9 && state.shares > 0) {
          state.sell_marks = {};
          state.grid_rebound_cycle_anchor_drawdown_pct = drawdown;
        }
        return;
      }
    }
  } else if (sellStrategy === 'cost_deleverage') {
    if (num(inputs.cost_deleverage_cooldown_days) > 0 && state.last_cost_deleverage_sell_trade_index !== null && tradeIndex - state.last_cost_deleverage_sell_trade_index < num(inputs.cost_deleverage_cooldown_days)) return;
    const anchor = costDeleverageCycleAnchor(state);
    if (anchor <= 0) return;
    const profit = current / anchor * 100 - 100;
    for (const [mark, threshold, sellPct] of [
      ['cost_1', num(inputs.cost_first_profit_pct), num(inputs.cost_first_sell_pct)],
      ['cost_2', num(inputs.cost_second_profit_pct), num(inputs.cost_second_sell_pct)],
      ['cost_3', num(inputs.cost_third_profit_pct), num(inputs.cost_third_sell_pct)]
    ]) {
      if (state.sell_marks[mark] || profit < Math.max(threshold, num(inputs.sell_min_profit_pct))) continue;
      if (sellShares(state, point, state.shares * sellPct / 100, inputs, tradeLog, sellStrategy, threshold, num(inputs.cost_min_sell_amount))) {
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
    sell_stage_rearm_drawdown_pct: candidate.sell_stage_rearm_drawdown_pct ?? base.sell_stage_rearm_drawdown_pct,
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
    sell_allow_same_day_sell: candidate.sell_allow_same_day_sell ?? base.sell_allow_same_day_sell,
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
      cost_deleverage_cycle_anchor_price: null,
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
        const tranches = tranchesBySymbol[symbol] || [];
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
  const metrics = sellMetrics(tradeLog, portfolioValues, cashValues);
  const result = {
    return_pct: totalContributed > 0 ? pct(finalValue / totalContributed - 1) : 0,
    max_drawdown_pct: maxDrawdown(portfolioValues),
    trade_count: Object.values(states).reduce((sum, state) => sum + state.trades, 0),
    contribution_count: contributionCount,
    leaps_signal: summarizeLeapsSignals(tradeLog, inputs, Boolean(workerState?.include_leaps_signal_details), task),
    ...metrics
  };
  if (workerState?.include_trades) result.trade_log = tradeLog;
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
        try {
          metrics = simulate(task, workerState.inputs, candidate);
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
