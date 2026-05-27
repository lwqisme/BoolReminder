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
  'grid_sell_pct',
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
  'buy_rearm_mode',
  'sell_stage_rearm_drawdown_pct'
];

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

function gridSellPct(params) {
  if (!params) return 0;
  if (params.grid_sell_pct !== null && params.grid_sell_pct !== undefined) return params.grid_sell_pct;
  return params.grid_second_sell_pct;
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
    parts.push(`gsell${formatCompact(gridSellPct(sellParams))}`);
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
  if (sellParams.buy_rearm_mode === 'restart_from_rearm') parts.push('rearmmode_restart');
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

function buildSellLabel(strategyKey, params, labels = {}, baseInputs = {}) {
  let label;
  if (strategyKey === 'repair_step') {
    label = `阶梯修复 ${formatCompact(params.sell_min_profit_pct)}%盈利 ${formatCompact(Math.trunc(num(params.repair_sell_cooldown_days)))}日冷却 ${formatCompact(params.repair_stage_sell_pct)}%单档`;
  } else if (strategyKey === 'grid_rebound') {
    label = `网格回弹 ${formatCompact(params.grid_rebound_step_pct)}%步长 每档${formatCompact(gridSellPct(params))}%卖出 ${formatCompact(params.sell_min_profit_pct ?? baseInputs.sell_min_profit_pct)}%最小盈利`;
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
  if (params.buy_rearm_mode === 'restart_from_rearm') label = `${label} / 重启后从首档`;
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
  const sellLabel = buildSellLabel(sellStrategy, sellVariant, sellLabels, packet.inputs || {});
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

async function simulateBatchViaBackend(candidates, packet) {
  const payload = {
    inputs: packet.inputs || {},
    tasks: (packet.tasks || []).map(t => {
      const targets = t.targets || [];
      const symbols = targets.map(tgt => tgt.symbol).filter(Boolean);
      const uniqueSymbols = [...new Set(symbols)];
      return uniqueSymbols.map(symbol => {
        const tgt = targets.find(t2 => t2.symbol === symbol);
        return {
          symbol,
          start: t.start,
          end: t.end,
          weight: tgt ? tgt.weight : 100
        };
      });
    }).flat(),
    candidates: candidates.map(c => ({
      key: c.key || c.variant_key || '',
      variant_key: c.variant_key || '',
      buy_strategy: c.buy_strategy,
      sell_strategy: c.sell_strategy,
      parameters: c.parameters || c
    })),
    include_trades: Boolean(workerState?.include_trades),
    include_series: Boolean(workerState?.include_series)
  };

  const response = await fetch('/api/strategy-lab/parameter-lab/evaluate-batch', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload)
  });
  const data = await response.json();
  if (!response.ok || !data.success) {
    throw new Error(data.message || 'Backend evaluation failed');
  }
  return data.results;
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
  const started = performance.now();
  const rows = [];

  postMessage({
    type: 'batch_start',
    run_id: runId,
    worker_index: workerIndex,
    batch_id: batchId,
    candidate_count: candidates.length,
    task_count: taskContexts.length,
    batch_total_simulations: candidates.length * Math.max(1, taskContexts.length),
    completed_simulations: workerState.completed_simulations,
    total_simulations: workerState.total_simulations
  });

  try {
    await gate(workerIndex, runId);
    const backendResults = await simulateBatchViaBackend(candidates, workerState.packet);

    for (let i = 0; i < candidates.length; i += 1) {
      if (cancelled) break;
      await gate(workerIndex, runId);

      const candidate = candidates[i];
      const backendResult = backendResults[i] || {};
      const observations = [];

      const taskResults = backendResult.results || [];
      for (let taskIndex = 0; taskIndex < taskContexts.length; taskIndex += 1) {
        const task = taskContexts[taskIndex];
        const r = taskResults[taskIndex] || {};

        if (r.error) {
          observations.push({
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
            error: r.error
          });
        } else {
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
            return_pct: r.return_pct || 0,
            max_drawdown_pct: r.max_drawdown_pct || 0,
            trade_count: r.trade_count || 0,
            buy_count: r.buy_count || 0,
            sell_count: r.sell_count || 0,
            avg_buy_drawdown_pct: r.avg_buy_drawdown_pct || 0,
            avg_sell_drawdown_pct: r.avg_sell_drawdown_pct || 0,
            avg_sell_profit_pct: r.avg_sell_profit_pct || 0,
            cash_reuse_pct: r.cash_reuse_pct || 0,
            avg_cash_pct: r.avg_cash_pct || 0,
            sell_quality_score: r.sell_quality_score || 0,
            contribution_count: r.contribution_count || 0,
            final_value: r.final_value || 0,
            total_contributed: r.total_contributed || 0
          };
          if (workerState.include_trades && r.trade_log) obs.trade_log = r.trade_log;
          if (workerState.include_series && r.series) obs.series = r.series;
          observations.push(obs);
        }

        workerState.completed_simulations += 1;
      }

      rows.push({ candidate_id: candidate.candidate_id, candidate_key: candidate.key, observations });

      postMessage({
        type: 'progress',
        run_id: runId,
        worker_index: workerIndex,
        batch_id: batchId,
        stage: 'simulate',
        completed_simulations: workerState.completed_simulations,
        batch_completed_simulations: i + 1,
        batch_total_simulations: candidates.length,
        total_simulations: workerState.total_simulations,
        message: `${i + 1} / ${candidates.length}`
      });
    }

    workerState.busy = false;
    postMessage({
      type: 'batch_done',
      run_id: runId,
      worker_index: workerIndex,
      batch_id: batchId,
      rows,
      completed_simulations: workerState.completed_simulations,
      batch_completed_simulations: candidates.length,
      batch_total_simulations: candidates.length * Math.max(1, taskContexts.length),
      simulate_elapsed_ms_sum: 0,
      simulate_elapsed_ms_max: 0,
      slow_simulation_count: 0,
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
