#!/usr/bin/env node
/**
 * Reproduce bug: TSLA 2023-10-30, 120d drawdown 32% but only 19.90% buy tranche triggered.
 * 
 * Params: 线性递增加权细切 (step 9.95% / max_dd 47.8%)
 *         cost_deleverage 10.3%/15%/29.2% profit, 40%/30%/21.6% sell
 *         24d cooldown, 买入日可卖, 卖后重启 4.21% rearm
 *         从首档 restart, 卖档重启 16.68% rearm
 */

const fs = require('fs');
const path = require('path');

// Load the worker JS (extract engine functions)
const workerCode = fs.readFileSync(
  path.join(__dirname, 'web/static/strategy_parameter_lab_worker.js'),
  'utf8'
);

// We need to extract the simulation functions. Since the worker uses module-level
// state, we'll eval the non-message-handler parts.
// Actually, let's just use the API to get the packet and run simulation via Node.

// Instead, let's call the Python API to get the packet, then run the JS engine manually.

const http = require('http');

function postJson(path, body) {
  return new Promise((resolve, reject) => {
    const data = JSON.stringify(body);
    const req = http.request({
      hostname: '127.0.0.1',
      port: 5000,
      path,
      method: 'POST',
      headers: {
        'Content-Type': 'application/json',
        'Content-Length': Buffer.byteLength(data),
      },
    }, (res) => {
      let chunks = [];
      res.on('data', chunk => chunks.push(chunk));
      res.on('end', () => {
        const raw = Buffer.concat(chunks).toString();
        try {
          resolve(JSON.parse(raw));
        } catch (e) {
          resolve(raw);
        }
      });
    });
    req.on('error', reject);
    req.write(data);
    req.end();
  });
}

async function main() {
  // Step 1: Get the packet for TSLA with our params
  const payload = {
    buy_strategies: ['linear_weighted_slice'],
    sell_strategies: ['cost_deleverage'],
    targets: [{ symbol: 'TSLA.US', weight: 100, name: 'TSLA', max_drawdown_pct: 47.8 }],
    start: '2023-06-01',
    end: '2023-11-30',
    initial_cash: 20000,
    monthly_contribution: 1000,
    max_drawdown_pct: 47.8,
    drawdown_basis: 'rolling_120',
    step_pct: 9.95,
    cost_first_profit_pct: 10.3,
    cost_second_profit_pct: 15,
    cost_third_profit_pct: 29.2,
    cost_first_sell_pct: 40,
    cost_second_sell_pct: 30,
    cost_third_sell_pct: 21.6,
    cost_deleverage_cooldown_days: 24,
    sell_allow_same_day_sell: true,
    dca_rearm_drawdown_pct: 4.21,
    buy_rearm_mode: 'restart_from_rearm',
    sell_stage_rearm_drawdown_pct: 16.68,
    include_trades: true,
    include_series: true,
  };

  console.log('Fetching parameter lab packet...');
  const packetResp = await postJson('/api/strategy-lab/parameter-lab/packet', payload);
  
  if (!packetResp.success) {
    console.error('Packet request failed:', packetResp);
    return;
  }

  const packet = packetResp.packet;
  console.log('Packet received. Tasks:', packet.tasks?.length);
  console.log('Candidates:', packet.candidate_pool?.length);

  // Step 2: Run evaluate-batch with our specific candidate
  const inputs = packet.inputs;
  console.log('\n=== Base Inputs ===');
  console.log('step_pct:', inputs.step_pct);
  console.log('max_drawdown_pct:', inputs.max_drawdown_pct);
  console.log('drawdown_basis:', inputs.drawdown_basis);
  console.log('cost_first_profit_pct:', inputs.cost_first_profit_pct);
  console.log('cost_second_profit_pct:', inputs.cost_second_profit_pct);
  console.log('cost_third_profit_pct:', inputs.cost_third_profit_pct);
  console.log('cost_first_sell_pct:', inputs.cost_first_sell_pct);
  console.log('cost_second_sell_pct:', inputs.cost_second_sell_pct);
  console.log('cost_third_sell_pct:', inputs.cost_third_sell_pct);
  console.log('cost_deleverage_cooldown_days:', inputs.cost_deleverage_cooldown_days);
  console.log('sell_allow_same_day_sell:', inputs.sell_allow_same_day_sell);
  console.log('dca_rearm_drawdown_pct:', inputs.dca_rearm_drawdown_pct);
  console.log('buy_rearm_mode:', inputs.buy_rearm_mode);
  console.log('sell_stage_rearm_drawdown_pct:', inputs.sell_stage_rearm_drawdown_pct);

  // Step 3: Get price points for the task
  const task = packet.tasks?.[0];
  if (!task) {
    console.error('No task found');
    return;
  }
  console.log('\nTask key:', task.key);
  console.log('Task start:', task.start);
  console.log('Task end:', task.end);

  // Check the price data around 2023-10-30
  const pricePoints = task.price_points?.TSLA?.US || task.price_points?.['TSLA.US'] || task.price_points?.[Object.keys(task.price_points || {})[0]];
  if (!pricePoints) {
    console.log('Available price_point keys:', Object.keys(task.price_points || {}));
    return;
  }

  console.log('\nPrice points count:', pricePoints.length);
  
  // Find 2023-10-30 data
  const targetDate = '2023-10-30';
  const targetPoint = pricePoints.find(p => p.date === targetDate);
  if (targetPoint) {
    console.log(`\n=== ${targetDate} Price Point ===`);
    console.log('close:', targetPoint.close);
    console.log('drawdown_120:', targetPoint.drawdown_120);
    console.log('drawdown_ath:', targetPoint.drawdown_ath);
    console.log('rolling_120_peak:', targetPoint.rolling_120_peak);
    console.log('rolling_peak:', targetPoint.rolling_peak);
    console.log('120d drawdown pct:', (Math.abs(targetPoint.drawdown_120) * 100).toFixed(2) + '%');
  } else {
    console.log(`${targetDate} not found. Dates around:`);
    pricePoints.filter(p => p.date >= '2023-10-25' && p.date <= '2023-11-05').forEach(p => {
      console.log(`  ${p.date} close=${p.close} dd120=${(Math.abs(p.drawdown_120)*100).toFixed(2)}% dd_ath=${(Math.abs(p.drawdown_ath)*100).toFixed(2)}%`);
    });
  }

  // Step 4: Run simulation via evaluate-batch
  console.log('\n\nRunning simulation via evaluate-batch...');
  const evalResp = await postJson('/api/strategy-lab/parameter-lab/evaluate-batch', {
    inputs: {
      initial_cash: 20000,
      monthly_contribution: 1000,
      max_drawdown_pct: 47.8,
      drawdown_basis: 'rolling_120',
      step_pct: 9.95,
      trade_fee: 0.35,
      hkd_to_usd: 0.128,
      reserve_position_pct: 40,
      sell_min_profit_pct: 10,
      cost_first_profit_pct: 10.3,
      cost_second_profit_pct: 15,
      cost_third_profit_pct: 29.2,
      cost_first_sell_pct: 40,
      cost_second_sell_pct: 30,
      cost_third_sell_pct: 21.6,
      cost_deleverage_cooldown_days: 24,
      sell_allow_same_day_sell: true,
      dca_rearm_drawdown_pct: 4.21,
      buy_rearm_mode: 'restart_from_rearm',
      sell_stage_rearm_drawdown_pct: 16.68,
    },
    tasks: [{ symbol: 'TSLA.US', start: '2023-06-01', end: '2023-11-30' }],
    candidates: [{
      buy_strategy: 'linear_weighted_slice',
      sell_strategy: 'cost_deleverage',
      parameters: {
        step_pct: 9.95,
        cost_first_profit_pct: 10.3,
        cost_second_profit_pct: 15,
        cost_third_profit_pct: 29.2,
        cost_first_sell_pct: 40,
        cost_second_sell_pct: 30,
        cost_third_sell_pct: 21.6,
        cost_deleverage_cooldown_days: 24,
        sell_allow_same_day_sell: true,
        dca_rearm_drawdown_pct: 4.21,
        buy_rearm_mode: 'restart_from_rearm',
        sell_stage_rearm_drawdown_pct: 16.68,
      }
    }],
    include_trades: true,
    include_series: true,
  });

  if (evalResp.error) {
    console.error('Evaluate-batch error:', evalResp.error);
  } else if (evalResp.results) {
    const result = evalResp.results[0];
    console.log('\n=== Python Simulation Result ===');
    console.log('return_pct:', result.return_pct);
    console.log('trade_count:', result.trade_count);
    
    // Find trades around 2023-10-30
    const trades = result.trade_log || [];
    console.log('\nAll buy trades:');
    trades.filter(t => t.action === 'buy').forEach(t => {
      console.log(`  ${t.date} buy drawdown=${t.drawdown_pct?.toFixed(2)}% threshold=${t.threshold_pct?.toFixed(2)}% base_threshold=${t.base_threshold_pct?.toFixed(2)}% anchor=${t.buy_rearm_anchor_drawdown_pct?.toFixed(2)}% shares=${t.shares?.toFixed(4)} price=${t.price}`);
    });
    console.log('\nAll sell trades:');
    trades.filter(t => t.action === 'sell').forEach(t => {
      console.log(`  ${t.date} sell drawdown=${t.drawdown_pct?.toFixed(2)}% stage=${t.stage} shares=${t.shares?.toFixed(4)} price=${t.price} profit_pct=${t.estimated_profit_pct?.toFixed(2)}%`);
    });

    // Focus on 2023-10-30
    const oct30Trades = trades.filter(t => t.date === '2023-10-30');
    console.log(`\n=== Trades on 2023-10-30 ===`);
    oct30Trades.forEach(t => {
      console.log(`  ${t.action} drawdown=${t.drawdown_pct?.toFixed(2)}% threshold=${t.threshold_pct?.toFixed(2)}% base_threshold=${t.base_threshold_pct?.toFixed(2)}`);
    });
  }

  // Step 5: Also check what the tranches look like
  console.log('\n=== Expected Tranches for linear_weighted_slice ===');
  const maxDd = 47.8;
  const step = 9.95;
  const thresholds = [];
  const count = Math.floor(maxDd / step);
  for (let i = 1; i <= count; i++) thresholds.push(step * i);
  if (Math.abs(thresholds[thresholds.length - 1] - maxDd) > 1e-9) thresholds.push(maxDd);
  const weightSum = thresholds.reduce((sum, _, index) => sum + index + 1, 0);
  thresholds.forEach((t, i) => {
    console.log(`  Tranche ${i+1}: threshold=${t.toFixed(2)}% allocation=${((i+1)/weightSum*100).toFixed(2)}%`);
  });
}

main().catch(console.error);
