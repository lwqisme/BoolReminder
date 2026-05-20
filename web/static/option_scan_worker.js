/** Option parameter scan Web Worker — wallet-mode client-side replay. */

let paused = false;
let cancelled = false;
let activeRunId = '';

self.onmessage = (event) => {
  const msg = event.data || {};
  const runId = msg.run_id || activeRunId || '';
  if (msg.type === 'pause') { paused = true; }
  if (msg.type === 'resume') { paused = false; }
  if (msg.type === 'cancel') { cancelled = true; }
  if (msg.type === 'start-option-scan') {
    paused = false;
    cancelled = false;
    activeRunId = runId;
    runScan(msg, runId).catch((err) => postError(err, runId, msg.worker_index || 0));
  }
};

function postError(err, runId, workerIndex) {
  postMessage({
    type: 'error', run_id: runId, worker_index: workerIndex,
    message: err && err.message ? err.message : String(err)
  });
}

async function gate(runId, workerIndex) {
  if (cancelled) throw new Error('__cancelled__');
  while (paused) {
    postMessage({ type: 'progress', run_id: runId, worker_index: workerIndex, stage: 'paused', paused: true });
    await new Promise(r => setTimeout(r, 160));
    if (cancelled) throw new Error('__cancelled__');
  }
}

const num = (v, fallback) => { const n = Number(v); return Number.isFinite(n) ? n : (fallback ?? 0); };

// ── Wallet-mode replay ──────────────────────────────────────────────────

function _exitValue(contracts, price, fee) {
  const gross = contracts * price * 100;
  const applied = Math.min(fee, gross);
  return { value: gross - applied, fee: applied };
}

/**
 * Simulate one strategy's trades through an option wallet.
 *
 * Wallet principal = wallet_pct/100 × (initial_cash + n × monthly_contribution)
 * Buy budget per signal = wallet.cash × trade_allocation_pct/100
 */
function replayOptionWallet(trades, lookup, settings, endDate, stockInputs) {
  const initialCash = num(stockInputs?.initial_cash, 20000);
  const monthlyContribution = num(stockInputs?.monthly_contribution, 1000);
  const walletPct = settings.wallet_pct / 100;
  const tradeAllocPct = settings.trade_allocation_pct / 100;

  // Initialize wallet
  const wallet = {
    cash: walletPct * initialCash,
    positions: [],
    realizedPnl: 0,
    totalPremiumPaid: 0,
    totalFeesPaid: 0,
    monthlyInjected: 0
  };

  const skipped = [];
  const positionsOut = [];

  // Build events from trades
  const events = [];
  const stockSellDates = {};  // symbol → [date, ...]
  for (const t of trades) {
    const d = String(t.date || '');
    if (!d) continue;
    const action = String(t.action || '');
    if (action === 'sell') {
      (stockSellDates[t.symbol] = stockSellDates[t.symbol] || []).push(d);
    }
    if (action === 'buy' || action === 'sell') {
      events.push({ date: d, type: action, trade: t });
    }
  }
  events.sort((a, b) => a.date.localeCompare(b.date));
  // Sort sell dates per symbol
  for (const sym of Object.keys(stockSellDates)) {
    stockSellDates[sym].sort();
  }

  if (events.length === 0) {
    return { wallet, positions: [], skipped: [{ status: 'skipped', reason: 'no_trades' }], metrics: walletMetrics(wallet, 0) };
  }

  // Determine simulation start
  const simStart = events[0].date;

  // Build monthly injection schedule
  const monthlyDates = [];
  const startD = new Date(simStart + 'T00:00:00Z');
  let d = new Date(Date.UTC(startD.getUTCFullYear(), startD.getUTCMonth(), 1));
  const endD = new Date(endDate + 'T00:00:00Z');
  while (d <= endD) {
    if (d > startD) {
      monthlyDates.push(d.toISOString().slice(0, 10));
    }
    // next month
    d = new Date(Date.UTC(d.getUTCFullYear(), d.getUTCMonth() + 1, 1));
  }
  const monthlyInjection = walletPct * monthlyContribution;
  let monthlyIdx = 0;
  let lastBarProcessDate = null;

  function closePositionContracts(pos, contracts, price, exitDate, reason, dte) {
    const ev = _exitValue(contracts, price, settings.trade_fee);
    pos.remaining_contracts = (pos.remaining_contracts || 0) - contracts;
    pos.realized_value = (pos.realized_value || 0) + ev.value;
    pos.fees = (pos.fees || 0) + ev.fee;
    (pos.exits = pos.exits || []).push({
      reason, date: exitDate, price, contracts, value: ev.value, fee: ev.fee, dte
    });
    wallet.cash += ev.value;
    wallet.realizedPnl += (ev.value - pos._premium_per_contract * contracts);
    wallet.totalFeesPaid += ev.fee;
    if ((pos.remaining_contracts || 0) <= 0) {
      pos.remaining_contracts = 0;
      pos.status = 'closed';
    }
  }

  function processBarsThrough(limitDate) {
    const barDates = new Set();
    for (const pos of wallet.positions) {
      if (pos.status === 'closed') continue;
      for (const bar of (pos._bars || [])) {
        if (bar.date < pos._buy_date || bar.date > limitDate) continue;
        if (lastBarProcessDate && bar.date <= lastBarProcessDate) continue;
        barDates.add(bar.date);
      }
    }

    for (const day of [...barDates].sort()) {
      for (const pos of wallet.positions) {
        if (pos.status === 'closed') continue;
        let remaining = pos.remaining_contracts || 0;
        if (remaining <= 0) { pos.status = 'closed'; continue; }
        if (day < pos._buy_date) continue;

        const bar = (pos._bars || []).find(b => b.date === day);
        if (!bar) continue;

        pos.last_mark_price = bar.close;
        pos.last_price = bar.close;
        pos.last_date = bar.date;
        const expiration = pos.expiration || pos._contract?.expiration || '';
        const dte = expiration ? Math.round((new Date(expiration + 'T00:00:00Z') - new Date(day + 'T00:00:00Z')) / 86400000) : 0;
        pos._dte_at_date = dte;

        const profitTaken = (pos.exits || []).some(ex => ex.reason === 'profit_take');
        if (!profitTaken && bar.close >= pos.entry_price * (1 + settings.profit_take_pct / 100)) {
          const sellCtr = Math.floor(remaining * settings.profit_take_sell_pct / 100);
          if (sellCtr > 0) {
            closePositionContracts(pos, sellCtr, bar.close, day, 'profit_take', Math.round(dte));
            remaining = pos.remaining_contracts || 0;
          }
        }

        if (remaining <= 0) continue;

        if (dte <= settings.exit_dte) {
          closePositionContracts(pos, remaining, bar.close, day, 'dte_exit', Math.round(dte));
        }
      }
    }
    lastBarProcessDate = limitDate;
  }

  // Process events in order
  for (const evt of events) {
    // Inject monthly contributions up to event date
    while (monthlyIdx < monthlyDates.length && monthlyDates[monthlyIdx] <= evt.date) {
      wallet.cash += monthlyInjection;
      wallet.monthlyInjected += monthlyInjection;
      monthlyIdx++;
    }

    processBarsThrough(evt.date);

    if (evt.type === 'sell') {
      // Stock sell → close all option positions for that symbol
      const und = _baseSymbol(String(evt.trade.symbol || ''));
      // Mark all open positions for this underlying as closed (stock_sell exit)
      for (let i = wallet.positions.length - 1; i >= 0; i--) {
        const p = wallet.positions[i];
        if (p.status === 'closed') continue;
        if (p.underlying !== und) continue;
        const rem = p.remaining_contracts || 0;
        if (rem <= 0) continue;
        // Use bar close on the sell date when available; fall back to last_mark_price
        const sellBar = (p._bars || []).find(b => b.date === evt.date);
        const mark = sellBar ? sellBar.close : (p.last_mark_price || 0);
        closePositionContracts(p, rem, mark, evt.date, 'stock_sell', p._dte_at_date || 0);
      }
      continue;
    }

    // evt.type === 'buy'
    const underlying = _baseSymbol(String(evt.trade.symbol || ''));
    if (!['TSM', 'GOOGL', 'TSLA'].includes(underlying)) continue;

    // Check budget
    const budget = wallet.cash * tradeAllocPct;
    if (budget <= settings.trade_fee) {
      skipped.push({ status: 'skipped', reason: 'wallet_insufficient', underlying,
        stock_symbol: evt.trade.symbol, stock_buy_date: evt.date,
        stock_buy_amount: num(evt.trade.gross_amount) });
      continue;
    }

    // Look up contract
    const lookupKey = `${underlying}|${evt.date}|${settings.moneyness}|${settings.target_dte}`;
    const entry = lookup[lookupKey];
    if (!entry) {
      // Debug: report first contract_not_found to help diagnose key mismatches
      if (!skipped.some(s => s.reason === 'contract_not_found')) {
        const availKeys = Object.keys(lookup).filter(k => k.startsWith(underlying + '|')).slice(0, 6);
        try {
          postMessage({
            type: 'debug',
            message: `contract_not_found: lookupKey="${lookupKey}" availKeys=[${availKeys.join(', ')}] total=${Object.keys(lookup).length}`
          });
        } catch (e) { /* ignore postMessage errors in replay context */ }
      }
      skipped.push({ status: 'skipped', reason: 'contract_not_found', underlying,
        stock_symbol: evt.trade.symbol, stock_buy_date: evt.date,
        stock_buy_amount: num(evt.trade.gross_amount) });
      continue;
    }

    const contract = entry.contract;
    const bars = entry.bars || [];

    // Find entry bar
    let entryBar = null;
    for (const b of bars) {
      if (b.date >= evt.date && b.close > 0) { entryBar = b; break; }
    }
    if (!entryBar) {
      try {
        postMessage({
          type: 'debug',
          message: `entry_price_not_found: ticker=${contract.ticker} buy_date=${evt.date} bars=${bars.length} first_bar=${bars[0]?.date||'none'}`
        });
      } catch (e) { /* ignore */ }
      skipped.push({ status: 'skipped', reason: 'entry_price_not_found', underlying,
        stock_symbol: evt.trade.symbol, stock_buy_date: evt.date,
        stock_buy_amount: num(evt.trade.gross_amount),
        option_ticker: contract.ticker, expiration: contract.expiration, strike: contract.strike });
      continue;
    }

    const entryPrice = entryBar.close;
    const entryFee = Math.min(settings.trade_fee, budget);
    let contracts = Math.floor((budget - entryFee) / (entryPrice * 100));
    if (contracts <= 0) {
      skipped.push({ status: 'skipped', reason: 'contracts_too_small', underlying,
        stock_symbol: evt.trade.symbol, stock_buy_date: evt.date,
        stock_buy_amount: num(evt.trade.gross_amount),
        option_ticker: contract.ticker, expiration: contract.expiration, strike: contract.strike });
      continue;
    }

    const premium = contracts * entryPrice * 100;
    const actualEntryFee = Math.min(settings.trade_fee, premium);
    const totalCost = premium + actualEntryFee;

    if (totalCost > wallet.cash) {
      skipped.push({ status: 'skipped', reason: 'wallet_insufficient', underlying,
        stock_symbol: evt.trade.symbol, stock_buy_date: evt.date,
        stock_buy_amount: num(evt.trade.gross_amount) });
      continue;
    }

    wallet.cash -= totalCost;
    wallet.totalPremiumPaid += premium;
    wallet.totalFeesPaid += actualEntryFee;

    const expiration = contract.expiration;
    const dteAtEntry = Math.round((new Date(expiration + 'T00:00:00Z') - new Date(entryBar.date + 'T00:00:00Z')) / 86400000);

    const pos = {
      status: 'open',
      underlying,
      stock_symbol: String(evt.trade.symbol || ''),
      stock_buy_date: evt.date,
      stock_buy_price: num(evt.trade.price),
      stock_buy_amount: num(evt.trade.gross_amount),
      wallet_pct: settings.wallet_pct,
      trade_allocation_pct: settings.trade_allocation_pct,
      option_ticker: contract.ticker,
      expiration,
      dte_at_entry: dteAtEntry,
      strike: contract.strike,
      moneyness: settings.moneyness,
      entry_date: entryBar.date,
      entry_price: entryPrice,
      premium,
      contracts,
      remaining_contracts: contracts,
      realized_value: 0,
      open_value: 0,
      total_value: 0,
      fees: actualEntryFee,
      return_pct: 0,
      exits: [],
      last_price: entryPrice,
      last_date: entryBar.date,
      last_mark_price: entryPrice,
      _premium_per_contract: premium / contracts,
      _contract: contract,
      _bars: bars,
      _buy_date: evt.date,
      _dte_at_date: dteAtEntry,
    };

    wallet.positions.push(pos);
    positionsOut.push(pos);
  }

  // Inject remaining monthly contributions
  processBarsThrough(endDate);
  while (monthlyIdx < monthlyDates.length && monthlyDates[monthlyIdx] <= endDate) {
    wallet.cash += monthlyInjection;
    wallet.monthlyInjected += monthlyInjection;
    monthlyIdx++;
  }

  // Backtest end exits any remaining option contracts.
  for (const pos of wallet.positions) {
    if (pos.status === 'closed') continue;
    const remaining = pos.remaining_contracts || 0;
    if (remaining <= 0) { pos.status = 'closed'; continue; }
    const mark = pos.last_mark_price || pos.entry_price || 0;
    const expiration = pos.expiration || pos._contract?.expiration || '';
    const dte = expiration ? Math.round((new Date(expiration + 'T00:00:00Z') - new Date(endDate + 'T00:00:00Z')) / 86400000) : 0;
    closePositionContracts(pos, remaining, mark, endDate, 'backtest_end', dte);
  }

  // ── Build price_bars for all positions (open + closed) ────────────
  for (const pos of wallet.positions) {
    if (pos._bars) {
      pos.price_bars = pos._bars.map(b => ({ date: b.date, close: b.close }));
    }
  }

  // ── Final mark-to-market (open positions only) ────────────────────
  let openValue = 0;
  for (const pos of wallet.positions) {
    const rem = pos.remaining_contracts || 0;
    const mark = pos.last_mark_price || 0;
    pos.open_value = rem * mark * 100;
    openValue += pos.open_value;
    pos.total_value = (pos.realized_value || 0) + pos.open_value;
    pos.return_pct = pos.premium > 0 ? (pos.total_value / pos.premium - 1) * 100 : 0;
  }

  return {
    wallet,
    positions: positionsOut,
    skipped,
    metrics: walletMetrics(wallet, openValue)
  };
}

function walletMetrics(wallet, openValue) {
  let optionTotalValue = wallet.positions.reduce((sum, pos) => sum + num(pos.total_value, 0), 0);
  if (optionTotalValue <= 0 && wallet.totalPremiumPaid > 0) {
    optionTotalValue = wallet.totalPremiumPaid + wallet.realizedPnl + openValue;
  }
  const walletTotalValue = wallet.cash + openValue;
  return {
    wallet_cash: Math.round(wallet.cash * 100) / 100,
    open_positions_value: Math.round(openValue * 100) / 100,
    realized_pnl: Math.round(wallet.realizedPnl * 100) / 100,
    total_premium_paid: Math.round(wallet.totalPremiumPaid * 100) / 100,
    total_fees_paid: Math.round(wallet.totalFeesPaid * 100) / 100,
    total_value: Math.round(optionTotalValue * 100) / 100,
    wallet_total_value: Math.round(walletTotalValue * 100) / 100,
    return_pct: wallet.totalPremiumPaid > 0 ? Math.round((optionTotalValue / wallet.totalPremiumPaid - 1) * 10000) / 100 : 0,
    monthly_injected: Math.round(wallet.monthlyInjected * 100) / 100,
    position_count: wallet.positions.length,
    open_count: wallet.positions.filter(p => p.status === 'open').length
  };
}

function _baseSymbol(symbol) {
  return String(symbol || '').toUpperCase().split('.')[0];
}

// ── Main scan logic ──────────────────────────────────────────────────

async function runScan(msg, runId) {
  const packet = msg.packet || {};
  const workerIndex = num(msg.worker_index, 0);
  const variantChunk = msg.variant_chunk || [];

  const strategies = packet.stock_strategies || [];
  const lookup = packet.option_data_lookup || {};
  const endDate = packet.end_date || '';
  const stockInputs = packet.stock_inputs || { initial_cash: 20000, monthly_contribution: 1000 };

  const started = performance.now();
  let completed = 0;
  const total = variantChunk.length * strategies.length;
  const results = [];

  for (const variant of variantChunk) {
    await gate(runId, workerIndex);
    const settings = {
      wallet_pct: num(variant.wallet_pct, 20),
      trade_allocation_pct: num(variant.trade_allocation_pct, 30),
      target_dte: num(variant.target_dte, 250),
      min_dte: num(variant.min_dte, 200),
      max_dte: num(variant.max_dte, 300),
      moneyness: String(variant.moneyness || 'otm_10'),
      profit_take_pct: num(variant.profit_take_pct, 100),
      profit_take_sell_pct: num(variant.profit_take_sell_pct, 50),
      exit_dte: num(variant.exit_dte, 60),
      trade_fee: 0.35
    };
    const perStrategy = [];

    for (const strategy of strategies) {
      await gate(runId, workerIndex);
      const trades = strategy.trades || [];
      const walletResult = replayOptionWallet(trades, lookup, settings, endDate, stockInputs);

      const positions = walletResult.positions || [];
      const skippedItems = walletResult.skipped || [];
      const metrics = walletResult.metrics || {};

      perStrategy.push({
        strategy_key: strategy.strategy_key,
        strategy_label: strategy.strategy_label,
        option_positions: positions,
        option_skipped: skippedItems,
        option_metrics: {
          position_count: metrics.position_count || 0,
          skipped_count: skippedItems.length,
          total_premium: metrics.total_premium_paid || 0,
          total_value: metrics.total_value || 0,
          return_pct: metrics.return_pct || 0
        }
      });
      completed++;
    }

    // Aggregate
    const returns = perStrategy.map(s => s.option_metrics.return_pct);
    const premiums = perStrategy.map(s => s.option_metrics.total_premium);
    const totalValues = perStrategy.map(s => s.option_metrics.total_value);
    results.push({
      variant_index: variant.variant_index,
      variant,
      per_strategy: perStrategy,
      aggregate_metrics: {
        avg_return_pct: returns.length ? returns.reduce((a, b) => a + b, 0) / returns.length : 0,
        max_return_pct: Math.max(...returns, 0),
        min_return_pct: Math.min(...returns, 0),
        total_premium: premiums.reduce((a, b) => a + b, 0),
        total_value: totalValues.reduce((a, b) => a + b, 0),
        combined_return_pct: premiums.reduce((a, b) => a + b, 0) > 0
          ? (totalValues.reduce((a, b) => a + b, 0) / premiums.reduce((a, b) => a + b, 0) - 1) * 100 : 0
      }
    });

    // Progress update every 10 variants
    if (results.length % 10 === 0 || results.length === variantChunk.length) {
      postMessage({
        type: 'progress', run_id: runId, worker_index: workerIndex,
        stage: 'scanning', completed_variants: results.length,
        total_variants: variantChunk.length, completed_simulations: completed,
        message: `${results.length}/${variantChunk.length} variants`
      });
    }
  }

  postMessage({
    type: 'done', run_id: runId, worker_index: workerIndex,
    results, completed_variants: results.length,
    total_variants: variantChunk.length,
    elapsed_ms: performance.now() - started
  });
}
